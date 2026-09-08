"""Pipeline 断言（readme 4.5 / 4.6 支柱四）。

验证的核心：**gate 返回 PENDING 时 Pipeline 正常结束，不阻塞等待**——
这是它与 LangGraph `interrupt` 的根本区别。
"""
import os, sys, time, uuid
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "/tmp/dl_pipe_t.db"
for suf in ("", "-wal", "-shm"):
    if os.path.exists(DB + suf): os.remove(DB + suf)
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "services"))
sys.path.insert(0, os.path.join(ROOT, "plugins"))

from langgraph.checkpoint.memory import MemorySaver
from datasteward_gate import store
from datasteward_gate.approvals import Store, action_hash
sys.path.insert(0, os.path.join(ROOT, "pipelines"))
import ingest_table as P

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== Pipeline：接入一张表 ===\n")
RUN = f"pipe-{uuid.uuid4().hex[:6]}"
saver = MemorySaver()

# 第一次：应挂起并正常结束
out1 = P.run("northwind", "orders", run_id=RUN, checkpointer=saver)
chk("第一次跑 → 挂起", out1.get("status") == "pending_approval", out1.get("status"))
chk("Pipeline 正常结束（非阻塞）", "message" in out1)
chk("画像已产出", out1.get("profile", {}).get("sampled_rows") == 830)
chk("DQ 已判定", out1.get("dq", {}).get("passed") is True)
chk("抓到高空值率列（业务合法的异常）",
    any(f["issue"] == "high_null_rate" for f in out1["dq"]["findings"]))
chk("提案含需人确认的列", bool(out1.get("proposal", {}).get("needs_human")),
    str(out1.get("proposal", {}).get("needs_human")))
chk("未执行 apply（门禁生效）", out1.get("status") != "ingested")

# 批准
h = action_hash("ingest_table", {"table": "orders", "source": "northwind"})
aid = store().pending(h, RUN)
chk("审批请求已落库", aid is not None, (aid or "")[:8])
admin = Store(DB, readonly=False)
admin.decide(aid, "approve", "owner@corp.com")

# 第二次：应放行并执行
out2 = P.run("northwind", "orders", run_id=RUN, checkpointer=saver)
chk("批准后 → 执行", out2.get("status") == "ingested", out2.get("status"))
chk("执行结果含行数", "830" in (out2.get("message") or ""))

# 第三次：票据一次性，重新挂起。
#
# **先把同步时间调老。** 门禁有一道 `_already_done`：刚接过的表不再发审批
# （人白点链接）。它排在票据检查之前，会把这条断言遮成 ALREADY_DONE ——
# 那样测的就不是「票用过一次还能不能再用」了。
# 调老之后走的是原来那条路，安全断言一个字都没松。
_admin = Store(DB, readonly=False)
_admin.db.execute("UPDATE sync_state SET last_synced_at=? WHERE asset=?",
                  (time.time() - 48 * 3600, "northwind.orders"))
_admin.db.commit()
out3 = P.run("northwind", "orders", run_id=RUN, checkpointer=saver)
chk("票据一次性 → 重新挂起", out3.get("status") == "pending_approval", out3.get("status"))

# 换表：指纹不同
out4 = P.run("northwind", "customers", run_id=RUN, checkpointer=saver)
chk("换表指纹不同 → 挂起", out4.get("status") == "pending_approval")
chk("换表画像独立", out4.get("profile", {}).get("sampled_rows") == 91,
    str(out4.get("profile", {}).get("sampled_rows")))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
