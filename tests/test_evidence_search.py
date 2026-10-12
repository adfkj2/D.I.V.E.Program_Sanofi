from dive_memory.evidence_search import required_probes, recover_evidence
from dive_memory.models import Memory, EvidenceState, RetrievalItem


def item(id, text):
    return RetrievalItem(Memory(id, "u", "episode", EvidenceState.OBSERVATION, text, {}), 1)


def test_plain_fact_or_under_specified_query_never_triggers_probe():
    assert required_probes("Where do I live?") == []
    assert required_probes("How many cats?") == []


def test_recovery_is_bounded_and_retains_known_evidence():
    base = [item("old", "I bought my hiking boots for 70 dollars.")]
    calls = []
    def search(q):
        calls.append(q)
        return [item("new", "My cycling helmet cost 20 dollars.")]
    result, audit = recover_evidence("How much did my hiking boots and cycling helmet cost in total?", base, search)
    assert len(calls) == 1 and len(result) == 2
    assert audit["added_memory_ids"] == ["new"]
    assert result[0].memory.id == "old"
    assert not audit["is_answerability_proof"]


def test_complete_numeric_sides_do_not_repeat_search():
    base = [item("a", "hiking boots 70 dollars"), item("b", "cycling helmet 20 dollars")]
    def fail(q):
        raise AssertionError("unnecessary probe")
    out, audit = recover_evidence("How much did my hiking boots and cycling helmet cost?", base, fail)
    assert out == base and not audit["searches"]


def test_recovery_never_exceeds_limit_or_duplicates_ids():
    base = [item(str(i), "hiking boots 70 dollars") for i in range(10)]
    out, audit = recover_evidence("How much did my hiking boots and cycling helmet cost?", base,
                                 lambda q: [item("n", "cycling helmet 20 dollars")] * 4)
    assert len(out) == len({i.memory.id for i in out}) == 10
    assert len(audit["displaced_memory_ids"]) == 1
