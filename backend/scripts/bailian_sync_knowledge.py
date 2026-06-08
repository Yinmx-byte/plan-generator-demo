#!/usr/bin/env python3
"""Sync local maintenance-plan documents to Alibaba Bailian knowledge base."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from alibabacloud_bailian20231229.client import Client as BailianClient
from alibabacloud_bailian20231229.models import (
    AddCategoryRequest,
    AddFileRequest,
    ApplyFileUploadLeaseRequest,
    CreateIndexRequest,
    DescribeFileRequest,
    ListCategoryRequest,
    ListFileRequest,
    SubmitIndexJobRequest,
)
from alibabacloud_tea_openapi.models import Config
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)


def build_client() -> BailianClient:
    access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not access_key_id or not access_key_secret:
        raise SystemExit("请在 backend/.env 中配置 ALIBABA_CLOUD_ACCESS_KEY_ID 和 ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    return BailianClient(
        Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=os.getenv("BAILIAN_ENDPOINT", "bailian.cn-beijing.aliyuncs.com"),
            region_id=os.getenv("BAILIAN_REGION_ID", "cn-beijing"),
            connect_timeout=int(os.getenv("BAILIAN_CONNECT_TIMEOUT", "10000")),
            read_timeout=int(os.getenv("BAILIAN_READ_TIMEOUT", "60000")),
        )
    )


def get_workspace_id() -> str:
    workspace_id = os.getenv("BAILIAN_WORKSPACE_ID", "").strip()
    if not workspace_id:
        raise SystemExit("请在 backend/.env 中配置 BAILIAN_WORKSPACE_ID")
    return workspace_id


def to_map(obj: Any) -> dict[str, Any]:
    method = getattr(obj, "to_map", None)
    return method() if method else {}


def get_response_data(response: Any) -> Any:
    return getattr(getattr(response, "body", None), "data", None)


def md5_hex(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_docx_files(source_dir: Path) -> list[Path]:
    return [
        path
        for path in sorted(source_dir.rglob("*.docx"))
        if not path.name.startswith("~$")
    ]


def find_or_create_category(client: BailianClient, workspace_id: str, category_name: str) -> str:
    next_token = None
    while True:
        response = client.list_category(
            workspace_id,
            ListCategoryRequest(
                category_name=category_name,
                category_type="UNSTRUCTURED",
                max_results=100,
                next_token=next_token,
            ),
        )
        data = get_response_data(response)
        for category in getattr(data, "category_list", None) or []:
            if getattr(category, "category_name", "") == category_name:
                return getattr(category, "category_id")
        next_token = getattr(data, "next_token", None)
        if not next_token:
            break

    response = client.add_category(
        workspace_id,
        AddCategoryRequest(category_name=category_name, category_type="UNSTRUCTURED"),
    )
    data = get_response_data(response)
    category_id = getattr(data, "category_id", None)
    if not category_id:
        raise RuntimeError(f"创建百炼类目失败：{to_map(response)}")
    return category_id


def list_existing_files(client: BailianClient, workspace_id: str, category_id: str) -> dict[str, str]:
    files: dict[str, str] = {}
    next_token = None
    while True:
        response = client.list_file(
            workspace_id,
            ListFileRequest(category_id=category_id, max_results=100, next_token=next_token),
        )
        data = get_response_data(response)
        for item in getattr(data, "file_list", None) or []:
            file_name = getattr(item, "file_name", "")
            file_id = getattr(item, "file_id", "")
            if file_name and file_id:
                files.setdefault(file_name, file_id)
        next_token = getattr(data, "next_token", None)
        if not next_token:
            break
    return files


def upload_by_lease(client: BailianClient, workspace_id: str, category_id: str, path: Path) -> str:
    lease_response = client.apply_file_upload_lease(
        category_id,
        workspace_id,
        ApplyFileUploadLeaseRequest(
            category_type="UNSTRUCTURED",
            file_name=path.name,
            md_5=md5_hex(path),
            size_in_bytes=str(path.stat().st_size),
        ),
    )
    lease_data = get_response_data(lease_response)
    lease_id = getattr(lease_data, "file_upload_lease_id", None)
    param = getattr(lease_data, "param", None)
    if not lease_id or param is None:
        raise RuntimeError(f"申请上传租约失败：{to_map(lease_response)}")

    headers = getattr(param, "headers", None) or {}
    method = str(getattr(param, "method", "PUT") or "PUT").upper()
    url = getattr(param, "url", None)
    if not url:
        raise RuntimeError(f"上传租约缺少 URL：{to_map(lease_response)}")

    with path.open("rb") as handle:
        upload_response = requests.request(method, url, data=handle, headers=headers, timeout=120)
    upload_response.raise_for_status()

    add_response = client.add_file(
        workspace_id,
        AddFileRequest(
            category_id=category_id,
            category_type="UNSTRUCTURED",
            lease_id=lease_id,
            parser=os.getenv("BAILIAN_FILE_PARSER", "DASHSCOPE_DOCMIND"),
            tags=["plan-generator", "ECS", "maintenance-plan"],
        ),
    )
    add_data = get_response_data(add_response)
    file_id = getattr(add_data, "file_id", None)
    if not file_id:
        raise RuntimeError(f"添加文件失败：{to_map(add_response)}")
    return file_id


def wait_file_parsed(client: BailianClient, workspace_id: str, file_id: str, timeout: int) -> str:
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        response = client.describe_file(workspace_id, file_id, DescribeFileRequest())
        data = get_response_data(response)
        status = str(getattr(data, "status", "") or "")
        last_status = status or last_status
        if status.upper() in {"PARSER_SUCCESS", "PARSE_SUCCESS", "SUCCESS", "DONE", "COMPLETED"}:
            return status
        if status.upper() in {"PARSER_FAIL", "PARSE_FAIL", "FAILED", "FAIL"}:
            raise RuntimeError(f"文件解析失败：file_id={file_id} status={status}")
        time.sleep(5)
    return last_status or "UNKNOWN"


def create_index(client: BailianClient, workspace_id: str, category_id: str, document_ids: list[str], name: str) -> str:
    response = client.create_index(
        workspace_id,
        CreateIndexRequest(
            name=name,
            description="检修方案生成项目 ECS 历史方案知识库",
            structure_type="unstructured",
            source_type="DATA_CENTER_FILE",
            document_ids=document_ids,
            category_ids=[category_id],
            sink_type="BUILT_IN",
            embedding_model_name=os.getenv("BAILIAN_EMBEDDING_MODEL_NAME", "text-embedding-v4"),
            rerank_model_name=os.getenv("BAILIAN_RERANK_MODEL_NAME", "qwen3-rerank-hybrid"),
            rerank_min_score=float(os.getenv("BAILIAN_RERANK_MIN_SCORE", "0.20")),
            chunk_size=int(os.getenv("BAILIAN_CHUNK_SIZE", "800")),
            overlap_size=int(os.getenv("BAILIAN_OVERLAP_SIZE", "100")),
            enable_rewrite=os.getenv("BAILIAN_ENABLE_REWRITE", "true").lower() in {"1", "true", "yes"},
        ),
    )
    data = get_response_data(response)
    index_id = getattr(data, "id", None)
    if not index_id:
        raise RuntimeError(f"创建知识库失败：{to_map(response)}")

    job_response = client.submit_index_job(workspace_id, SubmitIndexJobRequest(index_id=index_id))
    job_data = get_response_data(job_response)
    return index_id, getattr(job_data, "id", "")


def upsert_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        default=r"D:\个人工作\2026\微创项目\整理后文档\检修方案-整理\ECS",
    )
    parser.add_argument("--category-name", default="plan-generator-ecs")
    parser.add_argument("--index-name", default="pg-ecs-plan")
    parser.add_argument("--wait-file-timeout", type=int, default=900)
    parser.add_argument("--reuse-files", action="store_true", default=True)
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    if not source_dir.exists():
        raise SystemExit(f"源目录不存在：{source_dir}")

    client = build_client()
    workspace_id = get_workspace_id()
    category_id = find_or_create_category(client, workspace_id, args.category_name)
    files = iter_docx_files(source_dir)
    if not files:
        raise SystemExit(f"源目录没有可导入 docx：{source_dir}")

    existing_files = list_existing_files(client, workspace_id, category_id) if args.reuse_files else {}
    document_ids = []
    uploaded = []
    reused = []
    for path in files:
        existing_id = existing_files.get(path.name)
        if existing_id:
            file_id = existing_id
            reused.append({"path": str(path), "file_id": file_id})
        else:
            file_id = upload_by_lease(client, workspace_id, category_id, path)
            status = wait_file_parsed(client, workspace_id, file_id, args.wait_file_timeout)
            uploaded.append({"path": str(path), "file_id": file_id, "status": status})
        document_ids.append(file_id)

    index_id, job_id = create_index(client, workspace_id, category_id, document_ids, args.index_name)
    upsert_env_value(ROOT / ".env", "BAILIAN_INDEX_ID", index_id)
    if job_id:
        upsert_env_value(ROOT / ".env", "BAILIAN_LAST_INDEX_JOB_ID", job_id)

    print(
        json.dumps(
            {
                "status": "ok",
                "workspace_id": workspace_id,
                "category_id": category_id,
                "index_id": index_id,
                "index_job_id": job_id,
                "uploaded_count": len(uploaded),
                "reused_count": len(reused),
                "document_count": len(document_ids),
                "uploaded": uploaded,
                "reused": reused,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
