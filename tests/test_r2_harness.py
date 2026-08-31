import os, sys, time
sys.path.insert(0, '/Users/lingyukong/Documents/GitHub/datalaker')
DB = "/tmp/dl_r2.db"
for suf in ("", "-wal", "-shm"):
    if os.path.exists(DB+suf): os.remove(DB+suf)
os.environ.update(DATASTEWARD_DB=DB, PER_PERSON_WIP_LIMIT="2", GLOBAL_WIP_LIMIT="5")
os.environ.pop("DATASTEWARD_DSN", None)
from plugins.datasteward_gate import gate, store
from plugins.datasteward_gate.approvals import Store

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== R2：角色化 / WIP / 提问 / 知识沉淀 ===\n")
st = store()

# 角色化
st.assign_role("owner", "zhang@corp.com", granted_by="sponsor@corp.com", reason="初始指派")
chk("角色解析", st.resolve_role("owner") == "zhang@corp.com", st.resolve_role("owner"))
st.assign_role("owner", "li@corp.com", granted_by="sponsor@corp.com", reason="张某转岗")
chk("换人后自动指向新人", st.resolve_role("owner") == "li@corp.com", st.resolve_role("owner"))
rows = st.db.execute("SELECT count(*) FROM role_assignment WHERE role='owner'").fetchone()[0]
chk("历史记录保留（决定是历史事实）", rows == 2, f"{rows} 条")

# WIP
for i in range(2):
    gate("ingest_table", {"table": f"T{i}"}, "run-wip")
r = gate("ingest_table", {"table": "T9"}, "run-wip")
chk("每人在办超限被拦", isinstance(r, dict) and "WIP_LIMIT" in r.get("message", ""),
    r.get("message","")[:44] if isinstance(r,dict) else str(r))
chk("在办计数正确", st.open_count() == 2, f"{st.open_count()} 件")

# 提问
qid, created = st.ask("run-q", "orders.customer_id",
    "orders.customer_id 有 50 个 NULL（占 0.05%），这属于哪种情况？",
    [{"key":"A","label":"数据错误，应当修复"},
     {"key":"B","label":"匿名订单，业务上合法","recommended":True},
     {"key":"C","label":"历史遗留，应当过滤"},
     {"key":"other","label":"以上都不对，我来说明"}],
    approver="steward",
    evidence="这 50 条的 payment_type 全是 voucher，与匿名下单流程一致。")
chk("提问已创建", created and qid)
chk("提问与审批同表同机制", st.pending(
    __import__("plugins.datasteward_gate.approvals", fromlist=["action_hash"]).action_hash(
        "__question__", {"asset":"orders.customer_id",
        "q":"orders.customer_id 有 50 个 NULL（占 0.05%），这属于哪种情况？"}), "run-q") == qid)
qid2, created2 = st.ask("run-q", "orders.customer_id",
    "orders.customer_id 有 50 个 NULL（占 0.05%），这属于哪种情况？", [], "steward")
chk("重复提问幂等（不重复打扰）", not created2 and qid2 == qid)

# 知识沉淀
chk("未确认前查不到", st.known("orders.customer_id", "semantics") is None)
st.remember("orders.customer_id", "semantics", "NULL 表示匿名订单，业务上合法",
            "销售运营 张某", source_item=qid)
k = st.known("orders.customer_id", "semantics")
chk("答案已沉淀", k and "匿名订单" in k["value"], k["value"] if k else "")
st.remember("orders.customer_id", "semantics", "修订版说明", "李某")
k2 = st.known("orders.customer_id", "semantics")
chk("沉淀可更新（不重复插入）", k2["value"] == "修订版说明")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
