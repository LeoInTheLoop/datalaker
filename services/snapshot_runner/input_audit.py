"""Read-only Hermes API input recorder. No case files or agent decisions here.

The raw message/system hook fields preserve long prompts (the convenience
``request`` payload may be truncated by Hermes). Stored copies redact secrets;
hashes refer to the original input. Returning None never changes the request.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import time
import fcntl

_LOCK = threading.Lock()
_SECRET_KEY = re.compile(r"password|passwd|secret|token|api[_-]?key|authorization|cookie", re.I)
_DSN = re.compile(r"(?:postgres(?:ql)?|mysql|mongodb|rediss?)://[^\s\"'<>]+", re.I)
_TOKEN = re.compile(r"(https?://[^\s\"'<>]+[?&]t=)[^\s\"'<>]+", re.I)
_ASSIGNMENT = re.compile(r"((?:password|passwd|api[_-]?key|密码|口令)\s*[:=：]\s*)[^\s,;，；]+", re.I)


def scrub(value):
    if isinstance(value, dict):
        return {str(k): "<redacted>" if _SECRET_KEY.search(str(k)) else scrub(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _ASSIGNMENT.sub(r"\1<redacted>", _TOKEN.sub(r"\1<redacted>",
                                                       _DSN.sub("<redacted-dsn>", value)))
    return value


def encoded(value):
    def native(v):
        if hasattr(v, "model_dump"):
            return v.model_dump(mode="json")
        if hasattr(v, "__dict__"):
            return vars(v)
        raise TypeError(f"unserializable observation: {type(v).__name__}")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=native)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def truncated(value):
    if isinstance(value, dict):
        return any(str(k).startswith("_truncated") or truncated(v) for k, v in value.items())
    if isinstance(value, list):
        return any(truncated(v) for v in value)
    return isinstance(value, str) and ("...[truncated " in value or " depth limit>" in value)


def audit_dir():
    root, run = os.environ.get("DATASTEWARD_INPUT_AUDIT_DIR"), os.environ.get("DATASTEWARD_AUDIT_RUN_ID")
    if not root or not run:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run):
        raise ValueError("invalid audit run id")
    return pathlib.Path(root) / run


def _record(kind, payload):
    target = audit_dir()
    if target is None:
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        row = {"kind": kind, "ts": time.time(), **payload}
        with _LOCK, (target / "requests.jsonl").open("a", encoding="utf-8") as out:
            fcntl.flock(out.fileno(), fcntl.LOCK_EX)
            out.write(encoded(scrub(json.loads(encoded(row)))) + "\n")
            out.flush()
            os.fsync(out.fileno())
            fcntl.flock(out.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        # Business execution remains normal, but an evaluator must reject this
        # run as complete evidence. A missing file is also INCONCLUSIVE.
        try:
            (target / "audit_error.json").write_text(encoded({"error": type(exc).__name__}))
        finally:
            raise


def on_pre_api_request(request_messages=None, system_prompt=None, request=None, **kwargs):
    if audit_dir() is None:
        return
    body = (request or {}).get("body", {})
    tools = body.get("tools")
    inputs = {"messages": request_messages, "system": system_prompt, "tools": tools}
    complete = (isinstance(request_messages, list) and bool(request_messages)
                and not truncated(inputs)
                and (not kwargs.get("tool_count") or isinstance(tools, list)
                     and len(tools) == kwargs["tool_count"]))
    _record("request", {
        **{k: kwargs.get(k) for k in ("api_request_id", "task_id", "session_id", "platform",
                                      "model", "provider", "api_mode", "retry_count")},
        "input": inputs, "input_sha256": digest(inputs), "complete": bool(complete),
        "redacted": True,
    })


def on_post_api_request(**kwargs):
    message = kwargs.get("assistant_message")
    if hasattr(message, "model_dump"):
        message = message.model_dump(mode="json")
    elif message is not None and not isinstance(message, dict):
        message = {k: getattr(message, k, None) for k in ("role", "content", "tool_calls")}
    _record("response", {
        **{k: kwargs.get(k) for k in ("api_request_id", "task_id", "session_id", "turn_id",
                                     "usage", "response", "finish_reason")},
        "assistant_message": message,
        "complete": bool(message is not None and not truncated(message)),
    })


def on_api_request_error(**kwargs):
    _record("error", {k: str(kwargs[k]) if isinstance(kwargs.get(k), Exception) else kwargs.get(k)
                      for k in ("api_request_id", "task_id", "session_id", "error_type", "error", "error_message")})


def on_pre_tool_call(tool_name="", args=None, **kwargs):
    _record("tool_start", {"tool": tool_name, "args": args,
                           **{k: kwargs.get(k) for k in ("task_id", "session_id", "tool_call_id")}})


def on_post_tool_call(tool_name="", args=None, result=None, **kwargs):
    _record("tool_result", {"tool": tool_name, "args": args, "result": result,
                            **{k: kwargs.get(k) for k in ("task_id", "session_id", "tool_call_id", "status")}})
    from datasteward_gate import turn_finished
    turn_finished()
