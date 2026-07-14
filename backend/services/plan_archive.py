"""Maintenance plan archive service – SQLite metadata + Excel summary + structured diff."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from services.plan_generation import _generated_documents, safe_docx_filename

DEFAULT_ARCHIVE_ROOT = str(Path.home() / "检修方案归档")
_ARCHIVE_CONFIG_PATH = Path(__file__).parent.parent / ".archive_config.json"


def _load_persisted_root() -> str:
    """Read persisted archive root path from config file."""
    try:
        if _ARCHIVE_CONFIG_PATH.exists():
            data = json.loads(_ARCHIVE_CONFIG_PATH.read_text(encoding="utf-8"))
            root = str(data.get("archive_root", "")).strip()
            if root and Path(root).is_absolute() or root.startswith("~"):
                return str(Path(root).expanduser())
    except Exception:
        pass
    return ""


def _save_persisted_root(root: str) -> None:
    """Persist archive root path to config file."""
    _ARCHIVE_CONFIG_PATH.write_text(
        json.dumps({"archive_root": str(root)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

ARCHIVE_HEADERS = [
    "序号", "归档日期", "版本号", "产品类型", "系统名称", "检修动作",
    "方案标题", "网络环境", "实施地点", "所属组织", "所属资源集",
    "检修开始时间", "检修结束时间", "方案提供人", "检修执行人",
    "检修复核人", "安全责任人", "业务影响", "回滚方式",
    "关键参数", "变更摘要", "文件路径",
]

# Actions sorted by length (longest first) for greedy extraction.
MAINTENANCE_ACTIONS = [
    "维护性重启", "读写分离链路切换", "配置变更", "磁盘扩容",
    "回收只读实例", "创建只读实例", "规格变更", "组件扩缩容",
    "回收实例", "创建实例", "白名单变更", "参数调整", "降配",
    "升配", "回收", "创建", "重启", "扩容", "缩容", "变更",
]

PRODUCT_KEYWORDS = [
    ("PolarDB", re.compile(r"polardb", re.I)),
    ("RDS", re.compile(r"\bRDS\b|DRDS", re.I)),
    ("Redis", re.compile(r"redis", re.I)),
    ("SLB", re.compile(r"\bSLB\b|负载均衡", re.I)),
    ("OSS", re.compile(r"\bOSS\b|bucket", re.I)),
    ("K8s", re.compile(r"\bK8s\b|kubernetes|worker", re.I)),
    ("MQ", re.compile(r"\bMQ\b|RocketMQ|GroupID|Topic", re.I)),
    ("ECS", re.compile(r"\bECS\b|云服务器", re.I)),
]

EXCEL_COL_WIDTHS = [5, 12, 7, 10, 18, 10, 35, 10, 22, 14, 14,
                    16, 16, 10, 10, 10, 10, 22, 22, 32, 28, 45]

HEADER_FILL = PatternFill("solid", fgColor="4472C4")
HEADER_FONT = Font(name="微软雅黑", bold=True, size=10, color="FFFFFF")
BODY_FONT = Font(name="微软雅黑", size=9)
BODY_ALIGNMENT = Alignment(vertical="center", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
VERSION_FILLS = {
    1: PatternFill("solid", fgColor="FFFFFF"),
    2: PatternFill("solid", fgColor="FFF2CC"),
    3: PatternFill("solid", fgColor="FCE4D6"),
}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _maintenance_date(state: dict) -> str:
    """Extract YYYY-MM-DD from schedule_start. Supports ISO and Chinese formats."""
    schedule = str((state or {}).get("schedule_start", "")).strip()
    if not schedule:
        return _today()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", schedule)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", schedule)
    if m:
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    return _today()


def _extract_system_name(instances: str, title: str = "") -> str:
    """Extract the primary system/component name from the instances field."""
    if instances:
        first = re.split(r"\s*[|｜]\s*", instances, maxsplit=1)[0].strip()
        first = re.sub(r"(创建|新增|新建|申请|回收|释放|删除|调整|变更|重启|扩容|缩容|升配|降配).*$", "", first).strip()
        if first:
            return first
    if title:
        clean = title.replace("检修方案", "").strip()
        return clean[:30]
    return "未知系统"


def _extract_action(title: str, instances: str = "", maintenance_type: str = "") -> str:
    """Extract the maintenance action from title, instances, or type."""
    sources = [title, instances]
    for source in sources:
        for action in MAINTENANCE_ACTIONS:
            if action in source:
                return action
    type_map = {
        "配置变更": "配置变更", "组件升级": "组件升级",
        "组件扩缩容": "扩缩容", "数据库变更": "数据库变更",
        "日常维护（原硬件设备）": "日常维护", "其他": "检修",
    }
    return type_map.get(maintenance_type, "检修")


def _extract_product_type(title: str, instances: str = "", tech_params: str = "") -> str:
    """Infer the cloud product type from available text fields."""
    combined = f"{title} {instances} {tech_params}"
    for name, pattern in PRODUCT_KEYWORDS:
        if pattern.search(combined):
            return name
    return "通用"


def _extract_org_and_resource_set(instances: str) -> tuple[str, str]:
    """Parse org and resource set from instances field (pipe-separated)."""
    org, resource_set = "", ""
    if instances:
        parts = [p.strip() for p in re.split(r"\s*[|｜]\s*", instances) if p.strip()]
        if len(parts) >= 2:
            org = parts[1]
        if len(parts) >= 3:
            resource_set = parts[2]
    return org, resource_set


def _extract_impact(document: dict) -> str:
    """Extract a short business impact summary from the risk assessment section."""
    try:
        sections = document.get("document", {}).get("sections", [])
        for sec in sections:
            heading = str(sec.get("heading", "")).replace(" ", "")
            if "风险" not in heading:
                continue
            for block in sec.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "heading" and "影响" in str(block.get("text", "")):
                    continue
                for key in ("text", "items"):
                    val = block.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()[:120]
                    if isinstance(val, list) and val:
                        return str(val[0]).strip()[:120]
        return "待确认"
    except Exception:
        return "待确认"


def _extract_rollback(document: dict) -> str:
    """Extract a short rollback method summary."""
    try:
        sections = document.get("document", {}).get("sections", [])
        for sec in sections:
            heading = str(sec.get("heading", "")).replace(" ", "")
            if "回滚" not in heading:
                continue
            for block in sec.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "heading":
                    continue
                items = block.get("items") or []
                texts = []
                for item in items:
                    if isinstance(item, str):
                        texts.append(item.strip())
                if texts:
                    combined = "; ".join(texts[:2])
                    if "删除新建" in combined:
                        return "删除新建实例"
                    if "恢复原" in combined or "回退原" in combined:
                        return "恢复原配置"
                    if "不涉及回退" in combined or "不涉及回滚" in combined:
                        return "不涉及回退"
                    return combined[:100]
        return "待确认"
    except Exception:
        return "待确认"


def _extract_key_params(state: dict) -> str:
    """Build a concise key parameters summary from tech_params and instances."""
    parts = []
    tech = (state.get("tech_params") or "").strip()
    if tech:
        if len(tech) > 150:
            tech = tech[:147] + "..."
        parts.append(tech)
    instances = (state.get("instances") or "").strip()
    if instances and not parts:
        parts.append(instances[:150])
    if not parts:
        parts.append(state.get("maintenance_type", "检修"))
    return "; ".join(parts)


def _compute_diff_summary(old: dict, new: dict,
                          state_changes: Optional[list[str]] = None) -> str:
    """Generate a one-line change summary — state changes first, content diffs second."""
    parts: list[str] = []

    # Layer 1: precise state-level changes (user intent).
    if state_changes:
        parts.extend(state_changes)
    else:
        # Fallback: compute state changes from old/new plan_data if available
        pass

    # Layer 2: content-level comparison — only supplement when no state changes found.
    if not parts:
        try:
            old_secs = {_norm_heading(s.get("heading", "")): s
                        for s in old.get("document", {}).get("sections", []) if isinstance(s, dict)}
            new_secs = {_norm_heading(s.get("heading", "")): s
                        for s in new.get("document", {}).get("sections", []) if isinstance(s, dict)}

            content_changes = []
            for name in ["实施步骤", "风险评估", "回滚步骤", "现场环境", "实施计划"]:
                o_sec = old_secs.get(name)
                n_sec = new_secs.get(name)
                if not o_sec or not n_sec:
                    continue
                o_blocks = _block_sigs(o_sec)
                n_blocks = _block_sigs(n_sec)
                if o_blocks == n_blocks:
                    continue
                o_set = set(o_blocks)
                n_set = set(n_blocks)
                added = n_set - o_set
                removed = o_set - n_set
                if added and not removed:
                    content_changes.append(f"{name}新增{len(added)}处")
                elif removed and not added:
                    content_changes.append(f"{name}删除{len(removed)}处")
                elif added and removed:
                    content_changes.append(f"{name}修改{len(added) + len(removed)}处")
                else:
                    content_changes.append(f"{name}内容调整")

            if content_changes:
                # No state changes detected — label as LLM variance.
                parts.append("参数未变更（LLM重生成导致措辞微调）")
            else:
                parts.append("未检测到显著变更")
        except Exception:
            return "变更细节无法自动识别，请查看diff报告"
    else:
        # State changes exist — add a brief content supplement.
        try:
            old_secs = {_norm_heading(s.get("heading", "")): s
                        for s in old.get("document", {}).get("sections", []) if isinstance(s, dict)}
            new_secs = {_norm_heading(s.get("heading", "")): s
                        for s in new.get("document", {}).get("sections", []) if isinstance(s, dict)}
            affected = []
            for name in ["实施步骤", "风险评估", "回滚步骤", "现场环境", "实施计划"]:
                o_sec = old_secs.get(name)
                n_sec = new_secs.get(name)
                if not o_sec or not n_sec:
                    continue
                if _block_sigs(o_sec) != _block_sigs(n_sec):
                    affected.append(name)
            if len(affected) >= 4:
                parts.append("多章节内容联动更新")
            elif affected:
                parts.append(f"涉及章节：{'、'.join(affected[:2])}")
        except Exception:
            pass

    return "；".join(parts[:4])


STATE_COMPARE_FIELDS = [
    ("schedule_start", "检修开始时间"),
    ("schedule_end", "检修结束时间"),
    ("schedule_year", "检修年份"),
    ("tech_params", "技术参数"),
    ("instances", "目标实例"),
    ("network", "网络环境"),
    ("location", "实施地点"),
    ("provider", "方案提供人"),
    ("executor", "检修执行人"),
    ("reviewer", "检修复核人"),
    ("security_officer", "安全责任人"),
    ("maintenance_type", "检修类型"),
]


def _compute_state_diff(old_record: dict, new_state: dict) -> list[str]:
    """Compare form state fields between versions. Returns precise change descriptions."""
    changes = []
    new_state = new_state or {}
    for field, label in STATE_COMPARE_FIELDS:
        old_val = str(old_record.get(field, "") or "").strip()
        new_val = str(new_state.get(field, "") or "").strip()
        if old_val == new_val:
            continue
        if not old_val and not new_val:
            continue
        if not old_val:
            changes.append(f"{label}：新增「{new_val[:30]}」")
        elif not new_val:
            changes.append(f"{label}：移除「{old_val[:30]}」")
        else:
            short_old = old_val[:25] + ("…" if len(old_val) > 25 else "")
            short_new = new_val[:25] + ("…" if len(new_val) > 25 else "")
            changes.append(f"{label}：「{short_old}」→「{short_new}」")
    return changes


def _block_sigs(section: dict) -> list[str]:
    """Return a list of stable block signatures for comparison."""
    sigs = []
    for block in section.get("blocks", []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        text = str(block.get("text", ""))
        # Collect a canonical representation.
        parts = [btype, text]
        for item in block.get("items", []) or []:
            parts.append(str(item))
        for row in block.get("rows", []) or []:
            if isinstance(row, dict):
                parts.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(row))
        sigs.append("|".join(parts))
    return sigs


def _norm_heading(heading: str) -> str:
    return re.sub(r"\s+", "", heading.lower())


def _generate_diff_markdown(
    old_snapshot: dict, new_snapshot: dict,
    old_version: int, new_version: int, title: str,
    state_changes: Optional[list[str]] = None,
) -> str:
    """Generate a markdown diff report with state changes separated from content diffs."""
    lines = [
        f"# 版本对比: v{old_version} → v{new_version}",
        f"## {title}",
        f"",
        f"**对比时间**: {_now_iso()}",
        f"",
    ]

    # Layer 1: state-level changes (user intent).
    if state_changes:
        lines.append("## 参数变更（用户修改）")
        lines.append("")
        for change in state_changes:
            lines.append(f"- {change}")
        lines.append("")
    else:
        lines.append("## 参数变更（用户修改）")
        lines.append("")
        lines.append("未检测到参数级变更。以下内容差异为 LLM 重生成导致的措辞变化。")
        lines.append("")

    lines.append("## 内容对比（段落级）")
    lines.append("")

    try:
        old_secs = {_norm_heading(s.get("heading", "")): s
                    for s in old_snapshot.get("document", {}).get("sections", []) if isinstance(s, dict)}
        new_secs = {_norm_heading(s.get("heading", "")): s
                    for s in new_snapshot.get("document", {}).get("sections", []) if isinstance(s, dict)}

        all_names = list(dict.fromkeys(list(old_secs.keys()) + list(new_secs.keys())))
        has_changes = False

        for name in all_names:
            o_sec = old_secs.get(name)
            n_sec = new_secs.get(name)
            o_sigs = _block_sigs(o_sec) if o_sec else []
            n_sigs = _block_sigs(n_sec) if n_sec else []

            if o_sigs == n_sigs:
                continue

            has_changes = True
            display_name = o_sec.get("heading", "") if o_sec else (n_sec.get("heading", "") if n_sec else name)

            if not o_sigs:
                lines.append(f"### {display_name} (新增)")
            elif not n_sigs:
                lines.append(f"### {display_name} (已删除)")
            else:
                o_set = set(o_sigs)
                n_set = set(n_sigs)
                removed = o_set - n_set
                added = n_set - o_set
                changed_count = len(removed) + len(added)
                lines.append(f"### {display_name} ({changed_count} 处变更)")
                for sig in list(removed)[:3]:
                    short = sig.split("|", 1)[-1][:120] if "|" in sig else sig[:120]
                    if short.strip():
                        lines.append(f"- ~~{short}~~")
                for sig in list(added)[:3]:
                    short = sig.split("|", 1)[-1][:120] if "|" in sig else sig[:120]
                    if short.strip():
                        lines.append(f"+ **{short}**")

        if not has_changes:
            lines.append("未检测到显著内容变化。")
    except Exception:
        lines.append("差异计算失败，请手动对比两个版本的 plan_snapshot.json。")

    return "\n".join(lines)


class ArchiveStore:
    def __init__(self, archive_root: str = ""):
        if not archive_root:
            archive_root = os.getenv("PLAN_ARCHIVE_ROOT", "") or _load_persisted_root() or DEFAULT_ARCHIVE_ROOT
        self.root = Path(archive_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "archive_index.db"
        self.excel_path = self.root / "检修工作汇总表.xlsx"
        self._init_db()

    _RECORDS_DDL = """
        CREATE TABLE IF NOT EXISTS archive_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            file_id TEXT NOT NULL,
            title TEXT NOT NULL,
            product_type TEXT DEFAULT '',
            system_name TEXT DEFAULT '',
            action TEXT DEFAULT '',
            network TEXT DEFAULT '',
            location TEXT DEFAULT '',
            org TEXT DEFAULT '',
            resource_set TEXT DEFAULT '',
            schedule_start TEXT DEFAULT '',
            schedule_end TEXT DEFAULT '',
            schedule_start_norm TEXT DEFAULT '',
            provider TEXT DEFAULT '',
            executor TEXT DEFAULT '',
            reviewer TEXT DEFAULT '',
            security_officer TEXT DEFAULT '',
            business_impact TEXT DEFAULT '',
            rollback_method TEXT DEFAULT '',
            key_params TEXT DEFAULT '',
            change_summary TEXT DEFAULT '',
            archive_date TEXT NOT NULL,
            downloaded_at TEXT NOT NULL,
            docx_path TEXT NOT NULL,
            json_path TEXT NOT NULL,
            parent_file_id TEXT DEFAULT NULL
        )
    """

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # Main active-records table (query target, cleaned-up rows removed).
            conn.execute(self._RECORDS_DDL)
            # Immutable audit log — never deleted, for traceability.
            conn.execute(self._RECORDS_DDL.replace("archive_records", "archive_log"))

            # Migration: copy any rows from archive_records into archive_log that aren't there yet.
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO archive_log
                    SELECT * FROM archive_records
                """)
            except sqlite3.OperationalError:
                pass  # Column mismatch on very old DB — skip.

            # Remove already-cleaned rows from archive_records (docx_path emptied by old logic).
            conn.execute("DELETE FROM archive_records WHERE docx_path = ''")

            # Migration: add schedule_start_norm if table was created before this column existed.
            for tbl in ("archive_records", "archive_log"):
                try:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN schedule_start_norm TEXT DEFAULT ''")
                except sqlite3.OperationalError:
                    pass
                backfill_rows = conn.execute(
                    f"SELECT id, schedule_start FROM {tbl} "
                    "WHERE schedule_start_norm = '' OR schedule_start_norm IS NULL"
                ).fetchall()
                for row_id, sched_start in backfill_rows:
                    norm = _maintenance_date({"schedule_start": sched_start or ""})
                    if norm:
                        conn.execute(
                            f"UPDATE {tbl} SET schedule_start_norm = ? WHERE id = ?",
                            [norm, row_id],
                        )
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS archive_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_series_id ON archive_records(series_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_archive_date ON archive_records(archive_date)
            """)
            conn.commit()

    # ── public API ──────────────────────────────────────────────────

    def archive(self, file_id: str, docx_path: Path,
                state: Optional[dict] = None,
                plan_data: Optional[dict] = None) -> dict:
        """Archive a generated plan on download. Returns the archive record."""
        if plan_data is None:
            plan_data = deepcopy(_generated_documents.get(file_id, {}))
        if not plan_data:
            return {"status": "skipped", "reason": "no_plan_data"}

        if self._already_archived(file_id):
            return {"status": "skipped", "reason": "already_archived"}

        state = state or {}
        title = plan_data.get("title", "检修方案")
        system_name = _extract_system_name(
            state.get("instances", ""),
            title,
        )
        action = _extract_action(
            title,
            state.get("instances", ""),
            state.get("maintenance_type", ""),
        )
        product_type = _extract_product_type(
            title,
            state.get("instances", ""),
            state.get("tech_params", ""),
        )
        org, resource_set = _extract_org_and_resource_set(state.get("instances", ""))

        # Determine series and version.
        series_id, version_number, parent_file_id = self._match_series(
            title, system_name, action, product_type,
            schedule_start=state.get("schedule_start", ""),
        )
        change_summary = ""
        maintenance_date = _maintenance_date(state)
        agent_summary = str(state.get("_agent_change_summary", "")).strip()

        # Compute diff for non-initial versions.
        old_snapshot = None
        state_changes = None
        if version_number > 1 and parent_file_id:
            old_snapshot = self._load_snapshot(parent_file_id)
            # Get old record for state-level comparison.
            old_records = self.get_series_history(series_id)
            if old_records:
                old_record = old_records[-1]  # latest version = parent
                state_changes = _compute_state_diff(old_record, state)
            # Prefer Agent's own change description over automated diff.
            if agent_summary:
                change_summary = agent_summary
            elif old_snapshot:
                change_summary = _compute_diff_summary(old_snapshot, plan_data, state_changes)
            # Migrate all versions if maintenance date changed.
            if old_records:
                old_path = Path(old_records[0]["docx_path"])
                old_date_dir = old_path.parent.parent.name
                if old_date_dir != maintenance_date:
                    self._migrate_series_date(series_id, old_date_dir, maintenance_date)

        # Build archive directory.
        safe_name = safe_docx_filename(title)
        version_dir = self.root / maintenance_date / f"{safe_name}_v{version_number}"
        version_dir.mkdir(parents=True, exist_ok=True)

        archived_docx = version_dir / f"{safe_name}.docx"
        shutil.copy2(str(docx_path), str(archived_docx))

        json_path = version_dir / "plan_snapshot.json"
        json_path.write_text(
            json.dumps(plan_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Save diff report for v2+.
        if old_snapshot and version_number > 1:
            diff_text = _generate_diff_markdown(
                old_snapshot, plan_data,
                version_number - 1, version_number, title,
                state_changes,
            )
            diff_path = version_dir / f"diff_from_v{version_number - 1}.md"
            diff_path.write_text(diff_text, encoding="utf-8")

        # Persist to SQLite.
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
            "docx_path": str(archived_docx),
            "json_path": str(json_path),
            "parent_file_id": parent_file_id,
        }
        self._insert_record(record)
        self.rebuild_summary_excel()
        record["status"] = "archived"
        return record

    def query_summary(self, filters: Optional[dict] = None,
                      latest_only: bool = False) -> list[dict]:
        """Query archive records with optional filters.

        When latest_only is True, only the highest version per series_id
        is returned — useful for exporting a deduplicated summary.
        """
        filters = filters or {}
        if latest_only:
            sql = (
                "SELECT r.* FROM archive_records r "
                "INNER JOIN ("
                "  SELECT series_id, MAX(version) AS max_v "
                "  FROM archive_records WHERE 1=1 "
                "{where} "
                "  GROUP BY series_id"
                ") latest ON r.series_id = latest.series_id AND r.version = latest.max_v "
                "WHERE 1=1 "
            )
        else:
            sql = "SELECT * FROM archive_records WHERE 1=1"
        params: list = []
        where_clauses = ""

        def _add_filter(clause: str, *vals) -> None:
            nonlocal where_clauses
            where_clauses += f" AND {clause}"
            params.extend(vals)

        prefix = "r." if latest_only else ""
        if filters.get("start_date"):
            _add_filter(f"{prefix}schedule_start_norm >= ?", filters["start_date"])
        if filters.get("end_date"):
            _add_filter(f"{prefix}schedule_start_norm <= ?", filters["end_date"])
        if filters.get("system_name"):
            _add_filter(f"{prefix}system_name LIKE ?", f"%{filters['system_name']}%")
        if filters.get("person"):
            person = f"%{filters['person']}%"
            _add_filter(
                f"({prefix}provider LIKE ? OR {prefix}executor LIKE ? OR {prefix}reviewer LIKE ? OR {prefix}security_officer LIKE ?)",
                person, person, person, person,
            )
        if filters.get("product_type"):
            _add_filter(f"{prefix}product_type = ?", filters["product_type"])
        if filters.get("action"):
            _add_filter(f"{prefix}action = ?", filters["action"])

        if latest_only:
            sql = sql.replace("{where}", where_clauses)
        sql += where_clauses
        sql += " ORDER BY schedule_start_norm DESC, series_id, version DESC"

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_series_history(self, series_id: str) -> list[dict]:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM archive_records WHERE series_id = ? ORDER BY version",
                [series_id],
            ).fetchall()
            return [dict(row) for row in rows]

    def compare_versions(self, series_id: str, from_version: int, to_version: int) -> dict:
        """Compare two versions of the same series."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            old = conn.execute(
                "SELECT * FROM archive_records WHERE series_id = ? AND version = ?",
                [series_id, from_version],
            ).fetchone()
            new = conn.execute(
                "SELECT * FROM archive_records WHERE series_id = ? AND version = ?",
                [series_id, to_version],
            ).fetchone()

        if not old or not new:
            raise FileNotFoundError(f"版本不存在: series={series_id}, v{from_version}→v{to_version}")

        old_data = self._load_snapshot_by_path(old["json_path"])
        new_data = self._load_snapshot_by_path(new["json_path"])

        if not old_data or not new_data:
            raise FileNotFoundError("快照JSON文件丢失")

        section_diffs = self._compute_section_diffs(old_data, new_data)
        summary = _compute_diff_summary(old_data, new_data)

        return {
            "series_id": series_id,
            "from_version": from_version,
            "to_version": to_version,
            "old_title": old["title"],
            "new_title": new["title"],
            "old_downloaded_at": old["downloaded_at"],
            "new_downloaded_at": new["downloaded_at"],
            "summary": summary,
            "section_diffs": section_diffs,
        }

    def rebuild_summary_excel(self, output_path: Optional[Path] = None,
                              latest_only: bool = False):
        """Rebuild the summary Excel from SQLite."""
        output_path = output_path or self.excel_path
        records = self.query_summary(latest_only=latest_only)

        wb = Workbook()
        ws = wb.active
        ws.title = "检修工作汇总"

        for col_idx, header in enumerate(ARCHIVE_HEADERS, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = THIN_BORDER

        for row_idx, record in enumerate(records, 2):
            values = [
                row_idx - 1,
                record.get("archive_date", ""),
                f"v{record.get('version', 1)}",
                record.get("product_type", ""),
                record.get("system_name", ""),
                record.get("action", ""),
                record.get("title", ""),
                record.get("network", ""),
                record.get("location", ""),
                record.get("org", ""),
                record.get("resource_set", ""),
                record.get("schedule_start", ""),
                record.get("schedule_end", ""),
                record.get("provider", ""),
                record.get("executor", ""),
                record.get("reviewer", ""),
                record.get("security_officer", ""),
                record.get("business_impact", ""),
                record.get("rollback_method", ""),
                record.get("key_params", ""),
                record.get("change_summary", ""),
                record.get("docx_path", ""),
            ]
            version_fill = VERSION_FILLS.get(record.get("version", 1), PatternFill())
            for col_idx, value in enumerate(values, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.font = BODY_FONT
                cell.alignment = BODY_ALIGNMENT
                cell.border = THIN_BORDER
                if record.get("version", 1) > 1:
                    cell.fill = version_fill

        for col_idx, width in enumerate(EXCEL_COL_WIDTHS, 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = min(width, 50)

        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"

        wb.save(str(output_path))

    def delete_old_version_files(self, start_date: str = "", end_date: str = "") -> dict:
        """Delete files for non-latest versions within the date range.

        Returns counts of deleted version dirs and affected series.
        """
        filters = {}
        if start_date:
            filters["start_date"] = start_date
        if end_date:
            filters["end_date"] = end_date
        records = self.query_summary(filters)

        # Group by series_id, find latest version per series.
        series_latest: dict[str, int] = {}
        for r in records:
            sid = r["series_id"]
            ver = r["version"]
            if sid not in series_latest or ver > series_latest[sid]:
                series_latest[sid] = ver

        deleted = 0
        with sqlite3.connect(str(self.db_path)) as conn:
            for r in records:
                sid = r["series_id"]
                if r["version"] >= series_latest[sid]:
                    continue
                self._remove_version_files(r, conn)
                deleted += 1
            conn.commit()

        self.rebuild_summary_excel()
        return {"deleted_count": deleted, "series_affected": len(series_latest)}

    def delete_all_files(self, start_date: str = "", end_date: str = "") -> dict:
        """Delete all version files within the date range, keeping DB records."""
        filters = {}
        if start_date:
            filters["start_date"] = start_date
        if end_date:
            filters["end_date"] = end_date
        records = self.query_summary(filters)

        with sqlite3.connect(str(self.db_path)) as conn:
            for r in records:
                self._remove_version_files(r, conn)
            conn.commit()

        self.rebuild_summary_excel()
        return {"deleted_count": len(records)}

    def _remove_version_files(self, record: dict, conn: sqlite3.Connection) -> None:
        """Remove physical files and delete the record from archive_records (archive_log untouched)."""
        docx_path = record.get("docx_path", "")
        json_path = record.get("json_path", "")
        version_dir = Path(docx_path).parent if docx_path else None

        # Remove files.
        for path_str in (docx_path, json_path):
            if path_str:
                p = Path(path_str)
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass

        # Remove version directory if empty, plus any diff markdown files.
        if version_dir and version_dir.exists():
            for leftover in version_dir.glob("diff_from_*.md"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            try:
                remaining = list(version_dir.iterdir())
                if not remaining:
                    version_dir.rmdir()
                    # Try to clean up parent date directory too.
                    date_dir = version_dir.parent
                    if date_dir != self.root and not any(date_dir.iterdir()):
                        date_dir.rmdir()
            except OSError:
                pass

        # Delete from active records; archive_log keeps the immutable trace.
        conn.execute("DELETE FROM archive_records WHERE id = ?", [record["id"]])

    # ── internal helpers ────────────────────────────────────────────

    def _migrate_series_date(self, series_id: str, old_date: str, new_date: str):
        """Move all series version directories from old_date to new_date folder."""
        if old_date == new_date:
            return
        old_dir = self.root / old_date
        new_dir = self.root / new_date
        if not old_dir.exists():
            return
        new_dir.mkdir(parents=True, exist_ok=True)
        # Move version dirs that belong to this series and update both tables.
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id, docx_path, json_path FROM archive_records WHERE series_id = ?",
                [series_id],
            ).fetchall()
            for row_id, docx_path, json_path in rows:
                old_docx = Path(docx_path)
                old_json = Path(json_path)
                # Move the version directory.
                version_dir = old_docx.parent
                if version_dir.exists() and version_dir.parent == old_dir:
                    dest = new_dir / version_dir.name
                    if not dest.exists():
                        shutil.move(str(version_dir), str(dest))
                new_docx = str(docx_path).replace(str(old_dir), str(new_dir))
                new_json = str(json_path).replace(str(old_dir), str(new_dir))
                for tbl in ("archive_records", "archive_log"):
                    conn.execute(
                        f"UPDATE {tbl} SET docx_path = ?, json_path = ? "
                        "WHERE series_id = ? AND docx_path = ?",
                        [new_docx, new_json, series_id, docx_path],
                    )
            conn.commit()
        # Clean up empty old date directory.
        try:
            remaining = list(old_dir.iterdir())
            if not remaining:
                old_dir.rmdir()
        except OSError:
            pass

    def _already_archived(self, file_id: str) -> bool:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM archive_log WHERE file_id = ? LIMIT 1",
                [file_id],
            ).fetchone()
            return row is not None

    def _match_series(self, title: str, system_name: str, action: str,
                      product_type: str, schedule_start: str = "") -> tuple[str, int, Optional[str]]:
        """Three-tier matching with product-type guard and 5-day window.

        Tier 1: exact title match.
        Tier 2: fuzzy system+action — requires same product_type, similarity
                above 0.85, and schedule_start within 5 days of the latest
                version in the series.
        Tier 3: new series.
        """
        new_schedule_date = _maintenance_date({"schedule_start": schedule_start})

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Tier 1: exact title match — also enforce 5-day window.
            row = conn.execute(
                "SELECT r.series_id, MAX(r.version) as max_ver, r.file_id, r.schedule_start "
                "FROM archive_records r "
                "INNER JOIN ("
                "  SELECT series_id, MAX(version) as max_v "
                "  FROM archive_records WHERE title = ? GROUP BY series_id"
                ") latest ON r.series_id = latest.series_id AND r.version = latest.max_v "
                "WHERE r.title = ? "
                "GROUP BY r.series_id ORDER BY max_ver DESC LIMIT 1",
                [title, title],
            ).fetchone()
            if row:
                old_date = _maintenance_date({"schedule_start": row["schedule_start"] or ""})
                try:
                    old_dt = datetime.strptime(old_date, "%Y-%m-%d")
                    new_dt = datetime.strptime(new_schedule_date, "%Y-%m-%d")
                    if abs((new_dt - old_dt).days) <= 5:
                        return row["series_id"], row["max_ver"] + 1, row["file_id"]
                except ValueError:
                    return row["series_id"], row["max_ver"] + 1, row["file_id"]
                # Title matches but dates too far apart — fall through to Tier 2.

            # Tier 2: fuzzy system+action — product_type must match.
            all_rows = conn.execute(
                "SELECT r.series_id, r.system_name, r.action, r.product_type, "
                "       r.schedule_start, MAX(r.version) as max_ver, r.file_id "
                "FROM archive_records r "
                "INNER JOIN ("
                "  SELECT series_id, MAX(version) as max_v "
                "  FROM archive_records GROUP BY series_id"
                ") latest ON r.series_id = latest.series_id AND r.version = latest.max_v "
                "GROUP BY r.series_id",
            ).fetchall()
            best_ratio = 0
            best_match = None
            target = f"{system_name}{action}"
            for r in all_rows:
                # Product type guard — different products are never the same series.
                if r["product_type"] != product_type:
                    continue
                # 5-day window — same-named monthly maintenance is a different job.
                old_date = _maintenance_date({"schedule_start": r["schedule_start"] or ""})
                try:
                    old_dt = datetime.strptime(old_date, "%Y-%m-%d")
                    new_dt = datetime.strptime(new_schedule_date, "%Y-%m-%d")
                    if abs((new_dt - old_dt).days) > 5:
                        continue
                except ValueError:
                    pass  # Unparseable dates — don't filter out.
                candidate = f"{r['system_name']}{r['action']}"
                ratio = SequenceMatcher(None, target.lower(), candidate.lower()).ratio()
                if ratio > 0.85 and ratio > best_ratio:
                    best_ratio = ratio
                    best_match = r
            if best_match:
                return best_match["series_id"], best_match["max_ver"] + 1, best_match["file_id"]

            # Tier 3: new series.
            key = f"{system_name}|{action}|{product_type}"
            series_hash = hashlib.md5(key.encode()).hexdigest()[:10]
            new_id = f"AR-{_today().replace('-', '')}-{series_hash}"
            return new_id, 1, None

    _INSERT_SQL = """
        INSERT INTO {table} (
            series_id, version, file_id, title, product_type, system_name, action,
            network, location, org, resource_set,
            schedule_start, schedule_end, schedule_start_norm,
            provider, executor, reviewer, security_officer,
            business_impact, rollback_method, key_params, change_summary,
            archive_date, downloaded_at, docx_path, json_path, parent_file_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def _insert_record(self, record: dict):
        schedule_start_norm = _maintenance_date({"schedule_start": record.get("schedule_start", "")})
        values = [
            record["series_id"], record["version"], record["file_id"], record["title"],
            record["product_type"], record["system_name"], record["action"],
            record["network"], record["location"], record["org"], record["resource_set"],
            record["schedule_start"], record["schedule_end"], schedule_start_norm,
            record["provider"], record["executor"], record["reviewer"], record["security_officer"],
            record["business_impact"], record["rollback_method"], record["key_params"],
            record["change_summary"],
            record["archive_date"], record["downloaded_at"],
            record["docx_path"], record["json_path"], record.get("parent_file_id"),
        ]
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(self._INSERT_SQL.format(table="archive_records"), values)
            conn.execute(self._INSERT_SQL.format(table="archive_log"), values)
            conn.commit()

    def _load_snapshot(self, file_id: str) -> Optional[dict]:
        """Load a plan snapshot by file_id from the in-memory store or archive records."""
        data = _generated_documents.get(file_id)
        if data:
            return deepcopy(data)
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT json_path FROM archive_records WHERE file_id = ? LIMIT 1",
                [file_id],
            ).fetchone()
            if row:
                return self._load_snapshot_by_path(row[0])
        return None

    def _load_snapshot_by_path(self, json_path: str) -> Optional[dict]:
        path = Path(json_path)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _compute_section_diffs(self, old_data: dict, new_data: dict) -> list[dict]:
        diffs = []
        try:
            old_secs_map = {}
            for s in old_data.get("document", {}).get("sections", []):
                if isinstance(s, dict) and s.get("heading"):
                    old_secs_map[_norm_heading(s["heading"])] = s
            new_secs_map = {}
            for s in new_data.get("document", {}).get("sections", []):
                if isinstance(s, dict) and s.get("heading"):
                    new_secs_map[_norm_heading(s["heading"])] = s

            all_names = list(dict.fromkeys(list(old_secs_map.keys()) + list(new_secs_map.keys())))
            for name in all_names:
                o_sigs = _block_sigs(old_secs_map[name]) if name in old_secs_map else []
                n_sigs = _block_sigs(new_secs_map[name]) if name in new_secs_map else []

                if not o_sigs and not n_sigs:
                    continue
                if o_sigs == n_sigs:
                    status = "unchanged"
                elif not o_sigs:
                    status = "added"
                elif not n_sigs:
                    status = "removed"
                else:
                    o_set = set(o_sigs)
                    n_set = set(n_sigs)
                    changed = len(o_set ^ n_set)
                    if changed == 0:
                        status = "unchanged"
                    elif changed <= 2:
                        status = "modified"
                    else:
                        status = "modified"

                display_name = ""
                if name in new_secs_map:
                    display_name = new_secs_map[name].get("heading", "")
                elif name in old_secs_map:
                    display_name = old_secs_map[name].get("heading", "")

                diffs.append({
                    "heading": display_name,
                    "status": status,
                })
        except Exception:
            pass
        return diffs


# ── singleton ────────────────────────────────────────────────────────
_archive_store: Optional[ArchiveStore] = None


def get_archive_store() -> ArchiveStore:
    global _archive_store
    if _archive_store is None:
        _archive_store = ArchiveStore()
    return _archive_store


def reset_archive_store() -> None:
    """Reset the singleton so the next call to get_archive_store() creates a new instance."""
    global _archive_store
    _archive_store = None
