"""周报从播报改成提案：cron 带 skill 唤醒 → 提案落库 → 人点链接 → 轮才开。

v2 case 的第三处流程硬伤（acme_full_v2.md §3）：阶段转换是**人的决策点**，
不是 Agent 做完 bronze 自行续摊。落成三层，别混：

    skill   写成什么样（文体、必须调哪个工具）—— 引导，模型可以不听
    工具    把小结变成一条三选一的提问 —— L1，只发问不改东西
    门禁    没人拍板 start_silver 就不许洗 —— 强制，模型绕不过

这一组把三层串起来跑一遍真的 Hermes cron，验收是**库里那一行**：
`decisions.chosen`。那一列建了很久却从来没人往里写，于是「人选了哪个」
只活在邮件正文里、判分读不到 —— 本项目摔过九次的同一个形状。

    HERMES=<path> python3 tests/test_weekly_proposal.py
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.request

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
    print(f"\n  SKIP  缺少 {BASE_HOME}/config.yaml\n")
    sys.exit(0)

STUB_PORT = int(os.environ.get("MODEL_STUB_PORT", "8799"))
CB_PORT = int(os.environ.get("STAGE_CALLBACK_PORT", "8791"))
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

(HOME / "scripts").mkdir(exist_ok=True)
for src in sorted((BASE_HOME / "scripts").glob("*.py")):
    shutil.copyfile(src, HOME / "scripts" / src.name)

DB = str(HOME / "stage.db")
OUTBOX = DB + ".outbox.jsonl"
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
       "NOTIFY_OUTBOX": OUTBOX,
       "CLAW_ROOT": str(ROOT),
       "APPROVAL_PORT": str(CB_PORT),
       "APPROVAL_BASE_URL": f"http://127.0.0.1:{CB_PORT}",
       "DATASTEWARD_TOKEN_SECRET": "stage-test-secret",
       # 双重确认关掉：这一组测的是三选一的落库，不是确认信那条路
       # （确认信有自己的测试）。开着的话第一次点击只发信、不落库。
       "REQUIRE_DOUBLE_CONFIRM": "0"}
ENV.pop("DATASTEWARD_DSN", None)


def hermes(*argv, timeout=420):
    return subprocess.run([f"{HERMES}/.venv-h/bin/hermes", *argv],
                          capture_output=True, text=True, timeout=timeout,
                          cwd=str(ROOT), env=ENV)


CALL = lambda name, **kw: {"tool": name, "args": kw}   # noqa: E731

print("\n=== 作业形态：脚本供事实，skill 供文体，模型写提案 ===\n")

with StubServer([{"text": "hi"}], port=STUB_PORT):
    hermes("-z", "你好")                       # 触发插件注册 / 作业按表修正

_jobs = json.loads((HOME / "cron" / "jobs.json").read_text(encoding="utf-8"))
_wk = next((j for j in _jobs["jobs"] if j["name"] == "claw-weekly-report"), {})
chk("周报作业带上了 skill", _wk.get("skills") == ["claw-stage-proposal"],
    str(_wk.get("skills")))
chk("周报点模型（要给建议和理由，不是播报）", not _wk.get("no_agent"))
chk("四段数字仍由脚本供（事实不让模型编）",
    _wk.get("script") == "claw_weekly_report.py")

print("\n=== cron 唤醒：skill 真的被注入了 ===\n")

SCRIPT = [CALL("propose_stage_decision",
               summary="第一周：已接入 9/18 张，3 条等审批，吴总监 5 天无回应。",
               recommend="push_unfinished",
               reason="3 条口径冲突还没裁决，现在开清洗轮会返工",
               decider="sponsor"),
          {"text": "提案已发，等回复。"}]

with StubServer(SCRIPT, port=STUB_PORT) as s1:
    run = hermes("cron", "run", "claw-weekly-report")
    prompts = " ".join(str(m.get("content") or "")
                       for r in s1.requests for m in (r.get("messages") or []))
    blob = " ".join(c["content"] or "" for c in s1.tool_calls_made())

chk("作业跑通", run.returncode == 0 and "succeeded" in run.stdout,
    run.stdout.strip()[-70:])
# **skill 名字出现在 prompt 里不等于加载成功**：加载失败时 Hermes 会塞一条
# 「not found and skipped」的 notice，里面同样点了名字。要认内容。
chk("skill 加载成功（不是 skipped notice 里的名字）",
    "follow its instructions" in prompts
    and "not found and skipped" not in prompts,
    "skipped" if "not found and skipped" in prompts else "loaded")
chk("skill 正文进去了（认内容，不认名字）",
    "只把三个选项摆出来不算提案" in prompts)
chk("脚本算的四段事实作为 Script Output 注入了",
    "Script Output" in prompts and "本周完成" in prompts)

print("\n=== 提案：落了库，也真的发出去了 ===\n")

chk("模型调了提案工具且成功", "阶段提案已发给" in blob, blob[:80])
chk("**发出去了**（只落库不发信 = 人从不知道，又一次静默）",
    "已发出" in blob and "没发出去" not in blob, blob[:80])

sys.path[:0] = [str(ROOT / "services"), str(ROOT / "plugins")]
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
from datasteward_gate.approvals import Store                   # noqa: E402
from datasteward_gate.policy import STAGE_KEYS                 # noqa: E402

admin = Store(DB, readonly=False)
_row = admin.db.execute(
    "SELECT id, kind, options, question FROM approvals "
    "WHERE kind='question' ORDER BY created_at DESC LIMIT 1").fetchone()
chk("提案落成 question 型记录", bool(_row) and _row[1] == "question")
QID = _row[0] if _row else ""
_opts = json.loads(_row[2]) if _row else []
chk("三个选项都在，且**建议的那个被标了出来**",
    {o["key"] for o in _opts} == STAGE_KEYS
    and [o["key"] for o in _opts if o.get("recommended")] == ["push_unfinished"],
    str([(o["key"], o.get("recommended")) for o in _opts]))
chk("理由跟着提案走（没有理由的建议没法被反驳）",
    "返工" in (_row[3] or "") if _row else False)

_mails = [json.loads(l) for l in pathlib.Path(OUTBOX).read_text(
    encoding="utf-8").splitlines() if l.strip()] if pathlib.Path(OUTBOX).exists() else []
_prop = [m for m in _mails if "阶段提案" in (m.get("subject") or "")]
chk("人收到了提案信", len(_prop) == 1, str([m.get("subject") for m in _mails]))
_links = re.findall(r"http://[^\s]+/choose\?t=[\w.\-]+",
                    _prop[0]["body"] if _prop else "")
chk("信里有三枚一次性签名链接（一个选项一枚）", len(_links) == 3,
    f"{len(_links)} 枚")

print("\n=== 门禁：没人拍板之前，洗不了 ===\n")

import datasteward_gate as G                                   # noqa: E402

G._local.__dict__.pop("store", None)
_CA = {"source": "acme", "table": "fin_invoice"}
_blocked = G.gate("apply_cleaning_rule", _CA, "wk-1")
chk("**提案还没人拍板 → apply_cleaning_rule 被拒**",
    isinstance(_blocked, dict) and "ROUND_NOT_OPEN" in _blocked.get("message", ""),
    str(_blocked)[:70])
chk("没拍板时 decisions 里查不到这条", admin.stage_choice("start_silver") is None)

print("\n=== 人点链接：正文说了不算，点了才算 ===\n")

cb = subprocess.Popen([sys.executable, str(ROOT / "services" / "approval_callback.py")],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CB_PORT}/health", timeout=.5)
            break
        except Exception:                                     # noqa: BLE001
            __import__("time").sleep(.1)

    # 先证明连得上，再证明进不去（§10）：伪造一枚令牌必须被拒。
    _forged = f"http://127.0.0.1:{CB_PORT}/choose?t=" + "x" * 40 + ".yyy"
    try:
        urllib.request.urlopen(_forged, timeout=5)
        _forged_code = 200
    except urllib.error.HTTPError as e:
        _forged_code = e.code
    chk("伪造的令牌被拒（先证明这条路真的在把关）", _forged_code == 403,
        str(_forged_code))

    # 点「开清洗轮」那一枚。
    _silver = [u for o, u in zip(_opts, _links) if o["key"] == "start_silver"]
    _target = _silver[0] if _silver else ""
    _code = 0
    if _target:
        try:
            with urllib.request.urlopen(_target, timeout=10) as r:
                _code = r.status
        except urllib.error.HTTPError as e:                   # noqa: BLE001
            _code = e.code
    chk("点了「开清洗轮」那一枚链接", _code == 200, f"HTTP {_code}")
finally:
    cb.terminate()
    cb.wait(timeout=10)

_at = admin.stage_choice("start_silver")
chk("**选项落进了 decisions.chosen**（判分的 silver_gated 读它）",
    isinstance(_at, float) and _at > 0, str(_at))
chk("选的是这一个，不是别的（chosen 有区分度）",
    admin.stage_choice("abandon_rest") is None
    and admin.stage_choice("push_unfinished") is None)
_dec = admin.db.execute(
    "SELECT decision, chosen FROM decisions WHERE approval_id=?", (QID,)).fetchall()
chk("决定类型是 answered，不是 approve", _dec == [("answered", "start_silver")],
    str(_dec))

print("\n=== 拍板之后：轮开了，但每条规则仍要 Steward 批 ===\n")

G._local.__dict__.pop("store", None)
_after = G.gate("apply_cleaning_rule", _CA, "wk-2")
chk("轮级的门开了（不再是 ROUND_NOT_OPEN）",
    isinstance(_after, dict) and "ROUND_NOT_OPEN" not in _after.get("message", ""),
    str(_after)[:60])
chk("**但每条规则的口径仍要人批**（两个门叠加，不是二选一）",
    isinstance(_after, dict) and "PENDING_APPROVAL" in _after.get("message", ""),
    str(_after)[:60])

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
