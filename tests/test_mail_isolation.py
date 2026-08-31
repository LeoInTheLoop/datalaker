"""断言：通知与门禁是两回事。

邮件发不出去时，动作**依然被拦截** —— 只是审批人没收到通知。
这条断言防止「发信失败就放行」这种最危险的降级。
"""
import os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = "/tmp/dl_mail_iso.db"
for suf in ("", "-wal", "-shm"):
    if os.path.exists(DB + suf):
        os.remove(DB + suf)
os.environ["DATASTEWARD_DB"] = DB

from plugins.datasteward_gate import gate, store
from plugins.datasteward_gate.approvals import action_hash

ok, bad = [], []
def check(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== 通知失败 ≠ 门禁打开 ===\n")

args = {"table": "FIN.monthly"}
r = gate("ingest_table", args, "run-mail")
check("SMTP 未配置时仍然拦截", r is not None and r.get("action") == "block",
      r.get("message", "")[:36] if r else "")

aid = store().pending(action_hash("ingest_table", args), "run-mail")
check("审批请求照常落库", aid is not None)

# 等后台发信线程跑完
for _ in range(30):
    kinds = [k for _, k, _ in store().events(aid)]
    if any(k.startswith("MAIL_") for k in kinds):
        break
    time.sleep(0.1)
kinds = [k for _, k, _ in store().events(aid)]
check("发信结果被记录", any(k.startswith("MAIL_") for k in kinds), ",".join(kinds) or "无事件")

# 重复触发不重复发信
n_before = len([k for _, k, _ in store().events(aid) if k.startswith("MAIL_")])
gate("ingest_table", args, "run-mail")
time.sleep(0.3)
n_after = len([k for _, k, _ in store().events(aid) if k.startswith("MAIL_")])
check("重复触发不重复发信", n_before == n_after, f"{n_before} -> {n_after}")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
