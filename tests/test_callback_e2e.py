"""端到端：Agent 挂起 → 邮件链接点击 → 审批落库 → Agent 恢复放行。

前置：DATASTEWARD_DB=/tmp/dl_cb.db DATASTEWARD_TOKEN_SECRET=test-secret \
      python3 services/approval_callback.py &
"""
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services"))

import tokens
from plugins.datasteward_gate import gate, store
from plugins.datasteward_gate.approvals import action_hash

BASE = "http://127.0.0.1:8787"
RUN = "run-e2e"
ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


OUTBOX = os.environ.get("NOTIFY_OUTBOX", "/tmp/cb_outbox.jsonl")


def hit(path, token):
    try:
        with urllib.request.urlopen(f"{BASE}{path}?t={token}") as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def confirm_token(after=0.0):
    """从 outbox 里取出确认信中的第二枚令牌。

    双重确认（R3 决策 25）之后，第一次点击只发确认信、不落库。
    真实用户要在**注册邮箱收到的那封信里**再点一次 —— 测试就照着做，
    而不是把 REQUIRE_DOUBLE_CONFIRM 关掉绕过去。
    """
    import json as _j
    import re as _re
    import time as _t
    end = _t.time() + 5
    while _t.time() < end:
        try:
            recs = [_j.loads(l) for l in open(OUTBOX, encoding="utf-8") if l.strip()]
        except FileNotFoundError:
            recs = []
        for r in reversed(recs):
            if r["kind"] == "notice" and r["ts"] > after:
                m = _re.search(r"[?&]t=([A-Za-z0-9._\-]+)", r.get("body", ""))
                if m:
                    return m.group(1)
        _t.sleep(0.05)
    return None


def two_stage(path, token):
    """完整走一遍：点击 → 收确认信 → 再点。返回 (状态码, 页面, 确认令牌)。"""
    import time as _t
    t0 = _t.time()
    code, body = hit(path, token)
    if code != 200 or "确认" not in body:
        return code, body, None            # 未开启双重确认时就是一步到位
    t2 = confirm_token(after=t0)
    if not t2:
        return 0, "确认信里没找到令牌", None
    code, body = hit(path, t2)
    return code, body, t2


print("\n=== 端到端：审批闭环 ===\n")

with urllib.request.urlopen(f"{BASE}/health") as r:
    check("服务健康", r.status == 200)

# 1. Agent 尝试 L3 动作 → 挂起
args = {"table": "FIN.monthly"}
r = gate("ingest_table", args, RUN)
check("Agent 被拦截并发起审批", r and r.get("action") == "block")
h = action_hash("ingest_table", args)
aid = store().pending(h, RUN)
check("审批请求已落库", aid is not None, f"id={aid[:8] if aid else '-'}")

# 2. 签发邮件里的两枚令牌
t_ok = tokens.issue(aid, "approve", "owner@corp.com")
t_no = tokens.issue(aid, "deny", "owner@corp.com")

# 3. 改 URL 把 deny 令牌拿去 approve —— 必须被拒
code, _ = hit("/approve", t_no)
check("令牌意图绑定（deny 令牌不能用于 approve）", code == 403, f"http {code}")

# 4. 篡改签名
code, _ = hit("/approve", t_ok[:-4] + "AAAA")
check("篡改签名被拒", code == 403, f"http {code}")

# 5. 正常批准
code, body, t_ok2 = two_stage("/approve", t_ok)
check("点击批准成功（含双重确认）", code == 200 and "已批准" in body, f"http {code}")

# 6. Agent 恢复后放行
check("Agent 恢复后放行", gate("ingest_table", args, RUN) is None)

# 7. 票据一次性
check("票据消费后再次挂起", gate("ingest_table", args, RUN) is not None)

# 8. 重放同一链接
code, body = hit("/approve", t_ok2 or t_ok)
check("链接重放被拒", code == 409, f"http {code}")

# 9. deny 路径
args2 = {"table": "HR.salary"}
gate("ingest_table", args2, RUN)
aid2 = store().pending(action_hash("ingest_table", args2), RUN)
code, body, _ = two_stage("/deny", tokens.issue(aid2, "deny", "owner@corp.com"))
check("点击拒绝成功（含双重确认）", code == 200 and "已拒绝" in body, f"http {code}")
r = gate("ingest_table", args2, RUN)
check("拒绝后进 deny list", r and "DENIED" in r.get("message", ""))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
