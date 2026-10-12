"""LongMemEval **Oracle** answer pipeline.

This package implements the scored deliverable of the D.I.V.E. project: ingest
each question's haystack in real time order, answer the question from formed
memories, and emit the official ``{"question_id", "hypothesis"}`` JSONL for the
project group to re-run.

It is deliberately separate from ``dive_memory.longmemeval_benchmark``, which
measures *formation* and *evidence retrieval* on ``LongMemEval_S-cleaned``.
Those numbers are diagnostics; they are not Overall Accuracy and must never be
reported as the Benchmark score.
"""

from .contracts import (
    CANONICAL_ABSTENTION_HYPOTHESIS,
    ORACLE_DATASET_NAME,
    ORACLE_EXPECTED_CASES,
    LeakageError,
    OracleResumeConflictError,
    OracleSubmissionError,
    ReaderStatus,
    Stage,
)
from .loader import OracleDataset, OracleLoadError, load_oracle_dataset, select_cases
from .pipeline import CaseResult, PipelineConfig, run_case
from .reader import (
    DeterministicReader,
    GroundedChatReader,
    OracleReader,
    OracleReaderRequest,
    ReaderOutcome,
    ReaderSpec,
    reader_outcome,
)

__all__ = [
    "CANONICAL_ABSTENTION_HYPOTHESIS",
    "ORACLE_DATASET_NAME",
    "ORACLE_EXPECTED_CASES",
    "CaseResult",
    "DeterministicReader",
    "GroundedChatReader",
    "LeakageError",
    "OracleDataset",
    "OracleLoadError",
    "OracleReader",
    "OracleReaderRequest",
    "OracleResumeConflictError",
    "OracleSubmissionError",
    "PipelineConfig",
    "ReaderOutcome",
    "ReaderSpec",
    "ReaderStatus",
    "Stage",
    "load_oracle_dataset",
    "reader_outcome",
    "run_case",
    "select_cases",
]
