"""AgentScope 1.x RAG integration for maintenance-plan knowledge."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from agentscope.embedding import FileEmbeddingCache, OpenAITextEmbedding
from agentscope.rag import QdrantStore, SimpleKnowledge, TextReader


class MaintenanceKnowledgeBase:
    """Small wrapper around AgentScope 1.x ``SimpleKnowledge``."""

    def __init__(
        self,
        data_dir: Path,
        collection_name: str = "maintenance_plan_knowledge",
        similarity_top_k: int = 5,
    ) -> None:
        self.data_dir = data_dir
        self.collection_name = collection_name
        self.similarity_top_k = similarity_top_k
        self._knowledge: Optional[SimpleKnowledge] = None
        self._indexed = False

    @property
    def enabled(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY") or os.getenv("EMBEDDING_API_KEY"))

    def _build_knowledge(self) -> SimpleKnowledge:
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("EMBEDDING_API_KEY")
        if not api_key:
            raise RuntimeError("RAG 未启用：请配置 OPENAI_API_KEY 或 EMBEDDING_API_KEY。")

        dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
        store_location = os.getenv(
            "RAG_STORE_LOCATION",
            str(self.data_dir.parent / ".agentscope_rag" / "qdrant"),
        )
        cache_dir = os.getenv(
            "RAG_EMBEDDING_CACHE_DIR",
            str(self.data_dir.parent / ".agentscope_rag" / "embedding-cache"),
        )

        embedding_kwargs = {}
        base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("EMBEDDING_BASE_URL")
        if base_url:
            embedding_kwargs["base_url"] = base_url

        embedding_model = OpenAITextEmbedding(
            api_key=api_key,
            model_name=os.getenv("EMBEDDING_MODEL_NAME", "text-embedding-3-small"),
            dimensions=dimensions,
            embedding_cache=FileEmbeddingCache(cache_dir=cache_dir),
            **embedding_kwargs,
        )
        embedding_store = QdrantStore(
            location=store_location,
            collection_name=self.collection_name,
            dimensions=dimensions,
        )
        return SimpleKnowledge(
            embedding_store=embedding_store,
            embedding_model=embedding_model,
        )

    async def _ensure_indexed(self) -> None:
        if self._knowledge is None:
            self._knowledge = self._build_knowledge()

        if self._indexed:
            return

        reader = TextReader(
            chunk_size=int(os.getenv("RAG_CHUNK_SIZE", "800")),
            split_by=os.getenv("RAG_SPLIT_BY", "paragraph"),
        )
        documents = []
        for path in sorted(self.data_dir.glob("*/SKILL.md")):
            documents.extend(await reader(str(path)))
        for path in sorted(self.data_dir.glob("*/references/*.md")):
            documents.extend(await reader(str(path)))
        knowledge_dir = self.data_dir.parent / "knowledge"
        if knowledge_dir.exists():
            for pattern in ("**/*.md", "**/*.txt"):
                for path in sorted(knowledge_dir.glob(pattern)):
                    documents.extend(await reader(str(path)))

        if documents:
            batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
            for idx in range(0, len(documents), batch_size):
                await self._knowledge.add_documents(documents[idx : idx + batch_size])
        self._indexed = True

    async def get_knowledge(self) -> SimpleKnowledge:
        """Return an indexed AgentScope knowledge object."""
        await self._ensure_indexed()
        assert self._knowledge is not None
        return self._knowledge

    async def retrieve(self, query: str, top_k: Optional[int] = None) -> list[str]:
        """Retrieve relevant chunks as plain strings."""
        if not query.strip():
            return []
        knowledge = await self.get_knowledge()
        docs = await knowledge.retrieve(
            query=query,
            limit=top_k or self.similarity_top_k,
        )
        return [str(doc.metadata.content.get("text", "")) for doc in docs]


_knowledge_base: Optional[MaintenanceKnowledgeBase] = None


def get_knowledge_base(data_dir: Path) -> Optional[MaintenanceKnowledgeBase]:
    """Create the project knowledge base when embedding config is available."""
    global _knowledge_base

    if _knowledge_base is not None:
        return _knowledge_base

    knowledge_base = MaintenanceKnowledgeBase(
        data_dir=data_dir,
        collection_name=os.getenv("RAG_COLLECTION_NAME", "maintenance_plan_knowledge"),
        similarity_top_k=int(os.getenv("RAG_TOP_K", "5")),
    )
    if not knowledge_base.enabled:
        return None

    _knowledge_base = knowledge_base
    return _knowledge_base


def reset_knowledge_base() -> None:
    """Clear cached knowledge so new skill/reference files are re-indexed."""
    global _knowledge_base
    _knowledge_base = None
