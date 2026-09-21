from dive_memory.retrieval import RetrievalConfig, ablation_configs
from dive_memory.service import MemoryService


def test_ablation_presets_add_one_declared_capability_at_a_time():
    configs = ablation_configs()
    assert list(configs) == list("ABCDEFGH")
    assert configs["A"].enabled_channels == frozenset({"bm25"})
    assert configs["B"].enabled_channels == frozenset({"dense"})
    assert configs["C"].enabled_channels == frozenset({"bm25", "dense"})
    assert "predicate" in configs["D"].enabled_channels
    assert "temporal" in configs["E"].enabled_channels
    assert {"entity", "relation"} <= configs["F"].enabled_channels
    assert "multi_hop" in configs["G"].enabled_channels
    assert configs["H"].version == "retrieval-config-v1"


def test_channels_are_independently_switchable_and_reflected_in_trace():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)

    only_predicate = RetrievalConfig(enabled_channels=frozenset({"predicate", "temporal"}))
    result = service.retrieve("u1", "我喜欢什么", config=only_predicate)

    assert result.items
    assert result.items[0].channels == ["predicate"]
    assert result.trace["executed_channels"] == ["predicate", "temporal"]
    assert result.trace["channels"]["predicate"]["candidate_count"] == 1
    assert "bm25" not in result.trace["channels"]


def test_multi_hop_is_default_off_and_requires_query_intent_plus_opt_in():
    service = MemoryService()
    residence = service.ingest("u1", "我住在成都", explicit=True)
    preference = service.ingest("u1", "我喜欢绿茶", explicit=True)

    default = service.retrieve("u1", "与成都关联的记忆", limit=10)
    enabled = service.retrieve(
        "u1", "与成都关联的记忆", limit=10,
        config=RetrievalConfig.full(include_multi_hop=True),
    )

    assert "multi_hop" not in default.trace["executed_channels"]
    returned = {item.memory.id: item for item in enabled.items}
    assert set(returned) == {residence["memory_ids"][0], preference["memory_ids"][0]}
    assert "multi_hop" in returned[preference["memory_ids"][0]].channels


def test_scope_deletion_and_time_filters_remain_hard_boundaries():
    service = MemoryService()
    own = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    other = service.ingest("u2", "请记住我喜欢绿茶", explicit=True)
    deleted = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    service.forget(deleted["memory_ids"][0])

    result = service.retrieve("u1", "喜欢", config=RetrievalConfig.full())
    ids = {item.memory.id for item in result.items}
    assert own["memory_ids"][0] in ids
    assert other["memory_ids"][0] not in ids
    assert deleted["memory_ids"][0] not in ids
    assert result.trace["hard_filters"]["namespace"]
    assert result.trace["hard_filters"]["deletion_status"]


def test_rrf_constant_and_weights_are_configuration_not_literals():
    config = RetrievalConfig(
        enabled_channels=frozenset({"bm25", "predicate"}), rrf_k=20,
        channel_weights={"bm25": 2.0, "predicate": 0.5},
    )
    assert config.rrf_k == 20
    assert config.weight("bm25") == 2.0
    assert config.weight("predicate") == 0.5
