# Failure record: initial BGE-M3 snapshot download

Date: 2026-09-20  
Experiment: Phase 4 embedding comparison  
Outcome: **FAILED initial attempt; RESOLVED for the completed benchmark**

## Observed failure

The initial BGE-M3 acquisition path attempted redundant model formats in
addition to the PyTorch runtime files, including ONNX payloads. The transfer
then encountered repeated disconnects and did not produce a valid benchmark
result. No quality or latency number was taken from that attempt.

This was a distribution/reproducibility failure, not evidence about BGE-M3
retrieval quality. A complete raw transfer log was not retained, so the record
does not claim exact failed byte counts, retry counts or a specific remote root
cause.

## Corrective action

The benchmark runner now pins the repository revision and requests only runtime
files needed by sentence-transformers: configuration/tokenizer files, Python
model code and PyTorch/Safetensors weights. It explicitly ignores ONNX,
OpenVINO and image payloads. The final download used the configured
`https://hf-mirror.com` endpoint.

## Validation

- Successful model revision:
  `BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181`
- Completed isolated artifact:
  `eval/reports/embedding-bge-m3-latest.json`
- Artifact SHA-256:
  `6a1f2e454793cecc6a9eb8b2d99047c2cf54ae4e9ecd5d705f1f1a7b6ac6f696`
- Final environment: Python 3.12.14, PyTorch 2.14.0+cpu,
  Transformers 4.39.1, sentence-transformers 3.0.1.

## Residual risk

The successful run proves that the pinned snapshot was usable from the chosen
mirror in this environment. It does not prove mirror availability, cold-cache
download reliability or disaster-recovery availability. Future release
evidence should preserve transfer logs and validate an internal artifact cache
or other controlled model-distribution path.
