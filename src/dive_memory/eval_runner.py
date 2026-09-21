from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .ablation import run_ablation
from .eval_manifest import build_run_manifest
from .evaluation import EvalCase
from .service import MemoryService


def run_internal_retrieval(dataset_path: str | Path) -> dict[str, Any]:
    path = Path(dataset_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "dive-internal-retrieval-v1":
        raise ValueError("unsupported internal retrieval dataset schema")
    service = MemoryService()
    memory_ids_by_key: dict[str, list[str]] = {}
    for event in payload["events"]:
        result = service.ingest(
            event["namespace"], event["text"], explicit=event.get("explicit", False),
            observed_at=event.get("observed_at"), idempotency_key=f"internal:{event['key']}",
        )
        memory_ids_by_key[event["key"]] = result["memory_ids"]
    cases: list[EvalCase] = []
    for row in payload["queries"]:
        expected: list[str] = []
        for key in row.get("expected_event_keys", []):
            expected.extend(memory_ids_by_key[key])
        cases.append(EvalCase(
            row["case_id"], row["namespace"], row["question"], None, expected,
            should_abstain=row.get("should_abstain", False), query_type=row["query_type"],
        ))
    return {"dataset_schema": payload["schema_version"], "cases": len(cases),
            "ablation": run_ablation(service, cases)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the offline Phase 3 retrieval regression")
    parser.add_argument("dataset")
    parser.add_argument("--config", default="eval/configs/p0-offline.json")
    parser.add_argument("--output")
    args = parser.parse_args()
    dataset = Path(args.dataset)
    config_path = Path(args.config)
    configuration = json.loads(config_path.read_text(encoding="utf-8"))
    report = run_internal_retrieval(dataset)
    report["manifest"] = build_run_manifest(
        run_id="internal-p0-offline",
        dataset_name="internal-v1",
        dataset_version="1",
        dataset_path=dataset,
        configuration=configuration,
        repository_root=Path.cwd(),
    ).to_dict()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
