"""python -m services.snapshot_runner --help"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .bundle import SnapshotError, validate
from .docker_backend import DockerBackend
from .input_audit import scrub


def main(argv=None):
    parser = argparse.ArgumentParser(description="Restore all Snapshot state, run Hermes, export New Trajectory")
    parser.add_argument("--project", default="datalaker-demo", help="isolated Compose project")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("provision", help="start the isolated database/lake/mail/idle agent stack")
    for name in ("validate", "restore"):
        sub.add_parser(name).add_argument("snapshot", type=Path)
    capture = sub.add_parser("capture", help="quiesce the stack and capture private starting state")
    capture.add_argument("snapshot", type=Path)
    run = sub.add_parser("run")
    run.add_argument("snapshot", type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--seconds", type=float, default=60)
    run.add_argument("--state", type=Path, help="optional validated governance overlay, including approvals")
    run.add_argument("--mail", type=Path, help="optional single current mail; absent means autonomous continuation")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            value = validate(args.snapshot)
            result = {"state": "valid", "version": value["version"], "parts": sorted(value["artifacts"])}
        else:
            backend = DockerBackend(args.project)
            with backend.lock():
                if args.command == "provision":
                    backend.provision()
                    result = {"state": "ready"}
                elif args.command == "capture":
                    result = backend.capture(args.snapshot)
                elif args.command == "restore":
                    result = backend.restore(args.snapshot)
                else:
                    state = json.loads(args.state.read_text()) if args.state else None
                    mail = json.loads(args.mail.read_text()) if args.mail else None
                    result = backend.run(args.snapshot, args.output, seconds=args.seconds, state=state, mail=mail)
        print(json.dumps(scrub(result), ensure_ascii=False, indent=2))
        return 1 if result.get("state") == "failed" else 0
    except (SnapshotError, OSError, ValueError) as exc:
        print(json.dumps({"state": "failed", "error": scrub(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
