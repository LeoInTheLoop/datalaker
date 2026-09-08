#!/usr/bin/env python3
"""Run the real Hermes gateway and honour isolated demo reset requests.

The browser never talks to this process.  It only drops a reset request into a
shared control volume after its snapshot adapter has reset the data planes.
This process is the only component that restarts Hermes and clears its session
state, preventing a new demo run from inheriting model context from the last.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone


HOME = pathlib.Path(os.environ.get("HERMES_HOME", "/state/hermes"))
STATUS_DIR = pathlib.Path(os.environ.get("DEMO_AGENT_STATUS_DIR", "/status"))
CONTROL_DIR = pathlib.Path(os.environ.get("DEMO_CONTROL_DIR", "/control"))
CONFIG = HOME / "config.yaml"
CURRENT_RUN = ""
PROJECT_SCRIPTS = pathlib.Path("/app/.hermes/home/scripts")
ACTIVE_RUN = CONTROL_DIR / "active_run.json"
ACTIVE_MODEL = ""
ACTIVE_MODEL_EXPIRATION = ""
MODEL_SELECTION_DETAIL = ""


def parse_model_expirations(raw: str) -> dict[str, date]:
    """Parse the operator-owned model expiry allowlist.

    The gateway must not guess an expiry date from a model name.  Every model
    in the primary/fallback chain therefore needs an explicit ISO date.
    """
    out: dict[str, date] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"invalid model expiration entry: {item}")
        model, expiry = (part.strip() for part in item.split("=", 1))
        if not model or not expiry:
            raise ValueError(f"invalid model expiration entry: {item}")
        try:
            out[model] = date.fromisoformat(expiry)
        except ValueError as exc:
            raise ValueError(f"invalid model expiration date: {item}") from exc
    return out


def select_model(today: date | None = None) -> tuple[str, date]:
    """Select the first non-expired model, or fail closed.

    Conservative cutoff: stop using a model one calendar day before its
    configured expiration date.  Among models still inside their safe window,
    choose the earliest expiration first so free quota is consumed before it
    disappears.  Missing metadata is also a hard block so a newly added
    fallback cannot silently bypass the expiry policy.
    """
    global ACTIVE_MODEL, ACTIVE_MODEL_EXPIRATION, MODEL_SELECTION_DETAIL
    primary = os.environ.get("OPENAI_MODEL", "").strip()
    fallbacks = [m.strip() for m in os.environ.get(
        "OPENAI_MODEL_FALLBACKS", "").split(",") if m.strip()]
    candidates = list(dict.fromkeys([primary, *fallbacks]))
    if not candidates or not candidates[0]:
        raise RuntimeError("OPENAI_MODEL is empty")
    expirations = parse_model_expirations(
        os.environ.get("DASHSCOPE_MODEL_EXPIRATIONS", ""))
    missing = [model for model in candidates if model not in expirations]
    if missing:
        raise RuntimeError("model expiration metadata missing: " + ",".join(missing))
    now = today or datetime.now(timezone.utc).date()
    expired = [model for model in candidates
               if now >= expirations[model] - timedelta(days=1)]
    safe = [model for model in candidates if model not in expired]
    if safe:
        order = {model: index for index, model in enumerate(candidates)}
        model = min(safe, key=lambda item: (expirations[item], order[item]))
        ACTIVE_MODEL = model
        ACTIVE_MODEL_EXPIRATION = expirations[model].isoformat()
        os.environ["OPENAI_MODEL"] = model
        reason = "earliest_expiration"
        if model != primary:
            reason += f"; primary={primary}"
        MODEL_SELECTION_DETAIL = (
            f"model={model}; expires={ACTIVE_MODEL_EXPIRATION}; {reason}"
        )
        return model, expirations[model]
    raise RuntimeError(
        f"all configured models reached conservative cutoff on or before {now.isoformat()}: "
        + ",".join(expired)
    )


def write_status(state: str, detail: str = "", model_probe: str = "unknown") -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    (STATUS_DIR / "status.json").write_text(json.dumps({
        "state": state, "detail": detail[:160], "run_id": CURRENT_RUN,
        "model_probe": model_probe,
        "model": ACTIVE_MODEL or os.environ.get("OPENAI_MODEL", ""),
        "model_expires_on": ACTIVE_MODEL_EXPIRATION,
    }, ensure_ascii=False), encoding="utf-8")


def config() -> dict:
    return {
        "agent": {
            "enabled_toolsets": ["clarify", "memory", "todo"],
            "disabled_toolsets": [
                "terminal", "code_execution", "computer_use", "browser", "file",
                "delegation", "web", "search", "x_search", "vision", "video",
                "image_gen", "video_gen", "tts", "spotify", "homeassistant",
                "discord", "discord_admin", "yuanbao", "feishu_doc", "feishu_drive",
                "kanban", "project", "desktop_ui", "cronjob", "session_search",
                "context_engine", "coding", "debugging",
            ],
        },
        "plugins": {"enabled": ["data-steward"]},
        "tools": {"tool_search": {"enabled": False}},
        "skills": {"external_dirs": ["${CLAW_ROOT}/.hermes/skills"]},
        # Hermes deliberately refuses to leak OPENAI_API_KEY to an arbitrary
        # custom host.  Register this explicit provider with key_env instead:
        # the key stays only in process environment and the host is intentional.
        "providers": {"custom": {
            "name": "custom",
            "base_url": os.environ["OPENAI_BASE_URL"].rstrip("/"),
            "key_env": "OPENAI_API_KEY",
            "default_model": os.environ["OPENAI_MODEL"],
        }},
        "model": {"default": os.environ["OPENAI_MODEL"], "provider": "custom"},
    }


def preflight() -> None:
    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError("missing " + ", ".join(missing))
    payload = {
        "model": os.environ["OPENAI_MODEL"],
        "messages": [{"role": "user", "content": "Call the ping tool once."}],
        "tools": [{"type": "function", "function": {
            "name": "ping", "description": "Return a health acknowledgement.",
            "parameters": {"type": "object", "properties": {}},
        }}],
        "max_tokens": 32,
    }
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.loads(response.read())
    calls = (data.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
    if not calls:
        raise RuntimeError("endpoint response did not contain a tool call")


def reset_request() -> tuple[pathlib.Path, dict] | tuple[None, None]:
    requests = CONTROL_DIR / "requests"
    for request in sorted(requests.glob("*.json")):
        try:
            return request, json.loads(request.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            request.unlink(missing_ok=True)
    return None, None


def clear_runtime() -> None:
    if HOME.exists():
        for child in HOME.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()


def install_project_scripts() -> None:
    """Install the existing project cron monitor scripts into Hermes HOME.

    HERMES_HOME is a fresh named volume per demo stack and is cleared between
    runs.  Copying the checked-in scripts at each gateway start keeps Hermes'
    cron jobs usable without making the browser or the entrypoint implement
    resume logic.
    """
    target = HOME / "scripts"
    target.mkdir(parents=True, exist_ok=True)
    for script in PROJECT_SCRIPTS.glob("*.py"):
        destination = target / script.name
        shutil.copy2(script, destination)
        destination.chmod(0o755)


def acknowledge(request: pathlib.Path, run_id: str) -> None:
    done = CONTROL_DIR / "acknowledged"
    done.mkdir(parents=True, exist_ok=True)
    active_tmp = ACTIVE_RUN.with_suffix(".tmp")
    active_tmp.write_text(json.dumps({"run_id": run_id, "acknowledged_at": time.time()}),
                          encoding="utf-8")
    active_tmp.replace(ACTIVE_RUN)
    request.replace(done / f"{run_id}.json")


def stream_gateway(process: subprocess.Popen[str], ready: threading.Event) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        if "email connected" in line.lower():
            ready.set()


def run_gateway() -> str:
    """Return ``reset`` when a requested isolated run has replaced this one."""
    global CURRENT_RUN
    HOME.mkdir(parents=True, exist_ok=True)
    if not CURRENT_RUN:
        try:
            CURRENT_RUN = str(json.loads(ACTIVE_RUN.read_text(encoding="utf-8"))
                              .get("run_id") or "")
        except (OSError, json.JSONDecodeError):
            CURRENT_RUN = ""
    install_project_scripts()
    try:
        select_model()
    except (RuntimeError, ValueError) as exc:
        write_status("blocked", str(exc), model_probe="fail")
        print(f"gateway model policy blocked: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return "blocked"
    CONFIG.write_text(json.dumps(config(), ensure_ascii=False, indent=2), encoding="utf-8")
    write_status("checking", model_probe="unknown")
    try:
        preflight()
    except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        write_status("blocked", type(exc).__name__, model_probe="fail")
        print(f"gateway preflight blocked: {type(exc).__name__}", file=sys.stderr)
        return "blocked"

    write_status("connecting", model_probe="pass")
    process = subprocess.Popen(["hermes", "gateway", "run", "-v"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    ready = threading.Event()
    threading.Thread(target=stream_gateway, args=(process, ready), daemon=True).start()
    while process.poll() is None:
        if (ACTIVE_MODEL_EXPIRATION
                and datetime.now(timezone.utc).date()
                >= date.fromisoformat(ACTIVE_MODEL_EXPIRATION) - timedelta(days=1)):
            detail = (f"model cutoff reached: {ACTIVE_MODEL} "
                      f"(expiration={ACTIVE_MODEL_EXPIRATION})")
            write_status("blocked", detail, model_probe="fail")
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            return "blocked"
        request, payload = reset_request()
        if request:
            CURRENT_RUN = str((payload or {}).get("run_id") or "")
            write_status("resetting", model_probe="pass")
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            clear_runtime()
            acknowledge(request, CURRENT_RUN or "unknown")
            return "reset"
        if ready.is_set():
            write_status("ready", "GreenMail connected; data-steward plugin active",
                         model_probe="pass")
            ready.clear()
        time.sleep(0.5)
    write_status("blocked", f"gateway exited {process.returncode}", model_probe="pass")
    return "exited"


def main() -> int:
    CONTROL_DIR.joinpath("requests").mkdir(parents=True, exist_ok=True)
    while True:
        outcome = run_gateway()
        if outcome != "reset":
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
