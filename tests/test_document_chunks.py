"""Document passages are a reversible index over source-linked originals."""

from dive_memory.document_chunks import select_document_chunk, split_document
from dive_memory.models import DOCUMENT_ASSERTION, DOCUMENT_PREDICATE, EvidenceState, Memory
from dive_memory.retrieval import RetrievalConfig, ablation_configs
from dive_memory.store import SQLiteStore


def test_expansion_keeps_anchor_and_recovers_beginning_of_numbered_item():
    from dive_memory.document_chunks import expand_document_span
    text = "Introduction.\n\n" + "irrelevant. " * 30 + "\n\n5. Cedar syrup: It adds a woody note to the drink.\n\nClosing advice."
    start = text.index("It adds")
    end = text.index("\n\nClosing")
    chunk = expand_document_span(text, start, end, 120)
    assert chunk.content == text[chunk.start:chunk.end]
    assert chunk.start <= start < end <= chunk.end
    assert "5. Cedar syrup:" in chunk.content
    assert len(chunk.content) <= 120


def test_expansion_does_not_change_source_and_respects_tight_caps():
    from dive_memory.document_chunks import expand_document_span
    text = "x" * 100 + "anchor" + "y" * 100
    c = expand_document_span(text, 100, 106, 10)
    assert 0 <= c.start <= 100 and 106 <= c.end <= len(text)
    assert len(c.content) <= 10 and c.content == text[c.start:c.end]
    whole = expand_document_span("one paragraph", 4, 8, 100)
    assert whole.content == "one paragraph"


def _document(memory_id: str, namespace: str, text: str, observed_at: str) -> Memory:
    return Memory(
        memory_id, namespace, "episode", EvidenceState.OBSERVATION, text,
        {"subject": "assistant", "predicate": DOCUMENT_PREDICATE,
         "assertion": DOCUMENT_ASSERTION, "role": "assistant"},
        observed_at=observed_at, valid_from=observed_at,
        source_event_ids=[f"source-{memory_id}"],
    )


def test_passages_keep_exact_source_offsets_and_cover_late_sections():
    text = (
        "# Travel plan\n\n" + "Opening logistics and budgets. " * 16 +
        "\n\n## Equipment\n\n" + "Bring ordinary equipment. " * 16 +
        "\n\n## Final recommendation\n\nChoose the cobalt battery for the telescope."
    )
    chunks = split_document(text, max_chars=230, overlap_chars=25)

    assert len(chunks) > 2
    assert all(chunk.index == index for index, chunk in enumerate(chunks))
    assert all(chunk.content == text[chunk.start:chunk.end] for chunk in chunks)
    assert all(0 < len(chunk.content) <= 230 for chunk in chunks)
    assert any("cobalt battery" in chunk.content for chunk in chunks)
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)


def test_selector_finds_answer_after_prefix_and_falls_back_deterministically():
    text = (
        "# Travel plan\n\n" + "General plans and logistics. " * 24 +
        "\n\n## Final recommendation\n\nChoose the cobalt battery for the telescope."
    )
    chosen = select_document_chunk(text, "Which battery did you recommend for the telescope?", 150)
    fallback = select_document_chunk(text, "unmentioned marmalade", 150)

    assert "cobalt battery" in chosen.content
    assert chosen.start > 0
    assert chosen.content == text[chosen.start:chosen.end]
    assert len(chosen.content) <= 150
    assert fallback.start == 0


def test_chunk_index_is_source_linked_tenant_scoped_and_cutoff_safe():
    store = SQLiteStore()
    text = "# Plan\n\n" + "Background notes. " * 50 + "\n\nSelect the cobalt battery for telescope use."
    first = _document("d1", "alice", text, "2025-01-01T00:00:00+00:00")
    other = _document("d2", "bob", text, "2025-01-01T00:00:00+00:00")
    future = _document("d3", "alice", text, "2027-01-01T00:00:00+00:00")
    for memory in (first, other, future):
        store.add_memory(memory)

    hits = store.search_document_chunks(
        "alice", "cobalt battery", as_of="2026-01-01T00:00:00+00:00",
        observed_as_of="2026-01-01T00:00:00+00:00",
    )
    assert hits
    assert {hit.parent_memory_id for hit in hits} == {"d1"}
    assert all(hit.content == text[hit.start:hit.end] for hit in hits)
    assert "source-d1" in store.get_memory(hits[0].parent_memory_id).source_event_ids


def test_chunk_index_rebuild_and_forget_remove_all_passages_without_touching_other_tenant():
    store = SQLiteStore()
    text = "# Recommendation\n\nThe cobalt battery is appropriate for the telescope. " * 15
    store.add_memory(_document("d1", "alice", text, "2025-01-01T00:00:00+00:00"))
    store.add_memory(_document("d2", "bob", text, "2025-01-01T00:00:00+00:00"))
    before = store.db.execute("SELECT COUNT(*) FROM document_chunks WHERE memory_id='d1'").fetchone()[0]
    assert before > 1

    # Simulate a file-backed database predating the new disposable index.
    store.db.execute("DELETE FROM document_chunk_fts WHERE chunk_id IN "
                     "(SELECT chunk_id FROM document_chunks WHERE memory_id='d1')")
    store.db.execute("DELETE FROM document_chunks WHERE memory_id='d1'")
    store.db.commit()
    assert not store.search_document_chunks("alice", "cobalt battery")
    store.reindex_lexical("alice")
    after = store.db.execute("SELECT COUNT(*) FROM document_chunks WHERE memory_id='d1'").fetchone()[0]
    assert after == before
    assert store.search_document_chunks("alice", "cobalt battery")
    assert store.search_document_chunks("bob", "cobalt battery")

    store.tombstone_memory("d1", "forget", "2026-01-01T00:00:00+00:00")
    assert not store.search_document_chunks("alice", "cobalt battery")
    assert store.db.execute("SELECT 1 FROM document_chunks WHERE memory_id='d1'").fetchone() is None
    assert store.search_document_chunks("bob", "cobalt battery")

    store.clear_projections("bob")
    assert store.db.execute("SELECT 1 FROM document_chunks WHERE memory_id='d2'").fetchone() is None
    assert store.db.execute("SELECT 1 FROM document_chunk_fts").fetchone() is None


def test_hard_purge_erases_passage_content_and_index():
    store = SQLiteStore()
    text = "# Plan\n\nChoose the cobalt battery for the telescope."
    store.add_memory(_document("d1", "alice", text, "2025-01-01T00:00:00+00:00"))
    assert store.search_document_chunks("alice", "cobalt battery")

    store.purge_memory("d1", "retention request", "2026-01-01T00:00:00+00:00")

    assert store.get_memory("d1") is None
    assert store.db.execute("SELECT 1 FROM document_chunks WHERE memory_id='d1'").fetchone() is None
    assert store.db.execute("SELECT 1 FROM document_chunk_fts").fetchone() is None


def test_opt_in_chunk_channel_rescues_document_tail_at_top_one_without_changing_baseline():
    store = SQLiteStore()
    # The short topical remark wins whole-document BM25; it is not the answer.
    store.add_memory(Memory(
        "remark", "alice", "semantic_fact", EvidenceState.FACT,
        "Battery telescope recommendation.",
        {"subject": "user", "predicate": "statement", "value": "Battery telescope recommendation."},
    ))
    document = _document(
        "deliverable", "alice",
        "General logistics and budget. " * 200 +
        "\n\n## Final recommendation\n\nChoose the cobalt battery for the telescope.",
        "2025-01-01T00:00:00+00:00",
    )
    store.add_memory(document)
    query = "cobalt battery telescope recommendation"
    baseline = store.search(
        "alice", query, limit=1,
        config=RetrievalConfig(enabled_channels=frozenset({"bm25"})),
    )
    trace = {}
    improved = store.search(
        "alice", query, limit=1,
        config=RetrievalConfig(enabled_channels=frozenset({"bm25", "document_chunk"})),
        trace=trace,
    )

    assert [row[0].id for row in baseline] == ["remark"]
    assert [row[0].id for row in improved] == ["deliverable"]
    assert "document_chunk" in improved[0][2]
    assert trace["channels"]["document_chunk"]["candidate_count"] == 1
    selected_trace = next(row for row in trace["candidates"] if row["memory_id"] == "deliverable")
    start, end = selected_trace["document_chunk_span"]
    assert "cobalt battery" in document.content[start:end]
    assert "document_chunk" not in RetrievalConfig.full().enabled_channels
    assert "document_chunk" in RetrievalConfig.full(include_document_chunks=True).enabled_channels
    assert all("document_chunk" not in preset.enabled_channels
               for preset in ablation_configs().values())


def test_opt_in_chunk_channel_keeps_tenant_and_future_turns_out():
    store = SQLiteStore()
    text = "# Plan\n\n" + "General details. " * 60 + "\n\nChoose the cobalt battery."
    store.add_memory(_document("past", "alice", text, "2025-01-01T00:00:00+00:00"))
    store.add_memory(_document("future", "alice", text, "2027-01-01T00:00:00+00:00"))
    store.add_memory(_document("other", "bob", text, "2025-01-01T00:00:00+00:00"))
    only_chunks = RetrievalConfig(enabled_channels=frozenset({"document_chunk"}))

    returned = store.search(
        "alice", "cobalt battery", limit=10,
        as_of="2026-01-01T00:00:00+00:00",
        observed_as_of="2026-01-01T00:00:00+00:00",
        config=only_chunks,
    )
    assert [memory.id for memory, _score, _channels in returned] == ["past"]
