# Failure record: GTE with Transformers 5.17

Date: 2026-09-20  
Experiment: Phase 4 embedding comparison  
Outcome: **FAILED incompatible runtime; RESOLVED with pinned runtime**

## Observed failure

The first `Alibaba-NLP/gte-multilingual-base` attempt used Transformers 5.17
and the model's custom remote code failed during execution with:

```text
IndexError: index 4351307874304 is out of bounds for dimension 0 with size 17
```

The failed process produced no accepted quality or performance result. The full
stack trace was not committed, so this record preserves the observed exception
without claiming a narrower internal root cause than runtime/model-code
incompatibility.

## Corrective action

A dedicated model environment was aligned with the model-card-compatible
runtime and pinned to Transformers 4.39.1 with sentence-transformers 3.0.1 and
PyTorch 2.14.0+cpu. Both model weights and custom code are revision-pinned:

- model: `Alibaba-NLP/gte-multilingual-base@9bbca17d9273fd0d03d5725c7a4b0f6b45142062`
- remote code: `Alibaba-NLP/new-impl@40ced75c3017eb27626c9d4ea981bde21a2662f4`

## Validation

- Completed isolated artifact:
  `eval/reports/embedding-gte-latest.json`
- Artifact SHA-256:
  `f78d6475ed590640838a2204a4a9d04138a641bfabd5a89a8bd8b400f862b97f`
- Recorded output dimension: 768.
- Completed controlled-set quality: Top-1 87.50%, Recall@5 100%, MRR
  0.9375 and nDCG@10 0.9539.

## Residual risk

GTE requires `trust_remote_code=True`. A working version pin prevents accidental
runtime drift for this benchmark but does not remove custom-code supply-chain,
upgrade and long-term maintenance risk. Any later runtime upgrade must repeat
the compatibility test before the generation is staged. This successful run
does not establish compatibility with Transformers 5.x.
