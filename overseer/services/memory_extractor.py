"""MemoryExtractor — orchestration-layer component for memory extraction.

Evaluates LLM responses to determine what's worth persisting to long-term
memory. This is judgment logic that belongs in the orchestration layer,
NOT in the MemoryPlugin (which is pure CRUD).

Phase 1 implementation: rule-based with tightened indicators, context-window
extraction, and per-category frequency limits.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

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

    _category_counts: dict[str, int] = field(default_factory=dict)

    def evaluate(
        self,
        co_id: str,
        llm_response: str,
        step_title: str = "",
    ) -> Optional[dict]:
        """Return extraction result or None if nothing worth remembering.

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
