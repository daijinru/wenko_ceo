"""MemoryExtractor — orchestration-layer component for memory extraction.

Evaluates LLM responses to determine what's worth persisting to long-term
memory. This is judgment logic that belongs in the orchestration layer,
NOT in the MemoryPlugin (which is pure CRUD).

Phase 3 implementation: keyword pre-filter (cost control) → LLM judge
(secondary model) for refined evaluation. Falls back to rule-based
extraction when LLM is unavailable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from overseer.core.plugin_protocols import LLMPlugin, MemoryPlugin

logger = logging.getLogger(__name__)

# Indicator → category mapping.  Only low-false-positive compound phrases.
# High-frequency single words ("always", "never", "重要", etc.) are
# deliberately excluded — they trigger on normal LLM reasoning too often.
INDICATOR_MAP: dict[str, list[str]] = {
    "preference": ["user prefers", "用户偏好", "用户倾向"],
    "decision_pattern": ["decision pattern", "规律", "总是这样"],
    "domain_knowledge": ["important to note", "domain knowledge", "领域知识"],
    "lesson": ["lesson learned", "remember that", "经验教训", "教训"],
}

# Max extractions per category within a single CO execution.
_MAX_PER_CATEGORY = 2


@dataclass
class MemoryExtractor:
    """Evaluate LLM responses and decide what to persist as long-term memory.

    Instantiated per-CO-execution so frequency counters reset naturally.
    """

    llm: Optional[LLMPlugin] = field(default=None, repr=False)
    _category_counts: dict[str, int] = field(default_factory=dict)

    def evaluate(
        self,
        co_id: str,
        llm_response: str,
        step_title: str = "",
    ) -> Optional[dict]:
        """Rule-based extraction (Phase 1 fallback).

        Returns:
            ``{"category": str, "content": str, "tags": list[str]}`` or None.
        """
        response_lower = llm_response.lower()

        for category, indicators in INDICATOR_MAP.items():
            # Frequency limit per category within this CO execution.
            if self._category_counts.get(category, 0) >= _MAX_PER_CATEGORY:
                continue

            for indicator in indicators:
                if indicator in response_lower:
                    content = _extract_paragraph(llm_response, indicator)
                    if not content:
                        continue

                    self._category_counts[category] = (
                        self._category_counts.get(category, 0) + 1
                    )

                    tags = [step_title] if step_title else []
                    tags.append(category)

                    logger.info(
                        "Memory extraction [%s] from '%s': %s",
                        category,
                        step_title,
                        content[:60],
                    )
                    return {
                        "category": category,
                        "content": content,
                        "tags": tags,
                    }

        return None

    async def evaluate_with_llm(
        self,
        co_id: str,
        llm_response: str,
        step_title: str = "",
        co_title: str = "",
    ) -> Optional[dict]:
        """Keyword pre-filter → LLM judge → fallback to rule-based.

        Returns:
            ``{"category": str, "content": str, "tags": list[str]}`` or None.
        """
        # Step 1: rule-based pre-filter (cost control — skip LLM if no indicator)
        rule_result = self.evaluate(co_id, llm_response, step_title)
        if rule_result is None:
            return None

        # Step 2: if no LLM available, return rule result directly
        if self.llm is None:
            return rule_result

        # Step 3: LLM judge
        prompt = (
            f"任务：{co_title}\n"
            f"步骤：{step_title}\n\n"
            f"以下是 LLM 在该步骤中的响应片段：\n\n"
            f"{rule_result['content']}"
        )

        try:
            raw = await self.llm.judge(prompt)
            judgment = self.llm.parse_judge(raw)

            if judgment is None:
                logger.warning("LLM judge parse failed, falling back to rule result")
                return rule_result

            if not judgment.get("worth", False):
                # Undo the category count increment from evaluate()
                cat = rule_result["category"]
                if self._category_counts.get(cat, 0) > 0:
                    self._category_counts[cat] -= 1
                return None

            # Use LLM-refined result
            category = judgment.get("category", rule_result["category"])
            content = judgment.get("content", rule_result["content"])
            tags = judgment.get("tags", [])
            if step_title and step_title not in tags:
                tags.append(step_title)
            if category not in tags:
                tags.append(category)

            logger.info(
                "Memory extraction (LLM-judged) [%s] from '%s': %s",
                category,
                step_title,
                content[:60],
            )
            return {
                "category": category,
                "content": content,
                "tags": tags,
            }

        except Exception:
            logger.warning(
                "LLM judge call failed, falling back to rule result",
                exc_info=True,
            )
            return rule_result

    async def deduplicate(
        self,
        extraction: dict,
        memory: MemoryPlugin,
    ) -> Optional[dict]:
        """Check if extraction duplicates an existing memory; merge if possible.

        Returns:
            - None → no similar memory found, caller should save as new.
            - ``{"action": "skip"}`` → duplicate, caller should discard.
            - ``{"action": "update", "target_id": str, "content": str}``
              → caller should update existing memory.
        """
        if self.llm is None:
            return None

        # Retrieve top-3 similar existing memories by content keyword match
        similar = memory.retrieve_as_text(extraction["content"], limit=3)
        if not similar:
            return None

        existing_text = "\n".join(
            f"- [ID: mem_{i}] {text}" for i, text in enumerate(similar)
        )
        prompt = (
            f"新记忆：{extraction['content']}\n\n"
            f"已有记忆：\n{existing_text}"
        )

        try:
            raw = await self.llm.merge_judge(prompt)
            result = self.llm.parse_merge_judge(raw)

            if result is None:
                logger.warning("LLM merge_judge parse failed, treating as new")
                return None

            action = result.get("action")
            if action == "skip":
                logger.info("Memory dedup: skipping duplicate")
                return {"action": "skip"}
            elif action == "update":
                # Map synthetic ID back to real memory ID from retrieve results
                target_idx = result.get("target_id", "mem_0")
                try:
                    idx = int(target_idx.replace("mem_", ""))
                except (ValueError, AttributeError):
                    idx = 0
                # We need the real memory objects to get the ID.
                # retrieve_as_text returns strings; re-retrieve as objects.
                from overseer.services.memory_service import MemoryService
                if isinstance(memory, MemoryService):
                    real_memories = memory.retrieve(extraction["content"], limit=3)
                    if idx < len(real_memories):
                        target_memory = real_memories[idx]
                        merged_content = result.get("content", extraction["content"])
                        logger.info(
                            "Memory dedup: merging into #%s: %s",
                            target_memory.id,
                            merged_content[:60],
                        )
                        return {
                            "action": "update",
                            "target_id": target_memory.id,
                            "content": merged_content,
                        }
                # Fallback if can't resolve target
                return None
            else:
                # "new" or unrecognized → treat as new
                return None

        except Exception:
            logger.warning(
                "LLM merge_judge call failed, treating as new",
                exc_info=True,
            )
            return None


# ---------------------------------------------------------------------------
# Paragraph extraction helpers
# ---------------------------------------------------------------------------

# Split on double-newlines first (Markdown-style paragraphs), then single.
_PARA_SPLITTERS = [re.compile(r"\n\s*\n"), re.compile(r"\n")]


def _extract_paragraph(text: str, indicator: str, max_chars: int = 500) -> str:
    """Extract the paragraph containing *indicator*, with ±1 neighbor."""
    indicator_lower = indicator.lower()

    for splitter in _PARA_SPLITTERS:
        paragraphs = splitter.split(text)
        if len(paragraphs) < 2:
            continue

        for idx, para in enumerate(paragraphs):
            if indicator_lower in para.lower():
                start = max(0, idx - 1)
                end = min(len(paragraphs), idx + 2)
                merged = "\n\n".join(p.strip() for p in paragraphs[start:end] if p.strip())
                if len(merged) > max_chars:
                    merged = merged[:max_chars].rsplit(" ", 1)[0] + "…"
                return merged

    # Fallback: single paragraph text — return trimmed.
    trimmed = text.strip()
    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars].rsplit(" ", 1)[0] + "…"
    return trimmed
