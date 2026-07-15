"""Administrative APIs for the remote high-quality plan reference library."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from services.quality_reference_store import get_quality_reference_store

router = APIRouter(prefix="/api/quality-references", tags=["quality-references"])


@router.get("/status")
async def quality_reference_status():
    try:
        return get_quality_reference_store().status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("")
async def list_quality_references(limit: int = Query(default=100, ge=1, le=500)):
    try:
        records = get_quality_reference_store().list_records(limit)
        return {"records": records, "count": len(records)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/upload")
async def upload_quality_reference(
    file: UploadFile = File(...),
    product_type: str = Form(...),
    operation_type: str = Form(default="general"),
    network: str = Form(default=""),
    skill_name: str = Form(default=""),
):
    filename = Path(file.filename or "reference.docx").name
    if Path(filename).suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="优质方案参考库仅接受 DOCX 文件")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as temp:
            temp.write(raw)
            temp_path = Path(temp.name)
        result = get_quality_reference_store().import_file(
            temp_path,
            metadata={
                "title": Path(filename).stem,
                "source_filename": filename,
                "source_path": filename,
                "product_type": product_type,
                "operation_type": operation_type,
                "network": network,
            },
            skill_name=skill_name,
        )
        if result.get("status") == "skipped" and result.get("reason") == "unknown_product":
            raise HTTPException(status_code=400, detail="无法识别产品类型")
        return result
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
