"""真实邮件闭环回归（需人工点击，不进 run_all.sh）。

跑法：
    python3 services/approval_callback.py &
    .venv/bin/python tests/test_live_mail_loop.py

与 test_callback_e2e.py 的区别：那个用程序模拟点击，这个发真实邮件、等人点。
R1 已用它验证过一次完整链路，保留供演示与回归。
"""
import os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATASTEWARD_DB", os.path.expanduser("~/.datalaker/approvals.db"))

from plugins.datasteward_gate import gate, store
from plugins.datasteward_gate.approvals import action_hash

TABLE = os.environ.get("DEMO_TABLE", "FIN.monthly")
RUN = f"run-live-{int(time.time())}"
args = {"table": TABLE}
ok, bad = [], []

def check(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print(f"\n=== 真实邮件闭环（run={RUN}）===\n")

r = gate("ingest_table", args, RUN)
check("Gate 拦截", r and r.get("action") == "block")
aid = store().pending(action_hash("ingest_table", args), RUN)
check("请求落库", aid is not None, aid[:8] if aid else "")

for _ in range(80):
    ev = [k for _, k, _ in store().events(aid)]
    if any(k.startswith("MAIL_") for k in ev):
        break
    time.sleep(0.25)
ev = [k for _, k, _ in store().events(aid)]
check("邮件已发出", "MAIL_SENT" in ev, ",".join(ev))

print(f"\n  👉 去邮箱点「批准」(id={aid[:8]})，等待中...\n")
for i in range(180):
    if any(k.startswith("DECIDED_") for _, k, _ in store().events(aid)):
        break
    time.sleep(1)
decided = [k for _, k, _ in store().events(aid) if k.startswith("DECIDED_")]
check("收到人工决定", bool(decided), decided[0] if decided else "超时")

if decided == ["DECIDED_APPROVE"]:
    check("批准后放行", gate("ingest_table", args, RUN) is None)
    check("票据一次性", gate("ingest_table", args, RUN) is not None)
    check("指纹绑定：换表仍挂起",
          gate("ingest_table", {"table": "HR.salary"}, RUN) is not None)
elif decided:
    check("拒绝后仍拦截", gate("ingest_table", args, RUN) is not None)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
