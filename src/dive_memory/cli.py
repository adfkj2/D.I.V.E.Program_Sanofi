"""Installed-package entry point; no dependency on the source checkout."""

import argparse
import json
import sys
from pathlib import Path

from .application.demo import load_demo
from .application.records import ProjectRecord
from .application.settings import application_from_environment


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DIVE persistent project memory")
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Open a local workbench")
    serve.add_argument("--port", type=int, default=8765)
    sub.add_parser(
        "recover", help="Retry pending local projections, at most 100 events"
    )
    for name in ("demo", "snapshot", "lookup", "ask", "ingest", "record", "forget"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        if name in {"snapshot", "lookup", "ask"}:
            p.add_argument("--as-of")
        if name == "lookup":
            p.add_argument("--key", required=True)
        if name == "ask":
            p.add_argument("question")
        if name == "ingest":
            p.add_argument("text")
            p.add_argument("--idempotency-key", required=True)
            p.add_argument("--explicit", action="store_true")
            p.add_argument("--observed-at")
        if name == "record":
            p.add_argument("file", type=Path, help="JSON with ProjectRecord fields")
            p.add_argument("--idempotency-key", required=True)
            p.add_argument("--observed-at")
            p.add_argument("--replaces")
        if name == "forget":
            p.add_argument("memory_id")
    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from .web import create_workbench

        uvicorn.run(
            create_workbench(application_from_environment(args.db)),
            host="127.0.0.1",
            port=args.port,
        )
        return 0
    app = application_from_environment(args.db)
    try:
        if args.command == "recover":
            result = {"recovered": app.recover()}
        elif args.command == "demo":
            result = load_demo(app, args.project)
        elif args.command == "snapshot":
            result = app.snapshot(args.project, as_of=args.as_of)
        elif args.command == "lookup":
            result = app.lookup(args.project, args.key, as_of=args.as_of)
        elif args.command == "ask":
            result = app.answer(args.project, args.question, as_of=args.as_of)
        elif args.command == "ingest":
            result = app.ingest(
                args.project,
                args.text,
                idempotency_key=args.idempotency_key,
                explicit=args.explicit,
                observed_at=args.observed_at,
            )
        elif args.command == "record":
            result = app.record(
                args.project,
                ProjectRecord(**json.loads(args.file.read_text(encoding="utf-8"))),
                idempotency_key=args.idempotency_key,
                observed_at=args.observed_at,
                replaces=args.replaces,
            )
        else:
            result = app.forget(args.project, args.memory_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
