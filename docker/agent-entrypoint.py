#!/usr/bin/env python3
"""Run the real Hermes gateway and honour isolated demo reset requests.

The browser never talks to this process.  It only drops a reset request into a
shared control volume after its snapshot adapter has reset the data planes.
This process is the only component that restarts Hermes and clears its session
state, preventing a new demo run from inheriting model context from the last.
"""
from __future__ import annotations

import json
import hashlib
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

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
STOP_REQUESTED = False
RESTORED_RUNTIME = False
GATEWAY_PROCESS = None
GATEWAY_READY = False
FORCED_KILL = False


def prepared(run_id: str) -> dict | None:
    """Only an opaque run id and a wall-clock deadline cross this boundary."""
    try:
        value = json.loads((CONTROL_DIR / "prepared" / f"{run_id}.json").read_text())
        if (set(value) == {"run_id", "deadline"} and value["run_id"] == run_id
                and isinstance(value["deadline"], (float, int))):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def turn_stop_path() -> pathlib.Path:
    return STATUS_DIR / "turn_limit.json"


def clear_turn_signal() -> None:
    """抹掉上一轮的到线信号。

    幂等已经按 run id 判，旧文件不会误停新一轮；但留着它意味着每次轮询都要
    读一遍，而 run id 哪天撞上就变成「一起步就被停」。换轮次一起清掉。
    """
    turn_stop_path().unlink(missing_ok=True)


def turn_limit_signal() -> dict | None:
    """门禁写下的「本轮 turn 用满了」。

    **挡住工具不是停止运行。** 到线之后网关还会继续请求模型、再试下一个工具，
    一直烧到时间窗到期；那些调用产生不了任何动作，却照样计费。
    停机必须由外部做，并把停止原因记成 `turn_limit` 而不是 `window_closed` ——
    两者的含义完全不同：一个是「动作数够了」，一个是「时间到了」。
    """
    try:
        value = json.loads(turn_stop_path().read_text(encoding="utf-8"))
        return value if value.get("run_id") == CURRENT_RUN else None
    except (OSError, ValueError, TypeError):
        return None


def stop_gateway(process) -> None:
    global FORCED_KILL
    # Cron workers inherit this dedicated group; stop them as well.
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        FORCED_KILL = True
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def record_runtime(value: dict) -> None:
    from snapshot_runner.input_audit import scrub
    root = pathlib.Path(__file__).resolve().parent.parent
    target = pathlib.Path(os.environ["DATASTEWARD_INPUT_AUDIT_DIR"]) / CURRENT_RUN
    target.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for folder in (".hermes/plugins", ".hermes/skills", "plugins", "services", "ops"):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and path.suffix in (".py", ".md", ".yaml"):
                hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    revision = pathlib.Path("/opt/hermes/REVISION")
    (target / "runtime.json").write_text(json.dumps({
        "config": scrub(value), "code_sha256": hashes,
        "hermes_revision": revision.read_text().strip() if revision.exists() else "unknown",
        "driver": "hermes_gateway", "deadline": prepared(CURRENT_RUN)["deadline"],
        "environment": {key: os.environ.get(key) for key in (
            "MANUAL_MODE", "CLAW_MAX_TURN", "CLAW_TURN_SCOPE", "DAILY_TOKEN_LIMIT",
            "DAILY_COST_LIMIT_USD", "CLAW_RESUME_UNIT_SECONDS", "CLAW_NO_BOOTSTRAP_SOURCES",
            "NOTIFY_CHANNEL", "MAIL_TRANSPORT")},
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def archive_trajectory() -> None:
    """停进程并落盘后归档；必须早于下一轮 clear_runtime。"""
    if not CURRENT_RUN:
        return
    try:
        sys.path[:0] = ["/app", "/app/services"]
        from snapshot_runner import trajectory
        trajectory.archive(CURRENT_RUN)
    except Exception as exc:                                 # noqa: BLE001
        print(f"[entrypoint] trajectory archive failed: {type(exc).__name__}: {exc}",
              flush=True)


def record_completion(reason: str, returncode: int | None) -> None:
    target = pathlib.Path(os.environ["DATASTEWARD_INPUT_AUDIT_DIR"]) / CURRENT_RUN
    target.mkdir(parents=True, exist_ok=True)
    path = target / "completion.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"run_id": CURRENT_RUN, "reason": reason,
                               "stopped_at": time.time(), "returncode": returncode,
                               "gateway_ready": GATEWAY_READY, "forced_kill": FORCED_KILL}))
    temp.replace(path)


def archive_requests() -> None:
    """Archive Hermes' native full request dumps, without changing the client."""
    from snapshot_runner.input_audit import scrub
    target = pathlib.Path(os.environ["DATASTEWARD_INPUT_AUDIT_DIR"]) / CURRENT_RUN
    target.mkdir(parents=True, exist_ok=True)
    try:
        old = set(json.loads((target / "baseline.json").read_text())["request_dumps"])
    except (OSError, ValueError, KeyError):
        old = set()
    for source in HOME.rglob("request_dump_*.json"):
        if source.name in old:
            continue
        dest = target / source.name
        if dest.exists():
            continue
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
            if value.get("reason") != "preflight":
                continue  # An error dump of the same request is not a new call.
            temp = dest.with_suffix(".tmp")
            temp.write_text(json.dumps(scrub(value), ensure_ascii=False), encoding="utf-8")
            temp.replace(dest)
        except (OSError, ValueError) as exc:
            (target / "audit_error.json").write_text(json.dumps({"error": type(exc).__name__}))


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


def select_model(today: date | None = None,
                 skip: set[str] | None = None) -> tuple[str, date]:
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
    # `skip` 是**已经被真实调用证伪**的模型（额度耗尽、端点拒绝）。
    # 到期日是纸面规则，额度耗尽只有打过去才知道 —— 403 不可重试，
    # 撞上就是整轮死。所以证伪一个排除一个，继续往下试。
    safe = [model for model in candidates
            if model not in expired and model not in (skip or set())]
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
        f"no usable model on {now.isoformat()}; "
        f"past cutoff={','.join(expired) or '-'}; "
        f"probe-rejected={','.join(sorted(skip or set())) or '-'}"
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
    # Same tracked config as the normal project deployment. Per-case settings
    # never participate; only the explicitly selected provider is resolved.
    import yaml
    path = pathlib.Path(__file__).resolve().parent.parent / "infra/hermes/config.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["providers"] = {"custom": {
        "name": "custom", "base_url": os.environ["OPENAI_BASE_URL"].rstrip("/"),
        "key_env": "OPENAI_API_KEY", "default_model": os.environ["OPENAI_MODEL"],
    }}
    value["model"] = {"default": os.environ["OPENAI_MODEL"], "provider": "custom"}
    return value


def claw_preflight() -> list[str]:
    """Refuse to start unless the system was actually initialised.

    ``infra/claw.yaml`` names the agent, its mailbox and -- the part that
    matters -- who may approve.  Those role rows live in the governance
    database and are written once by ``ops/claw-init.py`` under an admin
    account; this container only holds ``agent_role``, so here we only read
    them back.

    **An empty role table must stop the gateway.**  Without this check the
    approval mail simply falls back to ``MAIL_OWNER`` from the environment
    (plugins/datasteward_gate/__init__.py, services/notify/__init__.py), so a
    deployment where nobody was ever appointed looks exactly like a deployment
    where somebody was.  That is the silent-fallback shape this project has
    already been bitten by three times.

    Derived values (``MAIL_FROM`` / ``EMAIL_ADDRESS``, and the approval dial
    translated into the existing ``MANUAL_MODE`` / ``REQUIRE_DOUBLE_CONFIRM``)
    are written into this process' environment, which the gateway inherits.
    Anything already set explicitly wins -- ops outranks the file.
    """
    # Import it as the top-level ``claw_init``, the same name the plugin and
    # the demo adapter use.  ``services.claw_init`` would be a *second* module
    # object with its own ``ClawInitError`` class, so an ``except`` on one side
    # would not catch the other's -- the repo already has that bug once, with
    # the two top-level ``plugins`` packages.
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    for path in (root, root + "/services", root + "/plugins"):
        if path not in sys.path:
            sys.path.insert(0, path)
    import claw_init
    from datasteward_gate.approvals import open_store

    settings = claw_init.load()
    applied = claw_init.derive_env(settings)
    store = open_store(readonly=True, init_schema=False)
    try:
        if RESTORED_RUNTIME or (CURRENT_RUN and os.environ.get("DEMO_CONTROL_DIR")):
            # Appointments are restored facts. The installation YAML must not
            # overwrite a subsequent role handover from the checkpoint.
            roles = []
            for role, _ in settings["roles"]:
                person = store.resolve_role(role)
                if not person:
                    raise RuntimeError(f"restored role has no current assignee: {role}")
                roles.append(f"{role}={person}")
        else:
            roles = claw_init.verify(store, settings)
    finally:
        store.close()
    return [f"claw.yaml: {settings['agent']['name']} @ "
            f"{settings['organization']['name']} · {settings['approval']['level']}",
            "roles: " + "、".join(roles),
            "derived: " + (", ".join(sorted(applied)) or "none (all set explicitly)")]


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
        # Reasoning-capable local models may need a short chain of thought
        # before emitting the required tool call.  Keep this probe cheap, but
        # do not reject a healthy endpoint merely because 32 tokens truncates
        # the call envelope.
        "max_tokens": 256,
    }
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                 "Content-Type": "application/json"},
    )
    # A local model may need to allocate a large KV cache on its first
    # request; the health probe must not fail solely during that cold start.
    window = prepared(CURRENT_RUN) if CURRENT_RUN else None
    remaining = max(1, window["deadline"] - time.time()) if window else 120
    with urllib.request.urlopen(request, timeout=min(120, remaining)) as response:
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
    """抹掉上一轮的运行时痕迹：会话目录，以及门禁写下的到线信号。"""
    if HOME.exists():
        for child in HOME.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    clear_turn_signal()


def accept_reset(request, run_id):
    """The operator already restored and verified HOME while the container stopped."""
    global RESTORED_RUNTIME
    marker = CONTROL_DIR / "restored" / f"{run_id}.json"
    RESTORED_RUNTIME = False
    if marker.exists():
        value = json.loads(marker.read_text())
        if set(value) != {"run_id", "snapshot_sha256"} or value["run_id"] != run_id:
            raise ValueError("invalid runtime restore marker")
        RESTORED_RUNTIME = True
        clear_turn_signal()
    else:
        clear_runtime()
    acknowledge(request, run_id)


def finish_run(reason, process=None):
    """Every exit path stops first, flushes evidence, and only then records completion."""
    if process is not None:
        stop_gateway(process)
    if not CURRENT_RUN or not os.environ.get("DEMO_CONTROL_DIR"):
        return
    try:
        archive_requests()
    finally:
        archive_trajectory()
        record_completion(reason, process.returncode if process else None)


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
    global GATEWAY_READY
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        if "email connected" in line.lower():
            GATEWAY_READY = True
            ready.set()


def _run_gateway() -> str:
    """Return ``reset`` when a requested isolated run has replaced this one."""
    global CURRENT_RUN, GATEWAY_PROCESS
    HOME.mkdir(parents=True, exist_ok=True)
    if not CURRENT_RUN:
        try:
            CURRENT_RUN = str(json.loads(ACTIVE_RUN.read_text(encoding="utf-8"))
                              .get("run_id") or "")
        except (OSError, json.JSONDecodeError):
            CURRENT_RUN = ""
    isolated = bool(os.environ.get("DEMO_CONTROL_DIR"))
    if isolated and time.time() >= prepared(CURRENT_RUN)["deadline"]:
        write_status("window_closed", "Fixed observation window ended")
        return "window_closed"
    if isolated:
        from snapshot_runner.trajectory import baseline
        baseline(CURRENT_RUN)
    if not RESTORED_RUNTIME:
        install_project_scripts()
    # Initialisation is checked before a model is even selected: a deployment
    # with nobody appointed should not burn a probe call to find that out.
    try:
        for line in claw_preflight():
            print(f"[claw-init] {line}")
    except Exception as exc:                                  # noqa: BLE001
        write_status("blocked", f"{type(exc).__name__}: {str(exc)[:300]}")
        print(f"gateway refused to start, system not initialised: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return "blocked"
    # 到期日挑一个 → 真打一次 → 不行就排除它再挑下一个。
    # 只挑不试的话，额度耗尽的模型照样通过纸面检查，然后在第一次真实
    # 对话时 403 且不可重试，整轮无声死掉（R6 演练实测撞到）。
    rejected: set[str] = set()
    while True:
        if STOP_REQUESTED:
            finish_run("external_stop")
            return "external_stop"
        if isolated and time.time() >= prepared(CURRENT_RUN)["deadline"]:
            finish_run("window_closed")
            return "window_closed"
        try:
            select_model(skip=rejected)
        except (RuntimeError, ValueError) as exc:
            write_status("blocked", str(exc), model_probe="fail")
            print(f"gateway model policy blocked: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return "blocked"
        runtime_config = config()
        if RESTORED_RUNTIME and CONFIG.exists():
            import yaml
            restored_config = yaml.safe_load(CONFIG.read_text())
            if not isinstance(restored_config, dict):
                raise ValueError("restored Hermes config must be a mapping")
            restored_config.update({key: runtime_config[key] for key in ("model", "providers")})
            runtime_config = restored_config
        CONFIG.write_text(json.dumps(runtime_config, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        write_status("checking", model_probe="unknown")
        try:
            preflight()
            break
        except (RuntimeError, urllib.error.URLError,
                urllib.error.HTTPError, ValueError) as exc:
            print(f"model {ACTIVE_MODEL} rejected at preflight: "
                  f"{type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)
            rejected.add(ACTIVE_MODEL)

    write_status("connecting", model_probe="pass")
    env = dict(os.environ)
    if isolated:
        record_runtime(runtime_config)
        env.update(DATASTEWARD_AUDIT_RUN_ID=CURRENT_RUN, HERMES_DUMP_REQUESTS="1")
        # 硬 turn 的**计数范围**就是这一轮的 run id —— 它已经在容器里了，
        # 不用再从控制包里多传一个字段（那条边界只许过 run_id + deadline）。
        # 上限值来自部署配置 `CLAW_MAX_TURN`，不来自 case 文件：
        # Agent 容器不挂载 `demo/`，门禁不该认识 Snapshot。没设就是不限，
        # 那时只剩固定时间窗这一道停止条件。
        env["CLAW_TURN_SCOPE"] = CURRENT_RUN
        # 门禁到线时往这里写一条，supervisor 据此真正停机（见 turn_limit_signal）。
        env["CLAW_TURN_STOP_FILE"] = str(turn_stop_path())
        if RESTORED_RUNTIME:
            env["HERMES_EMAIL_RESTORE_UNSEEN"] = "1"
            env["DATASTEWARD_RESTORE_CRON"] = "1"
    process = subprocess.Popen(["hermes", "gateway", "run", "-v"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, env=env, start_new_session=True)
    GATEWAY_PROCESS = process
    ready = threading.Event()
    threading.Thread(target=stream_gateway, args=(process, ready), daemon=True).start()
    while process.poll() is None:
        if STOP_REQUESTED:
            finish_run("external_stop", process)
            return "external_stop"
        if isolated:
            archive_requests()
        window = prepared(CURRENT_RUN)
        if isolated and (window is None or time.time() >= window["deadline"]):
            finish_run("window_closed", process)
            write_status("window_closed", "Fixed observation window ended", model_probe="pass")
            return "window_closed"
        spent = turn_limit_signal() if isolated else None
        if spent:
            finish_run("turn_limit", process)
            write_status("turn_limit",
                         f"Turn budget spent ({spent.get('used')}/{spent.get('limit')})",
                         model_probe="pass")
            return "turn_limit"
        if (ACTIVE_MODEL_EXPIRATION
                and datetime.now(timezone.utc).date()
                >= date.fromisoformat(ACTIVE_MODEL_EXPIRATION) - timedelta(days=1)):
            detail = (f"model cutoff reached: {ACTIVE_MODEL} "
                      f"(expiration={ACTIVE_MODEL_EXPIRATION})")
            write_status("blocked", detail, model_probe="fail")
            finish_run("model_cutoff", process)
            return "blocked"
        request, payload = reset_request()
        if request:
            write_status("resetting", model_probe="pass")
            finish_run("superseded", process)
            CURRENT_RUN = str((payload or {}).get("run_id") or "")
            accept_reset(request, CURRENT_RUN or "unknown")
            return "reset"
        if ready.is_set():
            write_status("ready", "GreenMail connected; data-steward plugin active",
                         model_probe="pass")
            ready.clear()
        time.sleep(0.5)
    if isolated:
        finish_run("gateway_exited", process)
    write_status("blocked", f"gateway exited {process.returncode}", model_probe="pass")
    return "exited"


def run_gateway() -> str:
    global GATEWAY_PROCESS, GATEWAY_READY, FORCED_KILL
    GATEWAY_PROCESS = None
    GATEWAY_READY = FORCED_KILL = False
    try:
        outcome = _run_gateway()
        # Includes preflight/config failures and already-expired windows.
        if outcome != "reset" and CURRENT_RUN:
            path = pathlib.Path(os.environ.get("DATASTEWARD_INPUT_AUDIT_DIR", "/audit")) / CURRENT_RUN / "completion.json"
            if not path.exists():
                finish_run(outcome)
        return outcome
    except Exception as exc:
        finish_run("startup_or_runtime_error", GATEWAY_PROCESS)
        write_status("blocked", f"{type(exc).__name__}: {str(exc)[:160]}")
        return "blocked"


def main() -> int:
    global CURRENT_RUN
    def stopping(signum, frame):
        global STOP_REQUESTED
        STOP_REQUESTED = True
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    if not os.environ.get("DEMO_CONTROL_DIR"):
        run_gateway()
        return 2
    CONTROL_DIR.joinpath("requests").mkdir(parents=True, exist_ok=True)
    write_status("idle", "Waiting for an isolated run")
    while not STOP_REQUESTED:
        # Start no model/cron while the previous state is being cleared and
        # restored. A container restart also requires an explicit new run.
        if not CURRENT_RUN:
            request, payload = reset_request()
            if not request:
                time.sleep(0.5)
                continue
            CURRENT_RUN = str(payload["run_id"])
            accept_reset(request, CURRENT_RUN)
            write_status("restoring", "Waiting for state restore")
        if prepared(CURRENT_RUN) is None:
            request, payload = reset_request()
            if request:
                CURRENT_RUN = str(payload["run_id"])
                accept_reset(request, CURRENT_RUN)
            time.sleep(0.5)
            continue
        outcome = run_gateway()
        if outcome != "reset":
            CURRENT_RUN = ""
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
