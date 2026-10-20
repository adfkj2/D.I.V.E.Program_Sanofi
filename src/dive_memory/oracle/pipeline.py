"""One-question Oracle pipeline: ingest -> retrieve -> pack -> read.

Ordering guarantees this module is responsible for:

* **Chronological ingest.** ``LongMemEvalCase.chronological_turns`` is used, not
  the published haystack order. 34 of the 500 official rows publish a session
  out of order; ingesting them as published replays part of the history
  backwards, and the write path resolves supersession *at ingest time*, so the
  last-written fact wins. Ordering is therefore a correctness property, not a
  tidiness one.
* **Cutoff enforcement.** Every turn is filtered against ``question_date``
  before ingest, and the retrieval carries both ``as_of`` (valid time) and
  ``observed_as_of`` (observation time) set to the question date. A session
  dated after the question cannot influence the answer.
* **Isolation.** One ``MemoryService`` with an in-memory store per question, in
  a namespace derived from the question id. No state survives a question, so
  cross-question contamination is structurally impossible.
* **Leakage.** The reader request is constructed from the question, the
  timestamps and the budget-limited packed context. The case's ``answer``,
  ``answer_session_ids``, ``has_answer`` and ``question_type`` are never read
  on this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Mapping, Sequence

from ..context import pack_context
from ..contextual_fact_retention import ContextualFactRetention
from ..episodic_retention import EpisodicRetentionPolicy
from ..eval_adapters.longmemeval import LongMemEvalCase, LongMemEvalTurn, parse_instant
from ..evidence_search import recover_evidence
from ..models import DOCUMENT_PREDICATE
from ..narrative_retention import NarrativeRetentionPolicy
from ..retrieval import SCORING_POLICIES as SCORING_POLICIES
from ..retrieval import RetrievalConfig, ScoringPolicy, scoring_policy
from ..service import MemoryService
from ..source_diversity import diversify_sources
from ..user_document_retention import ExplicitUserDocumentRetention
from .contracts import (
    OracleFailure,
    Stage,
    assert_writer_visible,
    find_evaluator_only_fields,
)
from .controlled_documents import (
    _controlled_candidates,
    _controlled_pack,
    _document_supplement_config,
)
from .evidence import (
    AnswerKind,
    build_evidence_views,
    classify_answer_kind,
    render_evidence_block,
)
from .reader import (
    OracleReaderRequest,
    ReaderPolicy,
    assert_prompt_is_clean,
    build_evidence_packet,
    build_reader_prompt,
    reader_outcome,
    reader_policy,
    render_reader_context,
)
from .source_coordinates import attach_source_coordinates
from .spare_documents import extend_documents
from .statement_provenance import attach_statement_provenance

CONTEXT_BUDGET_VERSION = "reader-visible-evidence-v2"


def role_source_type(turn_role: str) -> str:
    """Map a chat role onto the v2 provenance vocabulary.

    This decides who *spoke*, not what is worth remembering -- the write gate
    still makes that call.
    """

    return "user" if str(turn_role).casefold() in {"user", "human"} else "assistant"


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    limit: int = 10
    token_budget: int = 1500
    retrieval_config_name: str = "oracle-default"
    include_multi_hop: bool = False
    enforce_chronological_ingest: bool = True
    #: Which reading strategy the reader runs under. A name rather than an
    #: object so the config stays hashable and serialises into the manifest.
    reader_policy_name: str = "evidence_first"
    #: How retrieved candidates are re-ranked on top of reciprocal-rank fusion.
    #: A name for the same reason as ``reader_policy_name``.
    scoring_policy_name: str = "legacy"
    #: ``prefix`` preserves the historical character-cap behaviour. The
    #: experimental alternative retrieves a query-relevant, source-verbatim
    #: passage without changing the stored document.
    document_selection_name: str = "prefix"
    #: The chunk index can add document candidates to RRF. ``assistant_cued``
    #: uses only the visible wording of the question, never its gold type.
    document_chunk_search_name: str = "off"
    source_coordinates: bool = False
    statement_provenance: bool = False
    episodic_retention: bool = False
    semantic_retrieval_only: bool = False
    evidence_recovery: bool = False
    narrative_retention: bool = False
    source_diversity: bool = False
    narrative_clause_anchors: bool = False
    reported_detail_anchors: bool = False
    reported_detail_assertions_only: bool = False
    precise_detail_terms: bool = False
    contextual_fact_anchors: bool = False
    scoped_fact_anchors: bool = False
    explicit_user_documents: bool = False
    prefer_explicit_user_documents: bool = False
    controlled_document_limit: int = 2
    controlled_document_token_budget: int = 600
    controlled_document_char_budget: int | None = None
    expand_document_spans: bool = False
    spare_document_context: bool = False
    spare_document_token_budget: int = 2400
    spare_document_char_budget: int = 4800

    def __post_init__(self) -> None:
        if type(self.contextual_fact_anchors) is not bool:
            raise ValueError('contextual_fact_anchors must be boolean')
        if type(self.scoped_fact_anchors) is not bool:
            raise ValueError('scoped_fact_anchors must be boolean')
        if self.scoped_fact_anchors and not self.contextual_fact_anchors:
            raise ValueError('scoped facts require contextual retention')
        if type(self.explicit_user_documents) is not bool:
            raise ValueError('explicit_user_documents must be boolean')
        if type(self.prefer_explicit_user_documents) is not bool:
            raise ValueError('prefer_explicit_user_documents must be boolean')
        if self.prefer_explicit_user_documents and (not self.explicit_user_documents
                or self.document_chunk_search_name != 'controlled_supplement'):
            raise ValueError('explicit document priority requires retention and controlled supplement')
        if self.spare_document_context and self.document_chunk_search_name != 'controlled_supplement':
            raise ValueError('spare document context requires controlled supplement')
        if self.spare_document_context and (self.spare_document_token_budget < self.controlled_document_token_budget
                                           or self.spare_document_char_budget <= 0):
            raise ValueError('invalid spare document budget')
        if self.narrative_clause_anchors and not self.narrative_retention:
            raise ValueError("clause anchors require narrative retention")
        if self.reported_detail_anchors and not self.narrative_retention:
            raise ValueError("reported detail anchors require narrative retention")
        if self.reported_detail_assertions_only and not self.reported_detail_anchors:
            raise ValueError("assertion constraint requires reported detail anchors")
        if self.precise_detail_terms and not self.reported_detail_anchors:
            raise ValueError("precise detail terms require reported detail anchors")
        if self.controlled_document_limit < 0 or self.controlled_document_token_budget < 0:
            raise ValueError("document limits must be nonnegative")
        if self.controlled_document_char_budget is not None and self.controlled_document_char_budget <= 0:
            raise ValueError("document character cap must be positive")
        if self.document_selection_name not in {"prefix", "query_excerpt"}:
            raise ValueError("unknown document selection policy")
        if self.document_chunk_search_name not in {"off", "all", "assistant_cued", "controlled_supplement"}:
            raise ValueError("unknown document chunk search policy")
        if self.document_chunk_search_name == "controlled_supplement" and (
            self.scoring_policy_name != "balanced" or self.document_selection_name != "query_excerpt"
            or not self.policy().annotate_evidence
        ):
            raise ValueError("controlled_supplement requires balanced, query_excerpt and annotated evidence")
        if self.document_selection_name == "query_excerpt" and self.scoring().document_char_budget <= 0:
            raise ValueError("query_excerpt requires a scoring policy with a document character cap")

    def policy(self) -> ReaderPolicy:
        return reader_policy(self.reader_policy_name)

    def scoring(self) -> ScoringPolicy:
        return scoring_policy(self.scoring_policy_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "token_budget": self.token_budget,
            "retrieval_config_name": self.retrieval_config_name,
            "include_multi_hop": self.include_multi_hop,
            "enforce_chronological_ingest": self.enforce_chronological_ingest,
            "reader_policy": self.reader_policy_name,
            "scoring_policy": self.scoring_policy_name,
            "document_selection": self.document_selection_name,
            "document_chunk_search": self.document_chunk_search_name,
            "context_budget_version": CONTEXT_BUDGET_VERSION,
            "event_identity_version": "namespace-idempotency-sha256-v1",
            "source_coordinates": self.source_coordinates,
            "statement_provenance": self.statement_provenance,
            "episodic_retention": self.episodic_retention,
            "semantic_retrieval_only": self.semantic_retrieval_only,
            "evidence_recovery": self.evidence_recovery,
            "narrative_retention": self.narrative_retention,
            "source_diversity": self.source_diversity,
            "narrative_clause_anchors": self.narrative_clause_anchors,
            "reported_detail_anchors": self.reported_detail_anchors,
            "reported_detail_assertions_only": self.reported_detail_assertions_only,
            "precise_detail_terms": self.precise_detail_terms,
            "contextual_fact_anchors": self.contextual_fact_anchors,
            **({"scoped_fact_anchors": True} if self.scoped_fact_anchors else {}),
            "explicit_user_documents": self.explicit_user_documents,
            "prefer_explicit_user_documents": self.prefer_explicit_user_documents,
            "controlled_document_limit": self.controlled_document_limit,
            "controlled_document_token_budget": self.controlled_document_token_budget,
            "controlled_document_char_budget": self.controlled_document_char_budget,
            "expand_document_spans": self.expand_document_spans,
            "spare_document_context": self.spare_document_context,
            "spare_document_token_budget": self.spare_document_token_budget,
            "spare_document_char_budget": self.spare_document_char_budget,
        }


@dataclass(frozen=True, slots=True)
class CaseResult:
    record: Mapping[str, Any]
    hypothesis: str | None
    ok: bool


def _stage_failure(case: LongMemEvalCase, stage: Stage, exc: BaseException) -> dict[str, Any]:
    return OracleFailure(
        question_id=case.question_id,
        stage=stage,
        error_type=type(exc).__name__,
        message=str(exc)[:2000],
        retryable=isinstance(exc, (TimeoutError, ConnectionError, OSError)),
    ).to_dict()


def ordered_turns(case: LongMemEvalCase, *, enforce: bool = True) -> tuple[LongMemEvalTurn, ...]:
    return case.chronological_turns if enforce else case.turns


def packing_content_caps(scoring: ScoringPolicy) -> dict[str, int]:
    """Per-predicate character ceilings derived from the scoring policy.

    A document memory is capped; nothing else is. The mapping is derived from the
    policy rather than hard-coded so that changing the cap is a configuration
    change that shows up in the manifest.
    """

    caps: dict[str, int] = {}
    if scoring.document_char_budget > 0:
        caps[DOCUMENT_PREDICATE] = int(scoring.document_char_budget)
    return caps


def ingest_case(
    service: MemoryService,
    case: LongMemEvalCase,
    *,
    turns: Sequence[LongMemEvalTurn],
    warm_batch: int = 32,
    gate: Any | None = None,
) -> dict[str, Any]:
    """Ingest every non-empty turn in chronological order.

    The semantic gate is warmed in bounded blocks immediately before the block
    it will score. A single whole-case warm-up evicts its own earliest vectors
    from the bounded cache (see ``docs/benchmark/longmemeval-500case-profiling.md``).
    """

    warm = getattr(gate, "warm", None)
    requires_encoding = getattr(gate, "requires_encoding", None)
    nonempty = [turn for turn in turns if turn.content.strip()]
    encoder_inputs = 0
    events = 0
    memories: set[str] = set()
    skipped_duplicate = 0
    # ``turn_events`` and ``turn_memory_counts`` are the *only* per-turn
    # bookkeeping the pipeline keeps, and they cover every turn, gold or not.
    # The analysis stage needs them to say whether a wrong answer was a
    # formation failure, a retrieval failure or a reading failure -- without
    # them the 500-question failure list is unactionable. They stay in the
    # internal trace; nothing downstream of the memory layer sees them.
    turn_events: dict[str, str] = {}
    turn_memory_counts: dict[str, int] = {}
    for start in range(0, len(nonempty), max(1, warm_batch)):
        block = nonempty[start:start + max(1, warm_batch)]
        if callable(warm):
            pending: list[str] = []
            for turn in block:
                source_type = role_source_type(turn.role)
                values = [turn.content]
                if callable(requires_encoding):
                    values = [value for value in values
                              if requires_encoding(value, explicit=False, source_type=source_type)]
                pending.extend(values)
            encoder_inputs += len(pending)
            warm(pending)
        for turn in block:
            result = service.ingest(
                case.namespace,
                turn.content,
                event_type=f"longmemeval_{turn.role.casefold()}",
                explicit=False,
                observed_at=turn.occurred_at_iso,
                idempotency_key=f"longmemeval:{case.question_id}:{turn.ref}",
                source_message_id=turn.ref,
                source_type=role_source_type(turn.role),
            )
            events += 1
            if result.get("duplicate"):
                skipped_duplicate += 1
            memory_ids = list(result.get("memory_ids", []))
            turn_events[turn.ref] = str(result.get("event_id"))
            turn_memory_counts[turn.ref] = len(memory_ids)
            memories.update(memory_ids)
    return {
        "turns_offered": len(turns),
        "turns_ingested": len(nonempty),
        "events": events,
        "duplicate_events": skipped_duplicate,
        "memories_formed": len(memories),
        "encoder_inputs": encoder_inputs,
        "embedding_calls": getattr(gate, "encode_calls", None),
        "turn_events": turn_events,
        "turn_memory_counts": turn_memory_counts,
    }


def run_case(
    case: LongMemEvalCase,
    *,
    reader: Any,
    gate: Any | None = None,
    embedder: Any | None = None,
    config: PipelineConfig | None = None,
    gate_name: str = "utility-baseline-v1",
) -> CaseResult:
    """Run one question end to end and return a fully auditable record.

    The record is written even when a stage fails: the failure keeps its stage,
    its exception type and its message, because "which stage broke" is the only
    thing that makes a 500-question failure list actionable.
    """

    config = config or PipelineConfig()
    document_chunk_search = (
        config.document_chunk_search_name == "all" or
        (config.document_chunk_search_name == "assistant_cued" and
         classify_answer_kind(case.question) is AnswerKind.ASSISTANT_RECALL)
    )
    retrieval_config = RetrievalConfig.full(
        include_multi_hop=config.include_multi_hop,
        include_document_chunks=document_chunk_search,
        name=config.retrieval_config_name,
        scoring=config.scoring(),
    )
    if config.semantic_retrieval_only:
        if not getattr(embedder, "semantic_similarity", False):
            raise ValueError("semantic_retrieval_only requires a real semantic embedder")
        from dataclasses import replace
        retrieval_config = replace(retrieval_config,
                                   enabled_channels=retrieval_config.enabled_channels - {"bm25"})
    visibility_cutoff = case.visibility_cutoff_iso
    base: dict[str, Any] = {
        "schema_version": "dive-oracle-case-v1",
        "question_id": case.question_id,
        "namespace": case.namespace,
        "as_of": visibility_cutoff,
        "question_date": case.question_date_iso,
        "gate": gate_name,
        "retrieval_config": retrieval_config.name,
        "scoring_policy": retrieval_config.scoring.name,
        "scoring_policy_fingerprint": retrieval_config.scoring.fingerprint(),
        "document_chunk_search_enabled": document_chunk_search,
        "context_budget_version": CONTEXT_BUDGET_VERSION,
    }
    ordered = ordered_turns(case, enforce=config.enforce_chronological_ingest)
    turns = tuple(turn for turn in ordered if not _after(turn, case))
    haystack_chronological = case.haystack_is_chronological
    cutoff_rejected = [
        turn.ref for turn in case.turns
        if _after(turn, case)
    ]
    base["chronology"] = {
        "haystack_was_chronological": haystack_chronological,
        "turns_ingested": len([turn for turn in turns if turn.content.strip()]),
        "first_occurred_at": turns[0].occurred_at_iso if turns else None,
        "last_occurred_at": turns[-1].occurred_at_iso if turns else None,
        "turns_after_question_date": cutoff_rejected,
        "visibility_cutoff": visibility_cutoff,
    }

    # Structural leakage guard: the writer allow-list is asserted on every row
    # the pipeline would hand to ingest, so a future adapter change that starts
    # forwarding gold fails here rather than silently in a 500-question run.
    try:
        rows = list(case.system_input())
        assert_writer_visible(rows)
        base["writer_input_keys"] = sorted({key for row in rows for key in row})
        base["writer_input_rows"] = len(rows)
    except Exception as exc:  # noqa: BLE001 - reported as a LOAD-stage failure
        return CaseResult(
            {**base, "status": "FAILED", "hypothesis": None,
             "failure": _stage_failure(case, Stage.LOAD, exc)},
            None, False,
        )

    # ``embedder`` reaches the store here, not at construction of the runner:
    # the store decides whether the dense channel is semantic, and a v1 artefact
    # must keep using the offline hash stub. Passing ``None`` preserves exactly
    # the historical behaviour, so this is an opt-in upgrade rather than a
    # silent change to every published number.
    from ..gate import decide
    retention = (NarrativeRetentionPolicy(gate or decide, clause_anchors=config.narrative_clause_anchors,
                 reported_detail_anchors=config.reported_detail_anchors,
                 reported_detail_assertions_only=config.reported_detail_assertions_only,
                 precise_detail_terms=config.precise_detail_terms) if config.narrative_retention else
                 EpisodicRetentionPolicy() if config.episodic_retention else None)
    if config.explicit_user_documents:
        retention = ExplicitUserDocumentRetention(retention)
    if config.contextual_fact_anchors:
        retention = ContextualFactRetention(retention, scoped_assertions=config.scoped_fact_anchors)
    service = MemoryService(gate=gate, embedder=embedder, deterministic_ids=True,
                            episodic_policy=retention)
    try:
        total_started = perf_counter()
        try:
            ingest_started = perf_counter()
            formation = ingest_case(service, case, turns=turns, gate=gate)
            ingest_ms = (perf_counter() - ingest_started) * 1000
        except Exception as exc:  # noqa: BLE001
            return CaseResult(
                {**base, "status": "FAILED", "hypothesis": None,
                 "failure": _stage_failure(case, Stage.FORMATION, exc)},
                None, False,
            )

        try:
            retrieve_started = perf_counter()
            result = service.retrieve(
                case.namespace,
                case.question,
                limit=max(1, config.limit) * (3 if config.source_diversity else 1),
                as_of=visibility_cutoff,
                observed_as_of=visibility_cutoff,
                config=retrieval_config,
            )
            controlled = None
            recovery_audit = None
            if config.source_diversity:
                result.items, diversity_audit = diversify_sources(result.items, max(1, config.limit))
                result.trace["source_diversity"] = diversity_audit
            if config.evidence_recovery:
                def search_probe(query):
                    return service.retrieve(case.namespace, query, limit=max(1, config.limit),
                        as_of=visibility_cutoff, observed_as_of=visibility_cutoff,
                        config=retrieval_config).items
                result.items, recovery_audit = recover_evidence(
                    case.question, result.items, search_probe, limit=max(1, config.limit))
            if config.document_chunk_search_name == "controlled_supplement":
                supplement = service.retrieve(
                    case.namespace, case.question, limit=max(20, config.limit * 2),
                    as_of=visibility_cutoff, observed_as_of=visibility_cutoff,
                    config=_document_supplement_config(),
                )
                facts, documents, audit = _controlled_candidates(result.items, supplement.items,
                    limit=config.limit, max_documents=config.controlled_document_limit,
                    prefer_explicit_user_documents=config.prefer_explicit_user_documents)
                spans = {str(c["memory_id"]): tuple(c["document_chunk_span"])
                         for c in supplement.trace.get("candidates", ())
                         if c.get("selected") and c.get("document_chunk_span")}
                controlled = (facts, documents, spans)
                result.items = [*facts, *documents]
                result.trace["controlled_document_candidates"] = audit
                result.trace["supplement"] = supplement.trace
            retrieve_ms = (perf_counter() - retrieve_started) * 1000
        except Exception as exc:  # noqa: BLE001
            return CaseResult(
                {**base, "status": "FAILED", "hypothesis": None, "formation": formation,
                 "failure": _stage_failure(case, Stage.RETRIEVAL, exc)},
                None, False,
            )

        try:
            pack_started = perf_counter()
            spare_audit = None
            if config.source_coordinates:
                session_indices: dict[tuple[str, int], int] = {}
                positions = {}
                for turn in turns:
                    event_id = formation["turn_events"].get(turn.ref)
                    if not event_id:
                        continue
                    session = (turn.session_id, turn.session_occurrence)
                    session_indices.setdefault(session, len(session_indices) + 1)
                    positions[event_id] = {"source_ref": event_id,
                                           "session_index": session_indices[session],
                                           "turn_index": turn.turn_index + 1,
                                           "role": role_source_type(turn.role)}
                result.items = attach_source_coordinates(result.items, positions)
                if controlled is not None:
                    facts, documents, spans = controlled
                    controlled = (attach_source_coordinates(facts, positions),
                                  attach_source_coordinates(documents, positions), spans)
            if config.statement_provenance:
                statement_sources = {
                    formation["turn_events"][turn.ref]: {
                        "role": role_source_type(turn.role),
                        "statement_at": parse_instant(turn.occurred_at).isoformat(),
                    }
                    for turn in turns if formation["turn_events"].get(turn.ref)
                }
                result.items = attach_statement_provenance(result.items, statement_sources)
                if controlled is not None:
                    facts, documents, spans = controlled
                    controlled = (attach_statement_provenance(facts, statement_sources),
                                  attach_statement_provenance(documents, statement_sources), spans)
            current_only = result.plan.get("temporal_mode") not in {"historical", "earliest", "all"}
            policy = config.policy()
            budget_renderer = None
            if policy.annotate_evidence:
                def budget_renderer(visible_items: Sequence[Any]) -> str:
                    views = build_evidence_views(
                        visible_items, question=case.question,
                        question_date=visibility_cutoff,
                    )
                    return render_evidence_block(views)
            chunk_spans = {
                str(candidate["memory_id"]): tuple(candidate["document_chunk_span"])
                for candidate in result.trace.get("candidates", ())
                if candidate.get("selected") and candidate.get("document_chunk_span")
            }
            packed = pack_context(
                result.items, max(0, int(config.token_budget)), current_only=current_only,
                content_caps=packing_content_caps(config.scoring()),
                question=case.question,
                document_selection=config.document_selection_name,
                document_spans=chunk_spans,
                render_for_budget=budget_renderer,
            )
            if controlled is not None:
                facts, documents, spans = controlled
                packed, audit = _controlled_pack(
                    facts, documents, question=case.question, question_date=visibility_cutoff,
                    current_only=current_only, document_spans=spans,
                    token_budget=max(0, int(config.token_budget)),
                    document_token_budget=config.controlled_document_token_budget,
                    max_documents=config.controlled_document_limit,
                    document_char_budget=config.controlled_document_char_budget,
                    expand_document_spans=config.expand_document_spans,
                )
                result.trace["controlled_document_packing"] = audit
                if config.spare_document_context:
                    packed, spare_audit = extend_documents(
                        packed, documents, question=case.question, question_date=visibility_cutoff,
                        current_only=current_only, document_spans=spans,
                        token_budget=config.token_budget, max_documents=config.controlled_document_limit,
                        document_token_budget=config.spare_document_token_budget,
                        max_chars=config.spare_document_char_budget,
                        base_char_budget=config.controlled_document_char_budget,
                    )
                    result.trace['spare_document_packing'] = spare_audit
            pack_ms = (perf_counter() - pack_started) * 1000
        except Exception as exc:  # noqa: BLE001
            return CaseResult(
                {**base, "status": "FAILED", "hypothesis": None, "formation": formation,
                 "failure": _stage_failure(case, Stage.PACKING, exc)},
                None, False,
            )

        request = OracleReaderRequest(
            question=case.question,
            # The official slash-format clock is not a visibility boundary.
            # Keep the reader's temporal scaffold consistent with retrieval.
            question_date=visibility_cutoff,
            as_of=visibility_cutoff,
            # A reader may only see and cite the exact spans admitted by the
            # packer, never the full source memory behind a truncated passage.
            items=tuple(packed.visible_items),
            context_text=packed.text,
            token_budget=max(0, int(config.token_budget)),
            evidence_omitted=packed.omitted,
            policy=config.policy(),
        )
        prompt = build_reader_prompt(request, policy=config.policy())
        assert_prompt_is_clean(prompt)
        # The evidence layer is computed once and recorded, so the trace shows
        # exactly what the reader reasoned over without re-running anything.
        evidence_views, evidence_assessment, temporal_scaffold = build_evidence_packet(
            request, config.policy()
        )
        reader_context_text = render_reader_context(request, config.policy())
        if reader_context_text != packed.text:
            return CaseResult(
                {**base, "status": "FAILED", "hypothesis": None, "formation": formation,
                 "failure": {"question_id": case.question_id, "stage": Stage.PACKING.value,
                             "error_type": "ContextBudgetMismatch",
                             "message": "reader context differs from the budgeted context",
                             "retryable": False}},
                None, False,
            )
        leaked = find_evaluator_only_fields(
            {key: value for key, value in prompt.items() if key != "output_schema"}
        )
        if leaked:
            return CaseResult(
                {**base, "status": "FAILED", "hypothesis": None, "formation": formation,
                 "failure": {"question_id": case.question_id, "stage": Stage.READING.value,
                             "error_type": "LeakageError",
                             "message": f"prompt carries evaluator-only fields {leaked!r}",
                             "retryable": False}},
                None, False,
            )

        read_started = perf_counter()
        active_source = None
        bind = getattr(reader, "bind_evidence", None)
        if callable(bind):
            from ..retrieval_session import MemoryEvidenceSource
            active_source = MemoryEvidenceSource(service, case.namespace,
                as_of=visibility_cutoff, initial=request.items)
            reader = bind(active_source)
        outcome = reader_outcome(reader, request)
        read_ms = (perf_counter() - read_started) * 1000
        total_ms = (perf_counter() - total_started) * 1000

        hypothesis = outcome.resolved_hypothesis if outcome.origin else None
        record = {
            **base,
            "status": "COMPLETED" if not outcome.is_failure else "DEGRADED",
            "failure": None if not outcome.is_failure else {
                "question_id": case.question_id,
                "stage": Stage.READING.value,
                "error_type": outcome.error_type or outcome.status.value,
                "message": outcome.detail or outcome.reason_code,
                "retryable": outcome.status.value in {"TIMEOUT", "ERROR"},
            },
            "formation": formation,
            "retrieval": {
                "returned": len(result.items),
                "returned_memory_ids": [item.memory.id for item in result.items],
                "returned_source_refs": [list(item.source_refs) for item in result.items],
                "executed_channels": result.trace.get("executed_channels", []),
                "degraded": result.degraded,
                "empty": not result.items,
                "abstain_reason": result.abstain_reason,
                "temporal_mode": result.plan.get("temporal_mode"),
                **({"evidence_recovery": recovery_audit} if recovery_audit is not None else {}),
            },
            "packing": {
                "packed_memory_ids": [item.memory.id for item in packed.items],
                "packed_count": len(packed.items),
                "omitted": packed.omitted,
                "estimated_tokens": packed.estimated_tokens,
                "token_counter": packed.token_counter,
                "token_count_degraded": packed.token_count_degraded,
                "decisions": packed.decisions,
                "current_only": current_only,
                "document_selection": config.document_selection_name,
                "context_budget_version": CONTEXT_BUDGET_VERSION,
                **({'spare_document_expansion': spare_audit} if spare_audit is not None else {}),
            },
            "context_text": packed.text,
            # What the reader actually saw, plus the deterministic evidence
            # structure behind it. Kept in the trace so a reviewer can audit a
            # single question without re-running the pipeline.
            "reader_context_text": reader_context_text,
            "evidence": {
                "assessment": evidence_assessment.to_dict(),
                "views": [view.to_dict() for view in evidence_views],
                "temporal_scaffold": temporal_scaffold.to_dict(),
            },
            "reader": {
                **outcome.to_dict(),
                "spec": getattr(reader, "spec", None).to_dict() if getattr(reader, "spec", None) else None,
            },
            "hypothesis": hypothesis,
            "hypothesis_origin": outcome.origin.value if outcome.origin else None,
            "timing_ms": {
                "ingest": ingest_ms,
                "retrieve": retrieve_ms,
                "pack": pack_ms,
                "read": read_ms,
                "total": total_ms,
            },
            "tokens": {
                "reader_input": outcome.usage.input_tokens,
                "reader_output": outcome.usage.output_tokens,
                "reader_usage_source": outcome.usage.source,
                "context_estimated": packed.estimated_tokens,
            },
            "calls": {"retrieval": 1 + (active_source.search_calls if active_source else 0),
                      **({"memory_read": active_source.read_calls} if active_source else {}),
                      "reader": outcome.calls,
                      "embedding": formation["embedding_calls"]},
        }
        return CaseResult(record, hypothesis, outcome.origin is not None)
    finally:
        try:
            service.store.close()
        except Exception:  # noqa: BLE001 - closing must not mask the real result
            pass


def _after(turn: LongMemEvalTurn, case: LongMemEvalCase) -> bool:
    """True when a turn is dated after the question's cutoff.

    ``LongMemEvalAdapter`` already rejects a dataset that contains one; the
    pipeline re-checks so that a caller who builds a case by hand still cannot
    ingest the future.
    """

    return parse_instant(turn.occurred_at) > parse_instant(case.visibility_cutoff_iso)


def summarize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """The compact per-question projection the analysis stage reads."""

    reader = record.get("reader") or {}
    return {
        "question_id": record.get("question_id"),
        "status": record.get("status"),
        "failure_stage": (record.get("failure") or {}).get("stage"),
        "hypothesis": record.get("hypothesis"),
        "hypothesis_origin": record.get("hypothesis_origin"),
        "reader_status": reader.get("status"),
        "reader_reason": reader.get("reason_code"),
        "evidence_state": ((record.get("evidence") or {}).get("assessment") or {}).get("state"),
        "retrieved": (record.get("retrieval") or {}).get("returned"),
        "context_tokens": (record.get("packing") or {}).get("estimated_tokens"),
        "timing_ms": record.get("timing_ms"),
        "tokens": record.get("tokens"),
    }
