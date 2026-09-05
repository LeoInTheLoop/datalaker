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

print("\n=== 通道配置：进程环境变量必须压得过 .env ===\n")

# **同一个坑踩了四次**：NOTIFY_CHANNEL、APPROVAL_BASE_URL、SMTP_*、
# 以及收件人 MAIL_*。只读 `.env` 的话，演练与 eval 想把信发到本地模拟
# 邮箱就只能去改仓库里那份给生产用的配置 —— 而漏改一处，信就真发到
# 公网上的那个地址去了。实测撞过：审批信发去了 .env 里的真实 Gmail。
import os as _o2                                              # noqa: E402
import sys as _s3                                             # noqa: E402
import pathlib as _p3                                         # noqa: E402
_R3 = _p3.Path(__file__).resolve().parent.parent
_s3.path.insert(0, str(_R3 / "services"))
import notify as _nf                                          # noqa: E402

_saved = _o2.environ.get("MAIL_OWNER")
_o2.environ["MAIL_OWNER"] = "sim@acme.test"
try:
    check("**环境变量压过 .env**（否则演练会把信发到生产地址）",
        _nf.cfg("MAIL_OWNER") == "sim@acme.test", _nf.cfg("MAIL_OWNER"))
finally:
    if _saved is None:
        _o2.environ.pop("MAIL_OWNER", None)
    else:
        _o2.environ["MAIL_OWNER"] = _saved

# 明文 SMTP 只允许打本地 —— 忘了配的后果应该是「发不出去」，
# 不该是「明文发到公网」。
_ec = (_R3 / "services" / "notify" / "email_channel.py").read_text(encoding="utf-8")
check("明文 SMTP 只允许本地模拟邮箱（不许明文发公网）",
    "只允许打到本地模拟邮箱" in _ec and "127.0.0.1" in _ec)
check("说了要打本地就绝不走 Gmail API（漏配一个变量不该发错地方）",
    'SMTP_SECURITY", "").lower() != "plain"' in _ec)

# 收件人解析也必须走 cfg —— 这是漏得最久的一处。
for _f in ("plugins/datasteward_gate/__init__.py", "ops/stage-report.py",
           "ops/weekly-report.py"):
    _src = (_R3 / _f).read_text(encoding="utf-8")
    check(f"{_f.split('/')[-1]} 的收件人解析走 cfg（不是只读 .env）",
        "notify.E.get(\"MAIL" not in _src and "E.get(f\"MAIL" not in _src)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
