"""资产台账：**一张表的来历，一处记全。**

在这之前「这张表哪来的」要跨 5 张表现拼，找审批只能 `args_json LIKE
'%表名%'` 模糊匹配。实测的后果：开一个全新会话问溯源，它答「审批人姓名
未随记录留存」「口径是谁定的没有任何记录」—— 而东西都在库里，
只是没有一个工具去读。它还把连接账号猜成了 postgres 超级用户。
**查不到就会猜，那比不答更糟。**

    python3 tests/test_provenance.py
"""
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins"),
                str(ROOT / ".hermes" / "plugins")]

DB = "/tmp/dl_prov_test.db"
for suf in ("", "-wal", "-shm"):
    f = pathlib.Path(DB + suf)
    if f.exists():
        f.unlink()
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
os.environ.setdefault("NOTIFY_CHANNEL", "outbox")
os.environ.setdefault("NOTIFY_OUTBOX", "/tmp/dl_prov_outbox.jsonl")

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


import datasteward_gate as G                                   # noqa: E402
from datasteward_gate.approvals import Store                   # noqa: E402
import importlib
T = importlib.import_module("data-steward.tools")                                         # noqa: E402

st = Store(DB, readonly=False)

print("\n=== 动作发生时自动记账 ===\n")

DSN = "postgresql://fin_reader:TOPSECRET@127.0.0.1:5432/acme"
G.audit("connect_source", {"source_id": "acme", "dsn": DSN,
                           "given_by": "dba@acme.com"},
        result="已接入源 acme，能看到 6 张表。", status="DONE", task_id="p1")
G.audit("ingest_table", {"source": "acme", "table": "fin_invoice"},
        result="acme.fin_invoice 已落 iceberg.bronze：600 行", status="DONE",
        task_id="p2")
G.audit("apply_cleaning_rule",
        {"source": "acme", "table": "fin_invoice",
         "approved_rules": ["enum_drift__status"]},
        result="silver 600 行，生效 1 条规则", status="DONE", task_id="p3")

rows = st.provenance()
kinds = [r["event"] for r in rows]
chk("接源、入湖、清洗都进了台账", kinds == [
    "source_registered", "ingested", "cleaned"], str(kinds))

# **口令绝不进台账** —— 它是给审计看的，会被导出、被转发。
blob = json.dumps(rows, ensure_ascii=False)
chk("**口令没有进台账**（台账要给审计看）", "TOPSECRET" not in blob)
chk("但连接身份记下来了（审计要知道用谁连的）",
    "fin_reader@127.0.0.1:5432/acme" in blob)

# 失败的动作不该出现在台账里 —— 台账是「这张表经历过什么」。
G.audit("ingest_table", {"source": "acme", "table": "never"},
        result="错误：连不上", status="DONE", task_id="p4")
chk("失败的动作不入账（没成的事不是这张表的经历）",
    not any(r["asset"] == "acme.never" for r in st.provenance()))

print("\n=== 查一张表的来历 ===\n")

out = T._trace_asset({"asset": "acme.fin_invoice"})
chk("入湖与清洗都在", "入湖" in out and "清洗" in out, out[:70])
chk("bronze 表名写法也认得（acme__fin_invoice）",
    "入湖" in T._trace_asset({"asset": "acme__fin_invoice"}))
chk("**查不到就说无档，不猜**",
    "无档" in T._trace_asset({"asset": "acme.nosuch"}),
    T._trace_asset({"asset": "acme.nosuch"})[:60])
chk("台账里的口令不会被 trace 吐出来", "TOPSECRET" not in out)

print("\n=== append-only：血缘是历史事实 ===\n")

_ddl = (ROOT / "plugins" / "datasteward_gate"
        / "approvals.py").read_text(encoding="utf-8")
_pg = (ROOT / "infra" / "init-steward.sql").read_text(encoding="utf-8")
chk("PG 侧只给 Agent INSERT/SELECT（改不了已经写下的）",
    "GRANT SELECT, INSERT ON asset_provenance TO agent_role" in _pg
    and "UPDATE ON asset_provenance" not in _pg)
_src = (ROOT / "plugins" / "datasteward_gate"
        / "__init__.py").read_text(encoding="utf-8")
chk("代码里没有改台账的路径（只有 INSERT）",
    "UPDATE asset_provenance" not in _ddl and "DELETE FROM asset_provenance"
    not in _ddl)

print("\n=== 每加一个改数据的工具，都要记账 ===\n")

# 漏一个的后果是「那张表的这一步没有档」—— 而审计问的往往就是那一步。
from datasteward_gate.policy import POLICY, Level             # noqa: E402

_writers = {t for t, (lv, _) in POLICY.items()
            if lv >= Level.L2 and lv < Level.L4}
_missing = sorted(_writers - set(G._PROVENANCE_EVENTS))
chk("**所有改数据的工具都在记账表里**", not _missing,
    f"这些动作不会留下血缘：{_missing}")

print("\n=== 回填：反推的必须标出来 ===\n")

st.record_provenance("x.y", "ingested", actor="someone",
                     detail={"backfilled": True, "rows": 1})
_bf = [r for r in st.provenance("x.y") if r["detail"].get("backfilled")]
chk("回填记录带 backfilled 标记", bool(_bf))
_ops = (ROOT / "ops" / "provenance.py").read_text(encoding="utf-8")
chk("回填脚本明说了「反推不等于当时记的」",
    "反推不等于当时记的" in _ops or "反推不等于" in _ops)
chk("查询时把回填标记显示出来（审计要问得出来）", "⟨回填⟩" in _ops)

print("\n=== 外部拿得到：script 与 API ===\n")

r = subprocess.run([sys.executable, str(ROOT / "ops" / "provenance.py"),
                    "acme.fin_invoice"], capture_output=True, text=True,
                   env={**os.environ}, timeout=60)
chk("script 查得出来", r.returncode == 0 and "入湖" in r.stdout,
    r.stdout[:60] or r.stderr[:60])

_exp = "/tmp/dl_prov_export.json"
r2 = subprocess.run([sys.executable, str(ROOT / "ops" / "provenance.py"),
                     "--export", _exp], capture_output=True, text=True,
                    env={**os.environ}, timeout=60)
_d = json.loads(pathlib.Path(_exp).read_text(encoding="utf-8")) if \
    pathlib.Path(_exp).exists() else {}
chk("script 导得出 JSON（喂给审计工具）",
    _d.get("count", 0) >= 3 and "TOPSECRET" not in json.dumps(_d),
    f"{_d.get('count')} 条")

_cb = (ROOT / "services" / "approval_callback.py").read_text(encoding="utf-8")
chk("API 有只读的 /provenance 端点", '"/provenance"' in _cb)
# `/provenance` 只调 `store.provenance(...)` 读；写台账的唯一入口是
# post_tool_call。API 上多一条写路径 = 血缘可以被外部伪造。
chk("API 只读 —— 没有往台账写的调用",
    "store.record_provenance" not in _cb and ".record_provenance(" not in _cb)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
