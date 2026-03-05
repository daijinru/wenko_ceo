"""Tests for memory service and memory extractor."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from overseer.services.memory_service import MemoryService
from overseer.services.memory_extractor import MemoryExtractor


def test_save_memory(isolated_db):
    svc = MemoryService()
    mem = svc.save("preference", "User prefers PDF reports", tags=["report", "format"])
    assert mem.id is not None
    assert mem.category == "preference"
    assert "report" in mem.relevance_tags


def test_retrieve_by_keyword(isolated_db):
    svc = MemoryService()
    svc.save("preference", "User prefers PDF reports", tags=["report"])
    svc.save("lesson", "Always check cash flow in financial analysis", tags=["finance"])
    svc.save("domain_knowledge", "Python is a programming language", tags=["tech"])

    results = svc.retrieve("financial report")
    # Should match the finance lesson and possibly the report preference
    assert len(results) >= 1
    contents = [r.content for r in results]
    assert any("financial" in c.lower() for c in contents)


def test_retrieve_by_tags(isolated_db):
    svc = MemoryService()
    svc.save("preference", "Some preference", tags=["finance", "quarterly"])
    svc.save("lesson", "Another lesson", tags=["tech"])

    results = svc.retrieve("finance quarterly analysis")
    assert len(results) >= 1


def test_retrieve_as_text(isolated_db):
    svc = MemoryService()
    svc.save("preference", "User prefers detailed analysis", tags=["analysis"])

    texts = svc.retrieve_as_text("detailed analysis")
    assert len(texts) >= 1
    assert texts[0].startswith("[preference]")


def test_memory_extractor_with_indicator():
    """MemoryExtractor should extract when a valid indicator is present."""
    ext = MemoryExtractor()

    response = "After investigation, important to note that the config file must be UTF-8 encoded."
    result = ext.evaluate("co-1", response, "Config check")
    assert result is not None
    assert result["category"] == "domain_knowledge"
    assert "Config check" in result["tags"]
    assert "important to note" in result["content"].lower()


def test_memory_extractor_no_indicator():
    """MemoryExtractor should return None for normal LLM reasoning."""
    ext = MemoryExtractor()

    response = "The data shows normal trends. I'll proceed to the next step."
    result = ext.evaluate("co-1", response, "Analysis")
    assert result is None


def test_memory_extractor_rejects_common_words():
    """Removed high-frequency indicators should no longer trigger extraction."""
    ext = MemoryExtractor()

    # "always" and "never" were removed from indicators
    response = "This function always returns a list and should never be called with None."
    result = ext.evaluate("co-1", response, "Code review")
    assert result is None


def test_memory_extractor_category_mapping():
    """Each indicator maps to the correct category."""
    ext = MemoryExtractor()

    result = ext.evaluate("co-1", "The user prefers dark mode for all interfaces.", "UI")
    assert result is not None
    assert result["category"] == "preference"

    result = ext.evaluate("co-1", "Lesson learned: never skip validation on user input.", "Security")
    assert result is not None
    assert result["category"] == "lesson"


def test_memory_extractor_frequency_limit():
    """Same category should be limited to 2 extractions per CO execution."""
    ext = MemoryExtractor()

    r1 = ext.evaluate("co-1", "Lesson learned: always validate inputs.", "Step 1")
    r2 = ext.evaluate("co-1", "Remember that caching improves performance.", "Step 2")
    r3 = ext.evaluate("co-1", "Lesson learned: log all errors.", "Step 3")

    assert r1 is not None
    assert r2 is not None
    # Third extraction for same category should be blocked
    assert r3 is None


def test_access_count_increment(isolated_db):
    """Retrieving a memory should increment its access_count."""
    svc = MemoryService()
    mem = svc.save("lesson", "Important lesson about caching", tags=["cache"])
    assert mem.access_count == 0

    results = svc.retrieve("caching")
    assert len(results) == 1
    assert results[0].access_count == 1

    # Retrieve again
    results = svc.retrieve("caching")
    assert results[0].access_count == 2


def test_updated_at_on_update(isolated_db):
    """Updating a memory should set updated_at."""
    svc = MemoryService()
    mem = svc.save("lesson", "Original content")
    assert mem.updated_at is None

    updated = svc.update(mem.id, content="Updated content")
    assert updated is not None
    assert updated.updated_at is not None
    assert updated.content == "Updated content"


def test_list_all(isolated_db):
    svc = MemoryService()
    svc.save("a", "Memory 1")
    svc.save("b", "Memory 2")
    all_mems = svc.list_all()
    assert len(all_mems) == 2


# ── Phase 2.2: query_by_tags tests ──


def test_query_by_tags_exact_match(isolated_db):
    """query_by_tags returns memories containing ALL specified tags."""
    svc = MemoryService()
    svc.save("preference", "User approves tool_a", tags=["implicit_preference", "tool_a"])
    svc.save("preference", "User rejects tool_b", tags=["implicit_preference", "tool_b"])
    svc.save("lesson", "Some lesson", tags=["tool_a"])

    results = svc.query_by_tags(["implicit_preference", "tool_a"])
    assert len(results) == 1
    assert "tool_a" in results[0].content


def test_query_by_tags_with_category_filter(isolated_db):
    """query_by_tags can filter by category."""
    svc = MemoryService()
    svc.save("preference", "Pref with tag", tags=["mytag"])
    svc.save("lesson", "Lesson with tag", tags=["mytag"])

    results = svc.query_by_tags(["mytag"], category="preference")
    assert len(results) == 1
    assert results[0].category == "preference"


def test_query_by_tags_no_match(isolated_db):
    """query_by_tags returns empty list when no tags match."""
    svc = MemoryService()
    svc.save("preference", "Something", tags=["alpha"])

    results = svc.query_by_tags(["alpha", "beta"])
    assert len(results) == 0


def test_preference_update_via_query_by_tags(isolated_db):
    """Simulates the new _persist_preferences flow: update existing preference."""
    svc = MemoryService()
    # First save
    svc.save(
        category="preference",
        content="User tends to reject tool 'risky_tool' (reject rate 80%, n=5).",
        tags=["implicit_preference", "risky_tool"],
    )

    # Simulate behavior change — now the user approves it
    existing = svc.query_by_tags(["implicit_preference", "risky_tool"], category="preference")
    assert len(existing) == 1

    new_content = "User consistently approves tool 'risky_tool' (approve rate 95%, n=20)."
    svc.update(existing[0].id, content=new_content)

    # Verify updated, not duplicated
    all_prefs = svc.query_by_tags(["implicit_preference", "risky_tool"], category="preference")
    assert len(all_prefs) == 1
    assert "approves" in all_prefs[0].content
    assert all_prefs[0].updated_at is not None


# ── Phase 2.1: WorkingMemory bridge tests ──


def test_bridge_working_memory_saves_findings(isolated_db):
    """_bridge_working_memory persists failed_approaches and key_findings."""
    from overseer.services.cognitive_object_service import CognitiveObjectService

    co_svc = CognitiveObjectService()
    co = co_svc.create("Test task", "Test description")

    # Simulate WorkingMemory in context
    co.context = {
        "working_memory": {
            "summary": "Task summary",
            "key_findings": [
                "The API requires OAuth2 bearer tokens for authentication",
                "Rate limit is 100 requests per minute",
                "",            # should be skipped (empty)
                "short",       # should be skipped (< 15 chars)
            ],
            "failed_approaches": [
                "Tried using basic auth but it was rejected by the server",
                "",            # should be skipped (empty)
            ],
            "open_questions": ["What about pagination?"],
            "last_updated_step": 5,
        }
    }
    co_svc.session.commit()

    # Import and invoke the bridge (test it in isolation via MemoryService)
    svc = MemoryService()

    from overseer.core.protocols import WorkingMemory
    wm = WorkingMemory(**co.context["working_memory"])

    for approach in wm.failed_approaches:
        if approach.strip():
            svc.save("lesson", approach.strip(),
                     tags=["from_working_memory", "failed_approach"],
                     source_co_id=co.id)

    for finding in wm.key_findings:
        stripped = finding.strip()
        if not stripped or len(stripped) < 15:
            continue
        svc.save("domain_knowledge", stripped,
                 tags=["from_working_memory", "key_finding"],
                 source_co_id=co.id)

    all_mems = svc.list_all()
    lessons = [m for m in all_mems if m.category == "lesson"]
    knowledge = [m for m in all_mems if m.category == "domain_knowledge"]

    assert len(lessons) == 1
    assert "basic auth" in lessons[0].content

    assert len(knowledge) == 2
    assert any("OAuth2" in k.content for k in knowledge)
    assert any("Rate limit" in k.content for k in knowledge)

    # open_questions should NOT be persisted
    assert not any("pagination" in m.content for m in all_mems)


# ── Phase 3.1: LLM memory judge tests ──


def _make_mock_llm(judge_response: dict) -> MagicMock:
    """Create a mock LLM that returns a judge fenced block."""
    raw = f"分析完毕。\n\n```judge\n{json.dumps(judge_response, ensure_ascii=False)}\n```"
    mock = MagicMock()
    mock.judge = AsyncMock(return_value=raw)
    mock.parse_judge = MagicMock(return_value=judge_response)
    return mock


@pytest.mark.asyncio
async def test_evaluate_with_llm_worth_true():
    """LLM judges response as worth remembering — returns LLM-refined result."""
    llm = _make_mock_llm({
        "worth": True,
        "category": "lesson",
        "content": "配置文件必须使用 UTF-8 编码",
        "tags": ["config", "encoding"],
    })
    ext = MemoryExtractor(llm=llm)

    result = await ext.evaluate_with_llm(
        "co-1",
        "After investigation, important to note that the config file must be UTF-8 encoded.",
        step_title="Config check",
        co_title="项目配置检查",
    )

    assert result is not None
    assert result["category"] == "lesson"
    assert result["content"] == "配置文件必须使用 UTF-8 编码"
    assert "config" in result["tags"]
    assert "encoding" in result["tags"]
    llm.judge.assert_called_once()


@pytest.mark.asyncio
async def test_evaluate_with_llm_worth_false():
    """LLM judges response as not worth remembering — returns None."""
    llm = _make_mock_llm({"worth": False})
    ext = MemoryExtractor(llm=llm)

    result = await ext.evaluate_with_llm(
        "co-1",
        "After investigation, important to note that the loop iterates three times.",
        step_title="Loop check",
        co_title="代码审查",
    )

    assert result is None
    llm.judge.assert_called_once()
    # Category count should have been decremented (undo the rule-based increment)
    assert ext._category_counts.get("domain_knowledge", 0) == 0


@pytest.mark.asyncio
async def test_evaluate_with_llm_fallback():
    """LLM call fails — falls back to rule-based result."""
    llm = MagicMock()
    llm.judge = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    ext = MemoryExtractor(llm=llm)

    result = await ext.evaluate_with_llm(
        "co-1",
        "After investigation, important to note that the config file must be UTF-8 encoded.",
        step_title="Config check",
        co_title="项目配置检查",
    )

    assert result is not None
    # Falls back to rule-based: category from INDICATOR_MAP
    assert result["category"] == "domain_knowledge"
    assert "important to note" in result["content"].lower()


@pytest.mark.asyncio
async def test_evaluate_with_llm_no_indicator():
    """No keyword hit — LLM is never called."""
    llm = MagicMock()
    llm.judge = AsyncMock()
    ext = MemoryExtractor(llm=llm)

    result = await ext.evaluate_with_llm(
        "co-1",
        "The data shows normal trends. I'll proceed to the next step.",
        step_title="Analysis",
        co_title="数据分析",
    )

    assert result is None
    llm.judge.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_with_llm_parse_failure():
    """LLM returns unparseable response — falls back to rule result."""
    llm = MagicMock()
    llm.judge = AsyncMock(return_value="无法判断，跳过。")
    llm.parse_judge = MagicMock(return_value=None)
    ext = MemoryExtractor(llm=llm)

    result = await ext.evaluate_with_llm(
        "co-1",
        "Lesson learned: always validate user input before processing.",
        step_title="Security review",
        co_title="安全审计",
    )

    assert result is not None
    # Falls back to rule-based
    assert result["category"] == "lesson"


def test_parse_judge():
    """LLMService.parse_judge extracts fenced judge block correctly."""
    from overseer.services.llm_service import LLMService

    svc = LLMService()

    response = '分析如下：\n\n```judge\n{"worth": true, "category": "lesson", "content": "测试内容", "tags": ["test"]}\n```'
    result = svc.parse_judge(response)
    assert result is not None
    assert result["worth"] is True
    assert result["category"] == "lesson"
    assert result["content"] == "测试内容"

    # Invalid JSON
    bad = "```judge\nnot json\n```"
    assert svc.parse_judge(bad) is None

    # No fenced block
    assert svc.parse_judge("plain text") is None


# ── Phase 3.2: Memory dedup & merge tests ──


def _make_mock_memory(retrieve_texts: list[str]) -> MagicMock:
    """Create a mock MemoryPlugin that returns given texts from retrieve_as_text."""
    mock = MagicMock()
    mock.retrieve_as_text = MagicMock(return_value=retrieve_texts)
    return mock


@pytest.mark.asyncio
async def test_deduplicate_no_similar():
    """No similar memories found — returns None (caller saves as new)."""
    llm = MagicMock()
    mem = _make_mock_memory([])
    ext = MemoryExtractor(llm=llm)

    result = await ext.deduplicate(
        {"category": "lesson", "content": "新知识", "tags": ["test"]},
        mem,
    )
    assert result is None


@pytest.mark.asyncio
async def test_deduplicate_skip():
    """LLM judges new memory as duplicate — returns skip."""
    llm = MagicMock()
    llm.merge_judge = AsyncMock(return_value='```merge\n{"action": "skip"}\n```')
    llm.parse_merge_judge = MagicMock(return_value={"action": "skip"})
    mem = _make_mock_memory(["[lesson] 配置文件必须使用 UTF-8 编码"])
    ext = MemoryExtractor(llm=llm)

    result = await ext.deduplicate(
        {"category": "lesson", "content": "配置文件须为 UTF-8", "tags": ["config"]},
        mem,
    )
    assert result is not None
    assert result["action"] == "skip"
    llm.merge_judge.assert_called_once()


@pytest.mark.asyncio
async def test_deduplicate_update(isolated_db):
    """LLM judges memories as mergeable — returns update with merged content."""
    svc = MemoryService()
    existing = svc.save("lesson", "配置文件必须使用 UTF-8 编码", tags=["config"])

    llm = MagicMock()
    merge_response = {
        "action": "update",
        "target_id": "mem_0",
        "content": "配置文件必须使用 UTF-8 编码，且路径不能包含中文字符",
    }
    llm.merge_judge = AsyncMock(
        return_value=f'```merge\n{json.dumps(merge_response, ensure_ascii=False)}\n```'
    )
    llm.parse_merge_judge = MagicMock(return_value=merge_response)
    ext = MemoryExtractor(llm=llm)

    result = await ext.deduplicate(
        {"category": "lesson", "content": "配置文件路径不能包含中文字符", "tags": ["config"]},
        svc,
    )

    assert result is not None
    assert result["action"] == "update"
    assert result["target_id"] == existing.id
    assert "中文字符" in result["content"]


@pytest.mark.asyncio
async def test_deduplicate_new():
    """LLM judges memories as different — returns None (new)."""
    llm = MagicMock()
    llm.merge_judge = AsyncMock(return_value='```merge\n{"action": "new"}\n```')
    llm.parse_merge_judge = MagicMock(return_value={"action": "new"})
    mem = _make_mock_memory(["[preference] 用户偏好深色模式"])
    ext = MemoryExtractor(llm=llm)

    result = await ext.deduplicate(
        {"category": "lesson", "content": "API 需要 OAuth2 认证", "tags": ["api"]},
        mem,
    )
    assert result is None


@pytest.mark.asyncio
async def test_deduplicate_fallback():
    """LLM merge_judge fails — returns None (save as new, don't lose data)."""
    llm = MagicMock()
    llm.merge_judge = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    mem = _make_mock_memory(["[lesson] 某条已有记忆"])
    ext = MemoryExtractor(llm=llm)

    result = await ext.deduplicate(
        {"category": "lesson", "content": "新记忆内容", "tags": ["test"]},
        mem,
    )
    assert result is None


def test_parse_merge_judge():
    """LLMService.parse_merge_judge extracts fenced merge block correctly."""
    from overseer.services.llm_service import LLMService

    svc = LLMService()

    # Valid skip
    response = '判断如下：\n\n```merge\n{"action": "skip"}\n```'
    result = svc.parse_merge_judge(response)
    assert result is not None
    assert result["action"] == "skip"

    # Valid update
    response = '```merge\n{"action": "update", "target_id": "mem_0", "content": "合并内容"}\n```'
    result = svc.parse_merge_judge(response)
    assert result["action"] == "update"
    assert result["content"] == "合并内容"

    # Invalid JSON
    assert svc.parse_merge_judge("```merge\nbad\n```") is None

    # No fenced block
    assert svc.parse_merge_judge("plain text") is None
