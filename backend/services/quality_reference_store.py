"""RDS metadata and OSS files for high-quality maintenance-plan references."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import alibabacloud_oss_v2 as oss
import pymysql
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


PRODUCT_ALIASES = {
    "ecs": ("ecs", "云服务器"),
    "vpc": ("vpc", "vswitch", "交换机", "专有网络"),
    "oss": ("oss", "对象存储"),
    "slb": ("slb", "负载均衡"),
    "rds": ("rds",),
    "redis": ("redis",),
    "polardb": ("polardb",),
    "mq": ("mq", "rocketmq", "消息队列"),
    "k8s": ("k8s", "kubernetes", "worker"),
}

ACTION_ALIASES = {
    "create": ("创建", "新建", "申请"),
    "recycle": ("回收", "释放", "删除", "空闲"),
    "resize": ("升配", "降配", "扩容", "缩容", "规格调整", "规格变更"),
    "restart": ("维护性重启", "重启"),
    "upgrade": ("升级",),
    "switch": ("切换", "切流"),
    "drill": ("演练",),
    "change": ("配置调整", "配置变更", "规则添加"),
}


def normalize_product(value: str) -> str:
    text = (value or "").lower()
    for product, aliases in PRODUCT_ALIASES.items():
        if any(alias.lower() in text for alias in aliases):
            return product
    return ""


def normalize_action(value: str) -> str:
    text = (value or "").lower()
    if text in ACTION_ALIASES:
        return text
    for action, aliases in ACTION_ALIASES.items():
        if any(alias.lower() in text for alias in aliases):
            return action
    return "general"


def normalize_network(value: str) -> str:
    text = re.sub(r"[、，,。\s]", "", value or "")
    if "内外网" in text:
        return "内外网"
    if "外网" in text:
        return "外网"
    if "内网" in text:
        return "内网"
    return ""


def derive_reference_metadata(path: Path, source_root: Path | None = None) -> dict[str, str]:
    relative = path.relative_to(source_root) if source_root else path
    hint = " ".join(relative.parts)
    product = normalize_product(hint)
    action = normalize_action(hint)
    network = normalize_network(path.stem)
    return {
        "title": path.stem,
        "product_type": product,
        "operation_type": action,
        "network": network,
    }


def derive_query_metadata(state: dict[str, Any], skill_name: str = "") -> dict[str, str]:
    hint = "\n".join(
        str(state.get(key, ""))
        for key in ("background", "maintenance_type", "instances", "tech_params")
    )
    return {
        "product_type": normalize_product(f"{skill_name}\n{hint}"),
        "operation_type": normalize_action(hint),
        "network": normalize_network(str(state.get("network", "")) + "\n" + hint),
        "skill_name": skill_name,
    }


class QualityReferenceStore:
    """Store quality-reference metadata in RDS and source DOCX files in OSS."""

    TABLE = "quality_reference_documents"

    def __init__(self) -> None:
        self.host = _required_env("ARCHIVE_RDS_HOST")
        self.port = int(os.getenv("ARCHIVE_RDS_PORT", "3306"))
        self.database = _required_env("ARCHIVE_RDS_DATABASE")
        self.username = _required_env("ARCHIVE_RDS_USERNAME")
        self.password = _required_env("ARCHIVE_RDS_PASSWORD")
        self.bucket = os.getenv("QUALITY_REFERENCE_OSS_BUCKET", "high-quality-plan").strip()
        if not self.bucket:
            raise RuntimeError("缺少远程优质方案配置：QUALITY_REFERENCE_OSS_BUCKET")
        self.region = os.getenv(
            "QUALITY_REFERENCE_OSS_REGION",
            os.getenv("PLAN_ARCHIVE_OSS_REGION", "cn-beijing"),
        )
        self.prefix = os.getenv("QUALITY_REFERENCE_OSS_PREFIX", "quality-references").strip("/")
        self._oss_client = self._build_oss_client()
        self._init_db()

    @staticmethod
    def is_configured() -> bool:
        required = (
            "ARCHIVE_RDS_HOST",
            "ARCHIVE_RDS_DATABASE",
            "ARCHIVE_RDS_USERNAME",
            "ARCHIVE_RDS_PASSWORD",
        )
        return all(os.getenv(name, "").strip() for name in required)

    def _connection(self):
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.username,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
        )

    def _build_oss_client(self) -> oss.Client:
        access_key_id = os.getenv("QUALITY_REFERENCE_OSS_ACCESS_KEY_ID") or os.getenv(
            "ALIBABA_CLOUD_ACCESS_KEY_ID"
        )
        access_key_secret = os.getenv("QUALITY_REFERENCE_OSS_ACCESS_KEY_SECRET") or os.getenv(
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
        )
        if not access_key_id or not access_key_secret:
            raise RuntimeError("缺少 OSS 访问凭证：ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET")
        config = oss.Config(
            region=self.region,
            credentials_provider=oss.credentials.StaticCredentialsProvider(
                access_key_id,
                access_key_secret,
                os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN") or None,
            ),
        )
        return oss.Client(config)

    def _init_db(self) -> None:
        sql = f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE} (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                document_code VARCHAR(128) NOT NULL,
                title VARCHAR(512) NOT NULL,
                product_type VARCHAR(64) NOT NULL,
                operation_type VARCHAR(64) NOT NULL DEFAULT 'general',
                network VARCHAR(64) NOT NULL DEFAULT '',
                skill_name VARCHAR(128) NOT NULL DEFAULT '',
                quality_level VARCHAR(32) NOT NULL DEFAULT 'gold',
                benchmark_enabled TINYINT(1) NOT NULL DEFAULT 1,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                oss_object_key VARCHAR(1024) NOT NULL,
                source_filename VARCHAR(512) NOT NULL,
                source_path VARCHAR(1024) NOT NULL DEFAULT '',
                checksum_sha256 CHAR(64) NOT NULL,
                file_size BIGINT NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE KEY uq_quality_reference_code (document_code),
                UNIQUE KEY uq_quality_reference_checksum (checksum_sha256),
                KEY idx_quality_reference_match (product_type, operation_type, network),
                KEY idx_quality_reference_active (benchmark_enabled, status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
            conn.commit()

    def import_file(
        self,
        path: Path,
        *,
        metadata: dict[str, str] | None = None,
        source_root: Path | None = None,
        skill_name: str = "",
    ) -> dict[str, Any]:
        path = path.resolve()
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        existing = self._find_by_checksum(checksum)
        if existing:
            return {"status": "skipped", "reason": "duplicate", "record": existing}

        values = derive_reference_metadata(path, source_root)
        values.update({key: str(value) for key, value in (metadata or {}).items() if value is not None})
        product = normalize_product(values.get("product_type", ""))
        if not product:
            return {"status": "skipped", "reason": "unknown_product", "path": str(path)}
        action = normalize_action(values.get("operation_type", ""))
        network = normalize_network(values.get("network", ""))
        code = checksum[:24]
        source_filename = Path(values.get("source_filename") or path.name).name
        safe_name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", source_filename)
        object_key = f"{self.prefix}/{product}/{action}/{code}_{safe_name}"
        self._oss_client.put_object(
            oss.PutObjectRequest(
                bucket=self.bucket,
                key=object_key,
                body=raw,
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )
        now = datetime.now().replace(microsecond=0)
        record = {
            "document_code": code,
            "title": values.get("title") or path.stem,
            "product_type": product,
            "operation_type": action,
            "network": network,
            "skill_name": skill_name or values.get("skill_name", ""),
            "oss_object_key": object_key,
            "source_filename": source_filename,
            "source_path": values.get("source_path") or (
                str(path.relative_to(source_root)) if source_root else str(path)
            ),
            "checksum_sha256": checksum,
            "file_size": len(raw),
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(record)
        placeholders = ", ".join(["%s"] * len(record))
        try:
            with self._connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO {self.TABLE} ({columns}) VALUES ({placeholders})",
                        list(record.values()),
                    )
                    record["id"] = cursor.lastrowid
                conn.commit()
        except Exception:
            self._delete_object_quietly(object_key)
            raise
        return {"status": "imported", "record": self._public_record(record)}

    def import_directory(self, root: Path) -> dict[str, Any]:
        root = root.resolve()
        results = {"imported": 0, "duplicate": 0, "skipped": 0, "errors": []}
        for path in sorted(root.rglob("*.docx")):
            if path.name.startswith("~$") or "基础组方案" in path.parts:
                continue
            try:
                result = self.import_file(path, source_root=root)
            except Exception as exc:
                results["errors"].append({"path": str(path), "error": str(exc)})
                continue
            if result["status"] == "imported":
                results["imported"] += 1
            elif result.get("reason") == "duplicate":
                results["duplicate"] += 1
            else:
                results["skipped"] += 1
        results["total_processed"] = results["imported"] + results["duplicate"] + results["skipped"]
        return results

    def list_records(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM {self.TABLE} ORDER BY updated_at DESC, id DESC LIMIT %s",
                    [max(1, min(limit, 500))],
                )
                rows = cursor.fetchall()
        return [self._public_record(row) for row in rows]

    def select_references(
        self,
        metadata: dict[str, str],
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        product = normalize_product(metadata.get("product_type", ""))
        action = normalize_action(metadata.get("operation_type", ""))
        network = normalize_network(metadata.get("network", ""))
        if not product:
            return []
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT *,
                        CASE WHEN operation_type = %s THEN 4 WHEN operation_type = 'general' THEN 2 ELSE 0 END
                        + CASE WHEN network = %s THEN 2 WHEN network = '' OR network = '内外网' THEN 1 ELSE 0 END
                        + CASE WHEN skill_name = %s AND skill_name <> '' THEN 1 ELSE 0 END AS match_score
                    FROM {self.TABLE}
                    WHERE product_type = %s AND benchmark_enabled = 1 AND status = 'active'
                    ORDER BY match_score DESC, updated_at DESC, id DESC
                    LIMIT %s
                    """,
                    [action, network, metadata.get("skill_name", ""), product, max(1, min(limit, 20))],
                )
                rows = cursor.fetchall()
        return [self._public_record(row) for row in rows]

    def materialize(self, records: Iterable[dict[str, Any]]) -> Path:
        output_dir = Path(tempfile.mkdtemp(prefix="quality-references-"))
        for index, record in enumerate(records, start=1):
            key = str(record["oss_object_key"])
            response = self._oss_client.get_object(oss.GetObjectRequest(bucket=self.bucket, key=key))
            body = response.body
            raw = body.read() if hasattr(body, "read") else bytes(body)
            filename = Path(str(record.get("source_filename") or f"reference-{index}.docx")).name
            (output_dir / f"{index:02d}_{filename}").write_bytes(raw)
        return output_dir

    def status(self) -> dict[str, Any]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS total FROM {self.TABLE} WHERE status = 'active'")
                total = int(cursor.fetchone()["total"])
        return {
            "configured": True,
            "database": self.database,
            "table": self.TABLE,
            "bucket": self.bucket,
            "region": self.region,
            "prefix": self.prefix,
            "active_documents": total,
        }

    def _find_by_checksum(self, checksum: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM {self.TABLE} WHERE checksum_sha256 = %s LIMIT 1",
                    [checksum],
                )
                row = cursor.fetchone()
        return self._public_record(row) if row else None

    def _delete_object_quietly(self, key: str) -> None:
        try:
            self._oss_client.delete_object(oss.DeleteObjectRequest(bucket=self.bucket, key=key))
        except Exception:
            pass

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        for key in ("created_at", "updated_at"):
            if isinstance(result.get(key), datetime):
                result[key] = result[key].isoformat(timespec="seconds")
        result["benchmark_enabled"] = bool(result.get("benchmark_enabled", True))
        result.pop("checksum_sha256", None)
        return result


_store: QualityReferenceStore | None = None


def get_quality_reference_store() -> QualityReferenceStore:
    global _store
    if _store is None:
        _store = QualityReferenceStore()
    return _store


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少远程优质方案配置：{name}")
    return value
