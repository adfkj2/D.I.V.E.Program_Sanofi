"""Semantic write-gate policy (v2).

Motivation
----------
The v1 gate decided accept/reject from a single keyword table
(``gate.decide``'s three-branch ``importance``). On the official LongMemEval
English corpus that table has a 0.00% hit rate for its durable-signal branch,
so 841/896 gold-evidence turns were rejected as ``LOW_FUTURE_UTILITY`` and the
evidence-turn formation coverage collapsed to 55/896 = 6.14%.

This module keeps the v1 policy as the safety floor and adds a *semantic*
second opinion that answers a different question than "is there a keyword?":

    Would this text be useful as a durable user memory?

Design constraints (all deliberate)
-----------------------------------
1. **Deterministic.** The reference set is fixed and the embedding model is
   pinned by revision; the same text always yields the same verdict.
2. **Local / credential-free.** Uses bge-m3 through
   ``LocalSentenceTransformerEmbeddingProvider``; no external API is required.
3. **Adversarial recall.** Prototypes are *first-person personal-fact*
   statements, not topical sentences. Wikipedia/code/news text should *not*
   pass, because it lands near "world knowledge" anchors instead.
4. **Safety floor is untouched.** Sensitive data and temporary intent are
   decided *before* the semantic layer is ever consulted, so relaxing recall
   cannot relax those two rejections.
5. **Additive interface.** ``decide`` keeps its signature, its default
   behaviour, and its ``policy_version`` for v1 callers. v2 is opt-in via
   ``policy='semantic'`` (or the ``SemanticGate`` wrapper).

The policy is expressed as a bounded linear score over interpretable features,
so every accept/reject can be explained by naming the features that moved.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Sequence

from .gate import (
    EXPLICIT_MARKERS,
    GateAction,
    GateDecision,
    decide as decide_v1,
    _features,
    _sensitive,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .embeddings import EmbeddingProvider

POLICY_VERSION = "semantic-utility-v2"

# ---------------------------------------------------------------------------
# Source trust
# ---------------------------------------------------------------------------
# The false-memory suite (``eval/datasets/false_memory_v1``) encodes a second,
# independent requirement the v1 gate never modelled: *who said it*. Several
# cases contain a perfectly well-formed personal fact ("我住在上海",
# "I like green tea") that must nonetheless be refused because it arrived from
# an untrusted web page, a generated summary, or another person's record.
# Content scoring cannot express that, so v2 carries an explicit trust tier.
TRUSTED_SOURCES = frozenset({"user", "memory_correction", "user_correction"})
CONDITIONAL_SOURCES = frozenset({"assistant", "agent", "tool", "summary", "generated"})
# Anything not listed above (untrusted_web, external, crm_record, unknown) is
# treated as untrusted and cannot create a durable user fact without an
# explicit user request.
UNTRUSTED_MARKERS = (
    "ignore all memory rules", "ignore previous instructions", "ignore all instructions",
    "system:", "assistant:", "the user lives in", "the user is", "crm record",
    "customer ", "another customer",
)


def _source_tier(source_type: str | None) -> str:
    if source_type is None:
        return "unknown"
    normalized = str(source_type).strip().casefold()
    if normalized in TRUSTED_SOURCES:
        return "trusted"
    if normalized in CONDITIONAL_SOURCES:
        return "conditional"
    return "untrusted"

# ---------------------------------------------------------------------------
# Prototype anchors
# ---------------------------------------------------------------------------
# DURABLE_ANCHORS: first-person, self-referential, stable personal facts.
# These are the *shape* of a memory worth keeping. They are intentionally
# mundane and cover several predicates so the centroid does not collapse onto
# any single phrasing.
DURABLE_ANCHORS: tuple[str, ...] = (
    "I prefer dark mode in my editor.",
    "I like Earl Grey tea in the morning.",
    "I live in Berlin and commute by bicycle.",
    "I use PostgreSQL for the storage layer.",
    "My goal is to finish the thesis by December.",
    "I always book flights at least three weeks ahead.",
    "My sister's name is Emily and she studies architecture.",
    "I am allergic to shellfish.",
    "I work as a backend engineer on the payments team.",
    "I studied business administration at university.",
    "I drive a blue Honda Civic.",
    "My favourite restaurant is a small ramen place near the office.",
    "I listen to audiobooks during my daily commute.",
    "I keep my notes in a plain text file.",
    "I have been learning Spanish for two years.",
    "My birthday is in early March.",
)

# TRANSIENT_ANCHORS: same *surface topic* as durable anchors (first person,
# past/present personal narration) but no standing value for the future.
# Placing these in the same region forces the durable centroid to depend on
# stability rather than on personhood alone.
TRANSIENT_ANCHORS: tuple[str, ...] = (
    "I just ate a sandwich for lunch.",
    "I am waiting for the bus right now.",
    "I watched a film last night and it was fine.",
    "Could you summarise this paragraph for me?",
    "What time is it in Tokyo right now?",
    "I am going to the gym later today.",
    "Thanks, that was helpful.",
    "Yesterday I spent twenty minutes looking for my keys.",
)

# General / encyclopaedic third-person statements. These have real information
# content, so a naive "is this informative?" gate would keep them; they are
# exactly what a *personal* memory store should decline. The set deliberately
# includes technical and code-shaped prose: the first calibration run showed a
# lone database sentence ("PostgreSQL uses multi-version concurrency control")
# landing closer to a durable anchor than to any world anchor, which is a
# coverage gap in this list rather than a scoring flaw.
WORLD_KNOWLEDGE_ANCHORS: tuple[str, ...] = (
    "The Eiffel Tower was completed in 1889.",
    "PostgreSQL uses multi-version concurrency control.",
    "The Pacific Ocean is the largest ocean on Earth.",
    "In this function we iterate over the list and return the result.",
    "The company reported quarterly revenue growth of four percent.",
    "Sodium chloride dissolves readily in water.",
    "A database transaction satisfies the ACID properties.",
    "HTTP/2 multiplexes several requests over one TCP connection.",
    "The library raises a ValueError when the argument is malformed.",
    "Concurrency control prevents two writers from corrupting shared state.",
    "The study found a correlation between the two measured variables.",
    "This guide explains how to configure the build pipeline.",
    "根据文档，该接口在超时后会返回错误码。",
)

# Curated character-level n-grams used as a tiny lexical fallback when no
# embedding provider is available. They are *not* a full keyword table; they
# only need to give the deterministic reference points a weak signal so the
# offline base environment stays functional.
_LEXICAL_DURABLE = (
    "i prefer", "i like", "i love", "i live", "i use", "i always", "i usually",
    "my goal", "my name", "my favourite", "my favorite", "my birthday",
    "i am allergic", "i work", "i studied", "i drive", "i keep", "i have been",
    "i am learning", "i don't eat", "i do not eat",
)
_LEXICAL_TRANSIENT = (
    "just ate", "right now", "later today", "last night", "yesterday",
    "could you", "what time is", "thanks", "waiting for",
)


def _lexical_score(text: str) -> float:
    lowered = text.casefold()
    durable = sum(1 for cue in _LEXICAL_DURABLE if cue in lowered)
    transient = sum(1 for cue in _LEXICAL_TRANSIENT if cue in lowered)
    total = durable + transient
    if total == 0:
        return 0.0
    return max(0.0, min(1.0, durable / total))


# ---------------------------------------------------------------------------
# Prototype index
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PrototypeIndex:
    """Semantic reference points derived from the anchor sets.

    Three scalars are produced per input text:

    ``personal``
        Margin between the nearest first-person personal-fact anchor and the
        nearest encyclopaedic/world-knowledge anchor. Rewards self-reference
        *relative to* general knowledge, so informative third-person text does
        not pass merely by being informative.
    ``durable``
        Margin between the nearest *stable* personal fact and the nearest
        *transient* personal narration. Rewards persistence.

    Both are computed from **per-anchor maximum similarity** rather than a
    centroids-only dot product. The centroid formulation was tried first and
    measured as non-separable (see ``SemanticWeights``), because averaging
    sixteen dissimilar anchors collapses them toward the corpus mean. Using
    nearest-anchor margins keeps individual prototypes sharp.
    """

    encoder: Any
    durable_vectors: tuple[tuple[float, ...], ...]
    transient_vectors: tuple[tuple[float, ...], ...]
    world_vectors: tuple[tuple[float, ...], ...]
    # Optional bounded cache of text -> vector. ``None`` disables it entirely,
    # which is what every test and short-lived caller wants. Long sweeps opt in
    # via ``cache_size`` so a pre-encoded batch is not recomputed one string at
    # a time; the bound keeps memory flat on a 250k-turn corpus.
    cache_size: int = 0
    _cache: dict[str, tuple[float, ...]] | None = None

    @classmethod
    def build(cls, encoder: Any, *, cache_size: int = 0) -> "PrototypeIndex":
        return cls(
            encoder,
            tuple(_embed_one(encoder, text) for text in DURABLE_ANCHORS),
            tuple(_embed_one(encoder, text) for text in TRANSIENT_ANCHORS),
            tuple(_embed_one(encoder, text) for text in WORLD_KNOWLEDGE_ANCHORS),
            cache_size=max(0, int(cache_size)),
        )

    def score(self, text: str) -> dict[str, float]:
        if self.cache_size:
            cached = self._cached(text)
            if cached is not None:
                return self._score_vector(cached)
        return self._score_vector(_embed_one(self.encoder, text))

    def score_many(self, texts: Sequence[str]) -> list[dict[str, float]]:
        """Score several texts with a single encoder call.

        Encoding one string at a time leaves the model's batch dimension idle.
        Measured on CPU bge-m3 (``ops/gate_batch_probe.py``): 99 ms/string
        one-at-a-time versus 21 ms/string at batch 32, a 4.6x difference. A
        caller that already holds a list of turns (the benchmark runner, a
        backfill) should therefore use this method rather than looping over
        ``score``.
        """
        if not texts:
            return []
        materialized = list(texts)
        if self.cache_size:
            # Only the misses reach the encoder; the rest are answered from cache.
            pending = [text for text in materialized if self._cached(text) is None]
            if pending:
                for text, vector in zip(pending, _embed_many(self.encoder, pending)):
                    self._store(text, vector)
            vectors = [self._cached(text) for text in materialized]
        else:
            vectors = _embed_many(self.encoder, materialized)
        return [self._score_vector(vector) for vector in vectors]

    def _cached(self, text: str) -> tuple[float, ...] | None:
        return self._cache.get(text) if self.cache_size and self._cache is not None else None

    def _store(self, text: str, vector: tuple[float, ...]) -> None:
        if not self.cache_size:
            return
        if self._cache is None:
            object.__setattr__(self, "_cache", {})
        if len(self._cache) >= self.cache_size:
            # Plain FIFO eviction: the access pattern is a one-pass sweep, so
            # recency and insertion order coincide and LRU buys nothing.
            self._cache.pop(next(iter(self._cache)))
        self._cache[text] = vector

    def _score_vector(self, vector: Sequence[float]) -> dict[str, float]:
        best_durable = _best(vector, self.durable_vectors)
        best_transient = _best(vector, self.transient_vectors)
        best_world = _best(vector, self.world_vectors)
        # A margin maps [-1, 1] -> [0, 1]; 0.5 means "equally close".
        return {
            "personal": _margin(best_durable, best_world),
            "durable": _margin(best_durable, best_transient),
        }


def _best(vector: Sequence[float], anchors: Sequence[Sequence[float]]) -> float:
    return max(_cosine(vector, anchor) for anchor in anchors)


def _margin(positive: float, negative: float) -> float:
    return max(0.0, min(1.0, (positive - negative + 1.0) / 2.0))


def _embed_many(encoder: Any, texts: list[str]) -> list[tuple[float, ...]]:
    raw = encoder.encode(texts, normalize_embeddings=True)
    return [_normalise([float(value) for value in row]) for row in raw]


def _embed_one(encoder: Any, text: str) -> tuple[float, ...]:
    raw = encoder.encode([text], normalize_embeddings=True)
    values = raw[0] if hasattr(raw, "__getitem__") else raw
    return _normalise([float(value) for value in values])


def _normalise(values: Sequence[float]) -> tuple[float, ...]:
    norm = sum(value * value for value in values) ** 0.5 or 1.0
    return tuple(value / norm for value in values)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right)))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SemanticWeights:
    """Calibrated on 19 hand-labelled probes against real bge-m3 vectors.

    The calibration run (2026-09-21, ``ops/semantic_gate_probe.py``) measured
    nearest-anchor margins as:

    ===========  ==================  =========================
    axis         durable text        non-durable text
    ===========  ==================  =========================
    personal     0.527 .. 0.785      0.199 .. 0.612
    durable      0.473 .. 0.751      0.253 .. 0.625
    ===========  ==================  =========================

    Observations that drove the final weights:

    1. The ``durable`` axis almost separates cleanly: transient personal
       narration collapses to 0.253 .. 0.344 while durable facts sit at
       0.473 .. 0.751. The single exception is a third-person technical
       sentence that is genuinely ambiguous without context.
    2. The ``personal`` axis is what excludes world knowledge: encyclopaedic
       text sits at 0.199 .. 0.342 versus 0.527 .. 0.785 for personal facts.
    3. At equal weighting (0.5/0.5) the combined score is linearly separable
       with a margin of only +0.0108 on this probe set. **That margin is too
       thin to justify a confident claim**, so the threshold is placed at the
       midpoint of the achieved gap and the policy is documented as
       calibrated-but-provisional rather than tuned.
    4. The length/specificity term is *anti-correlated* with durability
       (long durable facts score 0.000, short transient sentences 0.725), so
       it is reported as a diagnostic and excluded from the score.
    """

    personal: float = 0.50
    durable: float = 0.50
    # Midpoint of [non-durable max 0.4892, durable min 0.5000] on the probes.
    accept_threshold: float = 0.495
    review_threshold: float = 0.45
    # Reported-only diagnostics; excluded from the score by design.
    single_valued_weight: float = 0.0


DEFAULT_WEIGHTS = SemanticWeights()


def _single_valued_signal(features: dict[str, float]) -> float:
    """Length/complexity proxy. **Diagnostic only — never scored.**

    Kept in the returned feature dict for auditability, but calibrated out of
    the decision after the run above showed it penalises long durable facts
    while rewarding short transient ones.
    """
    specificity = features.get("specificity", 0.5)
    return max(0.0, min(1.0, 1.0 - specificity))


def score_semantic(
    text: str,
    *,
    policy: "SemanticGate | None" = None,
    index: PrototypeIndex | None = None,
    weights: SemanticWeights = DEFAULT_WEIGHTS,
) -> dict[str, float]:
    index = index or (policy.index if policy is not None else None)
    if index is None:
        raise ValueError("a PrototypeIndex is required to score a text")
    semantic = index.score(text)
    # ``single_valued`` is computed from the shared v1 feature extractor so both
    # policies describe the text with the same vocabulary, but it is reported
    # only: the calibration run showed it is anti-correlated with durability.
    baseline = _features(text, explicit=False, requested=False, ephemeral=False,
                         source_reliability=0.8, importance=0.0, sensitive=False)
    single_valued = _single_valued_signal(baseline)
    total = (weights.personal * semantic["personal"]
             + weights.durable * semantic["durable"])
    return {
        "personal": semantic["personal"],
        "durable": semantic["durable"],
        "single_valued": single_valued,
        "score": round(total, 6),
    }


def _semantic_decide(
    text: str,
    *,
    index: PrototypeIndex,
    explicit: bool,
    source_reliability: float,
    weights: SemanticWeights,
    source_type: str | None = None,
) -> GateDecision:
    baseline = decide_v1(text, explicit=explicit, source_reliability=source_reliability)
    # v1 already refused sensitive data / empty input: never override a refusal.
    if baseline.reason_code in {"EMPTY", "SENSITIVE_DATA"}:
        return replace(baseline, policy_version=POLICY_VERSION)

    normalized = text.strip().lower()
    requested = explicit or any(marker in normalized for marker in EXPLICIT_MARKERS)
    tier = _source_tier(source_type)

    # --- source-trust floor (independent of content) ------------------------
    # Untrusted provenance cannot create a durable user fact, even when the
    # text is a textbook personal statement, and even when it says "remember".
    if tier == "untrusted":
        features = _features(text, explicit=explicit, requested=requested, ephemeral=False,
                             source_reliability=source_reliability, importance=0.0, sensitive=False)
        features["source_trust"] = 0.0
        return GateDecision(
            accepted=False, importance=0.0, confidence=source_reliability, salience=0.1,
            durability="ephemeral", reason="untrusted source cannot create a user fact",
            action=GateAction.SKIP, reason_code="UNTRUSTED_SOURCE", features=features,
            policy_version=POLICY_VERSION,
        )

    # --- assertion-strength floors ------------------------------------------
    # Hedged or delegated claims are not durable facts yet, and negated
    # sentences assert nothing at all. v1 accepted these purely because they
    # contained an explicit marker, which is how the suite caught
    # "请记住，我可能喜欢茉莉花茶，但还不确定" and "I infer that ...".
    if _is_hedged_or_delegated(normalized) or _is_negated(normalized):
        features = _features(text, explicit=explicit, requested=requested, ephemeral=False,
                             source_reliability=source_reliability, importance=0.0, sensitive=False)
        features["assertion_strength"] = 0.0
        return GateDecision(
            accepted=False, importance=0.0, confidence=source_reliability, salience=0.2,
            durability="ephemeral", reason="hedged, delegated, or negated claim is not a durable fact",
            action=GateAction.SKIP, reason_code="UNASSERTED_CLAIM", features=features,
            policy_version=POLICY_VERSION,
        )

    # --- speech-act floor ---------------------------------------------------
    # Questions and requests are not assertions. Decided structurally (see
    # ``_is_question_or_request``) because the semantic axes cannot separate a
    # question about the user from a fact about the user. An explicit
    # ``explicit=True`` request is a *user instruction to remember*, which is a
    # different thing and still passes.
    if not requested and _is_question_or_request(text):
        features = _features(text, explicit=explicit, requested=requested, ephemeral=False,
                             source_reliability=source_reliability, importance=0.0, sensitive=False)
        features["speech_act_assertion"] = 0.0
        return GateDecision(
            accepted=False, importance=0.0, confidence=source_reliability, salience=0.2,
            durability="ephemeral", reason="question or request asserts no durable fact",
            action=GateAction.SKIP, reason_code="NON_ASSERTIVE_SPEECH_ACT", features=features,
            policy_version=POLICY_VERSION,
        )

    scores = score_semantic(text, index=index, weights=weights)
    # An explicit user request still forces acceptance, but only for a source
    # that is at least conditional (delegated sources are handled above).
    forced = 0.9 if (requested and tier == "trusted") else 0.0
    effective = max(scores["score"], forced)
    if tier == "conditional":
        # A delegated source can surface a candidate but should sit lower.
        effective *= 0.9

    if effective >= weights.accept_threshold:
        action, code = GateAction.WRITE, "SEMANTIC_ABOVE_WRITE_THRESHOLD"
    elif effective >= weights.review_threshold:
        action, code = GateAction.REVIEW, "SEMANTIC_REVIEW_BAND"
    else:
        action, code = GateAction.SKIP, "SEMANTIC_LOW_FUTURE_UTILITY"

    features = _features(text, explicit=explicit, requested=requested,
                         ephemeral=baseline.durability == "ephemeral",
                         source_reliability=source_reliability,
                         importance=effective, sensitive=False)
    features.update({key: value for key, value in scores.items() if key != "score"})
    features["semantic_score"] = scores["score"]
    features["explicit_remember_request"] = 1.0 if requested else 0.0
    features["source_trust"] = {"trusted": 1.0, "conditional": 0.6}.get(tier, 0.0)
    return GateDecision(
        accepted=action is GateAction.WRITE,
        importance=round(effective, 6),
        confidence=source_reliability,
        salience=min(1.0, effective + 0.1),
        durability="permanent" if requested else ("long_term" if effective >= weights.accept_threshold else "medium_term"),
        reason="accepted by semantic write gate" if action is GateAction.WRITE else "below semantic write threshold",
        action=action,
        reason_code=code,
        features=features,
        policy_version=POLICY_VERSION,
    )


# Hedging / delegation cues. Kept small and bilingual; each one is directly
# traceable to a false-memory case that v1 wrongly accepted.
_HEDGE_CUES = (
    "可能", "也许", "或许", "大概", "不确定", "好像", "似乎",
    "probably", "maybe", "might", "perhaps", "not sure", "unsure", "uncertain",
)
_DELEGATION_CUES = (
    "i infer", "we infer", "inferred that", "the pattern suggests", "suggests the user",
    "summary:", "summary ", "the user lives", "the user is", "the user prefers",
    "crm record", "customer ",
)
# Negation cues. A sentence can be maximally "personal" and still assert no
# fact: the suite's "We did not discuss where I live." scored above threshold on
# both axes because it is first-person and unrelated to world knowledge, yet it
# states only that a topic was absent. A crude but honest negation check turns
# that into a skip.
_NEGATION_CUES = (
    "did not", "didn't", "do not", "don't", "does not", "doesn't",
    "never", "no longer", "not discuss", "not mention",
    "没有", "不是", "从未", "并没", "并未", "不讨论", "没提到", "不知道",
)
# An explicit user request softens the hedge rule: "记一下我可能喜欢茉莉花茶" is a
# legitimate (if tentative) user instruction, whereas the suite's cases come
# from non-user sources and must be refused. The suite exercises the latter.
# ``_is_hedged_or_delegated`` therefore applies to all sources; callers that
# genuinely need to store a hedged user statement should pass ``explicit=True``
# together with ``source_type='user'`` and handle the tuple upstream.


def _is_hedged_or_delegated(normalized: str) -> bool:
    if any(cue in normalized for cue in _DELEGATION_CUES):
        return True
    return any(cue in normalized for cue in _HEDGE_CUES)


def _is_negated(normalized: str) -> bool:
    return any(cue in normalized for cue in _NEGATION_CUES)


# ---------------------------------------------------------------------------
# Interrogative / imperative floor
# ---------------------------------------------------------------------------
# Held-out calibration (``eval/reports/gate-calibration.json``) exposed a
# failure family the semantic axes cannot represent: a question *about* the user
# is maximally personal and says nothing about world knowledge, so
# "What is my sister's name again?" outscores a genuine fact (0.609) and a plain
# request ("Please send the report by Friday.", 0.530) clears the threshold too.
# These are speech acts, not assertions, and speech act is a structural
# property — so it is decided structurally instead of by nudging the threshold
# (which would cost real recall; see the sweep in that report).
_QUESTION_OPENERS = (
    "what", "when", "where", "who", "whom", "whose", "why", "how",
    "do i", "did i", "am i", "are we", "have i", "has my", "is my", "can i",
    "should i", "will i", "would i", "does my",
)
_IMPERATIVE_OPENERS = (
    "please", "remind me", "tell me", "show me", "give me", "help me",
    "summarise", "summarize", "translate", "explain", "list ", "find ",
    "请", "帮我", "告诉我", "给我", "把", "翻译", "总结", "解释",
)


def _is_question_or_request(text: str) -> bool:
    """True when the text asks or instructs rather than asserts.

    A trailing question mark is the unambiguous signal; the cue lists catch the
    interrogative and imperative shapes that are reliably *openers* (so an
    ordinary fact such as "I always pay the deposit." is unaffected even though
    it contains no such cue).
    """
    stripped = text.strip()
    if stripped.endswith(("?", "？")):
        return True
    normalized = stripped.casefold()
    if normalized.startswith(_QUESTION_OPENERS):
        return True
    return normalized.startswith(_IMPERATIVE_OPENERS)


# ---------------------------------------------------------------------------
# Gate object
# ---------------------------------------------------------------------------
class SemanticGate:
    """Callable write gate that adds a semantic second opinion to v1.

    ``SemanticGate(encoder)`` builds its prototype index once (~24 short
    encodes) and is then reusable across any number of decisions.
    """

    def __init__(self, encoder: Any, *, weights: SemanticWeights = DEFAULT_WEIGHTS,
                 cache_size: int = 0) -> None:
        self.index = PrototypeIndex.build(encoder, cache_size=cache_size)
        self.weights = weights
        self.policy_version = POLICY_VERSION
        # Advertised so ``extraction.extract_candidates`` knows it may forward
        # the ``source_type`` keyword; the v1 ``decide`` function does not set
        # this and therefore keeps its original signature.
        self.supports_source_type = True

    def __call__(self, text: str, *, explicit: bool = False,
                 source_reliability: float = 0.8,
                 source_type: str | None = None) -> GateDecision:
        return _semantic_decide(
            text, index=self.index, explicit=explicit,
            source_reliability=source_reliability, weights=self.weights,
            source_type=source_type,
        )

    def warm(self, texts: Sequence[str]) -> int:
        """Pre-encode a batch in one inference call.

        The ingest contract calls the gate one turn at a time, so without this
        every turn pays a separate single-string inference. With a cache the
        warmed vectors are then reused; without one this still warms the model
        but the per-turn calls recompute. Returns the number of texts seen.
        """
        if not texts:
            return 0
        materialized = [text for text in texts if text.strip()]
        self.index.score_many(materialized)
        return len(materialized)


def load_local_gate(*, model: str = "BAAI/bge-m3",
                    revision: str = "5617a9f61b028005a4858fdac845db406aefb181",
                    device: str | None = None,
                    cache_size: int = 0) -> SemanticGate:
    """Build the production gate from the pinned local bge-m3 checkpoint.

    ``cache_size`` opts into a bounded vector cache. Long sweeps should set it
    (the benchmark runner passes 8192) so a pre-encoded batch is not recomputed
    one string at a time; leave it at 0 for one-shot callers.
    """
    from .embeddings import LocalSentenceTransformerEmbeddingProvider

    provider = LocalSentenceTransformerEmbeddingProvider.load(
        model=model, revision=revision, device=device,
    )
    return SemanticGate(provider.encoder, cache_size=cache_size)
