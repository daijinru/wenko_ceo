"""Memory service — cross-event persistent memory storage and retrieval."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from overseer.database import get_session
from overseer.models.memory import Memory

logger = logging.getLogger(__name__)


class MemoryService:
    def __init__(self, session: Session | None = None):
        self._session = session

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = get_session()
        return self._session

    def save(
        self,
        category: str,
        content: str,
        tags: list[str] | None = None,
        source_co_id: str | None = None,
    ) -> Memory:
        """Save a memory entry."""
        mem = Memory(
            category=category,
            content=content,
            relevance_tags=tags or [],
            source_co_id=source_co_id,
        )
        self.session.add(mem)
        self.session.commit()
        self.session.refresh(mem)
        logger.info("Saved memory [%s]: %s", category, content[:50])
        return mem

    _STOPWORDS = frozenset({
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
        "一", "个", "上", "也", "很", "到", "说", "要", "去", "你", "会",
        "着", "没有", "看", "好", "自己", "这", "他", "她", "它",
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "to", "of", "in", "for", "on", "with", "at", "by", "from",
    })

    @staticmethod
    def _segment(text: str) -> list[str]:
        """Segment text using jieba for Chinese, falling back to split()."""
        try:
            import jieba
            words = list(jieba.cut(text, cut_all=False))
        except ImportError:
            logger.debug("jieba not available, falling back to space-based splitting")
            words = text.split()
        return [w for w in words if len(w) >= 2 and w not in MemoryService._STOPWORDS]

    def retrieve(self, query: str, limit: int = 5) -> List[Memory]:
        """Retrieve relevant memories by keyword matching on content and tags.

        Uses jieba for Chinese word segmentation to enable proper Chinese
        keyword matching. Falls back to space-based splitting if jieba
        is not available.
        """
        query_lower = query.lower()
        query_words = self._segment(query_lower)

        all_memories = self.session.query(Memory).order_by(Memory.created_at.desc()).limit(100).all()

        scored: list[tuple[float, Memory]] = []
        for mem in all_memories:
            score = 0.0
            content_lower = mem.content.lower()

            # Full query match in content
            if query_lower in content_lower:
                score += 3.0

            # Segmented word matches (jieba-powered)
            content_words = set(self._segment(content_lower))
            for word in query_words:
                if word in content_words:
                    score += 1.5
                elif word in content_lower:
                    score += 0.5

            # Tag matches
            tags = mem.relevance_tags or []
            for tag in tags:
                if isinstance(tag, str):
                    tag_lower = tag.lower()
                    if tag_lower in query_lower:
                        score += 2.0
                    else:
                        tag_words = set(self._segment(tag_lower))
                        overlap = tag_words & set(query_words)
                        if overlap:
                            score += 1.0 * len(overlap)

            if score > 0:
                scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [mem for _, mem in scored[:limit]]

        # Increment access_count for retrieved memories.
        for mem in results:
            mem.access_count = (mem.access_count or 0) + 1
        if results:
            self.session.commit()

        return results

    def retrieve_as_text(self, query: str, limit: int = 5) -> List[str]:
        """Retrieve memories and return as text strings for prompt injection."""
        memories = self.retrieve(query, limit)
        return [f"[{m.category}] {m.content}" for m in memories]

    def update(
        self,
        memory_id: str,
        category: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory | None:
        """Update an existing memory. Returns the updated Memory or None."""
        mem = self.session.get(Memory, memory_id)
        if mem is None:
            return None
        if category is not None:
            mem.category = category
        if content is not None:
            mem.content = content
        if tags is not None:
            mem.relevance_tags = tags
        mem.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        self.session.refresh(mem)
        logger.info("Updated memory %s", memory_id)
        return mem

    def delete(self, memory_id: str) -> bool:
        """Delete a single memory by ID. Returns True if deleted."""
        mem = self.session.get(Memory, memory_id)
        if mem is None:
            return False
        self.session.delete(mem)
        self.session.commit()
        logger.info("Deleted memory %s", memory_id)
        return True

    def list_all(self) -> List[Memory]:
        return self.session.query(Memory).order_by(Memory.created_at.desc()).all()
