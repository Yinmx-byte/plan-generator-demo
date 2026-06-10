"""Administrative helpers for Alibaba Bailian knowledge-base operations."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any

import requests
from alibabacloud_bailian20231229.client import Client as BailianClient
from alibabacloud_bailian20231229.models import (
    AddCategoryRequest,
    AddFileRequest,
    ApplyFileUploadLeaseRequest,
    CreateIndexRequest,
    DeleteFileRequest,
    DescribeFileRequest,
    GetIndexJobStatusRequest,
    ListCategoryRequest,
    ListFileRequest,
    ListIndexFileDetailsRequest,
    RetrieveRequest,
    SubmitIndexJobRequest,
)
from alibabacloud_tea_openapi.models import Config

from runtime import ROOT


def build_client() -> BailianClient:
    access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not access_key_id or not access_key_secret:
        raise RuntimeError("请配置 ALIBABA_CLOUD_ACCESS_KEY_ID 和 ALIBABA_CLOUD_ACCESS_KEY_SECRET")
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


def workspace_id() -> str:
    value = os.getenv("BAILIAN_WORKSPACE_ID", "").strip()
    if not value:
        raise RuntimeError("请配置 BAILIAN_WORKSPACE_ID")
    return value


def data_of(response: Any) -> Any:
    return getattr(getattr(response, "body", None), "data", None)


def md5_hex(raw: bytes) -> str:
    return hashlib.md5(raw).hexdigest()


def category_name_default() -> str:
    return os.getenv("BAILIAN_CATEGORY_NAME", "plan-generator-ecs")


def index_name_default() -> str:
    return os.getenv("BAILIAN_INDEX_NAME", "pg-ecs-v4")


def next_index_name() -> str:
    prefix = os.getenv("BAILIAN_INDEX_NAME_PREFIX", "pg-ecs")
    return f"{prefix}-{datetime.now().strftime('%m%d%H%M')}"[:20]


def list_categories(category_name: str | None = None) -> list[dict[str, Any]]:
    client = build_client()
    token = None
    categories = []
    while True:
        response = client.list_category(
            workspace_id(),
            ListCategoryRequest(
                category_name=category_name,
                category_type="UNSTRUCTURED",
                max_results=100,
                next_token=token,
            ),
        )
        data = data_of(response)
        for item in getattr(data, "category_list", None) or []:
            categories.append(
                {
                    "category_id": item.category_id,
                    "category_name": item.category_name,
                    "category_type": item.category_type,
                    "is_default": item.is_default,
                    "parent_category_id": item.parent_category_id,
                }
            )
        token = getattr(data, "next_token", None)
        if not token:
            return categories


def find_or_create_category(category_name: str) -> str:
    for category in list_categories(category_name):
        if category["category_name"] == category_name:
            return category["category_id"]
    response = build_client().add_category(
        workspace_id(),
        AddCategoryRequest(category_name=category_name, category_type="UNSTRUCTURED"),
    )
    data = data_of(response)
    category_id = getattr(data, "category_id", None)
    if not category_id:
        raise RuntimeError("创建百炼类目失败")
    return category_id


def list_files(category_name: str | None = None) -> list[dict[str, Any]]:
    client = build_client()
    category_id = find_or_create_category(category_name or category_name_default())
    token = None
    files = []
    while True:
        response = client.list_file(
            workspace_id(),
            ListFileRequest(category_id=category_id, max_results=100, next_token=token),
        )
        data = data_of(response)
        for item in getattr(data, "file_list", None) or []:
            files.append(
                {
                    "file_id": item.file_id,
                    "file_name": item.file_name,
                    "file_type": item.file_type,
                    "size_in_bytes": item.size_in_bytes,
                    "status": item.status,
                    "parser": item.parser,
                    "category_id": item.category_id,
                    "create_time": item.create_time,
                    "tags": item.tags or [],
                }
            )
        token = getattr(data, "next_token", None)
        if not token:
            return files


def upload_file(raw: bytes, filename: str, category_name: str | None = None) -> dict[str, Any]:
    client = build_client()
    category_id = find_or_create_category(category_name or category_name_default())
    lease_response = client.apply_file_upload_lease(
        category_id,
        workspace_id(),
        ApplyFileUploadLeaseRequest(
            category_type="UNSTRUCTURED",
            file_name=filename,
            md_5=md5_hex(raw),
            size_in_bytes=str(len(raw)),
        ),
    )
    lease_data = data_of(lease_response)
    lease_id = getattr(lease_data, "file_upload_lease_id", None)
    param = getattr(lease_data, "param", None)
    if not lease_id or param is None:
        raise RuntimeError("申请百炼上传租约失败")

    method = str(getattr(param, "method", "PUT") or "PUT").upper()
    url = getattr(param, "url", None)
    headers = getattr(param, "headers", None) or {}
    if not url:
        raise RuntimeError("百炼上传租约缺少 URL")
    response = requests.request(method, url, data=raw, headers=headers, timeout=120)
    response.raise_for_status()

    add_response = client.add_file(
        workspace_id(),
        AddFileRequest(
            category_id=category_id,
            category_type="UNSTRUCTURED",
            lease_id=lease_id,
            parser=os.getenv("BAILIAN_FILE_PARSER", "DASHSCOPE_DOCMIND"),
            tags=["plan-generator", "maintenance-plan"],
        ),
    )
    data = data_of(add_response)
    return {
        "file_id": getattr(data, "file_id", None),
        "file_name": filename,
        "category_id": category_id,
        "parser": getattr(data, "parser", None),
    }


def delete_file(file_id: str) -> dict[str, Any]:
    build_client().delete_file(file_id, workspace_id(), DeleteFileRequest())
    return {"status": "ok", "file_id": file_id}


def describe_file(file_id: str) -> dict[str, Any]:
    data = data_of(build_client().describe_file(workspace_id(), file_id, DescribeFileRequest()))
    if data is None:
        raise RuntimeError("未找到百炼远程文件")
    parse_result_download_url = getattr(data, "parse_result_download_url", None)
    return {
        "file_id": getattr(data, "file_id", None),
        "file_name": getattr(data, "file_name", None),
        "file_type": getattr(data, "file_type", None),
        "size_in_bytes": getattr(data, "size_in_bytes", None),
        "status": getattr(data, "status", None),
        "parser": getattr(data, "parser", None),
        "category_id": getattr(data, "category_id", None),
        "create_time": getattr(data, "create_time", None),
        "tags": getattr(data, "tags", None) or [],
        "parse_result_download_url": parse_result_download_url,
        "content_preview": load_parse_result_preview(parse_result_download_url),
    }


def load_parse_result_preview(url: str | None) -> str:
    if not url:
        return ""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "text" not in content_type and "json" not in content_type and "xml" not in content_type:
            return ""
        response.encoding = response.encoding or "utf-8"
        return response.text[:20000]
    except Exception:
        return ""


def get_index_file_detail(file_id: str, index_id: str | None = None) -> dict[str, Any] | None:
    client = build_client()
    page = 1
    while True:
        response = client.list_index_file_details(
            workspace_id(),
            ListIndexFileDetailsRequest(
                index_id=index_id or os.getenv("BAILIAN_INDEX_ID"),
                page_number=page,
                page_size=100,
            ),
        )
        data = data_of(response)
        documents = getattr(data, "documents", None) or []
        for item in documents:
            if getattr(item, "id", None) == file_id:
                return {
                    "id": item.id,
                    "name": item.name,
                    "document_type": item.document_type,
                    "status": item.status,
                    "code": item.code,
                    "message": item.message,
                    "chunk_mode": item.chunk_mode,
                    "chunk_size": item.chunk_size,
                    "overlap_size": item.overlap_size,
                    "separator": item.separator,
                    "size": item.size,
                    "gmt_modified": item.gmt_modified,
                }
        total = int(getattr(data, "total_count", 0) or 0)
        if page * 100 >= total or not documents:
            return None
        page += 1


def create_index(category_name: str | None = None, index_name: str | None = None) -> dict[str, Any]:
    category = category_name or category_name_default()
    name = (index_name or next_index_name())[:20]
    category_id = find_or_create_category(category)
    files = list_files(category)
    document_ids = [item["file_id"] for item in files if item.get("file_id")]
    if not document_ids:
        raise RuntimeError("当前类目没有可加入索引的文件")
    client = build_client()
    response = client.create_index(
        workspace_id(),
        CreateIndexRequest(
            name=name,
            description="检修方案生成项目远程知识库",
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
    data = data_of(response)
    index_id = getattr(data, "id", None)
    if not index_id:
        raise RuntimeError("创建百炼知识库索引失败，可能是索引名称重复或参数不合法")
    job_response = client.submit_index_job(workspace_id(), SubmitIndexJobRequest(index_id=index_id))
    job_data = data_of(job_response)
    job_id = getattr(job_data, "id", "")
    upsert_env("BAILIAN_INDEX_ID", index_id)
    upsert_env("BAILIAN_INDEX_NAME", name)
    upsert_env("BAILIAN_CATEGORY_NAME", category)
    if job_id:
        upsert_env("BAILIAN_LAST_INDEX_JOB_ID", job_id)
    os.environ["BAILIAN_INDEX_ID"] = index_id
    os.environ["BAILIAN_INDEX_NAME"] = name
    os.environ["BAILIAN_CATEGORY_NAME"] = category
    if job_id:
        os.environ["BAILIAN_LAST_INDEX_JOB_ID"] = job_id
    return {
        "status": "ok",
        "category_name": category,
        "category_id": category_id,
        "index_id": index_id,
        "job_id": job_id,
        "document_count": len(document_ids),
    }


def get_index_job(job_id: str | None = None, index_id: str | None = None) -> dict[str, Any]:
    request = GetIndexJobStatusRequest(
        index_id=index_id or os.getenv("BAILIAN_INDEX_ID"),
        job_id=job_id or os.getenv("BAILIAN_LAST_INDEX_JOB_ID"),
        page_number=1,
        page_size=20,
    )
    response = build_client().get_index_job_status(workspace_id(), request)
    data = data_of(response)
    return {
        "job_id": getattr(data, "job_id", None),
        "status": getattr(data, "status", None),
        "documents": [
            {
                "doc_id": item.doc_id,
                "doc_name": item.doc_name,
                "status": item.status,
                "code": item.code,
                "message": item.message,
            }
            for item in (getattr(data, "documents", None) or [])
        ],
    }


def retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    response = build_client().retrieve(
        workspace_id(),
        RetrieveRequest(
            index_id=os.getenv("BAILIAN_INDEX_ID"),
            query=query,
            dense_similarity_top_k=top_k,
            sparse_similarity_top_k=top_k,
            enable_reranking=os.getenv("BAILIAN_RAG_ENABLE_RERANK", "true").lower() in {"1", "true", "yes"},
            rerank_top_n=top_k,
            rerank_min_score=float(os.getenv("BAILIAN_RAG_RERANK_MIN_SCORE", "0.2")),
        ),
    )
    nodes = getattr(data_of(response), "nodes", None) or []
    return [
        {
            "score": getattr(node, "score", None),
            "text": getattr(node, "text", "") or "",
            "metadata": getattr(node, "metadata", None),
        }
        for node in nodes
    ]


def upsert_env(key: str, value: str) -> None:
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
