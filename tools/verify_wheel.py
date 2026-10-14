"""Fresh virtual environment, installed wheel only, no checkout import paths."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel, = args.wheel_dir.resolve().glob("dive_memory-*.whl")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    environment = out / "environment"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {k:v for k,v in os.environ.items() if not k.startswith(("PYTHON", "DIVE_"))}
    env["PYTHONUTF8"] = "1"
    env["DIVE_MODE"] = "evidence_only"
    root = Path(__file__).resolve().parents[1]
    commands = [
        [str(python), "-m", "pip", "install", "-r", str(root / "requirements/dev.lock")],
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        [str(python), "-m", "pip", "check"],
    ]
    logs = []
    for command in commands:
        result = subprocess.run(command, cwd=out, env=env, text=True, capture_output=True, encoding="utf-8")
        logs.append({"command": command, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        (out / "install-log.json").write_text(json.dumps(logs, indent=2), encoding="utf-8")
        result.check_returncode()
    smoke = r'''
import json
from pathlib import Path
from importlib.resources import files
import dive_memory
from dive_memory.cli import main
from dive_memory.application.runtime import MemoryApplication
from dive_memory.web import create_workbench
from fastapi.testclient import TestClient
assert "site-packages" in dive_memory.__file__
assert main(["--db","fresh.db","demo","--project","fresh"]) == 0
assert main(["--db","fresh.db","casebook","--project","casebook"]) == 0
runtime = MemoryApplication("fresh.db")
assert runtime.lookup("fresh", "预算上限")["value"] == "40 元"
brief = runtime.brief("casebook")
assert "451/500" in brief["markdown"]
assert "未评估" in brief["markdown"]
assert runtime.consult("casebook", "R27 推广决定")["status"] == "ANSWER"
with TestClient(create_workbench(runtime)) as client:
    assert client.get("/").status_code == 200
    for asset in ("app.js", "app.css", "icon.svg"):
        assert client.get("/assets/" + asset).status_code == 200
    assert client.get("/api/verification").status_code == 200
    assert client.get("/api/brief", params={"project":"casebook"}).status_code == 200
    assert client.post("/api/consult",json={"project":"casebook","question":"官方成绩"}).json()["mode"] == "local_records"
    assert client.post("/api/answer",json={"project":"fresh","question":"预算上限"}).json()["status"] == "EVIDENCE_ONLY"
    personal = client.post("/api/projects",json={"name":"Installed project","goal":"resume tomorrow","request_id":"personal"}).json()["namespace"]
    assert client.post("/api/checkpoints",json={"project":personal,"note":"first session complete","next_step":"review results","request_id":"session"}).status_code == 200
    assert "review results" in client.get("/api/brief",params={"project":personal}).json()["markdown"]
    Path("backup.db").write_bytes(client.get("/api/backup").content)
restored = MemoryApplication("backup.db")
assert restored.project_details(personal)["tasks"][0]["title"] == "review results"
assert restored.project_details(personal)["checkpoints"][0]["note"] == "first session complete"
restored.close()
Path("smoke.json").write_text(json.dumps({"passed":True,"import_file":dive_memory.__file__,"cwd":str(Path.cwd()),"package_assets":6,"persistence_restart":True,"cli_api":True,"packaged_casebook":True,"local_consult":True,"handoff_markdown":True},indent=2),encoding="utf8")
'''
    result = subprocess.run([str(python), "-c", smoke], cwd=out, env=env, capture_output=True, text=True, encoding="utf-8")
    (out / "smoke-output.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    result.check_returncode()
    report = json.loads((out / "smoke.json").read_text(encoding="utf-8"))
    report.update(wheel=wheel.name, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                  python=sys.version, source_path_injected=False)
    (out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
