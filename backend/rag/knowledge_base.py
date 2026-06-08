"""RAG integration for maintenance-plan knowledge.

The production path uses Alibaba Cloud Model Studio (Bailian) knowledge-base
Retrieve API. The legacy AgentScope/Qdrant implementation is intentionally not
kept here: project business code should depend on one small ``retrieve`` API.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from alibabacloud_bailian20231229.client import Client as BailianClient
from alibabacloud_bailian20231229.models import RetrieveRequest
from alibabacloud_tea_openapi.models import Config


class MaintenanceKnowledgeBase:
    """Small wrapper around Bailian knowledge-base retrieval."""

    def __init__(self, similarity_top_k: int = 5) -> None:
        self.similarity_top_k = similarity_top_k
        self.workspace_id = os.getenv("BAILIAN_WORKSPACE_ID", "").strip()
        self.index_id = os.getenv("BAILIAN_INDEX_ID", "").strip()
        self.enable_rerank = os.getenv("BAILIAN_RAG_ENABLE_RERANK", "true").lower() in {
            "1",
            "true",
            "yes",
        }
        self.rerank_min_score = os.getenv("BAILIAN_RAG_RERANK_MIN_SCORE")
        self._client: Optional[BailianClient] = None

    @property
    def enabled(self) -> bool:
        return bool(
            self.workspace_id
            and self.index_id
            and os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
            and os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        )

    def _get_client(self) -> BailianClient:
        if self._client is None:
            self._client = BailianClient(
                Config(
                    access_key_id=os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID"),
                    access_key_secret=os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET"),
                    endpoint=os.getenv("BAILIAN_ENDPOINT", "bailian.cn-beijing.aliyuncs.com"),
                    region_id=os.getenv("BAILIAN_REGION_ID", "cn-beijing"),
                    connect_timeout=int(os.getenv("BAILIAN_CONNECT_TIMEOUT", "10000")),
                    read_timeout=int(os.getenv("BAILIAN_READ_TIMEOUT", "60000")),
                )
            )
        return self._client

    async def get_knowledge(self) -> "MaintenanceKnowledgeBase":
        """Compatibility shim for existing admin endpoints."""
        if not self.enabled:
            raise RuntimeError("RAG 未启用：请配置百炼 AK、业务空间 ID 和知识库索引 ID。")
        return self

    async def retrieve(self, query: str, top_k: Optional[int] = None) -> list[str]:
        """Retrieve relevant chunks as plain strings."""
        if not query.strip() or not self.enabled:
            return []

        limit = top_k or self.similarity_top_k
        request = RetrieveRequest(
            index_id=self.index_id,
            query=query,
            dense_similarity_top_k=limit,
            sparse_similarity_top_k=limit,
            enable_reranking=self.enable_rerank,
            rerank_top_n=limit,
            rerank_min_score=float(self.rerank_min_score)
            if self.rerank_min_score
            else None,
        )

        def _call() -> list[str]:
            response = self._get_client().retrieve(self.workspace_id, request)
            nodes = getattr(getattr(response.body, "data", None), "nodes", None) or []
            chunks = []
            for node in nodes:
                text = str(getattr(node, "text", "") or "").strip()
                if text:
                    chunks.append(text)
            return chunks

        return await asyncio.to_thread(_call)


_knowledge_base: Optional[MaintenanceKnowledgeBase] = None


def get_knowledge_base(_data_dir: Path) -> Optional[MaintenanceKnowledgeBase]:
    """Create the project knowledge base when Bailian config is available."""
    global _knowledge_base

    if _knowledge_base is not None:
        return _knowledge_base

    knowledge_base = MaintenanceKnowledgeBase(
        similarity_top_k=int(os.getenv("RAG_TOP_K", os.getenv("BAILIAN_RAG_TOP_K", "5"))),
    )
    if not knowledge_base.enabled:
        return None

    _knowledge_base = knowledge_base
    return _knowledge_base


def reset_knowledge_base() -> None:
    """Clear cached client/config so env changes take effect."""
    global _knowledge_base
    _knowledge_base = None
