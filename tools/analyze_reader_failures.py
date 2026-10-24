"""Offline, answer-free diagnostics for saved reader traces and evaluation rows.

No provider, benchmark loader or application imports are used. Correctness is
reported exactly as supplied; source visibility does not establish sufficiency.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

_HEADER = re.compile(r"(?m)^\[\d+\] id=([^;\n]+); source=([^;\n]*);")
_FAILURES = frozenset({"INVALID", "ERROR", "TIMEOUT"})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read(path):
    data = path.read_bytes()
    try:
        result = json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid JSON input: {path}") from exc
    return result, hashlib.sha256(data).hexdigest()


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _usage(rows, field):
    values = [_count(row.get(field)) for row in rows]
    known = sum(value for value in values if value is not None)
    unknown = sum(value is None for value in values)
    return {
        "known_subtotal": known,
        "unknown_records": unknown,
        "total": None if unknown else known,
    }


def _visible(turn):
    context = turn.get("context")
    if not isinstance(context, str):
        return None
    headers = list(_HEADER.finditer(context))
    if context.strip() and not headers:
        return None
    return {
        source.strip()
        for header in headers
        for source in header[2].split(",")
        if source.strip()
    }


def _detail(trace):
    reader = trace.get("reader") or {}
    if not isinstance(reader, dict):
        raise ValueError("reader must be an object")
    failure = trace.get("failure") or {}
    if not isinstance(failure, dict):
        raise ValueError("failure must be an object")
    detail = reader.get("detail") or failure.get("message")
    if detail is None:
        return None
    if isinstance(detail, str):
        try:
            detail = json.loads(detail, object_pairs_hook=_unique_object)
        except ValueError:
            # Some transport failures save a plain diagnostic, without turns.
            if reader.get("detail"):
                raise ValueError("Malformed reader detail") from None
            return None
    if not isinstance(detail, dict) or not isinstance(detail.get("turns"), list):
        raise ValueError("Reader detail must contain a turns list")
    if not all(isinstance(turn, dict) for turn in detail["turns"]):
        raise ValueError("Every reader turn must be an object")
    return detail["turns"]


def analyze(trace_directories, evaluation_path):
    """Return diagnostics without emitting questions, responses or references."""
    evaluation_path = Path(evaluation_path)
    evaluation, digest = _read(evaluation_path)
    if not isinstance(evaluation, dict) or not isinstance(
        evaluation.get("results"), list
    ):
        raise ValueError("Evaluation must contain a results list")
    results = evaluation["results"]
    selected = evaluation.get("selected")
    if type(selected) is not int or selected != len(results) or selected < 1:
        raise ValueError("Evaluation selected must equal its positive results count")
    ids = set()
    for row in results:
        if not isinstance(row, dict):
            raise ValueError("Every evaluation row must be an object")
        qid = row.get("question_id")
        if not isinstance(qid, str) or not qid or qid in ids:
            raise ValueError("Evaluation IDs must be unique nonempty strings")
        if "correct" not in row or (
            row["correct"] is not None and type(row["correct"]) is not bool
        ):
            raise ValueError("Correctness must be explicit boolean or null")
        if not isinstance(row.get("status"), str) or not row["status"]:
            raise ValueError("Every evaluation row requires a status")
        ids.add(qid)
    traces = {}
    hashes = {str(evaluation_path): digest}
    for directory in trace_directories:
        directory = Path(directory)
        if not directory.is_dir():
            raise ValueError(f"Trace directory does not exist: {directory}")
        for path in sorted(directory.rglob("*.json")):
            trace, digest = _read(path)
            if not isinstance(trace, dict):
                raise ValueError(f"Trace must be an object: {path}")
            qid = trace.get("question_id")
            if not isinstance(qid, str) or not qid or qid in traces:
                raise ValueError("Trace IDs must be unique nonempty strings")
            if qid not in ids:
                raise ValueError("Trace ID is absent from the evaluation")
            traces[qid] = trace
            hashes[str(path)] = digest

    calls = Counter()
    statuses = Counter()
    invalid = Counter()
    tool_counts = Counter()
    calculation_outcomes = Counter()
    reader_usage = []
    judge_usage = []
    unknown_judge_usage = []
    visibility_rows = []
    case_rows = []
    missing_trace = 0
    unknown_calls = 0
    visibility_unknown = 0
    for row in results:
        qid, status = row["question_id"], row["status"]
        statuses[status] += 1
        trace = traces.get(qid)
        turns = _detail(trace) if trace is not None else None
        if trace is None:
            missing_trace += 1
        if turns is None:
            unknown_calls += 1
        else:
            calls[len(turns)] += 1
        usage = (trace or {}).get("tokens") or {}
        if not isinstance(usage, dict):
            raise ValueError("Trace tokens must be an object")
        reader_usage.append(
            {
                "input_tokens": usage.get("reader_input"),
                "output_tokens": usage.get("reader_output"),
            }
        )
        attempts = row.get("judge_attempts")
        if attempts is None:
            attempts = [row["judge"]] if isinstance(row.get("judge"), dict) else []
        if not isinstance(attempts, list) or not all(
            isinstance(attempt, dict) for attempt in attempts
        ):
            raise ValueError("judge_attempts must be a list of objects")
        judge_usage.extend(attempts)
        if not attempts and row["correct"] is None:
            # An interrupted outcome can have no completed attempt object.
            # Preserve its unknown usage without inventing a request count.
            unknown_judge_usage.append({})
        if status == "INVALID":
            last = turns[-1] if turns else {}
            invalid[last.get("error") or last.get("validation_error") or "unknown"] += 1
        for turn in turns or []:
            action = turn.get("action") or {}
            if not isinstance(action, dict):
                raise ValueError("Logged action must be an object")
            kind = action.get("action")
            if isinstance(kind, str):
                tool_counts[kind] += 1
            if kind == "calculate":
                outcome = turn.get("error") or turn.get("validation_error")
                if outcome is None:
                    outcome = "success" if turn.get("calculation") else "unknown"
                calculation_outcomes[outcome] += 1
        visibility = [_visible(turn) for turn in turns or []]
        transitions = []
        for number, (previous, current) in enumerate(
            zip(visibility, visibility[1:]), start=1
        ):
            if previous is None or current is None:
                visibility_unknown += 1
                continue
            transitions.append(
                {
                    "from_call": number,
                    "to_call": number + 1,
                    "lost_source_refs": sorted(previous - current),
                    "gained_source_refs": sorted(current - previous),
                }
            )
        initial_to_final = None
        if visibility and visibility[0] is not None and visibility[-1] is not None:
            initial_to_final = sorted(visibility[0] - visibility[-1])
        visibility_rows.append(
            {
                "question_id": qid,
                "transitions": transitions,
                "initial_sources_missing_from_final": initial_to_final,
            }
        )
        case_rows.append(
            {
                "question_id": qid,
                "status": status,
                "correct": row["correct"],
                "trace_available": trace is not None,
                "reader_calls": len(turns) if turns is not None else None,
                "input_tokens": _count(usage.get("reader_input")),
                "output_tokens": _count(usage.get("reader_output")),
            }
        )
    correct = sum(row["correct"] is True for row in results)
    wrong = sum(row["correct"] is False for row in results)
    return {
        "schema_version": "reader-failure-analysis-v1",
        "scope": "Offline supplied verdicts and saved traces; visibility is not semantic sufficiency. No result changes, answer generation or accuracy prediction.",
        "selected": selected,
        "confirmed_correct": correct,
        "confirmed_wrong": wrong,
        "unknown_verdicts": selected - correct - wrong,
        "confirmed_accuracy": correct / selected,
        "saved_traces": len(traces),
        "missing_traces": missing_trace,
        "statuses": dict(statuses),
        "execution_failure_statuses": sum(
            count for status, count in statuses.items() if status in _FAILURES
        ),
        "invalid_final_types": dict(invalid),
        "reader_call_counts": dict(calls),
        "unknown_reader_call_cases": unknown_calls,
        "logged_tool_actions": dict(tool_counts),
        "calculation_outcomes": dict(calculation_outcomes),
        "source_visibility": {
            "cases_with_initial_source_lost": sum(
                bool(row["initial_sources_missing_from_final"])
                for row in visibility_rows
            ),
            "transitions_with_source_lost": sum(
                bool(transition["lost_source_refs"])
                for row in visibility_rows
                for transition in row["transitions"]
            ),
            "unknown_transition_count": visibility_unknown,
            "rows": visibility_rows,
        },
        "reader_usage": {
            field: _usage(reader_usage, field)
            for field in ("input_tokens", "output_tokens")
        },
        "judge_usage": {
            "logged_attempts": len(judge_usage),
            "unlogged_unknown_outcomes": len(unknown_judge_usage),
            **{
                field: _usage([*judge_usage, *unknown_judge_usage], field)
                for field in ("input_tokens", "output_tokens")
            },
            "scope": "Logged judge attempts plus unknown outcomes without completed attempts; exact interrupted request counts require the external ledger.",
        },
        "cost_cny": None,
        "cost_note": "Billing, pending request reservations and price assumptions are not inferred from traces.",
        "cases": case_rows,
        "input_sha256": hashes,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces", nargs="+", required=True, type=Path)
    parser.add_argument("--evaluation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = analyze(args.traces, args.evaluation)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects old diagnostics and also input files.
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "selected",
                    "confirmed_correct",
                    "confirmed_wrong",
                    "unknown_verdicts",
                    "saved_traces",
                    "missing_traces",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
