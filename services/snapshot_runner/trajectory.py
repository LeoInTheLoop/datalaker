"""New trajectory: persisted messages plus direct output/tool observations.

Old sessions are restored as context, then fingerprinted before boot. Inputs are
separate evidence and never substitute outputs. Archive after stopping the group.
"""
from __future__ import annotations

from collections import Counter
import json
import os
import pathlib
import re
import sqlite3

from .input_audit import scrub, digest
from .bundle import write_json

STATE_DB = "state.db"


def _home():
    return pathlib.Path(os.environ.get("HERMES_HOME", "/state/hermes"))


def _root(run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
        raise ValueError("invalid run id")
    return pathlib.Path(os.environ.get("DATASTEWARD_INPUT_AUDIT_DIR", "/audit")) / run_id


def _rows(db):
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT m.id, m.session_id, m.role, m.content, m.tool_calls,"
            " m.tool_name, m.tool_call_id, m.timestamp, s.source, s.session_key"
            " FROM messages m LEFT JOIN sessions s ON s.id=m.session_id ORDER BY m.timestamp,m.id")]


def _signature(row):
    return digest({k: row.get(k) for k in ("session_id", "role", "content", "tool_calls",
                                          "tool_name", "tool_call_id")})


def baseline(run_id):
    db = _home() / STATE_DB
    rows = _rows(db) if db.exists() else []
    value = {"messages": dict(Counter(_signature(row) for row in rows)), "count": len(rows),
             "request_dumps": [p.name for p in _home().rglob("request_dump_*.json")]}
    write_json(_root(run_id) / "baseline.json", value)
    return value


def _parse(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


def collect(blocked_tools=(), *, run_id=None):
    errors, rows, records, prior = [], [], [], Counter()
    root = _root(run_id) if run_id else None
    if root:
        try:
            prior = Counter(json.loads((root / "baseline.json").read_text())["messages"])
        except (OSError, ValueError, KeyError):
            errors.append("missing runtime baseline")
        try:
            path = root / "requests.jsonl"
            records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        except (OSError, ValueError):
            errors.append("unreadable output observations")
        if (root / "audit_error.json").exists():
            errors.append("observer reported an error")
    db = _home() / STATE_DB
    if db.exists():
        try:
            rows = _rows(db)
        except sqlite3.Error as exc:
            errors.append(f"session database unreadable: {exc}")
    elif not root:
        errors.append("no session database")
    messages = []
    for row in rows:
        signature = _signature(row)
        if prior[signature]:
            prior[signature] -= 1
            continue
        messages.append({**row, "session": row.get("session_key"),
                         "tool_calls": _parse(row.get("tool_calls")) or []})
    answered = {(r["session_id"], r.get("tool_call_id")) for r in messages if r["role"] == "tool"}
    interrupted = []
    for row in messages:
        for call in row["tool_calls"] if isinstance(row["tool_calls"], list) else []:
            if isinstance(call, dict) and call.get("id") and (row["session_id"], call["id"]) not in answered:
                interrupted.append({"session_id": row["session_id"], "tool_call_id": call["id"],
                                    "tool": (call.get("function") or {}).get("name")})
    outputs = [r for r in records if r.get("kind") in ("response", "error", "tool_start", "tool_result", "tool_blocked")]
    ended = {(r.get("session_id"), r.get("api_request_id")) for r in outputs if r["kind"] in ("response", "error")}
    pending = [{"session_id": r.get("session_id"), "api_request_id": r.get("api_request_id")}
               for r in records if r.get("kind") == "request"
               and (r.get("session_id"), r.get("api_request_id")) not in ended]
    if any(r.get("kind") == "response" and not r.get("complete") for r in records):
        errors.append("response hook omitted/truncated assistant output")
    assistants = [m for m in messages if m["role"] == "assistant"]
    direct = [r for r in outputs if r["kind"] == "response" and r.get("assistant_message")]
    return scrub({"state": "inconclusive" if errors else "recorded",
        "reason": "; ".join(errors) if errors else "new persisted messages and direct output observations",
        "messages": messages, "observations": outputs,
        "counts": {"messages": len(messages), "assistant": len(assistants),
                   "tool_calls": sum(len(m["tool_calls"]) for m in messages if isinstance(m["tool_calls"], list)),
                   "tool_results": len(answered), "responses": len(direct)},
        "final_assistant": direct[-1]["assistant_message"] if direct else (assistants[-1] if assistants else None),
        "interrupted_calls": interrupted, "interrupted_requests": pending,
        "blocked_after_limit": list(blocked_tools), "errors": errors})


def archive(run_id, blocked_tools=()):
    target = _root(run_id) / "trajectory.json"
    write_json(target, collect(blocked_tools, run_id=run_id))
    return target


def read(run_id):
    try:
        return json.loads((_root(run_id) / "trajectory.json").read_text())
    except (OSError, ValueError):
        return {"state": "inconclusive", "reason": "本轮没有轨迹导出", "messages": []}
