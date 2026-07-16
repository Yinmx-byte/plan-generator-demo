"""RDS + OSS implementation for maintenance-plan archive storage."""

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import alibabacloud_oss_v2 as oss
import pymysql

from services.plan_archive import (
    _compute_diff_summary,
    _compute_section_diffs,
    _compute_state_diff,
    _extract_action,
    _extract_impact,
    _extract_key_params,
    _extract_org_and_resource_set,
    _extract_product_type,
    _extract_rollback,
    _extract_system_name,
    _generate_diff_markdown,
    _maintenance_date,
    _now_iso,
    _today,
    write_summary_excel,
)
from services.plan_generation import _generated_documents, safe_docx_filename


class RemoteArchiveStore:
    """Persist archive metadata in RDS and generated artifacts in OSS.

    ``docx_path`` and ``json_path`` retain their existing record names for API
    compatibility. In remote mode their values are OSS object keys rather than
    local filesystem paths.
    """

    def __init__(self) -> None:
        self.host = _required_env("ARCHIVE_RDS_HOST")
        self.port = int(os.getenv("ARCHIVE_RDS_PORT", "3306"))
        self.database = _required_env("ARCHIVE_RDS_DATABASE")
        self.username = _required_env("ARCHIVE_RDS_USERNAME")
        self.password = _required_env("ARCHIVE_RDS_PASSWORD")
        self.bucket = _required_env("PLAN_ARCHIVE_OSS_BUCKET")
        self.region = os.getenv("PLAN_ARCHIVE_OSS_REGION", "cn-beijing")
        self.prefix = os.getenv("PLAN_ARCHIVE_OSS_PREFIX", "maintenance-plan-archive").strip("/")
        self.root = f"oss://{self.bucket}/{self.prefix}"
        self.db_path = f"rds://{self.host}:{self.port}/{self.database}"
        self.excel_path = Path(tempfile.gettempdir()) / "plan-generator" / "archive" / "检修工作汇总表.xlsx"
        self.excel_path.parent.mkdir(parents=True, exist_ok=True)
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
            read_timeout=20,
            write_timeout=20,
        )

    def _build_oss_client(self) -> oss.Client:
        access_key_id = os.getenv("PLAN_ARCHIVE_OSS_ACCESS_KEY_ID") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
        access_key_secret = os.getenv("PLAN_ARCHIVE_OSS_ACCESS_KEY_SECRET") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        if not access_key_id or not access_key_secret:
            raise RuntimeError("缺少 OSS 访问凭证：请设置 ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET 或 PLAN_ARCHIVE_OSS_ACCESS_KEY_ID/SECRET")
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
        columns = """
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            series_id VARCHAR(128) NOT NULL,
            version INT NOT NULL DEFAULT 1,
            file_id VARCHAR(128) NOT NULL,
            title VARCHAR(512) NOT NULL,
            product_type VARCHAR(64) DEFAULT '',
            system_name VARCHAR(512) DEFAULT '',
            action VARCHAR(128) DEFAULT '',
            network VARCHAR(128) DEFAULT '',
            location VARCHAR(512) DEFAULT '',
            org VARCHAR(256) DEFAULT '',
            resource_set VARCHAR(256) DEFAULT '',
            schedule_start VARCHAR(128) DEFAULT '',
            schedule_end VARCHAR(128) DEFAULT '',
            schedule_start_norm VARCHAR(16) DEFAULT '',
            provider VARCHAR(128) DEFAULT '',
            executor VARCHAR(128) DEFAULT '',
            reviewer VARCHAR(128) DEFAULT '',
            security_officer VARCHAR(128) DEFAULT '',
            business_impact TEXT,
            rollback_method TEXT,
            key_params TEXT,
            change_summary TEXT,
            archive_date VARCHAR(16) NOT NULL,
            downloaded_at VARCHAR(32) NOT NULL,
            docx_path VARCHAR(1024) NOT NULL,
            json_path VARCHAR(1024) NOT NULL,
            object_prefix VARCHAR(1024) NOT NULL,
            parent_file_id VARCHAR(128) DEFAULT NULL,
            UNIQUE KEY uq_archive_record_file (file_id),
            UNIQUE KEY uq_archive_record_series_version (series_id, version),
            KEY idx_archive_record_series (series_id),
            KEY idx_archive_record_schedule (schedule_start_norm),
            KEY idx_archive_record_product (product_type)
        """
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE IF NOT EXISTS maintenance_plan_archive_records (" + columns + ") "
                    "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
                )
                cursor.execute(
                    "CREATE TABLE IF NOT EXISTS maintenance_plan_archive_log (" + columns.replace(
                        "UNIQUE KEY uq_archive_record_file (file_id),\n", ""
                    ).replace(
                        "UNIQUE KEY uq_archive_record_series_version (series_id, version),\n", ""
                    ) + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
                )
            conn.commit()

    # ── public API ──────────────────────────────────────────────────

    def archive(
        self,
        file_id: str,
        docx_path: Path,
        state: Optional[dict] = None,
        plan_data: Optional[dict] = None,
    ) -> dict:
        if plan_data is None:
            plan_data = deepcopy(_generated_documents.get(file_id, {}))
        if not plan_data:
            return {"status": "skipped", "reason": "no_plan_data"}
        if self._already_archived(file_id):
            return {"status": "skipped", "reason": "already_archived"}

        state = state or {}
        title = plan_data.get("title", "检修方案")
        system_name = _extract_system_name(state.get("instances", ""), title)
        action = _extract_action(title, state.get("instances", ""), state.get("maintenance_type", ""))
        product_type = _extract_product_type(title, state.get("instances", ""), state.get("tech_params", ""))
        org, resource_set = _extract_org_and_resource_set(state.get("instances", ""))
        series_id, version_number, parent_file_id = self._match_series(
            title,
            system_name,
            action,
            product_type,
            schedule_start=state.get("schedule_start", ""),
        )
        old_snapshot = self._load_snapshot(parent_file_id) if parent_file_id else None
        old_records = self.get_series_history(series_id) if version_number > 1 else []
        state_changes = _compute_state_diff(old_records[-1], state) if old_records else None
        agent_summary = str(state.get("_agent_change_summary", "")).strip()
        change_summary = agent_summary or (
            _compute_diff_summary(old_snapshot, plan_data, state_changes) if old_snapshot else ""
        )

        maintenance_date = _maintenance_date(state)
        safe_name = safe_docx_filename(title)
        object_prefix = "/".join(
            part for part in (self.prefix, maintenance_date, series_id, f"{safe_name}_v{version_number}") if part
        )
        docx_key = f"{object_prefix}/{safe_name}.docx"
        json_key = f"{object_prefix}/plan_snapshot.json"
        diff_key = f"{object_prefix}/diff_from_v{version_number - 1}.md" if old_snapshot else ""
        uploaded_keys: list[str] = []
        try:
            self._put_object(docx_key, Path(docx_path).read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            uploaded_keys.append(docx_key)
            self._put_object(json_key, json.dumps(plan_data, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8")
            uploaded_keys.append(json_key)
            if diff_key:
                diff_text = _generate_diff_markdown(
                    old_snapshot,
                    plan_data,
                    version_number - 1,
                    version_number,
                    title,
                    state_changes,
                )
                self._put_object(diff_key, diff_text.encode("utf-8"), "text/markdown; charset=utf-8")
                uploaded_keys.append(diff_key)

            record = {
                "series_id": series_id,
                "version": version_number,
                "file_id": file_id,
                "title": title,
                "product_type": product_type,
                "system_name": system_name,
                "action": action,
                "network": state.get("network", ""),
                "location": state.get("location", "国网亦庄数据中心二期运维专区"),
                "org": org,
                "resource_set": resource_set,
                "schedule_start": state.get("schedule_start", ""),
                "schedule_end": state.get("schedule_end", ""),
                "provider": state.get("provider", ""),
                "executor": state.get("executor", ""),
                "reviewer": state.get("reviewer", ""),
                "security_officer": state.get("security_officer", ""),
                "business_impact": _extract_impact(plan_data),
                "rollback_method": _extract_rollback(plan_data),
                "key_params": _extract_key_params(state),
                "change_summary": change_summary,
                "archive_date": _today(),
                "downloaded_at": _now_iso(),
                "docx_path": docx_key,
                "json_path": json_key,
                "object_prefix": object_prefix,
                "parent_file_id": parent_file_id,
            }
            record["id"] = self._insert_record(record)
        except Exception:
            for key in uploaded_keys:
                self._delete_object_quietly(key)
            raise

        self.rebuild_summary_excel()
        record["status"] = "archived"
        return record

    def query_summary(self, filters: Optional[dict] = None, latest_only: bool = False) -> list[dict]:
        filters = filters or {}
        where, params = self._where_clause(filters)
        if latest_only:
            sql = (
                "SELECT r.* FROM maintenance_plan_archive_records r "
                "INNER JOIN (SELECT series_id, MAX(version) AS max_v "
                "FROM maintenance_plan_archive_records WHERE 1=1 " + where + " GROUP BY series_id) latest "
                "ON r.series_id = latest.series_id AND r.version = latest.max_v "
                "ORDER BY r.schedule_start_norm DESC, r.series_id, r.version DESC"
            )
        else:
            sql = "SELECT * FROM maintenance_plan_archive_records WHERE 1=1 " + where + " ORDER BY schedule_start_norm DESC, series_id, version DESC"
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def get_series_history(self, series_id: str) -> list[dict]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM maintenance_plan_archive_records WHERE series_id = %s ORDER BY version",
                    [series_id],
                )
                return list(cursor.fetchall())

    def compare_versions(self, series_id: str, from_version: int, to_version: int) -> dict:
        old = self._find_version(series_id, from_version)
        new = self._find_version(series_id, to_version)
        if not old or not new:
            raise FileNotFoundError(f"版本不存在: series={series_id}, v{from_version}→v{to_version}")
        old_data = self._load_snapshot_by_path(old["json_path"])
        new_data = self._load_snapshot_by_path(new["json_path"])
        if not old_data or not new_data:
            raise FileNotFoundError("快照 JSON 文件丢失")
        return {
            "series_id": series_id,
            "from_version": from_version,
            "to_version": to_version,
            "old_title": old["title"],
            "new_title": new["title"],
            "old_downloaded_at": old["downloaded_at"],
            "new_downloaded_at": new["downloaded_at"],
            "summary": _compute_diff_summary(old_data, new_data),
            "section_diffs": _compute_section_diffs(old_data, new_data),
        }

    def rebuild_summary_excel(self, output_path: Optional[Path] = None, latest_only: bool = False) -> None:
        write_summary_excel(self.query_summary(latest_only=latest_only), output_path or self.excel_path)

    def delete_old_version_files(self, start_date: str = "", end_date: str = "") -> dict:
        records = self.query_summary(_date_filters(start_date, end_date))
        latest = {record["series_id"]: 0 for record in records}
        for record in records:
            latest[record["series_id"]] = max(latest[record["series_id"]], record["version"])
        deleted = 0
        for record in records:
            if record["version"] < latest[record["series_id"]]:
                self._remove_version(record)
                deleted += 1
        self.rebuild_summary_excel()
        return {"deleted_count": deleted, "series_affected": len(latest)}

    def delete_all_files(self, start_date: str = "", end_date: str = "") -> dict:
        records = self.query_summary(_date_filters(start_date, end_date))
        for record in records:
            self._remove_version(record)
        self.rebuild_summary_excel()
        return {"deleted_count": len(records)}

    def read_archived_docx(self, record_id: int) -> tuple[bytes, str]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT title, docx_path FROM maintenance_plan_archive_records WHERE id = %s", [record_id])
                record = cursor.fetchone()
        if not record:
            raise FileNotFoundError("归档记录不存在或其文件已清理")
        return self._get_object(record["docx_path"]), f"{safe_docx_filename(record['title'])}.docx"

    # ── database and object helpers ─────────────────────────────────

    def _where_clause(self, filters: dict) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, column, operator in (
            ("start_date", "schedule_start_norm", ">="),
            ("end_date", "schedule_start_norm", "<="),
            ("product_type", "product_type", "="),
            ("action", "action", "="),
        ):
            value = filters.get(key)
            if value:
                clauses.append(f" AND {column} {operator} %s")
                params.append(value)
        if filters.get("system_name"):
            clauses.append(" AND REPLACE(system_name, ' ', '') LIKE %s")
            params.append(f"%{filters['system_name'].replace(' ', '')}%")
        if filters.get("person"):
            clauses.append(" AND (provider LIKE %s OR executor LIKE %s OR reviewer LIKE %s OR security_officer LIKE %s)")
            params.extend([f"%{filters['person']}%"] * 4)
        return "".join(clauses), params

    def _already_archived(self, file_id: str) -> bool:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM maintenance_plan_archive_log WHERE file_id = %s LIMIT 1", [file_id])
                return cursor.fetchone() is not None

    def _match_series(self, title: str, system_name: str, action: str, product_type: str, schedule_start: str = "") -> tuple[str, int, Optional[str]]:
        target_date = _maintenance_date({"schedule_start": schedule_start})
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM maintenance_plan_archive_records r "
                    "INNER JOIN (SELECT series_id, MAX(version) AS max_v FROM maintenance_plan_archive_records GROUP BY series_id) latest "
                    "ON r.series_id = latest.series_id AND r.version = latest.max_v "
                    "ORDER BY r.downloaded_at DESC"
                )
                rows = cursor.fetchall()
        for row in rows:
            if row["title"] == title and _within_days(target_date, _maintenance_date({"schedule_start": row.get("schedule_start", "")})):
                return row["series_id"], row["version"] + 1, row["file_id"]
        target = f"{system_name}{action}".lower()
        best: Optional[dict] = None
        best_ratio = 0.0
        for row in rows:
            if row["product_type"] != product_type or not _within_days(target_date, _maintenance_date({"schedule_start": row.get("schedule_start", "")})):
                continue
            ratio = SequenceMatcher(None, target, f"{row['system_name']}{row['action']}".lower()).ratio()
            if ratio > 0.85 and ratio > best_ratio:
                best, best_ratio = row, ratio
        if best:
            return best["series_id"], best["version"] + 1, best["file_id"]
        digest = hashlib.md5(f"{system_name}|{action}|{product_type}".encode()).hexdigest()[:10]
        return f"AR-{_today().replace('-', '')}-{digest}", 1, None

    def _insert_record(self, record: dict) -> int:
        columns = [
            "series_id", "version", "file_id", "title", "product_type", "system_name", "action",
            "network", "location", "org", "resource_set", "schedule_start", "schedule_end", "schedule_start_norm",
            "provider", "executor", "reviewer", "security_officer", "business_impact", "rollback_method",
            "key_params", "change_summary", "archive_date", "downloaded_at", "docx_path", "json_path",
            "object_prefix", "parent_file_id",
        ]
        values = [record.get(column, "") for column in columns]
        values[13] = _maintenance_date({"schedule_start": record.get("schedule_start", "")})
        placeholders = ", ".join(["%s"] * len(columns))
        column_sql = ", ".join(columns)
        with self._connection() as conn:
            with conn.cursor() as cursor:
                record_id = 0
                for table in ("maintenance_plan_archive_records", "maintenance_plan_archive_log"):
                    cursor.execute(f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})", values)
                    if table == "maintenance_plan_archive_records":
                        record_id = int(cursor.lastrowid)
            conn.commit()
        return record_id

    def _find_version(self, series_id: str, version: int) -> Optional[dict]:
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM maintenance_plan_archive_records WHERE series_id = %s AND version = %s",
                    [series_id, version],
                )
                return cursor.fetchone()

    def _load_snapshot(self, file_id: Optional[str]) -> Optional[dict]:
        if not file_id:
            return None
        data = _generated_documents.get(file_id)
        if data:
            return deepcopy(data)
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT json_path FROM maintenance_plan_archive_records WHERE file_id = %s LIMIT 1", [file_id])
                record = cursor.fetchone()
        return self._load_snapshot_by_path(record["json_path"]) if record else None

    def _load_snapshot_by_path(self, object_key: str) -> Optional[dict]:
        try:
            return json.loads(self._get_object(object_key).decode("utf-8"))
        except Exception:
            return None

    def _remove_version(self, record: dict) -> None:
        for key in (record.get("docx_path", ""), record.get("json_path", "")):
            self._delete_object_quietly(key)
        diff_key = f"{record.get('object_prefix', '')}/diff_from_v{int(record['version']) - 1}.md"
        self._delete_object_quietly(diff_key)
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM maintenance_plan_archive_records WHERE id = %s", [record["id"]])
            conn.commit()

    def _put_object(self, key: str, data: bytes, content_type: str) -> None:
        self._oss_client.put_object(oss.PutObjectRequest(bucket=self.bucket, key=key, body=data, content_type=content_type))

    def _get_object(self, key: str) -> bytes:
        response = self._oss_client.get_object(oss.GetObjectRequest(bucket=self.bucket, key=key))
        body = response.body
        return body.read() if hasattr(body, "read") else bytes(body)

    def _delete_object_quietly(self, key: str) -> None:
        if not key:
            return
        try:
            self._oss_client.delete_object(oss.DeleteObjectRequest(bucket=self.bucket, key=key))
        except Exception:
            pass


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少远程归档配置：{name}")
    return value


def _date_filters(start_date: str, end_date: str) -> dict:
    return {key: value for key, value in {"start_date": start_date, "end_date": end_date}.items() if value}


def _within_days(left: str, right: str, days: int = 5) -> bool:
    try:
        return abs((datetime.strptime(left, "%Y-%m-%d") - datetime.strptime(right, "%Y-%m-%d")).days) <= days
    except ValueError:
        return True
