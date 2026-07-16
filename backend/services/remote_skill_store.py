"""RDS metadata and OSS packages for versioned AgentScope Skills."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import alibabacloud_oss_v2 as oss
import pymysql
import yaml
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


class RemoteSkillStore:
    """Use OSS as the Skill source of truth and a local directory as cache."""

    TABLE = "iterated_skill_versions"

    def __init__(self) -> None:
        self.host = _required_env("ARCHIVE_RDS_HOST")
        self.port = int(os.getenv("ARCHIVE_RDS_PORT", "3306"))
        self.database = _required_env("ARCHIVE_RDS_DATABASE")
        self.username = _required_env("ARCHIVE_RDS_USERNAME")
        self.password = _required_env("ARCHIVE_RDS_PASSWORD")
        self.bucket = os.getenv("ITERATED_SKILL_OSS_BUCKET", "iterated-skill").strip()
        if not self.bucket:
            raise RuntimeError("缺少远程 Skill 配置：ITERATED_SKILL_OSS_BUCKET")
        self.region = os.getenv(
            "ITERATED_SKILL_OSS_REGION",
            os.getenv("PLAN_ARCHIVE_OSS_REGION", "cn-beijing"),
        )
        self.prefix = os.getenv("ITERATED_SKILL_OSS_PREFIX", "skill-versions").strip("/")
        self._oss_client = self._build_oss_client()
        self._init_db()

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
        access_key_id = os.getenv("ITERATED_SKILL_OSS_ACCESS_KEY_ID") or os.getenv(
            "ALIBABA_CLOUD_ACCESS_KEY_ID"
        )
        access_key_secret = os.getenv("ITERATED_SKILL_OSS_ACCESS_KEY_SECRET") or os.getenv(
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
        )
        if not access_key_id or not access_key_secret:
            raise RuntimeError("缺少远程 Skill OSS 访问凭证")
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
                version_id VARCHAR(64) NOT NULL,
                skill_name VARCHAR(128) NOT NULL,
                display_name VARCHAR(256) NOT NULL DEFAULT '',
                semantic_version VARCHAR(32) NOT NULL DEFAULT '',
                reason VARCHAR(128) NOT NULL DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                markdown_object_key VARCHAR(1024) NOT NULL,
                package_object_key VARCHAR(1024) NOT NULL,
                markdown_checksum CHAR(64) NOT NULL,
                package_checksum CHAR(64) NOT NULL,
                metadata_json TEXT,
                source_version_id VARCHAR(64) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL,
                activated_at DATETIME DEFAULT NULL,
                UNIQUE KEY uq_iterated_skill_version (version_id),
                KEY idx_iterated_skill_name_time (skill_name, created_at),
                KEY idx_iterated_skill_active (skill_name, status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
            conn.commit()

    def publish_directory(
        self,
        skill_dir: Path,
        *,
        reason: str,
        source_version_id: str = "",
    ) -> dict[str, Any]:
        skill_dir = skill_dir.resolve()
        markdown_path = skill_dir / "SKILL.md"
        if not markdown_path.is_file():
            raise FileNotFoundError(f"Skill 缺少 SKILL.md：{skill_dir}")
        markdown = markdown_path.read_bytes()
        metadata = _read_frontmatter(markdown.decode("utf-8"))
        skill_name = _safe_name(str(metadata.get("name") or skill_dir.name))
        package = _build_skill_package(skill_dir)
        markdown_checksum = hashlib.sha256(markdown).hexdigest()
        package_checksum = _skill_tree_checksum(skill_dir)
        active = self.get_active(skill_name)
        if active and active.get("package_checksum") == package_checksum:
            return self._public_record(active)

        version_id = datetime.now().strftime("%Y%m%d%H%M%S%f") + "-" + uuid.uuid4().hex[:8]
        object_root = "/".join(
            part for part in (self.prefix, skill_name, version_id) if part
        )
        markdown_key = f"{object_root}/SKILL.md"
        package_key = f"{object_root}/{skill_name}.zip"
        uploaded: list[str] = []
        try:
            self._put_object(markdown_key, markdown, "text/markdown; charset=utf-8")
            uploaded.append(markdown_key)
            self._put_object(package_key, package, "application/zip")
            uploaded.append(package_key)
            now = datetime.now().replace(microsecond=0)
            record = {
                "version_id": version_id,
                "skill_name": skill_name,
                "display_name": str(metadata.get("display_name") or skill_name),
                "semantic_version": str(metadata.get("version") or ""),
                "reason": reason[:128],
                "status": "active",
                "markdown_object_key": markdown_key,
                "package_object_key": package_key,
                "markdown_checksum": markdown_checksum,
                "package_checksum": package_checksum,
                "metadata_json": json.dumps(metadata, ensure_ascii=False, default=str),
                "source_version_id": source_version_id[:64],
                "created_at": now,
                "activated_at": now,
            }
            with self._connection() as conn:
                with conn.cursor() as cursor:
                    lock_name = f"iterated-skill:{skill_name}"
                    cursor.execute("SELECT GET_LOCK(%s, 10) AS acquired", [lock_name])
                    if int(cursor.fetchone().get("acquired") or 0) != 1:
                        raise TimeoutError(f"Skill 正在被其他用户更新：{skill_name}")
                    try:
                        cursor.execute(
                            f"UPDATE {self.TABLE} SET status = 'archived' "
                            "WHERE skill_name = %s AND status = 'active'",
                            [skill_name],
                        )
                        columns = ", ".join(record)
                        placeholders = ", ".join(["%s"] * len(record))
                        cursor.execute(
                            f"INSERT INTO {self.TABLE} ({columns}) VALUES ({placeholders})",
                            list(record.values()),
                        )
                        record["id"] = cursor.lastrowid
                        conn.commit()
                    finally:
                        cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
        except Exception:
            for key in uploaded:
                self._delete_object_quietly(key)
            raise
        return self._public_record(record)

    def list_versions(self, skill_name: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM {self.TABLE} WHERE skill_name = %s "
                    "ORDER BY created_at DESC, id DESC",
                    [_safe_name(skill_name)],
                )
                rows = cursor.fetchall()
        return [self._public_record(row) for row in rows]

    def get_active(self, skill_name: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM {self.TABLE} WHERE skill_name = %s AND status = 'active' "
                    "ORDER BY activated_at DESC, id DESC LIMIT 1",
                    [_safe_name(skill_name)],
                )
                return cursor.fetchone()

    def restore_version(self, skill_name: str, version_id: str, target_dir: Path) -> None:
        record = self._find_version(skill_name, version_id)
        package = self._get_object(str(record["package_object_key"]))
        _restore_skill_package(package, target_dir)

    def deactivate_skill(self, skill_name: str) -> None:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self.TABLE} SET status = 'deleted' "
                    "WHERE skill_name = %s AND status = 'active'",
                    [_safe_name(skill_name)],
                )
            conn.commit()

    def synchronize_runtime(self, seed_root: Path, runtime_root: Path) -> dict[str, int]:
        seed_root = seed_root.resolve()
        runtime_root = runtime_root.resolve()
        runtime_root.mkdir(parents=True, exist_ok=True)
        active = {row["skill_name"]: row for row in self._list_active()}
        published = 0
        for skill_file in sorted(seed_root.glob("*/SKILL.md")):
            metadata = _read_frontmatter(skill_file.read_text(encoding="utf-8"))
            name = _safe_name(str(metadata.get("name") or skill_file.parent.name))
            if name not in active:
                self.publish_directory(skill_file.parent, reason="bootstrap-from-repository")
                published += 1
        active = {row["skill_name"]: row for row in self._list_active()}

        for child in runtime_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for skill_name, record in active.items():
            package = self._get_object(str(record["package_object_key"]))
            target = runtime_root / skill_name
            _restore_skill_package(package, target)
            tree_checksum = _skill_tree_checksum(target)
            if tree_checksum != record.get("package_checksum"):
                self._update_package_checksum(int(record["id"]), tree_checksum)
        return {"active": len(active), "published": published}

    def status(self) -> dict[str, Any]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active "
                    f"FROM {self.TABLE}"
                )
                counts = cursor.fetchone()
        return {
            "configured": True,
            "database": self.database,
            "table": self.TABLE,
            "bucket": self.bucket,
            "region": self.region,
            "prefix": self.prefix,
            "total_versions": int(counts.get("total") or 0),
            "active_skills": int(counts.get("active") or 0),
        }

    def _list_active(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM {self.TABLE} WHERE status = 'active' "
                    "ORDER BY skill_name"
                )
                return list(cursor.fetchall())

    def _find_version(self, skill_name: str, version_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM {self.TABLE} WHERE skill_name = %s AND version_id = %s LIMIT 1",
                    [_safe_name(skill_name), version_id],
                )
                record = cursor.fetchone()
        if not record:
            raise FileNotFoundError(f"Skill 远程版本不存在：{skill_name}/{version_id}")
        return record

    def _update_package_checksum(self, record_id: int, checksum: str) -> None:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self.TABLE} SET package_checksum = %s WHERE id = %s",
                    [checksum, record_id],
                )
            conn.commit()

    def _put_object(self, key: str, body: bytes, content_type: str) -> None:
        self._oss_client.put_object(
            oss.PutObjectRequest(
                bucket=self.bucket,
                key=key,
                body=body,
                content_type=content_type,
            )
        )

    def _get_object(self, key: str) -> bytes:
        response = self._oss_client.get_object(
            oss.GetObjectRequest(bucket=self.bucket, key=key)
        )
        body = response.body
        return body.read() if hasattr(body, "read") else bytes(body)

    def _delete_object_quietly(self, key: str) -> None:
        try:
            self._oss_client.delete_object(
                oss.DeleteObjectRequest(bucket=self.bucket, key=key)
            )
        except Exception:
            pass

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        for key in ("created_at", "activated_at"):
            if isinstance(result.get(key), datetime):
                result[key] = result[key].isoformat(timespec="seconds")
        result.pop("metadata_json", None)
        result.pop("markdown_checksum", None)
        result.pop("package_checksum", None)
        result["skill_version"] = result.get("semantic_version", "")
        return result


def _build_skill_package(skill_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            info = zipfile.ZipInfo(
                path.relative_to(skill_dir).as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def _skill_tree_checksum(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(skill_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _restore_skill_package(package: bytes, target_dir: Path) -> None:
    target_dir = target_dir.resolve()
    with tempfile.TemporaryDirectory(prefix="skill-restore-") as temp_name:
        extracted = Path(temp_name) / "skill"
        extracted.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                parts = PurePosixPath(member.filename).parts
                if not parts or ".." in parts:
                    raise ValueError("远程 Skill 包含非法路径")
                output = extracted.joinpath(*parts).resolve()
                if not str(output).startswith(str(extracted.resolve())):
                    raise ValueError("远程 Skill 包含越界路径")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.read(member))
        if not (extracted / "SKILL.md").is_file():
            raise ValueError("远程 Skill 包缺少 SKILL.md")
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(extracted, target_dir)


def _read_frontmatter(content: str) -> dict[str, Any]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if not match:
        return {}
    return yaml.safe_load(match.group(1)) or {}


def _safe_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    if not result:
        raise ValueError("Skill 名称不能为空")
    return result


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少远程 Skill 配置：{name}")
    return value


_store: RemoteSkillStore | None = None


def get_remote_skill_store() -> RemoteSkillStore:
    global _store
    if _store is None:
        _store = RemoteSkillStore()
    return _store


def mirror_seed_skills(seed_root: Path, runtime_root: Path) -> int:
    """Populate the ignored runtime cache when remote storage is unavailable."""
    seed_root = seed_root.resolve()
    runtime_root = runtime_root.resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    for skill_file in sorted(seed_root.glob("*/SKILL.md")):
        target = runtime_root / skill_file.parent.name
        if target.exists():
            continue
        shutil.copytree(skill_file.parent, target)
        copied += 1
    return copied
