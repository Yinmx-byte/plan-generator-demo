"""Archive API routes – summary query, Excel export, version history, diff comparison."""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from services.plan_archive import _save_persisted_root, get_archive_store

router = APIRouter()


class ArchiveConfigUpdate(BaseModel):
    archive_root: str = ""


def _parse_summary_params(
    start_date: str = "",
    end_date: str = "",
    system_name: str = "",
    person: str = "",
    product_type: str = "",
    action: str = "",
) -> dict:
    filters = {}
    if start_date:
        filters["start_date"] = start_date
    if end_date:
        filters["end_date"] = end_date
    if system_name:
        filters["system_name"] = system_name
    if person:
        filters["person"] = person
    if product_type:
        filters["product_type"] = product_type
    if action:
        filters["action"] = action
    return filters


@router.get("/api/archive/summary")
async def archive_summary(
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
    system_name: str = Query(default=""),
    person: str = Query(default=""),
    product_type: str = Query(default=""),
    action: str = Query(default=""),
    latest_only: bool = Query(default=False),
):
    store = get_archive_store()
    filters = _parse_summary_params(start_date, end_date, system_name, person, product_type, action)
    records = store.query_summary(filters, latest_only=latest_only)
    return {
        "records": records,
        "count": len(records),
        "filters": {k: v for k, v in filters.items() if v},
        "latest_only": latest_only,
    }


@router.get("/api/archive/summary/excel")
async def archive_summary_excel(
    latest_only: bool = Query(default=False),
):
    store = get_archive_store()
    store.rebuild_summary_excel(latest_only=latest_only)
    path = store.excel_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="汇总表尚未生成")
    filename = "检修工作汇总表.xlsx"
    return FileResponse(
        path=str(path),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/api/archive/series/{series_id}")
async def archive_series_history(series_id: str):
    store = get_archive_store()
    records = store.get_series_history(series_id)
    if not records:
        raise HTTPException(status_code=404, detail="未找到该系列归档记录")
    return {
        "series_id": series_id,
        "records": records,
        "count": len(records),
    }


@router.get("/api/archive/compare")
async def archive_compare_versions(
    series_id: str = Query(...),
    from_version: int = Query(...),
    to_version: int = Query(...),
):
    store = get_archive_store()
    try:
        result = store.compare_versions(series_id, from_version, to_version)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


@router.get("/api/archive/config")
async def archive_get_config():
    store = get_archive_store()
    return {
        "archive_root": str(store.root),
        "db_path": str(store.db_path),
        "excel_path": str(store.excel_path),
    }


@router.put("/api/archive/config")
async def archive_update_config(request: ArchiveConfigUpdate):
    if not request.archive_root.strip():
        raise HTTPException(status_code=400, detail="归档根目录不能为空")
    os.environ["PLAN_ARCHIVE_ROOT"] = request.archive_root
    _save_persisted_root(request.archive_root)
    from services.plan_archive import reset_archive_store

    reset_archive_store()
    store = get_archive_store()
    return {
        "archive_root": str(store.root),
        "db_path": str(store.db_path),
        "excel_path": str(store.excel_path),
    }


@router.get("/api/archive/cleanup/scope")
async def archive_cleanup_scope(
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
):
    """Preview how many files / series would be affected by a cleanup."""
    store = get_archive_store()
    filters = _parse_summary_params(start_date=start_date, end_date=end_date)
    records = store.query_summary(filters)
    series_set = set(r["series_id"] for r in records)
    series_latest: dict[str, int] = {}
    for r in records:
        sid = r["series_id"]
        if sid not in series_latest or r["version"] > series_latest[sid]:
            series_latest[sid] = r["version"]
    old_count = sum(1 for r in records if r["version"] < series_latest[r["series_id"]])
    return {
        "total_records": len(records),
        "series_count": len(series_set),
        "old_version_count": old_count,
    }


@router.delete("/api/archive/files/old-versions")
async def archive_delete_old_versions(
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
):
    """Delete old version files, keeping the latest per series."""
    store = get_archive_store()
    result = store.delete_old_version_files(start_date, end_date)
    return {"status": "ok", **result}


@router.delete("/api/archive/files/all")
async def archive_delete_all_files(
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
):
    """Delete all version files in the date range, keeping DB records."""
    store = get_archive_store()
    result = store.delete_all_files(start_date, end_date)
    return {"status": "ok", **result}