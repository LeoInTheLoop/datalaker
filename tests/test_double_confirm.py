"""双重确认断言（readme 10.5）。

**堵的是「转发链接就能批」这个洞**：
Owner 把审批邮件转给助理，助理点了链接——在 L1 档下会成功，
而审计里记的仍是 Owner。

修法不是判断谁是谁，而是加一次回执确认：
第一次点击只发确认信，确认信只到 approver 的注册邮箱。
"""
import os, sys, urllib.error, urllib.request, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "/tmp/dl_dc.db"
for suf in ("", "-wal", "-shm"):
    if os.path.exists(DB + suf): os.remove(DB + suf)
os.environ.update(DATASTEWARD_DB=DB, DATASTEWARD_TOKEN_SECRET="dc-secret",
                  REQUIRE_DOUBLE_CONFIRM="1", APPROVAL_PORT="8791",
                  APPROVAL_BASE_URL="http://127.0.0.1:8791",
                  # **走 outbox，不走真 Gmail。** 这一组测的是「第一次点击
                  # 只发确认信、不落库；第二次才落」—— 那是本地逻辑，与
                  # 通道无关。挂在真 Gmail 上的代价实测过：发信配额一超
                  # （HttpError 429 user-rate limit），确认信发不出去，
                  # 整条路返回 503，这一组就红 —— 而机制本身好好的。
                  # 「通知失败 ≠ 门禁打开」那条另有 test_mail_isolation 专测。
                  NOTIFY_CHANNEL="outbox",
                  NOTIFY_OUTBOX="/tmp/dl_dc_outbox.jsonl")
os.environ.pop("DATASTEWARD_DSN", None)
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "services"))
sys.path.insert(0, os.path.join(ROOT, "plugins"))

import tokens
from datasteward_gate import gate, store
from datasteward_gate.approvals import Store, action_hash

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

def hit(path, token):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8791{path}?t={token}", timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

print("\n=== 双重确认 ===\n")

# 令牌带 stage
t1 = tokens.issue("a-1", "approve", "owner@corp.com")
chk("默认签发 stage=click", tokens.verify(t1)["st"] == "click")
t2 = tokens.issue("a-1", "approve", "owner@corp.com", stage="confirm")
chk("可签发 stage=confirm", tokens.verify(t2)["st"] == "confirm")
chk("两阶段令牌不同", t1 != t2)

# 起服务
import subprocess, time
proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "services/approval_callback.py")],
                        env={**os.environ}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try:
        urllib.request.urlopen("http://127.0.0.1:8791/health", timeout=1); break
    except Exception: time.sleep(0.2)

try:
    RUN = f"dc-{uuid.uuid4().hex[:6]}"
    args = {"table": "FIN.dc"}
    gate("ingest_table", args, RUN)
    aid = store().pending(action_hash("ingest_table", args), RUN)
    chk("审批已发起", aid is not None)

    # 第一次点击：不落库
    code, body = hit("/approve", tokens.issue(aid, "approve", "owner@corp.com"))
    chk("第一次点击返回待确认", code == 200 and "确认" in body, f"http {code}")
    chk("第一次点击后仍未放行（未落库）",
        gate("ingest_table", args, RUN) is not None)

    # 第二次点击（confirm）：才生效
    code, body = hit("/approve", tokens.issue(aid, "approve", "owner@corp.com", stage="confirm"))
    chk("确认后落库", code == 200 and "已批准" in body, f"http {code}")
    chk("确认后放行", gate("ingest_table", args, RUN) is None)

    # 关掉双重确认时的行为（内网部署可降档，13.2）
    chk("stage 字段可用于区分两阶段",
        tokens.verify(tokens.issue(aid, "deny", "x", stage="confirm"))["st"] == "confirm")
finally:
    proc.terminate()

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
