"""M1 断言：工具真的跑在 Hermes 里，且门禁在那条路上生效。

在这之前，「Agent 会不会调工具」是我的脚本自问自答 —— 脚本直接 import
services 然后按剧本调用，Hermes 只被验证过「门禁能挂上」。
这一组第一次把**被测对象换成 Hermes**：工具由它下发给模型、由它分发、
由它把结果回传。

模型换成本地桩（`tests/model_stub.py`），因为回归需要确定性；
真模型那条路另测（见文件末尾说明）。

    HERMES=<path> ./.venv/bin/python tests/test_hermes_tool_loop.py
"""
import json
import os
import subprocess
import sys
import pathlib

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

# lake 探活走和干活同一条路（sync._trino，与 run_all 9.5 同款）。
# 不探的话，docker 不可用时「真写 bronze」两条会 FAIL 而不是 SKIP ——
# docker-free 门槛就永远达不到全绿。只探 lake：其余断言不依赖它。
def _lake_ok():
    try:
        sys.path[:0] = [str(ROOT / "services"), str(ROOT / "plugins")]
        import sync
        sync._trino("SELECT 1")
        return True
    except Exception:                                         # noqa: BLE001
        return False


LAKE_OK = _lake_ok()

# 桩用固定端口：**Hermes 的配置压过环境变量**（跟 NOTIFY_CHANNEL 那个坑同款），
# 所以只能在配置里写死 base_url，而配置写死就要求端口固定。
STUB_PORT = int(os.environ.get("MODEL_STUB_PORT", "8799"))
HOME = ROOT / ".hermes" / "test-home"
HOME.mkdir(parents=True, exist_ok=True)
import re as _re
_cfg = (BASE_HOME / "config.yaml").read_text(encoding="utf-8")
_cfg = _re.sub(r'^(\s*base_url:).*$', r'\1 "http://127.0.0.1:%d/v1"' % STUB_PORT,
               _cfg, flags=_re.M)
_cfg = _re.sub(r'^(\s*api_key:).*$', r'\1 "stub"', _cfg, flags=_re.M)
_cfg = _re.sub(r'^(\s*default:)\s*".*"$', r'\1 "stub-model"', _cfg, flags=_re.M)
# **关掉流式**：Hermes 默认走 SSE，而桩返回的是普通 JSON，
# 它会报「empty stream with no finish_reason」。配置样例里
# streaming 本来就留了这个逃生口给自建端点。
if "streaming:" not in _cfg:
    _cfg = _re.sub(r'^(model:\s*)$', r'\1\n  streaming: false', _cfg, flags=_re.M)
(HOME / "config.yaml").write_text(_cfg, encoding="utf-8")


def run_hermes(prompt, stub, timeout=180):
    """把 Hermes 指到桩上跑一轮。**用它自己的 venv**，依赖不共享。"""
    env = {**os.environ,
           "HERMES_HOME": str(HOME),
           "HERMES_ENABLE_PROJECT_PLUGINS": "1",
           "OPENAI_API_KEY": "stub"}
    r = subprocess.run([f"{HERMES}/.venv-h/bin/hermes", "-z", prompt],
                       capture_output=True, text=True, timeout=timeout,
                       cwd=str(ROOT), env=env)
    return r.stdout.strip(), r.stderr.strip()


print("\n=== 工具在 Hermes 里被调用 ===\n")

# 我们关掉了延迟下发（config: tools.tool_search.enabled=off）：工具集本来就
# 被白名单砍到很小，全部塞进 prompt 也就几千 token，而藏起来的代价是
# 弱模型找不到它们。所以剧本直接点名工具，不再经由 tool_call 外壳。
CALL = lambda name, **kw: {"tool": name, "args": kw}   # noqa: E731

script = [CALL("list_source_tables", source="northwind"),
          {"text": "已列出。"}]
with StubServer(script, port=STUB_PORT) as stub:
    out, err = run_hermes("northwind 里有哪些表？", stub)
    offered = stub.tools_offered()
    called = stub.tool_calls_made()

# **工具直接下发，不走 tool_call 那个延迟外壳**（config: tools.tool_search.off）。
# 藏起来省的那点 token，代价是弱模型根本找不到工具 —— 实测 qwen3.8-flash
# 在延迟下发时 tool_turns=0，只回一句「我来处理」。
chk("claw 的工具按名字直接下发（不藏在 tool_search 后面）",
    "ingest_table" in offered and "connect_source" in offered,
    f"下发 {len(offered)} 个工具")

# **发现这一段也必须放行。** 真模型不会直接 tool_call —— 它先 tool_search
# 找、tool_describe 看签名。这两个不声明就会被「未声明 = L4」挡住，
# 于是模型找不到任何工具，看起来像它什么都不会干。
# 桩模式发现不了：剧本直接给 tool_call，跳过了发现这一段。
import sys as _sys0; _sys0.path.insert(0, str(ROOT / "plugins"))
from datasteward_gate.policy import POLICY as _P0, Level as _L0   # noqa: E402
chk("**工具发现的两步是 L0**（挡住它们 = 模型找不到工具）",
    _P0.get("tool_search") == (_L0.L0, None)
    and _P0.get("tool_describe") == (_L0.L0, None),
    f'{_P0.get("tool_search")} / {_P0.get("tool_describe")}')
chk("**白名单生效**：terminal 没有被下发", "terminal" not in offered)
chk("**白名单生效**：execute_code 没有被下发", "execute_code" not in offered)
chk("claw 的工具被真的执行了（有结果回传）",
    any("list_source_tables" in (c["name"] or "") or "northwind"
        in (c["content"] or "") for c in called),
    str([c["name"] for c in called]))

res = " ".join(c["content"] or "" for c in called)
chk("结果来自真实数据库而不是编的", "orders" in res and "行" in res, res[:60])

print("\n=== 门禁在这条路上生效 ===\n")

# ingest_table 是 L3，门禁应当挂起而不是执行。
# 注意 oneshot 会开 HERMES_YOLO_MODE 绕过 Hermes 自带的审批 ——
# **我们的门禁是 pre_tool_call 钩子，不受它影响**，这正是要验的。
script2 = [CALL("list_source_tables", source="northwind"), {"text": "好。"}]
with StubServer(script2, port=STUB_PORT) as stub2:
    out2, _ = run_hermes("看看 northwind 有什么表", stub2)
    called2 = stub2.tool_calls_made()

chk("L0 工具被放行（门禁没有误拦）",
    any("northwind" in (c["content"] or "") for c in called2),
    str([c["name"] for c in called2]))
chk("放行的结果里没有门禁消息",
    all("PENDING_APPROVAL" not in c["content"] for c in called2))

print("\n=== M2：L3 动作在 Hermes 里被挂起 ===\n")

# oneshot 会开 HERMES_YOLO_MODE 绕过 Hermes 自带的审批。
# **我们的门禁是 pre_tool_call 钩子，不受它影响** —— 这正是铁律 1 的意思：
# 限制在钩子里，不在提示词里，也不在 Hermes 的审批开关里。
DB = str(ROOT / ".hermes" / "test-home" / "m2.db")
for suf in ("", "-wal", "-shm"):
    f = pathlib.Path(DB + suf)
    if f.exists():
        f.unlink()

script3 = [CALL("ingest_table", source="northwind", table="shippers"),
           {"text": "已发起审批。"}]
with StubServer(script3, port=STUB_PORT) as s4:
    env_extra = {"DATASTEWARD_DB": DB, "NOTIFY_CHANNEL": "outbox",
                 "NOTIFY_OUTBOX": DB + ".outbox.jsonl"}
    saved = dict(os.environ)
    os.environ.update(env_extra)
    try:
        out4, _ = run_hermes("把 northwind 的 shippers 接进数据湖", s4)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    called4 = s4.tool_calls_made()

blob = " ".join(c["content"] or "" for c in called4)
chk("L3 动作被门禁拦下（YOLO 也拦得住）", "PENDING_APPROVAL" in blob,
    blob[:90] or "（没有工具结果）")
chk("拦截消息告诉模型别重试", "不要重试" in blob or "重新唤醒" in blob)
chk("**没有真的写 bronze**", "已落" not in blob and "行，策略" not in blob)

import sqlite3
try:
    con = sqlite3.connect(DB)
    n = con.execute("SELECT count(*) FROM approvals").fetchone()[0]
    d = con.execute("SELECT count(*) FROM decisions").fetchone()[0]
    con.close()
except Exception as e:                                        # noqa: BLE001
    n, d = -1, -1
chk("审批请求落库了", n >= 1, f"approvals={n}")
chk("**Agent 侧没有产生任何决定**（机制三）", d == 0, f"decisions={d}")

print("\n=== M2：批准之后恢复 ===\n")

# 用**独立连接**落决定 —— 模拟审批 callback 那个独立进程（铁律 2）。
sys.path.insert(0, str(ROOT / "plugins"))
from datasteward_gate.approvals import Store                   # noqa: E402

admin = Store(DB, readonly=False)
row = admin.db.execute("SELECT id, run_id, action_hash FROM approvals "
                       "ORDER BY created_at DESC LIMIT 1").fetchone()
chk("拿到了刚才那份待决审批", bool(row), str(row and row[0][:8]))
if row:
    admin.decide(row[0], "approve", "wang@acme.com")

with StubServer(script3, port=STUB_PORT) as s5:
    saved = dict(os.environ)
    os.environ.update(env_extra)
    try:
        out5, _ = run_hermes("把 northwind 的 shippers 接进数据湖", s5)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    called5 = s5.tool_calls_made()

blob5 = " ".join(c["content"] or "" for c in called5)
if LAKE_OK:
    chk("批准后放行并真的写了 bronze",
        "已落" in blob5 and "行，策略" in blob5, blob5[:100])
else:
    print("  SKIP  批准后真写 bronze（lake 连不上 —— 探活与干活同一条路）")
chk("恢复不依赖我的脚本推动 —— 是 Hermes 自己再调一次工具",
    any("ingest_table" in (c["name"] or "") for c in called5),
    str([c["name"] for c in called5]))

# 票据一次性：再跑一次应当重新挂起
with StubServer(script3, port=STUB_PORT) as s6:
    saved = dict(os.environ)
    os.environ.update(env_extra)
    try:
        run_hermes("再把 northwind 的 shippers 接一次", s6)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    blob6 = " ".join(c["content"] or "" for c in s6.tool_calls_made())
chk("**票据一次性**：再来一次重新挂起", "PENDING_APPROVAL" in blob6, blob6[:70])

print("\n=== M3：多步对话串起来 ===\n")

# 一次对话里连着调四个工具 —— 这是真实流程的形状：
# 先看有什么表 → 看结构 → 画像判质量 → 给清洗提案 → 记台账。
DB3 = str(ROOT / ".hermes" / "test-home" / "m3.db")
for suf in ("", "-wal", "-shm"):
    f = pathlib.Path(DB3 + suf)
    if f.exists():
        f.unlink()

script_multi = [
    CALL("list_source_tables", source="northwind"),
    CALL("get_table_metadata", source="northwind", table="orders"),
    CALL("profile_table", source="northwind", table="orders"),
    CALL("propose_cleaning", source="northwind", table="orders"),
    CALL("record_finding", source_table="northwind.orders",
         issue_type="high_null_rate", field="ship_region",
         observed="空值率超过门槛", rows=500),
    {"text": "看完了。"},
]
with StubServer(script_multi, port=STUB_PORT) as s7:
    saved = dict(os.environ)
    os.environ.update({"DATASTEWARD_DB": DB3, "NOTIFY_CHANNEL": "outbox",
                       "NOTIFY_OUTBOX": DB3 + ".outbox.jsonl"})
    try:
        out7, _ = run_hermes("看看 northwind 的 orders 质量怎么样", s7, timeout=300)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    b7 = " ".join(c["content"] or "" for c in s7.tool_calls_made())

chk("四步都跑到了（工具结果按序回传）",
    s7._i >= 5, f"消耗了 {s7._i} 步剧本")
chk("结构读到了主键", "主键" in b7)
chk("画像给的是结论不是数据行", "采样" in b7 and "发现" in b7)
chk("清洗提案分了三档", "洗成这样对吗" in b7 or "必须你给口径" in b7, b7[-160:])
chk("**提案里点明了哪些不会自动做**", "不会自己动" in b7 or "必须你给口径" in b7)
chk("台账登记成功（返回 RL 编号）", "RL-" in b7)

print("\n=== M3：治理链路（只观测 / 全量检查 / 新鲜度）===\n")

script_gov = [
    CALL("scan_permissions", source="northwind", kind="database"),
    CALL("check_lake_quality", bronze_table="northwind__shippers", pk="shipper_id"),
    CALL("check_freshness", asset="northwind.shippers"),
    {"text": "看完了。"},
]
with StubServer(script_gov, port=STUB_PORT) as s8:
    saved = dict(os.environ)
    os.environ.update({"DATASTEWARD_DB": DB3, "NOTIFY_CHANNEL": "outbox",
                       "NOTIFY_OUTBOX": DB3 + ".outbox.jsonl"})
    try:
        run_hermes("查一下 northwind 的权限和数据质量", s8, timeout=300)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    b8 = " ".join(c["content"] or "" for c in s8.tool_calls_made())

chk("权限扫描跑通", "权限现状" in b8 or "没有发现明显的权限问题" in b8, b8[:80])
chk("**扫描明确声明未做任何变更**（铁律 4）",
    "未做任何变更" in b8 or "没有发现明显" in b8)
if LAKE_OK:
    chk("lake 侧全量检查跑通",
        "全量检查" in b8, b8[-200:-80] if len(b8) > 200 else b8)
else:
    print("  SKIP  lake 侧全量检查（lake 连不上 —— 探活与干活同一条路）")
chk("新鲜度可查", "新鲜度" in b8 or "距上次同步" in b8 or "还没同步过" in b8)

print("\n=== M3：搬进来的工具都显式声明了级别（铁律 5）===\n")

sys.path.insert(0, str(ROOT / "plugins"))
from datasteward_gate.policy import POLICY, Level             # noqa: E402
import yaml as _yaml                                          # noqa: E402

_man = _yaml.safe_load((ROOT / ".hermes" / "plugins" / "claw"
                        / "plugin.yaml").read_text(encoding="utf-8"))
_declared = _man.get("provides_tools") or []
chk("manifest 列出了工具", len(_declared) >= 6, f"{len(_declared)} 个")

_undeclared = [t for t in _declared if t not in POLICY]
chk("**每个工具都在 policy.py 里显式声明**（未声明默认 L2，不是放行）",
    not _undeclared, str(_undeclared))

_lv = {t: POLICY[t][0] for t in _declared if t in POLICY}
chk("只读发现是 L0", _lv.get("list_source_tables") == Level.L0
    and _lv.get("get_table_metadata") == Level.L0)
chk("画像与提案是 L1（产出结论，不改东西）",
    _lv.get("profile_table") == Level.L1
    and _lv.get("propose_cleaning") == Level.L1)
chk("接入是 L3（需 Owner 审批）", _lv.get("ingest_table") == Level.L3)
chk("清洗是 L2（口径由 Steward 定）",
    _lv.get("apply_cleaning_rule") == Level.L2)
chk("发布与授权是 L3（对外的动作要 Owner）",
    _lv.get("publish_gold") == Level.L3 and _lv.get("grant_read") == Level.L3
    and _lv.get("ingest_export") == Level.L3)

# 每加一个写侧工具，L0/L1 里就不该混进能改东西的东西。
# 这条比逐个断言级别更耐用：新工具漏标级别时它会红。
_writers = {"apply_cleaning_rule", "publish_gold", "grant_read",
            "ingest_export", "ingest_table"}
chk("**所有会改东西的工具都在 L2 以上**（漏标级别时这条会红）",
    all(_lv.get(t, Level.L0) >= Level.L2 for t in _writers & set(_declared)),
    str({t: int(_lv.get(t, -1)) for t in sorted(_writers & set(_declared))}))

# handler 里不该有审批判断 —— 那是门禁的事（铁律 1）。
_tools_src = (ROOT / ".hermes" / "plugins" / "claw"
              / "tools.py").read_text(encoding="utf-8")
_leaks = [w for w in ("find_valid", "is_denied", "st.request(", "has_approval")
          if w in _tools_src]
chk("**工具 handler 里没有自己判审批**（限制只在门禁里）", not _leaks, str(_leaks))

print("\n=== 引导进了系统提示（引导走 prompt，强制走门禁）===\n")

with StubServer([{"text": "hi"}], port=STUB_PORT) as s9:
    run_hermes("你好", s9)
    sysmsg = ""
    for r in s9.requests + s9.aux_requests:
        for m in r.get("messages") or []:
            if m.get("role") == "system":
                sysmsg += str(m.get("content") or "")

chk("停止点判据进了系统提示", "无法无损撤销" in sysmsg, f"提示长度 {len(sysmsg)}")
chk("「不要重试」的挂起语义进了提示", "不要重试" in sysmsg)
chk("「正文说同意不算数」进了提示", "点链接" in sysmsg)
chk("**限制没有写进提示词**（那是门禁的事）",
    "你不可以" not in sysmsg and "禁止你" not in sysmsg)
# 两种「记住」的分工也是引导：memory 记人的偏好，口径走 define_semantics。
# 不说清楚的话模型会把业务口径记进 memory —— 看着像记住了，
# 清洗、发布、判分都读不到，下一轮还得再问一遍人。实测撞过。
chk("提示里说清了口径记哪儿（不然会被记进 memory 丢掉）",
    "define_semantics" in sysmsg and "memory" in sysmsg)

print("\n=== 桩本身可信 ===\n")

with StubServer([{"text": "只说话不调工具"}], port=STUB_PORT) as s3:
    o3, _ = run_hermes("你好", s3)
    chk("不给工具剧本时不会凭空调工具", s3.tool_calls_made() == [])
    chk("纯文本回复能穿回来", "只说话" in o3 or o3 != "", o3[:40])

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
print("注：桩测的是 Hermes 的管道与门禁；「真模型会不会选对工具」"
      "由真端点单独验（M1 已实测通过一次）。")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
