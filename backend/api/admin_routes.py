"""Administrative routes for skills, knowledge documents, and RAG."""

import os
import re
import json
import difflib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from rag import get_knowledge_base, reset_knowledge_base
from rag import bailian_admin
from runtime import ROOT, SKILLS_ROOT, get_skill_registry, reset_skill_runtime
from services.plan_generation import get_generated_file

router = APIRouter()


class SkillUpdateRequest(BaseModel):
    content: str
    reason: str = "manual-save"


class SkillDraftRequest(BaseModel):
    content: str
    reason: str = "draft"


class SkillRollbackRequest(BaseModel):
    version_id: str


class SkillIteratorRequest(BaseModel):
    skill_name: str
    reference_dir: str
    message: str = ""
    state_json: str = ""
    allow_partial: bool = False
    api_url: str = ""


class GeneratedDocumentEvaluationRequest(BaseModel):
    reference_dir: str
    skill_name: str = ""


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

SKILL_VERSIONS_ROOT = ROOT / "skill_versions"

INTERNAL_SKILL_NAMES = {
    "cloud-maintenance-master-workflow",
    "docx-document-editor",
    "maintenance-plan-workflow",
}

MAINTENANCE_SKILL_HINTS = (
    "maintenance",
    "plan",
    "检修",
    "方案",
    "ecs",
    "rds",
    "oss",
    "slb",
    "redis",
    "polardb",
    "mq",
    "k8s",
    "component",
    "database",
)


def skill_payload(skill) -> dict:
    display_name = skill.metadata.get("display_name") or SKILL_DISPLAY_NAMES.get(skill.name) or skill.name
    return {
        "name": skill.name,
        "display_name": display_name,
        "description": skill.description,
        "version": str(skill.metadata.get("version") or ""),
        "plan_generation": is_maintenance_plan_skill(skill),
        "path": str(skill.path),
    }


@router.get("/api/skills")
async def list_skills(scope: str = Query(default="all")):
    registry = get_skill_registry()
    skills = registry.skills
    if scope in {"maintenance", "plan", "plan-generation"}:
        skills = [skill for skill in skills if is_maintenance_plan_skill(skill)]
    return {
        "skills": [skill_payload(skill) for skill in skills]
    }


def is_maintenance_plan_skill(skill) -> bool:
    if skill.name in INTERNAL_SKILL_NAMES:
        return False
    text = "\n".join(
        [
            skill.name,
            skill.description or "",
            str(skill.metadata.get("description") or ""),
            str(skill.metadata.get("display_name") or ""),
        ]
    ).lower()
    return any(hint.lower() in text for hint in MAINTENANCE_SKILL_HINTS)


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


def skill_markdown_path(skill) -> Path:
    skill_file = skill.path / "SKILL.md"
    ensure_within_directory(SKILLS_ROOT, skill_file)
    if not skill_file.exists():
        raise HTTPException(status_code=404, detail="SKILL.md not found")
    return skill_file


def read_skill_markdown(skill) -> str:
    return skill_markdown_path(skill).read_text(encoding="utf-8")


def validate_skill_markdown(content: str) -> str:
    content = content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Skill content cannot be empty")
    if len(content) > 200_000:
        raise HTTPException(status_code=400, detail="Skill content is too large")
    if "name:" not in content and "# " not in content:
        raise HTTPException(status_code=400, detail="Invalid SKILL.md content")
    return content + "\n"


def skill_version_dir(skill_name: str) -> Path:
    return SKILL_VERSIONS_ROOT / safe_skill_dir_name(skill_name)


def snapshot_skill(skill, reason: str = "snapshot") -> dict:
    content = read_skill_markdown(skill)
    version_dir = skill_version_dir(skill.name)
    version_dir.mkdir(parents=True, exist_ok=True)
    version_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    md_path = version_dir / f"{version_id}.md"
    meta_path = version_dir / f"{version_id}.json"
    md_path.write_text(content, encoding="utf-8")
    meta = {
        "version_id": version_id,
        "skill_name": skill.name,
        "display_name": skill_payload(skill)["display_name"],
        "skill_version": str(skill.metadata.get("version") or ""),
        "reason": reason,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "path": str(md_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def list_skill_version_snapshots(skill_name: str) -> list[dict]:
    version_dir = skill_version_dir(skill_name)
    if not version_dir.exists():
        return []
    snapshots: list[dict] = []
    for meta_path in sorted(version_dir.glob("*.json"), reverse=True):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        md_path = version_dir / f"{meta_path.stem}.md"
        if md_path.exists():
            meta["path"] = str(md_path)
            snapshots.append(meta)
    return snapshots


def make_unified_diff(old: str, new: str, fromfile: str = "current/SKILL.md", tofile: str = "candidate/SKILL.md") -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def bump_skill_version(content: str) -> tuple[str, str]:
    version_re = re.compile(r"(?m)^version:\s*([0-9]+)\.([0-9]+)\.([0-9]+)\s*$")
    match = version_re.search(content)
    if match:
        major, minor, patch = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        new_version = f"{major}.{minor}.{patch + 1}"
        return version_re.sub(f"version: {new_version}", content, count=1), new_version
    if content.startswith("---\n"):
        new_version = "0.1.0"
        return content.replace("---\n", f"---\nversion: {new_version}\n", 1), new_version
    return content, ""


def build_iterator_skill_draft(skill, current_content: str, result: dict) -> dict:
    evaluation = result.get("evaluation") or result
    findings = evaluation.get("findings") or []
    summary = evaluation.get("recommended_patch_summary") or ""
    suggestions: list[str] = []
    for finding in findings:
        message = str(finding.get("message") or "").strip()
        change = str(finding.get("suggested_skill_change") or "").strip()
        category = str(finding.get("category") or "quality").strip()
        severity = str(finding.get("severity") or "medium").strip()
        if not message and not change:
            continue
        suggestions.append(
            f"- 【{severity}/{category}】{message}"
            + (f"\n  - Skill 调整建议：{change}" if change else "")
        )
    if not suggestions and not summary:
        return {"has_changes": False, "content": current_content, "diff": "", "suggested_version": ""}

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = [
        "",
        "## 质量迭代记录",
        "",
        f"### {timestamp}",
        "",
        "以下内容来自生成文档与优质历史方案的对比评估。应用前请人工审阅差异，确认不会把一次测试样例的个性化内容固化进通用 Skill。",
    ]
    if summary:
        block.extend(["", f"总体建议：{summary}"])
    if suggestions:
        block.extend(["", "待固化规则：", *suggestions])

    draft = current_content.rstrip() + "\n" + "\n".join(block) + "\n"
    draft, suggested_version = bump_skill_version(draft)
    return {
        "has_changes": draft != current_content,
        "content": draft,
        "diff": make_unified_diff(current_content, draft),
        "suggested_version": suggested_version,
    }


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
    skill_file = skill_markdown_path(skill)
    ensure_within_directory(SKILLS_ROOT, skill_file)
    if not skill_file.exists():
        raise HTTPException(status_code=404, detail="未找到 SKILL.md")
    return {
        "name": skill.name,
        "display_name": skill_payload(skill)["display_name"],
        "description": skill.description,
        "version": str(skill.metadata.get("version") or ""),
        "path": str(skill.path),
        "content": skill_file.read_text(encoding="utf-8"),
    }


@router.put("/api/skills/{skill_name}")
async def update_skill_detail(skill_name: str, request: SkillUpdateRequest):
    skill = get_skill_or_404(skill_name)
    content = validate_skill_markdown(request.content)
    snapshot = snapshot_skill(skill, request.reason or "manual-save")
    if not content:
        raise HTTPException(status_code=400, detail="Skill 内容不能为空")
    if len(content) > 200_000:
        raise HTTPException(status_code=400, detail="Skill 内容过大")
    if "name:" not in content and "# " not in content:
        raise HTTPException(status_code=400, detail="请保存有效的 SKILL.md 内容")
    skill_file = skill_markdown_path(skill)
    ensure_within_directory(SKILLS_ROOT, skill_file)
    skill_file.write_text(content, encoding="utf-8")
    await reset_skill_runtime()
    updated = get_skill_or_404(skill_name)
    return {
        "status": "ok",
        "name": updated.name,
        "display_name": skill_payload(updated)["display_name"],
        "description": updated.description,
        "version": str(updated.metadata.get("version") or ""),
        "snapshot": snapshot,
        "path": str(updated.path),
    }


@router.get("/api/skills/{skill_name}/versions")
async def list_skill_versions(skill_name: str):
    get_skill_or_404(skill_name)
    return {
        "skill_name": skill_name,
        "versions": list_skill_version_snapshots(skill_name),
    }


@router.post("/api/skills/{skill_name}/draft")
async def create_skill_draft(skill_name: str, request: SkillDraftRequest):
    skill = get_skill_or_404(skill_name)
    current = read_skill_markdown(skill)
    candidate = validate_skill_markdown(request.content)
    return {
        "status": "ok",
        "skill": skill_payload(skill),
        "reason": request.reason,
        "has_changes": candidate != current,
        "content": candidate,
        "diff": make_unified_diff(current, candidate),
    }


@router.post("/api/skills/{skill_name}/apply")
async def apply_skill_draft(skill_name: str, request: SkillDraftRequest):
    skill = get_skill_or_404(skill_name)
    current = read_skill_markdown(skill)
    candidate = validate_skill_markdown(request.content)
    snapshot = snapshot_skill(skill, request.reason or "apply-draft")
    skill_markdown_path(skill).write_text(candidate, encoding="utf-8")
    await reset_skill_runtime()
    updated = get_skill_or_404(skill_name)
    return {
        "status": "ok",
        "skill": skill_payload(updated),
        "snapshot": snapshot,
        "diff": make_unified_diff(current, candidate),
    }


@router.post("/api/skills/{skill_name}/rollback")
async def rollback_skill_version(skill_name: str, request: SkillRollbackRequest):
    skill = get_skill_or_404(skill_name)
    version_dir = skill_version_dir(skill.name)
    version_file = version_dir / f"{safe_skill_dir_name(request.version_id)}.md"
    ensure_within_directory(version_dir, version_file)
    if not version_file.exists():
        raise HTTPException(status_code=404, detail="Skill version snapshot not found")
    restored = validate_skill_markdown(version_file.read_text(encoding="utf-8"))
    snapshot = snapshot_skill(skill, f"rollback-before-{request.version_id}")
    skill_markdown_path(skill).write_text(restored, encoding="utf-8")
    await reset_skill_runtime()
    updated = get_skill_or_404(skill_name)
    return {
        "status": "ok",
        "skill": skill_payload(updated),
        "restored_version_id": request.version_id,
        "snapshot": snapshot,
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
    script = ROOT / "quality_iterator" / "scripts" / name
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
    draft = build_iterator_skill_draft(skill, read_skill_markdown(skill), result)
    return {
        "status": "ok",
        "mode": "generated_docx" if has_generation_input else "skill_static_preflight",
        "skill": skill_payload(skill),
        "reference_dir": str(reference_dir),
        "resolved_reference_dir": str(resolved_reference_dir),
        "draft": draft,
        "result": result,
    }


@router.post("/api/documents/{file_id}/evaluate")
async def evaluate_generated_document(file_id: str, request_body: GeneratedDocumentEvaluationRequest):
    candidate_docx = get_generated_file(file_id)
    if candidate_docx is None:
        raise HTTPException(status_code=404, detail="生成文档不存在或已过期")
    reference_dir = Path(request_body.reference_dir)
    if not reference_dir.exists() or not reference_dir.is_dir():
        raise HTTPException(status_code=400, detail="优质文档路径不存在或不是目录")

    source_skill = None
    skill_payload_data = None
    if request_body.skill_name.strip():
        skill = get_skill_or_404(request_body.skill_name.strip())
        source_skill_path = skill.path / "SKILL.md"
        ensure_within_directory(SKILLS_ROOT, source_skill_path)
        source_skill = str(source_skill_path)
        skill_payload_data = skill_payload(skill)

    resolved_reference_dir = resolve_iterator_reference_dir(
        reference_dir,
        request_body.skill_name.strip(),
    )
    script = iterator_script_path("evaluate_plan_quality.py")
    command = [
        sys.executable,
        str(script),
        "--reference-dir",
        str(resolved_reference_dir),
        "--candidate-docx",
        str(candidate_docx),
    ]
    if source_skill:
        command.extend(["--source-skill", source_skill])
    result = await run_blocking(run_iterator_subprocess, command)
    return {
        "status": "ok",
        "mode": "generated_docx",
        "file_id": file_id,
        "candidate_docx": str(candidate_docx),
        "skill": skill_payload_data,
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


