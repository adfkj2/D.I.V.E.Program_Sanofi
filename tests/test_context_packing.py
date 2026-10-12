from dive_memory.context import pack_context
from dive_memory.models import (
    DOCUMENT_ASSERTION,
    DOCUMENT_PREDICATE,
    EvidenceState,
    Memory,
    MemoryStatus,
    RetrievalItem,
)
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


def test_deduplication_with_source_coordinates_keeps_position_index_for_next_item():
    a = _item("a", "same preference", predicate="preference", value="tea", sources=["e1"])
    b = _item("b", "same preference", predicate="preference", value="tea", sources=["e2"])
    c = _item("c", "a different fact")
    for item in (a,b):
        item.memory.structured_content["_reader_sources"] = [{"source_ref": item.source_refs[0]}]
    packed = pack_context([a,b,c], 1000)
    assert [i.memory.id for i in packed.items] == ["a","c"]
    assert packed.items[0].source_refs == ["e1","e2"]


def test_deduplication_without_coordinates_keeps_index_for_next_duplicate():
    items = [_item(str(i), "same", predicate="preference", value="tea") for i in range(3)]
    packed = pack_context(items, 1000)
    assert len(packed.items) == 1
    assert packed.items[0].source_refs == ["evt-0", "evt-1", "evt-2"]


def test_expanded_controlled_documents_preserve_facts_sources_and_global_budget():
    from dive_memory.oracle.controlled_documents import _controlled_pack
    fact = _item("fact", "I own a green bicycle.")
    text = "Opening context. " * 40 + "\n\n5. Cedar syrup: It adds a woody note.\n\nClosing advice."
    documents = [_document_item("doc"+str(i), text) for i in range(4)]
    spans = {d.memory.id:(text.index("It adds"),text.index("\n\nClosing")) for d in documents}
    # Distinguish source documents so canonical deduplication does not merge them.
    for i,d in enumerate(documents):
        d.memory.structured_content["value"] = str(i)
    packed,audit = _controlled_pack([fact],documents,question="What was the fifth recommendation?",
        question_date="2026-01-01T00:00:00+00:00",current_only=True,document_spans=spans,
        token_budget=1000,document_token_budget=800,max_documents=4,document_char_budget=120,
        expand_document_spans=True)
    assert packed.items[0].memory.id == "fact"
    assert packed.estimated_tokens <= 1000 and audit["document_context_tokens"] <= 800
    shown = [i for i in packed.visible_items if i.memory.id != "fact"]
    assert shown and all("5. Cedar syrup:" in i.memory.content for i in shown)
    assert all(i.memory.content in text for i in shown)
    assert all(d.memory.content == text for d in documents)
    assert all(i.source_refs == ["evt-"+i.memory.id] for i in shown)


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


# --------------------------------------------------------------------------- #
# content_caps: the document-memory ceiling
#
# Document memories are stored verbatim and are long by nature (2,166 characters
# on average in the corpus against a 1,500-token budget). Packing is
# first-come-first-served, so an uncapped document can consume the budget and
# exclude every later item -- including the short scalar fact that actually
# answers the question. ``content_caps`` truncates such a memory instead of
# dropping it, so the reader still learns that it exists and how it starts.
# --------------------------------------------------------------------------- #


def _document_item(memory_id, content, *, score=1.0, sources=None):
    """A document memory: ``artifact_document`` predicate, no scalar slot."""

    memory = Memory(
        memory_id, "u1", "episode", EvidenceState.OBSERVATION, content,
        {"subject": "assistant", "predicate": DOCUMENT_PREDICATE,
         "assertion": DOCUMENT_ASSERTION, "role": "assistant",
         "document_chars": len(content)},
        status=MemoryStatus.ACTIVE,
        source_event_ids=sources or [f"evt-{memory_id}"],
    )
    return RetrievalItem(memory, score, ["bm25"], list(memory.source_event_ids))


def test_document_content_cap_truncates_without_dropping_the_memory():
    content = "d" * 400
    item = _document_item("doc1", content)

    packed = pack_context([item], 1000, content_caps={DOCUMENT_PREDICATE: 50})

    assert [entry.memory.id for entry in packed.items] == ["doc1"]
    assert packed.items[0].memory.content == content, "the stored memory must not be mutated"
    assert "d" * 50 in packed.text, "the visible head of the document is kept"
    assert "d" * 51 not in packed.text
    assert "...[truncated to fit the context budget]" in packed.text

    row = next(r for r in packed.decisions if r["decision"] == "TRUNCATE")
    assert row["reason"] == "content_cap"
    assert row["predicate"] == DOCUMENT_PREDICATE
    assert row["cap"] == 50
    assert row["original_chars"] == len(content)
    assert not any(r["decision"] == "EXCLUDE" for r in packed.decisions)


def test_capped_document_no_longer_crowds_out_a_later_fact():
    """The measured reason ``content_caps`` exists, stated as a behaviour.

    Uncapped, the long document fits, soaks up the budget and the following
    scalar fact is excluded by the token budget. Capped, both survive.
    """

    document = _document_item("doc1", "d" * 350)
    fact = _item("fact1", "residence: Chengdu", predicate="residence", value="Chengdu")
    counter = CallableTokenCounter("characters", len)

    uncapped = pack_context([document, fact], 400, token_counter=counter)
    capped = pack_context([document, fact], 400, token_counter=counter,
                          content_caps={DOCUMENT_PREDICATE: 40})

    assert [entry.memory.id for entry in uncapped.items] == ["doc1"], "the document crowds out the fact"
    assert any(r["memory_id"] == "fact1" and r["reason"] == "token_budget" for r in uncapped.decisions)
    assert [entry.memory.id for entry in capped.items] == ["doc1", "fact1"], "capping lets both fit"
    assert capped.omitted == 0


def test_absent_or_empty_content_caps_leave_packing_byte_identical():
    items = [
        _document_item("doc1", "d" * 500),
        _item("fact1", "residence: Chengdu", predicate="residence", value="Chengdu"),
    ]

    baseline = pack_context(items, 1000)
    empty = pack_context(items, 1000, content_caps={})
    zero = pack_context(items, 1000, content_caps={DOCUMENT_PREDICATE: 0})

    for variant in (empty, zero):
        assert variant.text == baseline.text
        assert variant.estimated_tokens == baseline.estimated_tokens
        assert [entry.memory.id for entry in variant.items] == [entry.memory.id for entry in baseline.items]
        assert len(variant.decisions) == len(baseline.decisions)
    assert not any(r["decision"] == "TRUNCATE" for r in baseline.decisions)


def test_content_cap_above_the_content_length_emits_no_truncate_decision():
    packed = pack_context([_document_item("doc1", "d" * 30)], 1000,
                          content_caps={DOCUMENT_PREDICATE: 500})

    assert "truncated" not in packed.text
    assert not any(r["decision"] == "TRUNCATE" for r in packed.decisions)


def test_content_cap_does_not_leak_to_unmapped_predicates():
    statement = _item("s1", "s" * 500, predicate="statement", value="s" * 500)

    packed = pack_context([statement], 1000, content_caps={DOCUMENT_PREDICATE: 20})

    assert "s" * 500 in packed.text
    assert not any(r["decision"] == "TRUNCATE" for r in packed.decisions)


def test_packing_content_caps_is_derived_from_the_scoring_policy():
    """The cap is configuration, not a constant: ``legacy`` disables it."""

    from dive_memory.oracle.pipeline import packing_content_caps
    from dive_memory.retrieval import ScoringPolicy

    assert packing_content_caps(ScoringPolicy.legacy()) == {}
    assert packing_content_caps(ScoringPolicy.balanced()) == {DOCUMENT_PREDICATE: 1200}


def test_balanced_policy_truncates_a_corpus_average_document_where_legacy_does_not():
    """End-to-end guard: the default policy must not silently start truncating."""

    from dive_memory.oracle.pipeline import packing_content_caps
    from dive_memory.retrieval import ScoringPolicy

    item = _document_item("doc1", "d" * 2166)

    legacy = pack_context([item], 1500, content_caps=packing_content_caps(ScoringPolicy.legacy()))
    balanced = pack_context([item], 1500, content_caps=packing_content_caps(ScoringPolicy.balanced()))

    assert "d" * 2166 in legacy.text
    assert not any(r["decision"] == "TRUNCATE" for r in legacy.decisions)

    row = next(r for r in balanced.decisions if r["decision"] == "TRUNCATE")
    assert row["cap"] == 1200
    assert row["original_chars"] == 2166
    assert "d" * 1200 in balanced.text
    assert "d" * 1201 not in balanced.text
