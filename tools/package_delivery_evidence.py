"""Publish a small evidence view; preserve original local reports unchanged."""

import argparse
import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/reports/engineering-delivery-20261012"
TARGET = ROOT / "artifacts/engineering-20261012"
VALIDATED_CODE_COMMIT = "c0ca43dfff92a7566cbcd8768277f6264fe66757"


def sanitize(value):
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return (
            value.replace(str(ROOT), "${REPOSITORY}")
            .replace(ROOT.as_posix(), "${REPOSITORY}")
            .replace(str(ROOT.parent), "${WORKSPACE}")
            .replace(ROOT.parent.as_posix(), "${WORKSPACE}")
        )
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sanitize(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def add_evidence(provenance, name, original=None):
    item = {
        "file": name,
        "public_sha256": hashlib.sha256((TARGET / name).read_bytes()).hexdigest(),
    }
    if original is not None:
        item["original_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    else:
        item["generated_summary"] = True
    provenance.append(item)


def copy_test_report(original, name, provenance):
    tree = ET.parse(original)
    for node in tree.iter():
        node.attrib.update(sanitize(node.attrib))
        node.text = sanitize(node.text)
        node.tail = sanitize(node.tail)
    for suite in tree.iter("testsuite"):
        if "hostname" in suite.attrib:
            suite.set("hostname", "local-host-redacted")
    target = TARGET / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    )
    add_evidence(provenance, name, original)
    return tree


def package_clean_check(clean_checkout, provenance):
    commit = subprocess.check_output(
        ["git", "-C", str(clean_checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != VALIDATED_CODE_COMMIT:
        raise ValueError(
            "The clean validation checkout must remain at its tested commit"
        )
    subprocess.run(
        ["git", "-C", str(clean_checkout), "diff", "--exit-code", "HEAD"], check=True
    )
    source = clean_checkout / "artifacts/clean-check"
    tree = copy_test_report(
        source / "full-tests-final.xml", "clean-check/full-tests-final.xml", provenance
    )
    suites = list(tree.iter("testsuite"))
    counts = {
        key: sum(int(s.get(key, "0")) for s in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    replay = json.loads((source / "replay.json").read_bytes())
    structured = json.loads((source / "structured/summary.json").read_bytes())
    for original, name in [
        (source / "replay.json", "clean-check/replay.json"),
        (source / "structured/summary.json", "clean-check/structured-summary.json"),
    ]:
        write_json(TARGET / name, json.loads(original.read_bytes()))
        add_evidence(provenance, name, original)
    summary = {
        "completion_date": "2026-10-13",
        "validated_code_commit": commit,
        "clean_checkout": True,
        "historical_untracked_outputs_available": False,
        "tests": {
            "passed": counts["tests"]
            - counts["failures"]
            - counts["errors"]
            - counts["skipped"],
            "failed": counts["failures"],
            "errors": counts["errors"],
            "skipped": counts["skipped"],
            "junit_elapsed_seconds": sum(float(s.get("time", "0")) for s in suites),
            "terminal_elapsed_seconds": 26.42,
            "started_at": suites[0].get("timestamp"),
            "skip_reasons": dict(
                Counter(s.get("message", "") for s in tree.iter("skipped"))
            ),
        },
        "environment": {
            "python": "3.14",
            "test_tools": "Existing development environment; application and tests loaded from the clean checkout",
            "separate_fresh_install": "../wheel-python312-final/summary.json",
        },
        "reader_replay": replay,
        "structured_validation": {
            "stories": structured["stories"],
            "template_families": structured["template_families"],
            "blind": structured["blind"],
            "arms": structured["arms"],
        },
        "initial_failure": {
            "passed": 1122,
            "skipped": 10,
            "failed": 1,
            "cause": "Historical budget test depended on an untracked local request ledger",
            "fix": "Temporary shared-budget plan and pending-ledger fixtures; original assertion retained",
            "original_report_sha256": hashlib.sha256(
                (source / "full-tests.xml").read_bytes()
            ).hexdigest(),
        },
    }
    write_json(TARGET / "clean-check/summary.json", summary)
    add_evidence(provenance, "clean-check/summary.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean-checkout", type=Path, required=True)
    args = parser.parse_args()
    TARGET.mkdir(parents=True, exist_ok=True)
    names = [
        "portable-replay.json",
        "replay-bundle.json",
        "structured-validation/summary.json",
        "structured-validation/results.json",
        "structured-validation/failures.json",
        "performance/summary.json",
        "performance-batched/summary.json",
        "ui-final/summary.json",
        "wheel-python312/summary.json",
        "wheel-python312-final/summary.json",
    ]
    provenance = []
    for name in names:
        original = SOURCE / name
        target = TARGET / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = original.read_bytes()
        write_json(target, json.loads(raw))
        add_evidence(provenance, name, original)
    copy_test_report(SOURCE / "full-tests.xml", "full-tests.xml", provenance)
    package_clean_check(args.clean_checkout.resolve(), provenance)
    shutil.copyfile(
        SOURCE / "ui-final/01-current.png", ROOT / "docs/assets/workbench-current.png"
    )
    shutil.copyfile(
        SOURCE / "ui-final/05-mobile.png", ROOT / "docs/assets/workbench-mobile.png"
    )
    manifest = {
        "schema": "dive-engineering-delivery-v1",
        "date": "2026-10-12",
        "completion_date": "2026-10-13",
        "validated_code_commit": VALIDATED_CODE_COMMIT,
        "final_wheel_sha256": json.loads(
            (SOURCE / "wheel-python312-final/summary.json").read_bytes()
        )["wheel_sha256"],
        "original_reports_retained": True,
        "public_transformation": "absolute repository paths and test hostname redacted; numerical results unchanged",
        "public_text_encoding": "JSON/XML: UTF-8, LF newlines (stable across Git checkouts)",
        "new_model_calls": 0,
        "new_model_cost_cny": "0",
        "official_score": "NOT_EVALUATED",
        "external_pending": [
            "organizer rule interpretation",
            "official judge",
            "real user task observation",
            "independent natural-language holdout",
            "remote CI including PostgreSQL",
        ],
        "evidence": provenance,
    }
    write_json(TARGET / "manifest.json", manifest)


if __name__ == "__main__":
    main()
