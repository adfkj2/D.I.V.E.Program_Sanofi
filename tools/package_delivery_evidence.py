"""Publish a small evidence view; preserve original local reports unchanged."""

import hashlib
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/reports/engineering-delivery-20261012"
TARGET = ROOT / "artifacts/engineering-20261012"


def sanitize(value):
    if isinstance(value, dict):
        return {k:sanitize(v) for k,v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return value.replace(str(ROOT), "${REPOSITORY}").replace(ROOT.as_posix(), "${REPOSITORY}")
    return value


def main():
    TARGET.mkdir(parents=True, exist_ok=True)
    names = ["portable-replay.json", "replay-bundle.json", "structured-validation/summary.json",
             "structured-validation/results.json", "structured-validation/failures.json",
             "performance/summary.json", "performance-batched/summary.json", "ui-final/summary.json",
             "wheel-python312/summary.json"]
    provenance = []
    for name in names:
        original = SOURCE / name
        target = TARGET / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = original.read_bytes()
        target.write_text(json.dumps(sanitize(json.loads(raw)),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        provenance.append({"file": name, "original_sha256": hashlib.sha256(raw).hexdigest(),
                           "public_sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    tree = ET.parse(SOURCE / "full-tests.xml")
    for suite in tree.iter("testsuite"):
        if "hostname" in suite.attrib:
            suite.set("hostname", "local-host-redacted")
    tree.write(TARGET / "full-tests.xml", encoding="utf-8", xml_declaration=True)
    shutil.copyfile(SOURCE / "ui-final/01-current.png", ROOT / "docs/assets/workbench-current.png")
    shutil.copyfile(SOURCE / "ui-final/05-mobile.png", ROOT / "docs/assets/workbench-mobile.png")
    manifest = {"schema": "dive-engineering-delivery-v1", "date": "2026-10-12",
        "original_reports_retained": True, "public_transformation": "absolute repository paths and test hostname redacted; numerical results unchanged",
        "new_model_calls": 0, "new_model_cost_cny": "0", "official_score": "NOT_EVALUATED",
        "external_pending": ["organizer rule interpretation", "official judge", "real user task observation", "independent natural-language holdout", "remote CI including PostgreSQL"],
        "evidence": provenance}
    (TARGET / "manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__ == "__main__":
    main()
