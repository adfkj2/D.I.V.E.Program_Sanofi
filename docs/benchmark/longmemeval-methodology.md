# LongMemEval-cleaned methodology

## Evidence scope

LongMemEval-cleaned is the primary public conversational-memory benchmark for
Phase 4. The completed run uses the official `LongMemEval_S` cleaned artifact,
but only executes the D.I.V.E memory-formation and retrieval stage.

The evidence boundary is explicit:

| Stage | Status | What may be claimed |
|---|---|---|
| Official dataset adapter | COMPLETED | All 500 published `LongMemEval_S` cases were parsed and validated. |
| D.I.V.E formation and retrieval | COMPLETED | All 500 cases produced retrieval-stage rows; retrieval metrics may be reported. |
| Reader answer generation | NOT COMPLETED | No generated-answer accuracy may be reported. |
| Official GPT-4o judge | NOT COMPLETED | No official overall/full QA score may be reported. |

This is therefore a completed retrieval-stage run on the official dataset, not
an official full LongMemEval QA result. The machine-readable report uses the
overall status `PARTIAL` for exactly this reason. Numerical results are in
[`longmemeval-report.md`](longmemeval-report.md).

## Pinned sources and inputs

The source investigation and run pin the following identities:

| Item | Pinned identity |
|---|---|
| Official repository | [xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval), commit `9e0b455f4ef0e2ab8f2e582289761153549043fc` |
| Official cleaned dataset repository | [xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned), commit `98d7416c24c778c2fee6e6f3006e7a073259d48f` |
| Published license | MIT in the pinned official repository/dataset metadata |
| Evaluated file | `longmemeval_s_cleaned.json`, 277,383,467 bytes, SHA-256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Downloaded oracle file | `longmemeval_oracle.json`, 15,388,478 bytes, SHA-256 `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c` |
| Adapter | `longmemeval-adapter-v2` |

The oracle file was preserved for provenance but was not used by this
retrieval run. The official artifact is stored under the ignored
`eval/external/longmemeval/` directory rather than vendored into Git.

## Task and schema handling

The adapter preserves the question-type strings published in the artifact:

- `single-session-user`
- `single-session-assistant`
- `single-session-preference`
- `multi-session`
- `temporal-reasoning`
- `knowledge-update`

It does not relabel or collapse those types into a locally invented category.
The 30 abstention questions are identified primarily by the official `_abs`
question-id suffix and are scored separately from evidence retrieval.

The official cleaned artifact required the following explicit compatibility
rules:

- string and finite numeric answers are accepted; numeric answers are rendered
  as their JSON text because the official answer judge consumes text;
- dates in `YYYY/MM/DD (Day) HH:MM` format are parsed;
- official timestamps are treated as day-precision for cutoff validation when
  both timestamps use that published format, because 65 sessions have a later
  randomized clock time but none have a later calendar date;
- repeated filler session ids are occurrence-qualified when forming turn refs;
- empty string turns remain visible to schema validation but are skipped by the
  ingestion runner;
- haystack arrays must align, question ids must be unique, and every evidence
  session must exist in the case;
- a session on a calendar date after the question cutoff is rejected.

Observed release characteristics are recorded, not silently repaired: 13
cases contain duplicate filler session ids and 12 turns are empty.

## Leakage boundary

For each turn, the memory writer receives only this allow-list:
`namespace`, `session_id`, occurrence-qualified `turn_id`, `role`, `content`,
and `occurred_at`.

The question, answer, question type, `answer_session_ids`, turn-level
`has_answer` labels, and all evaluation mappings stay outside the writer. Gold
labels are consulted only after ingestion to score returned provenance. Each
question is isolated in its own namespace and fresh `MemoryService` instance.

## Formation and retrieval protocol

For each of the 500 cases, the runner:

1. ingests every non-empty haystack turn in published order;
2. uses the unchanged core heuristic formation provider with
   `explicit=false`;
3. assigns stable idempotency/source references derived from the question and
   occurrence-qualified turn ref;
4. retrieves at the published question cutoff with top-k set to 10;
5. executes BM25, dense, predicate, temporal, entity and relation channels;
   multi-hop is disabled;
6. excludes deterministic hash vectors from semantic dense scoring;
7. maps each returned memory's source provenance back to gold turns and gold
   sessions for evaluation.

The frozen run configuration is `limit=10`, `max_cases=null`, no reader or
judge, and no configured model temperature or token budget. The runner exposes
no seed parameter and the artifact contains no seed field. CPU and RAM details
were also not captured; only the operating system and Python runtime were
recorded. These omissions limit exact environment reproduction even though the
input, configuration and output hashes are pinned.

No benchmark-specific rule was added to the core memory service. Formation can
discard a gold turn, and that loss is deliberately visible in the formation
coverage and turn-level retrieval results.

Abstention prediction is the existing retrieval behavior: a case is predicted
as abstention only when retrieval returns no items. There is no benchmark-only
confidence threshold or answer-aware abstention policy.

## Metrics

Evidence-retrieval macro averages use the 470 answerable cases. The 30
abstention cases have no evidence location and are excluded from those averages.
They are evaluated separately for abstention accuracy.

Two relevance units are reported:

- **Turn level:** a returned memory is relevant only if its source refs include
  a gold `has_answer` turn event.
- **Session level:** a returned memory is relevant if any source event belongs
  to an `answer_session_ids` session.

Session relevance is intentionally coarser. It can credit a memory from the
correct session even when the exact evidence turn never formed a memory, so it
must never be used to conceal turn-level or formation results.

At k in `{1, 5, 10}`, the report records:

- `recall_any_at_k`: fraction of cases with at least one relevant unit;
- `recall_all_at_k`: fraction of cases whose complete gold unit set is covered;
- `evidence_coverage_at_k`: mean fraction of gold units covered;
- `precision_at_k`: mean relevant ranked memories divided by the fixed k;
- `ndcg_at_k`: binary unit-level discounted gain normalized by the number of
  gold units;
- `mrr`: reciprocal rank of the first relevant returned memory.

For nDCG, a gold evidence unit contributes gain only on its first occurrence.
Multiple returned memories from the same gold turn or session cannot inflate
nDCG above 1.0.

Latency uses all completed rows. Percentiles are order statistics over 500
samples; ingest latency is per complete case and retrieval latency is per
question. This run has no separate cold/warm protocol, concurrency treatment,
PostgreSQL backend, or production load, so these numbers are local harness
measurements only.

## QA protocol status

The official end-to-end protocol requires reader answer generation followed by
the official GPT-4o answer judge. No reader or judge credentials were configured
for this run. Consequently:

- evaluation model invocation: `NOT COMPLETED`;
- generation prompt and token settings: not instantiated;
- judge prompt and token settings: not instantiated;
- generated answers: none;
- official overall QA score: unavailable.

Retrieval metrics are not substituted for, calibrated to, or described as the
missing official QA score.

## Failure accounting

The report stores a row for every successfully evaluated case and separate
`errors` and `timeouts` arrays. The completed artifact contains 500 rows, zero
case errors and zero timeouts. It has no separate `unsupported_cases` field;
there were no skipped retrieval cases because all 500 dataset cases have rows.
The unexecuted QA stage is recorded at stage level rather than misclassified as
500 retrieval errors.

## Reproduction contract

From the repository root, with the official artifact already present:

```powershell
$env:PYTHONPATH = "src"
python -m dive_memory.longmemeval_benchmark `
  eval/external/longmemeval/longmemeval_s_cleaned.json `
  --output eval/reports/longmemeval-s-retrieval-latest.json `
  --limit 10
```

The final artifact is
`eval/reports/longmemeval-s-retrieval-latest.json`, SHA-256
`b574c4a07bc901842186b91cdcabf4d7431611dada3de202d304ffa4fc0d8e4b`.
It records D.I.V.E commit `16f213464aab60c3e14563ede4109087a14fcaf4`
on branch `main`, a dirty worktree, configuration SHA-256
`c8361bd7259f7904546e2c34b47ec03d92b58bede5b77bfb0339c51b3c12c30b`,
CPython 3.14.0 and Windows 11. The dirty-worktree flag is a reproducibility
limitation: the commit alone does not identify the uncommitted Phase 4 code,
so the artifact hash and reviewed source state must be retained together.

## Interpretation boundary

This methodology supports claims about D.I.V.E formation and memory retrieval
on the official `LongMemEval_S` cleaned questions under the documented local
adapter. It does not establish final-answer quality, official overall score,
semantic-model quality, PostgreSQL behavior, concurrency, scale behavior, or
production readiness. The ranked objects are normalized D.I.V.E memories, not
the official raw turn/session retrieval objects, so direct baseline comparison
also requires a separately matched protocol.
