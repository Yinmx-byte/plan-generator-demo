"""Administrative routes for skills, knowledge documents, and RAG."""

import os
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from rag import get_knowledge_base, reset_knowledge_base
from runtime import ROOT, SKILLS_ROOT, get_skill_registry, reset_skill_runtime
from services.plan_generation import docx_to_markdown

router = APIRouter()

@router.get("/api/skills")
async def list_skills():
    registry = get_skill_registry()
    return {
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
            }
            for skill in registry.skills
        ]
    }


def safe_skill_dir_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip(".-")
    if not value:
        raise HTTPException(status_code=400, detail="Skill 名称不能为空")
    return value


def safe_relative_dir(name: str) -> str:
    value = name.strip().replace("\\", "/").strip("/")
    if not value:
        return ""
    parts = [safe_skill_dir_name(part) for part in value.split("/") if part.strip()]
    return "/".join(parts)


def safe_file_stem(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "-", name.strip()).strip(".-")
    return value or "knowledge"


def ensure_within_directory(base: Path, target: Path) -> None:
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    if not str(target_resolved).startswith(str(base_resolved)):
        raise HTTPException(status_code=400, detail="非法文件路径")


def list_knowledge_documents() -> list[dict[str, Any]]:
    knowledge_root = ROOT / "knowledge"
    if not knowledge_root.exists():
        return []
    docs = []
    for path in sorted([*knowledge_root.glob("**/*.md"), *knowledge_root.glob("**/*.txt")]):
        if path.name == ".gitkeep":
            continue
        docs.append(
            {
                "name": path.name,
                "path": str(path.relative_to(knowledge_root)),
                "size": path.stat().st_size,
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            }
        )
    return docs


@router.post("/api/skills/upload")
async def upload_skill(
    file: UploadFile = File(...),
    skill_name: str = Form(default=""),
):
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")

    if suffix == ".zip":
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(tmp_path) as archive:
                skill_entries = [
                    name
                    for name in archive.namelist()
                    if not name.endswith("/") and Path(name).name == "SKILL.md"
                ]
                if not skill_entries:
                    raise HTTPException(status_code=400, detail="zip 中未找到 SKILL.md")
                skill_root_parts = Path(skill_entries[0]).parent.parts
                inferred_name = skill_root_parts[-1] if skill_root_parts else Path(filename).stem
                target_dir = SKILLS_ROOT / safe_skill_dir_name(skill_name or inferred_name)
                target_dir.mkdir(parents=True, exist_ok=True)
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    member_path = Path(member.filename)
                    if ".." in member_path.parts:
                        raise HTTPException(status_code=400, detail="zip 中包含非法路径")
                    parts = member_path.parts
                    if skill_root_parts and parts[: len(skill_root_parts)] == skill_root_parts:
                        parts = parts[len(skill_root_parts) :]
                    if not parts:
                        continue
                    output_path = target_dir.joinpath(*parts)
                    ensure_within_directory(target_dir, output_path)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(archive.read(member))
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        text = raw.decode("utf-8")
        if "name:" not in text and "# " not in text:
            raise HTTPException(status_code=400, detail="请上传有效的 SKILL.md")
        target_name = safe_skill_dir_name(skill_name or Path(filename).stem or "uploaded-skill")
        target_dir = SKILLS_ROOT / target_name
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(text, encoding="utf-8")

    await reset_skill_runtime()
    return {
        "status": "ok",
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
            }
            for skill in get_skill_registry().skills
        ],
    }


@router.get("/api/knowledge")
async def list_knowledge():
    return {
        "documents": list_knowledge_documents(),
        "supported_extensions": [".md", ".txt", ".docx"],
        "rag_enabled": get_knowledge_base(SKILLS_ROOT) is not None,
        "chunk_size": int(os.getenv("RAG_CHUNK_SIZE", "800")),
        "split_by": os.getenv("RAG_SPLIT_BY", "paragraph"),
        "top_k": int(os.getenv("RAG_TOP_K", "5")),
        "score_threshold": os.getenv("RAG_SCORE_THRESHOLD"),
    }


@router.post("/api/knowledge/upload")
async def upload_knowledge(
    file: UploadFile = File(...),
    category: str = Form(default="uploaded"),
):
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".md", ".txt", ".docx"}:
        raise HTTPException(status_code=400, detail="仅支持 .md、.txt、.docx 知识文档")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")

    knowledge_root = ROOT / "knowledge"
    target_dir = knowledge_root / safe_relative_dir(category or "uploaded")
    target_dir.mkdir(parents=True, exist_ok=True)
    ensure_within_directory(knowledge_root, target_dir)

    if suffix == ".docx":
        content = docx_to_markdown(raw)
        target_path = target_dir / f"{safe_file_stem(Path(filename).stem)}.md"
    else:
        content = raw.decode("utf-8")
        target_path = target_dir / f"{safe_file_stem(Path(filename).stem)}{suffix}"
    ensure_within_directory(knowledge_root, target_path)
    target_path.write_text(content, encoding="utf-8")

    reset_knowledge_base()
    return {
        "status": "ok",
        "path": str(target_path.relative_to(knowledge_root)),
        "documents": list_knowledge_documents(),
    }


@router.post("/api/rag/reindex")
async def reindex_rag():
    reset_knowledge_base()
    knowledge_base = get_knowledge_base(SKILLS_ROOT)
    if knowledge_base is None:
        return {
            "status": "disabled",
            "message": "RAG 未启用：请配置 OPENAI_API_KEY 或 EMBEDDING_API_KEY。",
        }
    await knowledge_base.get_knowledge()
    return {
        "status": "ok",
        "documents": list_knowledge_documents(),
    }


@router.get("/api/rag/retrieve")
async def retrieve_rag(query: str = Query(default="", description="检索问题")):
    knowledge_base = get_knowledge_base(SKILLS_ROOT)
    if knowledge_base is None:
        return {
            "enabled": False,
            "chunks": [],
            "message": "RAG 未启用：请配置 OPENAI_API_KEY 或 EMBEDDING_API_KEY。",
        }
    return {
        "enabled": True,
        "chunks": await knowledge_base.retrieve(query),
    }


