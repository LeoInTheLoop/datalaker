#!/usr/bin/env python3
"""回信归属：一个人同时挂着几条线时，他回的是哪一条。

**这是长时任务真正会塌的地方。** 点审批链接的归属是精确的 —— 令牌绑死了
`approval_id`，`runs.resumable()` 只拉起 `waiting_on` 对得上的那条线
（`tests/test_runs.py` 已经证过）。但「口径确认」这类不是点链接，是回一封信；
正文里没有任何东西说明这是在回 orders 还是 customers。

所以断言的重点不是「能解析一封信」，而是：
  · 同一个人的两条线，回其中一条不会动到另一条
  · 三条确定性线索（In-Reply-To / 主题 token / plus-address）各自都能单独成立
  · 一条都没命中时**不猜** —— certain=False，run_id 为空
  · 归属判对判错都不改变授权：正文里写「我同意」永远不作数
"""
import json
import os
import pathlib
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
DB = f"/tmp/dl_threads_{uuid.uuid4().hex[:8]}.db"
os.environ["DATASTEWARD_DB"] = DB

import inbound
import mail_threads as MT
import runs as R
from plugins.datasteward_gate.approvals import Store, action_hash

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


admin = Store(DB, readonly=False)


def line(tool, args, approver):
    """开一条线并给它挂一张待批的票，返回 (run_id, approval_id)。"""
    rid = R.create(tool, args)
    aid, _ = admin.request(rid, action_hash(tool, args), tool,
                           json.dumps(args), approver)
    R.suspend(rid, aid, {"stage": "gate"}, f"等 {approver}")
    return rid, aid


print("\n=== token 必须能被 inbound 的正则认出来 ===\n")

rid_a, aid_a = line("define_semantics",
                    {"asset": "northwind.orders", "key": "status"}, "wang@acme.com")
rid_b, aid_b = line("define_semantics",
                    {"asset": "northwind.customers", "key": "tier"}, "wang@acme.com")
tok_a, tok_b = MT.token_of(rid_a), MT.token_of(rid_b)

chk("token 由 run_id 确定性派生", MT.token_of(rid_a) == tok_a)
chk("两条线的 token 不同", tok_a != tok_b)
# run_id 长这样 `define_semantics-a1b2c3d4`：直接塞进主题正则根本不匹配，
# 所以线的对外标识必须与线的 id 分开。
chk("run_id 本身进不了主题正则",
    inbound.SUBJ_RE.search(f"[#{rid_a[:8]}]") is None, rid_a[:8])
chk("token 进得了主题正则",
    inbound.SUBJ_RE.search(f"标题 [#{tok_a}]").group(1) == tok_a)
chk("token 进得了 plus-address 正则",
    inbound.PLUS_RE.search(f"claw+ap-{tok_a}@acme.test").group(1) == tok_a)

print("\n=== 出站三条线索 ===\n")

chk("主题打标", MT.tag_subject("请确认口径", tok_a).endswith(f"[#{tok_a}]"))
chk("回信带原主题不会重复打标",
    MT.tag_subject(f"Re: 请确认口径 [#{tok_a}]", tok_a).count(tok_a) == 1)
chk("plus-address 只动 local part",
    MT.plus_address("claw@acme.test", tok_a) == f"claw+ap-{tok_a}@acme.test")
chk("已经带 + 的地址不再叠加",
    MT.plus_address(f"claw+ap-{tok_b}@acme.test", tok_a)
    == f"claw+ap-{tok_b}@acme.test")

MID_A = "a-orders-2026@acme.test"
MID_B = "b-customers-2026@acme.test"
chk("登记 A 的信", MT.record(MID_A, run_id=rid_a, approval_id=aid_a,
                            to_addr="wang@acme.com", subject="orders 口径",
                            kind="approval"))
chk("登记 B 的信", MT.record(MID_B, run_id=rid_b, approval_id=aid_b,
                            to_addr="wang@acme.com", subject="customers 口径",
                            kind="approval"))
chk("重复登记同一封不炸", MT.record(MID_A, run_id=rid_a, to_addr="wang@acme.com"))
chk("票能反查到它属于哪条线", MT.run_of_approval(aid_b) == rid_b)

print("\n=== 一个人两条线：回 B 不能动到 A ===\n")

# 王工同时挂着 orders 和 customers 两条。他回的是 customers 那封。
reply_b = {"From": "wang@acme.com", "To": "claw@acme.test",
           "Subject": "Re: customers 口径",
           "In-Reply-To": f"<{MID_B}>",
           "Message-ID": "<reply-b@acme.com>"}
got = MT.resolve_run(reply_b)
chk("第 1 层：In-Reply-To 绑回 B", got["run_id"] == rid_b, got["run_id"])
chk("层级与确定性如实标注", got["layer"] == 1 and got["certain"] is True)
chk("A 没有被牵连", R.get(rid_a)["status"] == "waiting_human")
chk("A 仍在等它自己那张票", R.get(rid_a)["waiting_on"] == aid_a)
chk("没有决定落库时两条都不可恢复", R.resumable() == [])

print("\n=== 另外两条线索各自单独成立 ===\n")

fwd = {"From": "wang@acme.com", "To": "claw@acme.test",
       "Subject": f"Fwd: customers 口径 [#{tok_b}]"}
got = MT.resolve_run(fwd)
chk("第 4 层：人手动转发丢了 header，主题 token 仍绑得回 B",
    got["run_id"] == rid_b and got["layer"] == 4, json.dumps(got, ensure_ascii=False))

plus = {"From": "wang@acme.com", "To": f"claw+ap-{tok_a}@acme.test",
        "Subject": "回复"}
got = MT.resolve_run(plus)
chk("第 3 层：plus-address 绑回 A",
    got["run_id"] == rid_a and got["layer"] == 3, json.dumps(got, ensure_ascii=False))

print("\n=== 没线索的时候不猜 ===\n")

blind = {"From": "wang@acme.com", "To": "claw@acme.test", "Subject": "同意"}
got = MT.resolve_run(blind)
chk("三条都落空 → run_id 为空", got["run_id"] == "")
chk("三条都落空 → 明确标不确定", got["certain"] is False)
chk("落到第 5 层（交给模型且必须带候选集）", got["layer"] == 5, str(got["layer"]))

ghost = {"From": "wang@acme.com", "To": "claw@acme.test",
         "Subject": "回复 [#0123456789ab]"}
got = MT.resolve_run(ghost)
chk("token 格式对但库里没有 → 不确定，且说明原因",
    got["run_id"] == "" and got["certain"] is False and "登记表" in got["via"],
    got["via"])

print("\n=== 归属不是授权 ===\n")

# 归属判错的代价必须停在「体验损失」，不能变成「授权事故」。
cls = inbound.classify_intent("我同意，用 DELIVERED")
chk("正文能被分类成 DECISION", cls["intent"] == "DECISION")
chk("但正文表态永远不作数，仍须点签名链接",
    inbound.decision_still_requires_click(cls) is True)
chk("没人点链接时 B 也不可恢复", R.resumable() == [])

admin.decide(aid_b, "approve", "wang@acme.com")
ready = R.resumable()
chk("点了 B 的链接之后，只有 B 可恢复",
    len(ready) == 1 and ready[0]["run_id"] == rid_b,
    str([r["run_id"] for r in ready]))

print("\n=== 出站邮件真的带上了这三条线索 ===\n")

os.environ["MAIL_FROM"] = "claw@acme.test"
from notify.email_channel import EmailNotifier

m = EmailNotifier()._build("wang@acme.com", "请确认口径", "正文", "<p>正文</p>",
                           run_id=rid_a)
chk("自己签了 Message-ID", bool(m["Message-ID"]))
chk("主题带 token", f"[#{tok_a}]" in m["Subject"], m["Subject"])
chk("Reply-To 带 plus-address", m["Reply-To"] == f"claw+ap-{tok_a}@acme.test",
    str(m["Reply-To"]))
chk("From 不动（改它会打乱 SPF/DKIM 对齐）", m["From"] == "claw@acme.test")

plain = EmailNotifier()._build("wang@acme.com", "周报", "正文", "<p>正文</p>")
chk("没有 run_id 时不硬造 token", "[#" not in plain["Subject"])
chk("没有 run_id 时不设 Reply-To", plain["Reply-To"] is None)

print("\n=== 归属线索不能改变动作指纹 ===\n")

# 手动模式扣信时，letter 会被算成动作指纹。run_id 若混进去，同一封信
# 会因为线不同变成两张票（人在信里看不出任何差别），存量待批票也当场失配。
letter = {"to": "wang@acme.com", "subject": "口径", "body": "正文"}
chk("同一封信的指纹与 run_id 无关",
    action_hash("send_notice", letter)
    == action_hash("send_notice", dict(letter)))
chk("run_id 混进 letter 会改变指纹（所以它必须留在 args 里、不进哈希）",
    action_hash("send_notice", letter)
    != action_hash("send_notice", dict(letter, run_id=rid_a)))

_src = pathlib.Path(ROOT, "services", "notify", "__init__.py").read_text(encoding="utf-8")
chk("HoldNotifier 用不含 run_id 的 letter 算指纹",
    'st.action_hash("send_notice", letter)' in _src)
chk("但存进 args 的是含 run_id 的那份",
    "json.dumps(stored, ensure_ascii=False)" in _src)

print("\n=== 这条线发过哪些信（排查归属用）===\n")
chk("能列出一条线的全部出站信",
    [t["message_id"] for t in MT.threads_of(rid_a)] == [MID_A],
    str(MT.threads_of(rid_a)))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
