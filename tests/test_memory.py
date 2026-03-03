"""Tests for memory service and memory extractor."""

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
