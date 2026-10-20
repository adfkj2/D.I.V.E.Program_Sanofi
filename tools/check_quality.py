"""Reproducible local/CI quality gate; a new directory preserves every run."""

import argparse
import fnmatch
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def coverage_groups(report: dict, groups: dict, *, root: Path | None = None) -> dict:
    """Reject absent files/branch data; never silently shrink the denominator."""
    if not report.get("meta", {}).get("branch_coverage"):
        raise ValueError("branch coverage was not collected")
    files = {name.replace("\\", "/"): data for name, data in report["files"].items()}
    results = {}
    for name, group in groups.items():
        selected = set()
        for pattern in group["include"]:
            matches = {path for path in files if fnmatch.fnmatchcase(path, pattern)}
            if not matches:
                raise ValueError(f"coverage group {name} has no data for {pattern}")
            if root is not None:
                expected = {
                    path.relative_to(root).as_posix() for path in root.glob(pattern)
                }
                missing = expected - matches
                if missing:
                    raise ValueError(
                        f"coverage group {name} omitted source files: {sorted(missing)}"
                    )
            selected.update(matches)
        totals = {
            key: sum(files[path]["summary"][key] for path in selected)
            for key in (
                "num_statements",
                "covered_lines",
                "num_branches",
                "covered_branches",
            )
        }
        if not totals["num_statements"] or not totals["num_branches"]:
            raise ValueError(f"coverage group {name} has an empty denominator")
        lines = totals["covered_lines"] * 100 / totals["num_statements"]
        branches = totals["covered_branches"] * 100 / totals["num_branches"]
        results[name] = {
            **totals,
            "files": sorted(selected),
            "line_percent": lines,
            "branch_percent": branches,
            "line_min": group["line-min"],
            "branch_min": group["branch-min"],
            "passed": lines >= group["line-min"] and branches >= group["branch-min"],
        }
    return results


def portable(text: str) -> str:
    for path, marker in (
        (str(ROOT), "${REPOSITORY}"),
        (str(Path.home()), "${USER_HOME}"),
    ):
        text = text.replace(path, marker).replace(path.replace("\\", "/"), marker)
    return text


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def source_manifest() -> dict[str, str]:
    paths = {
        *ROOT.joinpath("src").rglob("*.py"),
        *ROOT.joinpath("tests").rglob("*.py"),
        *ROOT.joinpath("tools").rglob("*.py"),
        *ROOT.joinpath("requirements").glob("*.lock"),
        ROOT / "pyproject.toml",
        ROOT / ".github/workflows/quality.yml",
    }
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for path in sorted(paths)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        parser.error(
            "use a new output directory; previous evidence is never overwritten"
        )
    output.mkdir(parents=True)
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["dive-quality"]
    manifest = source_manifest()
    write_json(
        output / "source-manifest.json",
        {"hash_normalization": "UTF-8 source bytes, CRLF to LF", "files": manifest},
    )
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("DIVE_")
    }
    env.update(
        PYTHONUTF8="1",
        DIVE_MODE="evidence_only",
        COVERAGE_FILE=str(output / ".coverage"),
    )
    commands = [
        (
            "source-correctness",
            ["-m", "ruff", "check", "src"],
        ),
        ("delivery-lint", ["-m", "ruff", "check", *config["lint-targets"]]),
        (
            "delivery-format",
            ["-m", "ruff", "format", "--check", *config["lint-targets"]],
        ),
        ("types", ["-m", "mypy"]),
        (
            "tests",
            [
                "-m",
                "coverage",
                "run",
                "-m",
                "pytest",
                "-q",
                "-ra",
                f"--junitxml={output / 'tests.xml'}",
            ],
        ),
        (
            "coverage-json",
            ["-m", "coverage", "json", "-o", str(output / "coverage.json")],
        ),
        ("casebook-sources", ["tools/build_project_casebook.py", "--check"]),
    ]
    results = []
    for name, command in commands:
        print(f"Checking {name}...", flush=True)
        started = time.monotonic()
        try:
            process = subprocess.run(
                [sys.executable, *command],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            code = process.returncode
            log = process.stdout + process.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, log = 1, str(exc)
        (output / f"{name}.txt").write_text(
            portable(log), encoding="utf-8", newline="\n"
        )
        results.append(
            {
                "name": name,
                "command": ["${PYTHON}", *[portable(arg) for arg in command]],
                "returncode": code,
                "seconds": round(time.monotonic() - started, 3),
                "log": f"{name}.txt",
            }
        )
    summary: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": sys.platform,
        "checks": results,
    }
    try:
        report = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
        summary["coverage"] = coverage_groups(report, config["coverage"], root=ROOT)
        summary["all_source_coverage"] = report["totals"]
    except (OSError, ValueError, KeyError) as exc:
        summary["coverage_error"] = portable(str(exc))
        summary["coverage"] = {}
    test_file = output / "tests.xml"
    if test_file.exists():
        tree = ET.parse(test_file)
        suites = list(tree.getroot().iter("testsuite"))
        summary["tests"] = {
            key: sum(int(s.get(key, "0")) for s in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
        summary["skips"] = [
            {
                "test": case.get("classname", "") + "." + case.get("name", ""),
                "reason": skip.get("message", ""),
            }
            for case in tree.iter("testcase")
            for skip in case.findall("skipped")
        ]
        for suite in suites:
            suite.attrib.pop("hostname", None)
        test_file.write_text(
            portable(ET.tostring(tree.getroot(), encoding="unicode")),
            encoding="utf-8",
            newline="\n",
        )
    summary["source_unchanged_during_checks"] = manifest == source_manifest()
    summary["passed"] = (
        all(row["returncode"] == 0 for row in results)
        and bool(summary["coverage"])
        and all(group["passed"] for group in summary["coverage"].values())
        and summary["source_unchanged_during_checks"]
    )
    write_json(output / "summary.json", summary)
    print(
        json.dumps(
            {"passed": summary["passed"], "report": str(output / "summary.json")},
            indent=2,
        )
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
