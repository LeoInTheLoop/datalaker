"""M5 分水岭：恢复整链在**真 Hermes 的 cron** 里跑一次。

零件此前各测各的：`ops/resumable.py` 单测过、`ensure_jobs` 在假 cron 面上
测过、跨会话指纹收尾在门禁单测里测过。**但整条链一次都没接起来过** ——
而这个项目摔过的六次都是「闸门读的量根本没人写」，零件全绿、链子断掉。

这一组把它接起来，主语是 Hermes：

    会话 A：调 ingest_table（L3）→ 门禁挂起、登记表记线
    tick   ：没人批 → monitor 输出没变 → **一次模型都不点**
    批准   ：独立连接写 decisions（铁律 2）
    tick   ：输出变了 → 唤醒 → **Agent 自己再调一次工具** → 票据消费 → 真写 bronze
    收尾   ：新会话的 task_id 对不上，按动作指纹收掉**旧会话**那条线

验收是 bronze 里查得到 + 旧线变 done，不是状态栏好看。

    HERMES=<path> python3 tests/test_resume_cron.py
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.model_stub import StubServer                       # noqa: E402

HERMES = os.environ.get("HERMES", "")
ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


if not HERMES or not pathlib.Path(HERMES).is_dir():
    print("\n  SKIP  未设置 HERMES（见 docs/handoff/R1.md）\n")
    sys.exit(0)

BASE_HOME = ROOT / ".hermes" / "home"
if not (BASE_HOME / "config.yaml").exists():
    print(f"\n  SKIP  缺少 {BASE_HOME}/config.yaml（见 docs/restructure.md M1）\n")
    sys.exit(0)


def _lake_ok():
    """探活走和干活同一条路（sync._trino）—— 与 5.5 组同款。

    不探的话，docker 不可用时「真写 bronze」会 FAIL 而不是 SKIP，
    docker-free 门槛就永远达不到全绿。
    """
    try:
        sys.path[:0] = [str(ROOT / "services"), str(ROOT / "plugins")]
        import sync
        sync._trino("SELECT 1")
        return True
    except Exception:                                         # noqa: BLE001
        return False


LAKE_OK = _lake_ok()

# 桩用固定端口：Hermes 的配置压过环境变量，只能在配置里写死 base_url。
STUB_PORT = int(os.environ.get("MODEL_STUB_PORT", "8799"))
HOME = ROOT / ".hermes" / "test-home"
HOME.mkdir(parents=True, exist_ok=True)
_cfg = (BASE_HOME / "config.yaml").read_text(encoding="utf-8")
_cfg = re.sub(r'^(\s*base_url:).*$', r'\1 "http://127.0.0.1:%d/v1"' % STUB_PORT,
              _cfg, flags=re.M)
_cfg = re.sub(r'^(\s*api_key:).*$', r'\1 "stub"', _cfg, flags=re.M)
_cfg = re.sub(r'^(\s*default:)\s*".*"$', r'\1 "stub-model"', _cfg, flags=re.M)
if "streaming:" not in _cfg:
    _cfg = re.sub(r'^(model:\s*)$', r'\1\n  streaming: false', _cfg, flags=re.M)
(HOME / "config.yaml").write_text(_cfg, encoding="utf-8")

# 壳脚本必须是 HERMES_HOME/scripts/ 下的**真文件** ——
# Hermes `resolve()` 之后再校验路径，软链会被穿透后拒掉。
(HOME / "scripts").mkdir(exist_ok=True)
for src in sorted((BASE_HOME / "scripts").glob("*.py")):
    shutil.copyfile(src, HOME / "scripts" / src.name)

DB = str(HOME / "m5.db")
for suf in ("", "-wal", "-shm", ".outbox.jsonl"):
    f = pathlib.Path(DB + suf)
    if f.exists():
        f.unlink()

ENV = {**os.environ,
       "HERMES_HOME": str(HOME),
       "HERMES_ENABLE_PROJECT_PLUGINS": "1",
       "OPENAI_API_KEY": "stub",
       "DATASTEWARD_DB": DB,
       "NOTIFY_CHANNEL": "outbox",
       "NOTIFY_OUTBOX": DB + ".outbox.jsonl",
       "CLAW_ROOT": str(ROOT)}
ENV.pop("DATASTEWARD_DSN", None)

JOBS_JSON = HOME / "cron" / "jobs.json"


def hermes(*argv, timeout=420):
    """用 Hermes 自己的 venv 跑一条命令。依赖不共享。"""
    r = subprocess.run([f"{HERMES}/.venv-h/bin/hermes", *argv],
                       capture_output=True, text=True, timeout=timeout,
                       cwd=str(ROOT), env=ENV)
    return r


def jobs():
    if not JOBS_JSON.exists():
        return {}
    d = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    return {j.get("name"): j for j in (d.get("jobs") if isinstance(d, dict) else d)}


CALL = lambda name, **kw: {"tool": name, "args": kw}   # noqa: E731
SCRIPT = [CALL("ingest_table", source="northwind", table="shippers"),
          {"text": "好。"}]

print("\n=== 作业登记：名字相同不代表定义相同 ===\n")

# 把存量作业**故意改回老形态**（no_agent 脚本），验证 `ensure_jobs` 在真
# cron 面上按表修正 —— 而不是按名字判重跳过。假 cron 面上测过（5.6 组），
# 这里测真的：test-home 里躺过的那条老定义就是活证据。
have = jobs()
if have.get("claw-resume"):
    d = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    for j in (d["jobs"] if isinstance(d, dict) else d):
        if j.get("name") == "claw-resume":
            j["monitor_script"] = None
            j["script"] = "claw_resume.py"
            j["no_agent"] = True
    JOBS_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                         encoding="utf-8")

with StubServer([{"text": "hi"}], port=STUB_PORT):
    reg = hermes("-z", "你好")

j = jobs().get("claw-resume") or {}
chk("三个作业都登记进了真 cron", set(jobs()) >= {
    "claw-resume", "claw-escalate", "claw-weekly-report"}, str(sorted(jobs())))
chk("**存量作业按定义表修正**（不是按名字跳过）",
    j.get("monitor_script") == "claw_resumable.py"
    and not j.get("script") and not j.get("no_agent"),
    f'script={j.get("script")} monitor={j.get("monitor_script")}')
if have.get("claw-resume"):
    # **不能只喊给流**：Hermes 在注册阶段把 stdout / stderr 都吞掉了
    # （实测两条流里都搜不到）。所以查那份留得下来的 —— 事件日志。
    import sqlite3
    _ev = sqlite3.connect(DB).execute(
        "SELECT kind, payload FROM events WHERE run_id='cron'").fetchall()
    chk("修正被**喊出来**了（静默修正等于没修）",
        any(k == "CRON_JOB_CORRECTED" and "按表修正" in p for k, p in _ev),
        str(_ev[:2]) or "events 里没有 cron 记录")
else:
    print("  SKIP  「喊出来」（本次是首次登记，走的是 created 分支）")

_shells = sorted(p.name for p in (HOME / "scripts").glob("*.py"))
chk("壳脚本是 scripts/ 下的真文件（软链会被 Hermes 拒掉）",
    {"claw_resumable.py", "claw_escalate.py"} <= set(_shells)
    and not any((HOME / "scripts" / n).is_symlink() for n in _shells),
    str(_shells))

print("\n=== 会话 A：L3 被挂起，登记表记下这条线 ===\n")

# 先打一次 baseline tick：monitor 第一次跑没有存量哈希，一律算「变了」。
# 不先打的话，下面那条「没人批就不点模型」测的是 baseline，不是抑制。
with StubServer([{"text": "无。"}], port=STUB_PORT):
    base = hermes("cron", "run", "claw-resume")
chk("baseline tick 跑通（monitor 源没崩）",
    base.returncode == 0 and "succeeded" in base.stdout, base.stdout.strip()[-80:])

with StubServer(SCRIPT, port=STUB_PORT) as s1:
    hermes("-z", "把 northwind 的 shippers 接进数据湖")
    blob1 = " ".join(c["content"] or "" for c in s1.tool_calls_made())

chk("L3 动作被门禁拦下", "PENDING_APPROVAL" in blob1, blob1[:90])

sys.path[:0] = [str(ROOT / "services"), str(ROOT / "plugins")]
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
import runs                                                   # noqa: E402

waiting = runs.by_status("waiting_human")
chk("登记表记下了这条线（门禁一拦，工具 handler 根本不跑）",
    len(waiting) == 1 and waiting[0]["kind"] == "ingest_table",
    str([(r["run_id"][:8], r["kind"]) for r in waiting]))
RUN_A = waiting[0]["run_id"] if waiting else ""

print("\n=== 没人批：monitor 输出没变 → 一次模型都不点 ===\n")

with StubServer([{"text": "不该被叫到。"}], port=STUB_PORT) as s2:
    tick = hermes("cron", "run", "claw-resume")
    quiet = len(s2.requests)
chk("tick 本身成功", tick.returncode == 0, tick.stdout.strip()[-80:])
chk("**一次模型都没点**（一分钟一次的成本是一次 SQL）",
    quiet == 0, f"stub 收到 {quiet} 个请求")

print("\n=== 独立连接批准（铁律 2）===\n")

from datasteward_gate.approvals import Store                   # noqa: E402

admin = Store(DB, readonly=False)
row = admin.db.execute("SELECT id, run_id, action_hash FROM approvals "
                       "ORDER BY created_at DESC LIMIT 1").fetchone()
chk("拿到了待决审批", bool(row), str(row and row[0][:8]))
if row:
    admin.decide(row[0], "approve", "wang@acme.com")

mon = subprocess.run([sys.executable, str(ROOT / "ops" / "resumable.py")],
                     capture_output=True, text=True, env=ENV, cwd=str(ROOT))
chk("monitor 源看得见这条线了（批准之后输出非空）",
    RUN_A and RUN_A in mon.stdout, repr(mon.stdout[:80]))

print("\n=== tick：唤醒 → Agent 自己再调一次工具 → 真写 bronze ===\n")

with StubServer(SCRIPT, port=STUB_PORT) as s3:
    wake = hermes("cron", "run", "claw-resume")
    woke = len(s3.requests)
    blob3 = " ".join(c["content"] or "" for c in s3.tool_calls_made())
    prompts = " ".join(str(m.get("content") or "")
                       for r in s3.requests for m in (r.get("messages") or []))

chk("输出变了 → 这次点模型了", woke > 0, f"stub 收到 {woke} 个请求")
chk("唤醒时把「哪条线可以走了」交给了模型",
    RUN_A and RUN_A in prompts, "MONITOR CHANGE" if "MONITOR" in prompts else "?")
chk("恢复不依赖我的脚本推动 —— 是 Hermes 自己再调一次工具",
    any("ingest_table" in (c["name"] or "") for c in s3.tool_calls_made()),
    str([c["name"] for c in s3.tool_calls_made()]))
chk("票据被消费（这次没有重新挂起）", "PENDING_APPROVAL" not in blob3,
    blob3[:90])
if LAKE_OK:
    chk("**真的写进了 bronze**（验收是查得到，不是状态栏好看）",
        "已落" in blob3 and "行，策略" in blob3, blob3[:110])
else:
    print("  SKIP  真写 bronze（lake 连不上 —— 探活与干活同一条路）")

print("\n=== 跨会话收尾：线的身份是动作指纹，不是会话 ===\n")

done = runs.by_status("done")
chk("**旧会话那条线被收掉了**（新会话的 task_id 对不上，按指纹找回）",
    any(r["run_id"] == RUN_A for r in done),
    str([(r["run_id"][:8], r["status"]) for r in
         runs.by_status("waiting_human") + done]))
chk("没有开出第二条线（同一动作 = 同一条线）",
    sum(len(runs.by_status(s)) for s in
        ("running", "waiting_human", "done", "abandoned", "failed")) == 1,
    str({s: len(runs.by_status(s)) for s in
         ("running", "waiting_human", "done", "abandoned", "failed")}))

with StubServer([{"text": "不该被叫到。"}], port=STUB_PORT) as s4:
    hermes("cron", "run", "claw-resume")   # 收尾后输出变空 —— 这是一次变化
    hermes("cron", "run", "claw-resume")   # 再一次：空对空，应当被抑制
    settle = len(s4.requests)
    after = len([r for r in s4.requests if r.get("tools")])
chk("收尾后重新静默（不会每分钟为已走完的线白唤醒）", after <= 1,
    f"两次 tick 里主对话请求 {after} 个（共 {settle}）")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
