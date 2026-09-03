"""可观测接入（OpenTelemetry → Phoenix）。

**这一层的验收不是「能看到 trace」，而是「看不到 trace 时什么都不会坏」。**
它与「通知失败 ≠ 门禁打开」是同一条原则：可观测组件是旁路，
旁路挂了主流程必须照跑，一条事件都不许少记。

在此之上才谈可见性：`WAITING_FOR_HUMAN` 必须是一条**跨越真实时长**的 span，
名字精确、带得上审批 id 和等谁 —— 否则「Agent 会等人」这件事在
Phoenix 时间线上仍然看不出来（readme 14 第 21 幕 / 19 表第 44 项）。
"""
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
os.environ.pop("CLAW_TRACE", None)
DB = f"/tmp/dl_tracing_{uuid.uuid4().hex[:8]}.db"
os.environ["DATASTEWARD_DB"] = DB
# 导出失败是本测试的**被测场景之一**，不是噪音
logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)

import runs as R                                             # noqa: E402
import tracing                                               # noqa: E402
import trajectory as TJ                                      # noqa: E402
from plugins.datasteward_gate.approvals import Store, action_hash   # noqa: E402

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def reset(**env):
    """把 tracing 的模块级缓存清干净 —— 它按设计只初始化一次。"""
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "CLAW_TRACE", "PHOENIX_PROJECT_NAME"):
        os.environ.pop(k, None)
    os.environ.update(env)
    tracing._tracer = None
    tracing._provider = None
    tracing._root = None
    tracing._waits.clear()
    TJ._TRACING = tracing


admin = Store(DB, readonly=False)


def a_line(table, approver, note=""):
    """建一条线并挂起等人，返回 (run_id, approval_id)。"""
    rid = R.create("t", {"table": table})
    aid, _ = admin.request(rid, action_hash("ingest_table", {"t": table}),
                           "ingest_table", json.dumps({"t": table}), approver)
    R.suspend(rid, aid, {"stage": "gate"}, note or f"等 {approver} 批 {table}")
    return rid, aid


def ev(name="list_tables", **kw):
    e = {"step_index": 1, "event_type": "tool_call", "name": name,
         "tool_name": name, "arguments": {"source": "olist"},
         "result_summary": "9 张表", "status": "ok",
         "timestamp_ms": int(time.time() * 1000), "metadata": {}}
    e.update(kw)
    return e


# ================================================================ 1. no-op
print("\n=== 没配端点 = 完全无害的 no-op ===\n")

reset()
chk("没设任何环境变量时不启用", tracing.enabled() is False)

calls = [
    ("start_root", ("run:x",), {}),
    ("event_span", (ev(),), {}),
    ("suspend_begin", ("r1", "a1", "owner:fin", "note"), {}),
    ("suspend_end", ("r1",), {"since": time.time() - 3600}),
    ("end_root", ("success",), {}),
    ("flush", (), {}),
]
raised = []
for n, a, k in calls:
    try:
        getattr(tracing, n)(*a, **k)
    except Exception as e:                                   # noqa: BLE001
        raised.append(f"{n}: {type(e).__name__}")
chk("全部 API 在 no-op 下都不抛", not raised, ", ".join(raised))
chk("no-op 下不建 provider（不留线程、不建连接）", tracing._provider is None)

t = TJ.Trajectory("noop-case")
for i in range(3):
    t.tool_call(f"tool{i}", {"i": i}, "ok")
t.gate_block("ingest_table", {"t": "orders"}, "[PENDING_APPROVAL] 等王姐")
t.finish("success")
chk("no-op 下轨迹照记", t.to_dict()["event_count"] == 4,
    str(t.to_dict()["event_count"]))

rid, aid = a_line("orders", "owner:fin")
chk("no-op 下挂起照常落库", R.get(rid)["status"] == "waiting_human")
admin.decide(aid, "approve", "wang@acme.com")
R.register("t", lambda run_id, params, checkpoint=None: R.finish(run_id, "done"))
chk("no-op 下恢复照常", R.resume_one(rid)["status"] == "done")

# ================================================================ 2. span 内容
print("\n=== span 名称与属性 ===\n")

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter)
    HAVE_SDK = True
except Exception:                                            # noqa: BLE001
    HAVE_SDK = False

if not HAVE_SDK:
    print("  SKIP  未安装 opentelemetry-sdk（./.venv/bin/pip install "
          "opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc）")
else:
    # 直接换成内存 exporter：断言 span 内容不该依赖 Phoenix 起没起
    reset()
    mem = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(mem))
    tracing._provider = prov
    tracing._tracer = prov.get_tracer("test")

    chk("装好后即启用", tracing.enabled() is True)

    t = TJ.Trajectory("span-case", run_id="span-case.rid")
    t.tool_call("list_source_tables", {"source": "olist"}, "9 张表")
    t.gate_block("ingest_table", {"table": "orders"},
                 "[PENDING_APPROVAL] 已就 ingest_table 向 owner:fin 发起审批")
    t.human_decision("approve", "wang@acme.com", approval_id="ap-1")
    t.finish("success")

    spans = {s.name: s for s in mem.get_finished_spans()}
    chk("根 span 覆盖整次运行", "run:span-case" in spans, str(sorted(spans))[:120])
    chk("事件 span 名 = 事件类型:名字",
        "tool_call:list_source_tables" in spans and
        "gate_block:PENDING_APPROVAL" in spans and
        "human_decision:approve" in spans, str(sorted(spans))[:160])

    tc = spans["tool_call:list_source_tables"].attributes
    chk("工具 span 标成 TOOL（Phoenix 靠它渲染）",
        tc.get("openinference.span.kind") == "TOOL", str(tc.get("openinference.span.kind")))
    chk("带工具名与 run_id",
        tc.get("tool.name") == "list_source_tables" and
        tc.get("claw.run_id") == "span-case.rid")
    chk("入参进 input.value（只有参数，没有数据行）",
        json.loads(tc["input.value"]) == {"source": "olist"}, tc.get("input.value"))

    gb = spans["gate_block:PENDING_APPROVAL"].attributes
    chk("被拦下的 span 状态是 blocked", gb.get("claw.status") == "blocked",
        str(gb.get("claw.status")))
    hd = spans["human_decision:approve"].attributes
    chk("人的决定带审批 id 与审批人",
        hd.get("claw.approval_id") == "ap-1" and hd.get("claw.approver") == "wang@acme.com")

    # ---- 成本维度：事件带了用量就上报 ----
    mem.clear()
    t2 = TJ.Trajectory("cost-case")
    t2.tool_call("ask_llm", tokens_in=1200, tokens_out=300, cost_usd=0.004)
    cs = [s for s in mem.get_finished_spans() if s.name == "tool_call:ask_llm"][0]
    chk("token 用量映射成 Phoenix 认的属性",
        cs.attributes.get("llm.token_count.prompt") == 1200 and
        cs.attributes.get("llm.token_count.completion") == 300 and
        cs.attributes.get("claw.cost_usd") == 0.004)

    # ---- WAITING_FOR_HUMAN：成功判据 ----
    print("\n=== WAITING_FOR_HUMAN 是一条跨越真实时长的 span ===\n")
    mem.clear()
    rid, aid = a_line("crm_customer", "owner:crm", "等周经理批 crm_customer")
    chk("挂起时不急着建 span（还不知道等多久）",
        not [s for s in mem.get_finished_spans() if s.name == "WAITING_FOR_HUMAN"])
    chk("挂起被记下来了", rid in tracing._waits)

    # 时间引擎（evals/clock.py）就是这样把 updated_at 往前回拨的
    admin.decide(aid, "approve", "zhou@acme.com")
    con = sqlite3.connect(DB)
    con.execute("UPDATE runs SET updated_at=updated_at-? WHERE run_id=?",
                (3 * 86400, rid))
    con.commit()
    con.close()
    R.resume_one(rid)

    w = [s for s in mem.get_finished_spans() if s.name == "WAITING_FOR_HUMAN"]
    chk("恢复时补出挂起 span", len(w) == 1, str(len(w)))
    if w:
        a = w[0].attributes
        chk("span 名精确等于 WAITING_FOR_HUMAN", w[0].name == "WAITING_FOR_HUMAN")
        chk("属性里有审批 id", a.get("claw.approval_id") == aid, str(a.get("claw.approval_id")))
        chk("属性里有等谁", a.get("claw.waiting_for") == "owner:crm",
            str(a.get("claw.waiting_for")))
        dur_days = (w[0].end_time - w[0].start_time) / 86400e9
        chk("span 跨度 = 真的等了多久（不是本进程等了多久）",
            2.9 < dur_days < 3.1, f"{dur_days:.3f} 天")
        chk("跨度也写进属性，便于直接筛",
            abs(a.get("claw.wait_days", 0) - dur_days) < 0.01,
            str(a.get("claw.wait_days")))
        chk("自成一条 trace（等待发生在两次进程之间，没有父 span）",
            w[0].parent is None)
    chk("等待记录用完即清，不在常驻进程里堆积", rid not in tracing._waits)

    # ---- 等 WIP 容量不是等人 ----
    mem.clear()
    rid2 = R.create("t", {"table": "wip_line"})
    R.suspend(rid2, None, {"stage": "wip"}, "[WIP_LIMIT] 王姐待办已满")
    chk("被 WIP 挡回不算 WAITING_FOR_HUMAN（那是排队，不是等谁拍板）",
        rid2 not in tracing._waits and
        not [s for s in mem.get_finished_spans() if s.name == "WAITING_FOR_HUMAN"])

# ================================================================ 3. 挂了照跑
print("\n=== 可观测组件挂掉，主流程照跑 ===\n")


class Exploding:
    """每个方法都炸的 tracer —— 模拟 SDK 内部出问题。"""

    def start_span(self, *a, **kw):
        raise RuntimeError("tracer 炸了")


reset()
tracing._tracer = Exploding()
tracing._provider = None
raised = []
for n, a, k in calls:
    try:
        getattr(tracing, n)(*a, **k)
    except Exception as e:                                   # noqa: BLE001
        raised.append(f"{n}: {type(e).__name__}")
chk("tracer 炸了，tracing 的 API 一个都不往外抛", not raised, ", ".join(raised))

t = TJ.Trajectory("boom-case")
for i in range(5):
    t.tool_call(f"tool{i}", {"i": i}, "ok")
t.finish("success")
chk("tracer 炸了，轨迹一条不少", t.to_dict()["event_count"] == 5,
    str(t.to_dict()["event_count"]))

rid, aid = a_line("boom_table", "owner:ops")
admin.decide(aid, "approve", "sun@acme.com")
chk("tracer 炸了，挂起与恢复照常", R.resume_one(rid)["status"] == "done")


class BrokenModule:
    """连 tracing 模块本身都换成坏的 —— trajectory 侧也必须自己兜住。"""

    def __getattr__(self, name):
        raise ImportError("tracing 模块坏了")


TJ._TRACING = BrokenModule()
t = TJ.Trajectory("broken-mod-case")
t.tool_call("x", {"a": 1}, "ok")
t.gate_block("y", {}, "[DENIED] 不行")
t.finish("success")
chk("tracing 模块整个坏掉，轨迹照记", t.to_dict()["event_count"] == 2,
    str(t.to_dict()["event_count"]))
TJ._TRACING = tracing

# ---- 真·端点不可达（Phoenix 没起 / 挂了）----
reset(OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9")
# 装了 SDK 就照报（该报的还是要报），没装就退回 no-op —— 两条都不许影响主流程
chk("端点不可达不改变启用与否", tracing.enabled() is HAVE_SDK,
    f"enabled={tracing.enabled()} sdk={HAVE_SDK}")
t0 = time.time()
t = TJ.Trajectory("dead-endpoint-case")
for i in range(10):
    t.tool_call(f"tool{i}", {"i": i}, "ok")
rid, aid = a_line("dead_table", "owner:fin")
admin.decide(aid, "approve", "wang@acme.com")
out = R.resume_one(rid)
t.finish("success")
elapsed = time.time() - t0
chk("端点不可达，轨迹一条不少", t.to_dict()["event_count"] == 10,
    str(t.to_dict()["event_count"]))
chk("端点不可达，长时任务照常恢复", out["status"] == "done")
chk("端点不可达不会把主流程拖慢（导出在后台，不阻塞）", elapsed < 10,
    f"{elapsed:.2f}s")
tracing._provider = None            # 别让退出时的 flush 去等一个死端点

# ================================================================ 4. 真实落库
print("\n=== Phoenix 真的收得到（活着才测）===\n")

PHOENIX = os.environ.get("PHOENIX_URL", "http://localhost:6006")


def phoenix_up():
    try:
        with urllib.request.urlopen(PHOENIX + "/v1/projects", timeout=2) as r:
            return r.status == 200
    except Exception:                                        # noqa: BLE001
        return False


if not HAVE_SDK:
    print("  SKIP  未安装 opentelemetry-sdk")
elif not phoenix_up():
    print("  SKIP  Phoenix 未启动（docker compose --project-directory infra "
          "--env-file .env --profile obs up -d phoenix）")
else:
    proj = f"claw-selftest-{uuid.uuid4().hex[:8]}"
    reset(OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317",
          PHOENIX_PROJECT_NAME=proj)
    marker = uuid.uuid4().hex[:12]
    t = TJ.Trajectory(f"selftest-{marker}")
    t.tool_call("list_source_tables", {"source": "olist"}, "9 张表")
    rid, aid = a_line(f"selftest_{marker}", "owner:fin")
    admin.decide(aid, "approve", "wang@acme.com")
    con = sqlite3.connect(DB)
    con.execute("UPDATE runs SET updated_at=updated_at-? WHERE run_id=?",
                (5 * 86400, rid))
    con.commit()
    con.close()
    R.resume_one(rid)
    t.finish("success")
    chk("导出成功", tracing.flush(10000) is True)

    got = []
    for _ in range(20):                 # Phoenix 落库有延迟，轮询而不是猜
        try:
            with urllib.request.urlopen(
                    f"{PHOENIX}/v1/projects/{proj}/spans?limit=100", timeout=3) as r:
                got = json.loads(r.read()).get("data", [])
        except Exception:                                    # noqa: BLE001
            got = []
        if any(s["name"] == "WAITING_FOR_HUMAN" for s in got):
            break
        time.sleep(0.5)

    names = [s["name"] for s in got]
    chk("Phoenix 里查得到本次运行的 trace", len(got) >= 3, f"{len(got)} 个 span")
    chk("时间线上有工具调用", any(n.startswith("tool_call:") for n in names),
        str(names)[:120])
    ws = [s for s in got if s["name"] == "WAITING_FOR_HUMAN"]
    chk("时间线上看得见 WAITING_FOR_HUMAN", len(ws) == 1, str(names)[:160])
    if ws:
        a = ws[0]["attributes"]
        chk("Phoenix 里的挂起 span 带审批 id 和等谁",
            a.get("claw.approval_id") == aid and a.get("claw.waiting_for") == "owner:fin",
            f"{a.get('claw.waiting_for')} / {a.get('claw.approval_id')}")
        chk("Phoenix 里的挂起 span 跨越 5 天", 4.9 < a.get("claw.wait_days", 0) < 5.1,
            f"{a.get('claw.wait_days')} 天")
    print(f"  ->  {PHOENIX}/projects  项目 {proj}")

for suf in ("", "-wal", "-shm"):
    try:
        os.unlink(DB + suf)
    except OSError:
        pass

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
