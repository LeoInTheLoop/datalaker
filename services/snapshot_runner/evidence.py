"""Evaluator-side input evidence. This module is never a Hermes hook."""
from __future__ import annotations

import json
import os
import pathlib
import re

from .input_audit import scrub


def read_inputs(run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
        return {"state": "inconclusive", "reason": "没有本轮输入记录", "requests": []}
    root = pathlib.Path(os.environ.get("DATASTEWARD_INPUT_AUDIT_DIR", "/audit")) / run_id
    try:
        runtime = json.loads((root / "runtime.json").read_text())
        records = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
        requests = [json.loads(path.read_text()) for path in sorted(root.glob("request_dump_*.json"))]
    except (OSError, ValueError):
        return {"state": "inconclusive", "reason": "输入记录缺失或不可读", "requests": []}
    calls = [r for r in records if r.get("kind") == "request"]
    finished = {r.get("api_request_id") for r in records if r.get("kind") in ("response", "error")}
    complete = (bool(calls) and len(requests) == len(calls)
                and all(r.get("api_request_id") in finished and r.get("complete") for r in calls)
                and all(isinstance(r.get("request", {}).get("body"), dict) for r in requests)
                and not (root / "audit_error.json").exists()
                and runtime.get("hermes_revision") not in (None, "unknown"))
    return scrub({"state": "recorded" if complete else "inconclusive",
                  "reason": "已记录完整请求（凭证脱敏）" if complete else "请求未结束或输入记录不完整",
                  "runtime": runtime, "calls": records, "requests": requests,
                  "completion": completion(run_id)})


def completion(run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
        return {}
    path = pathlib.Path(os.environ.get("DATASTEWARD_INPUT_AUDIT_DIR", "/audit")) / run_id / "completion.json"
    try:
        value = json.loads(path.read_text())
        return value if value.get("run_id") == run_id else {}
    except (OSError, ValueError, TypeError):
        return {}


def input_check(run_id: str, snapshot: dict | None) -> dict:
    evidence = read_inputs(run_id)
    # Compare long oracle-only text; ordinary business facts may legitimately
    # match both the opening and the expected result, so don't flag those.
    snapshot = snapshot or {}
    allowed = "\n".join(str((snapshot.get("opening") or {}).get(k) or "")
                        for k in ("from", "subject", "body"))
    hidden = [snapshot.get("note"), snapshot.get("terminal_condition"), snapshot.get("title"),
              snapshot.get("table_scope"),
              (snapshot.get("expected_outcome") or {}).get("answer")]
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    texts = list(strings(evidence.get("requests", [])))
    leaked = any(isinstance(v, str) and len(v) >= 12 and v not in allowed
                 and any(v in text for text in texts) for v in hidden)
    leaked = leaked or any("[SNAPSHOT_SCOPE]" in text for text in texts)
    return {"state": "fail" if leaked else evidence["state"],
            "reason": "发现测试专属指示进入实际请求" if leaked else evidence["reason"],
            "requests": len(evidence.get("requests", [])), "url": "/api/inputs"}
