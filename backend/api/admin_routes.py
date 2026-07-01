"""Administrative routes for skills, knowledge documents, and RAG."""

import os
import re
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from rag import get_knowledge_base, reset_knowledge_base
from rag import bailian_admin
from runtime import ROOT, SKILLS_ROOT, get_skill_registry, reset_skill_runtime

router = APIRouter()


class SkillUpdateRequest(BaseModel):
    content: str


class SkillIteratorRequest(BaseModel):
    skill_name: str
    reference_dir: str
    message: str = ""
    state_json: str = ""
    allow_partial: bool = False
    api_url: str = ""


class BailianIndexCreateRequest(BaseModel):
    category_name: str = ""
    index_name: str = ""


SKILL_DISPLAY_NAMES = {
    "cloud-maintenance-master-workflow": "云平台主控工作流",
    "maintenance-plan-composer": "检修方案通用编排",
    "ecs-lifecycle-maintenance": "ECS 生命周期检修",
    "slb-maintenance-plan": "负载均衡 SLB 检修",
    "oss-maintenance-plan": "对象存储 OSS 检修",
    "rds-maintenance-plan": "RDS 数据库检修",
    "redis-maintenance-plan": "Redis 检修",
    "mq-maintenance-plan": "消息队列 MQ 检修",
    "polardb-maintenance-plan": "PolarDB 数据库检修",
    "database-maintenance-plan": "数据库通用检修",
    "component-scaling-plan": "组件扩缩容检修",
    "restart-maintenance-plan": "重启类检修",
    "k8s-worker-maintenance": "K8s Worker 检修",
    "generic-maintenance-plan": "通用兜底检修",
    "docx-document-editor": "DOCX 文档修订",
}


def skill_payload(skill) -> dict:
    display_name = skill.metadata.get("display_name") or SKILL_DISPLAY_NAMES.get(skill.name) or skill.name
    return {
        "name": skill.name,
        "display_name": display_name,
        "description": skill.description,
        "path": str(skill.path),
    }


@router.get("/api/skills")
async def list_skills():
    registry = get_skill_registry()
    return {
        "skills": [skill_payload(skill) for skill in registry.skills]
    }


def safe_skill_dir_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip(".-")
    if not value:
        raise HTTPException(status_code=400, detail="Skill 名称不能为空")
    return value


def get_skill_or_404(skill_name: str):
    registry = get_skill_registry()
    skill = registry.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="未找到指定 Skill")
    ensure_within_directory(SKILLS_ROOT, skill.path)
    return skill


def ensure_within_directory(base: Path, target: Path) -> None:
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    if not str(target_resolved).startswith(str(base_resolved)):
        raise HTTPException(status_code=400, detail="非法文件路径")


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
        "skills": [skill_payload(skill) for skill in get_skill_registry().skills],
    }


@router.get("/api/skills/{skill_name}")
async def get_skill_detail(skill_name: str):
    skill = get_skill_or_404(skill_name)
    skill_file = skill.path / "SKILL.md"
    ensure_within_directory(SKILLS_ROOT, skill_file)
    if not skill_file.exists():
        raise HTTPException(status_code=404, detail="未找到 SKILL.md")
    return {
        "name": skill.name,
        "display_name": skill_payload(skill)["display_name"],
        "description": skill.description,
        "path": str(skill.path),
        "content": skill_file.read_text(encoding="utf-8"),
    }


@router.put("/api/skills/{skill_name}")
async def update_skill_detail(skill_name: str, request: SkillUpdateRequest):
    skill = get_skill_or_404(skill_name)
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Skill 内容不能为空")
    if len(content) > 200_000:
        raise HTTPException(status_code=400, detail="Skill 内容过大")
    if "name:" not in content and "# " not in content:
        raise HTTPException(status_code=400, detail="请保存有效的 SKILL.md 内容")
    skill_file = skill.path / "SKILL.md"
    ensure_within_directory(SKILLS_ROOT, skill_file)
    skill_file.write_text(content + "\n", encoding="utf-8")
    await reset_skill_runtime()
    updated = get_skill_or_404(skill_name)
    return {
        "status": "ok",
        "name": updated.name,
        "display_name": skill_payload(updated)["display_name"],
        "description": updated.description,
        "path": str(updated.path),
    }


@router.delete("/api/skills/{skill_name}")
async def delete_skill(skill_name: str):
    skill = get_skill_or_404(skill_name)
    ensure_within_directory(SKILLS_ROOT, skill.path)
    if skill.path == SKILLS_ROOT:
        raise HTTPException(status_code=400, detail="不能删除 Skill 根目录")
    shutil.rmtree(skill.path)
    await reset_skill_runtime()
    return {
        "status": "ok",
        "deleted": skill.name,
        "skills": [skill_payload(item) for item in get_skill_registry().skills],
    }


def iterator_script_path(name: str) -> Path:
    script = Path.home() / ".codex" / "skills" / "maintenance-skill-iterator" / "scripts" / name
    if not script.exists():
        raise HTTPException(status_code=500, detail=f"未找到自迭代脚本：{script}")
    return script


def run_iterator_subprocess(command: list[str], timeout: int = 420) -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "自迭代脚本执行失败",
                "returncode": process.returncode,
                "stdout": process.stdout[-4000:],
                "stderr": process.stderr[-4000:],
            },
        )
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "自迭代脚本输出不是合法 JSON",
                "stdout": process.stdout[-4000:],
            },
        ) from exc


def skill_product_hint(skill_name: str) -> str:
    lowered = skill_name.lower()
    for product in ("ecs", "slb", "oss", "rds", "redis", "mq", "polardb", "k8s"):
        if product in lowered:
            return product
    return ""


def resolve_iterator_reference_dir(reference_dir: Path, skill_name: str) -> Path:
    if any(path.suffix.lower() == ".docx" and not path.name.startswith("~$") for path in reference_dir.iterdir()):
        return reference_dir
    product = skill_product_hint(skill_name)
    doc_dirs = sorted(
        {
            path.parent
            for path in reference_dir.rglob("*.docx")
            if not path.name.startswith("~$")
        },
        key=lambda item: (len(item.parts), str(item)),
    )
    if not doc_dirs:
        raise HTTPException(status_code=400, detail="优质文档路径下未找到可评估的 docx 文件")
    if product:
        for path in doc_dirs:
            if product.lower() in str(path).lower():
                return path
    return doc_dirs[0]


@router.post("/api/skill-iterator/run")
async def run_skill_iterator(request_body: SkillIteratorRequest, request: Request):
    skill = get_skill_or_404(request_body.skill_name)
    reference_dir = Path(request_body.reference_dir)
    if not reference_dir.exists() or not reference_dir.is_dir():
        raise HTTPException(status_code=400, detail="优质文档路径不存在或不是目录")
    resolved_reference_dir = resolve_iterator_reference_dir(reference_dir, skill.name)
    source_skill = skill.path / "SKILL.md"
    ensure_within_directory(SKILLS_ROOT, source_skill)
    output_dir = ROOT.parent / "docs" / "iterator-output"
    output_dir.mkdir(parents=True, exist_ok=True)

    has_generation_input = bool(request_body.message.strip() or request_body.state_json.strip())
    if has_generation_input:
        script = iterator_script_path("generate_and_evaluate_plan.py")
        api_url = request_body.api_url.strip() or str(request.base_url).rstrip("/") + "/api/dev/plan-test"
        command = [
            sys.executable,
            str(script),
            "--api-url",
            api_url,
            "--reference-dir",
            str(resolved_reference_dir),
            "--source-skill",
            str(source_skill),
            "--output-dir",
            str(output_dir),
        ]
        if request_body.message.strip():
            command.extend(["--message", request_body.message.strip()])
        if request_body.state_json.strip():
            command.extend(["--state-json", request_body.state_json.strip()])
        if request_body.allow_partial:
            command.append("--allow-partial")
    else:
        script = iterator_script_path("evaluate_plan_quality.py")
        command = [
            sys.executable,
            str(script),
            "--reference-dir",
            str(resolved_reference_dir),
            "--candidate-skill",
            str(source_skill),
        ]

    result = await run_blocking(run_iterator_subprocess, command)
    return {
        "status": "ok",
        "mode": "generated_docx" if has_generation_input else "skill_static_preflight",
        "skill": skill_payload(skill),
        "reference_dir": str(reference_dir),
        "resolved_reference_dir": str(resolved_reference_dir),
        "result": result,
    }


@router.get("/api/bailian/knowledge/status")
async def bailian_knowledge_status():
    return {
        "rag_enabled": get_knowledge_base(SKILLS_ROOT) is not None,
        "provider": os.getenv("RAG_PROVIDER", "bailian"),
        "workspace_id": os.getenv("BAILIAN_WORKSPACE_ID"),
        "index_id": os.getenv("BAILIAN_INDEX_ID"),
        "last_index_job_id": os.getenv("BAILIAN_LAST_INDEX_JOB_ID"),
        "category_name": os.getenv("BAILIAN_CATEGORY_NAME", "plan-generator-ecs"),
        "index_name": os.getenv("BAILIAN_INDEX_NAME", "pg-ecs-v4"),
        "chunk_size": int(os.getenv("BAILIAN_CHUNK_SIZE", "800")),
        "overlap_size": int(os.getenv("BAILIAN_OVERLAP_SIZE", "100")),
        "top_k": int(os.getenv("RAG_TOP_K", "5")),
        "rerank_enabled": os.getenv("BAILIAN_RAG_ENABLE_RERANK", "true"),
        "rerank_min_score": os.getenv("BAILIAN_RAG_RERANK_MIN_SCORE"),
        "embedding_model": os.getenv("BAILIAN_EMBEDDING_MODEL_NAME", "text-embedding-v4"),
        "rerank_model": os.getenv("BAILIAN_RERANK_MODEL_NAME", "qwen3-rerank-hybrid"),
    }


@router.get("/api/bailian/files")
async def list_bailian_files(category_name: str = Query(default="")):
    try:
        files = await run_blocking(bailian_admin.list_files, category_name or None)
        return {"files": files, "count": len(files)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/bailian/files/upload")
async def upload_bailian_file(
    file: UploadFile = File(...),
    category_name: str = Form(default=""),
):
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".md", ".txt", ".docx", ".pdf"}:
        raise HTTPException(status_code=400, detail="仅支持 .md、.txt、.docx、.pdf 知识文档")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        result = await run_blocking(
            bailian_admin.upload_file,
            raw,
            filename,
            category_name or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "status": "ok",
        "file": result,
    }


@router.delete("/api/bailian/files/{file_id}")
async def delete_bailian_file(file_id: str):
    try:
        return await run_blocking(bailian_admin.delete_file, file_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/bailian/files/{file_id}")
async def describe_bailian_file(file_id: str):
    try:
        file_detail = await run_blocking(bailian_admin.describe_file, file_id)
        index_detail = await run_blocking(bailian_admin.get_index_file_detail, file_id)
        return {
            "file": file_detail,
            "index_document": index_detail,
            "message": "百炼接口不直接返回原始文档正文；如果文件提供解析结果下载地址，则这里会返回解析文本预览。",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/bailian/index/create")
async def create_bailian_index(request: BailianIndexCreateRequest):
    try:
        result = await run_blocking(
            bailian_admin.create_index,
            request.category_name or None,
            request.index_name or None,
        )
        reset_knowledge_base()
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/bailian/index/job")
async def get_bailian_index_job(
    job_id: str = Query(default=""),
    index_id: str = Query(default=""),
):
    try:
        return await run_blocking(
            bailian_admin.get_index_job,
            job_id or None,
            index_id or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/bailian/retrieve")
async def retrieve_bailian(query: str = Query(default="", description="检索问题")):
    if not query.strip():
        return {"nodes": [], "count": 0}
    try:
        nodes = await run_blocking(
            bailian_admin.retrieve,
            query,
            int(os.getenv("RAG_TOP_K", "5")),
        )
        return {"nodes": nodes, "count": len(nodes)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def run_blocking(func, *args):
    import asyncio

    return await asyncio.to_thread(func, *args)


