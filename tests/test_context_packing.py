from dive_memory.context import pack_context
from dive_memory.models import EvidenceState, Memory, MemoryStatus, RetrievalItem
from dive_memory.token_budget import CallableTokenCounter


def _item(memory_id, content, *, status=MemoryStatus.ACTIVE, predicate="statement", value=None,
          sources=None, score=1.0, contradicts_id=None):
    memory = Memory(
        memory_id, "u1", "semantic_fact", EvidenceState.FACT, content,
        {"subject": "user", "predicate": predicate, "value": value or content,
         "normalized_value": (value or content).casefold()},
        status=status, source_event_ids=sources or [f"evt-{memory_id}"],
        contradicts_id=contradicts_id,
    )
    return RetrievalItem(memory, score, ["bm25"], list(memory.source_event_ids))


def test_context_packing_canonical_deduplicates_and_merges_sources():
    first = _item("m1", "preference: Green Tea", predicate="preference", value="green tea", sources=["e1"])
    duplicate = _item("m2", "preference: green tea", predicate="preference", value="GREEN TEA", sources=["e2"])

    packed = pack_context([first, duplicate], 1000)

    assert len(packed.items) == 1
    assert packed.items[0].source_refs == ["e1", "e2"]
    assert packed.text.count("preference:") == 1
    assert any(row["reason"] == "canonical_duplicate" for row in packed.decisions)


def test_current_context_excludes_outdated_memory_but_history_can_include_it():
    old = _item("old", "residence: Chengdu", status=MemoryStatus.SUPERSEDED,
                predicate="residence", value="Chengdu")
    current = _item("new", "residence: Shanghai", predicate="residence", value="Shanghai")

    packed = pack_context([old, current], 1000, current_only=True)
    historical = pack_context([old, current], 1000, current_only=False)

    assert [item.memory.id for item in packed.items] == ["new"]
    assert [item.memory.id for item in historical.items] == ["old", "new"]
    assert any(row["memory_id"] == "old" and row["reason"] == "outdated_for_current_query"
               for row in packed.decisions)


def test_unresolved_contradiction_is_labeled_in_context():
    original = _item("a", "allergy: penicillin", predicate="allergy", value="penicillin")
    conflict = _item("b", "allergy: none", predicate="allergy", value="none", contradicts_id="a")

    packed = pack_context([original, conflict], 1000)

    assert "conflict_group=a" in packed.text
    assert "allergy: penicillin" in packed.text
    assert "allergy: none" in packed.text


def test_actual_token_counter_never_exceeds_budget_for_chinese_or_english():
    counter = CallableTokenCounter("character-tokenizer", lambda text: len(text))
    items = [_item("zh", "我喜欢绿茶"), _item("en", "I prefer green tea")]

    packed = pack_context(items, 80, token_counter=counter)

    assert packed.estimated_tokens == len(packed.text)
    assert packed.estimated_tokens <= 80
    assert packed.token_counter == "character-tokenizer"
    assert not packed.token_count_degraded
    assert packed.omitted >= 1


def test_default_counter_is_explicitly_approximate_and_zero_budget_is_empty():
    packed = pack_context([_item("m1", "some memory")], 0)
    assert packed.items == []
    assert packed.text == ""
    assert packed.estimated_tokens == 0
    assert packed.token_count_degraded
