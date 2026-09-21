from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


RETRIEVAL_CHANNELS = frozenset({"bm25", "dense", "predicate", "temporal", "entity", "relation", "multi_hop"})


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    enabled_channels: frozenset[str] = frozenset({
        "bm25", "dense", "predicate", "temporal", "entity", "relation",
    })
    candidate_depths: Mapping[str, int] = field(default_factory=dict)
    rrf_k: int = 60
    channel_weights: Mapping[str, float] = field(default_factory=dict)
    version: str = "retrieval-config-v1"
    name: str = "default"

    def __post_init__(self) -> None:
        unknown = set(self.enabled_channels) - RETRIEVAL_CHANNELS
        if unknown:
            raise ValueError(f"unknown retrieval channels: {sorted(unknown)!r}")
        if self.rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        for channel, depth in self.candidate_depths.items():
            if channel not in RETRIEVAL_CHANNELS or isinstance(depth, bool) or int(depth) <= 0:
                raise ValueError("candidate depths require known channels and positive integers")
        for channel, weight in self.channel_weights.items():
            if channel not in RETRIEVAL_CHANNELS or float(weight) < 0:
                raise ValueError("channel weights require known channels and non-negative values")

    def depth(self, channel: str) -> int:
        return int(self.candidate_depths.get(channel, 100))

    def weight(self, channel: str) -> float:
        return float(self.channel_weights.get(channel, 1.0))

    @classmethod
    def full(cls, *, include_multi_hop: bool = False, name: str = "full") -> "RetrievalConfig":
        channels = set(RETRIEVAL_CHANNELS)
        if not include_multi_hop:
            channels.remove("multi_hop")
        return cls(frozenset(channels), name=name)


def ablation_configs() -> dict[str, RetrievalConfig]:
    variants = {
        "A": {"bm25"},
        "B": {"dense"},
        "C": {"bm25", "dense"},
        "D": {"bm25", "dense", "predicate"},
        "E": {"bm25", "dense", "predicate", "temporal"},
        "F": {"bm25", "dense", "predicate", "temporal", "entity", "relation"},
        "G": set(RETRIEVAL_CHANNELS),
        "H": set(RETRIEVAL_CHANNELS),
    }
    return {name: RetrievalConfig(frozenset(channels), name=name) for name, channels in variants.items()}
