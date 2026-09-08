"""手动模式：**每个动作、每封对外的信，都要人先点头。**

    MANUAL_MODE=steward

两半各测一遍，外加三条「开了之后不能死锁」的边界：

  · 门禁半边：L1 提到 L2（照常建票、发信、可恢复），L0 不动，L4 仍然拒
  · 出口半边：`send_notice` 被扣住变成一张待批的票，审批信本身照发
  · 批准之后：callback 进程把扣住的那封信真的发出去

关掉时行为必须**逐字不变** —— 手动模式是加一道闸，不是换一套门禁。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services")]

DB = "/tmp/dl_manual.db"
for suf in ("", "-wal", "-shm"):
    if os.path.exists(DB + suf):
        os.remove(DB + suf)
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_TOKEN_SECRET"] = os.environ.get(
    "DATASTEWARD_TOKEN_SECRET", "test-secret")
OUTBOX = "/tmp/dl_manual_outbox.jsonl"
if os.path.exists(OUTBOX):
    os.remove(OUTBOX)
os.environ["NOTIFY_CHANNEL"] = "outbox"
os.environ["NOTIFY_OUTBOX"] = OUTBOX
os.environ["MAIL_STEWARD"] = "steward@acme.test"

import notify                                                # noqa: E402
from notify import outbox as outbox_mod                      # noqa: E402
from plugins.datasteward_gate import gate, store             # noqa: E402
from plugins.datasteward_gate.approvals import action_hash, open_store  # noqa: E402
from plugins.datasteward_gate.policy import Level, effective, lookup    # noqa: E402

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def manual(on, who="steward"):
    if on:
        os.environ["MANUAL_MODE"] = who
    else:
        os.environ.pop("MANUAL_MODE", None)


def mails():
    return outbox_mod.read(OUTBOX)


print("\n=== 关掉时行为不变（手动模式是加闸，不是换门禁） ===\n")
manual(False)
check("L1 仍然是 L1", effective("profile_table") == lookup("profile_table"))
check("L0 仍然是 L0", effective("get_table_metadata")[0] == Level.L0)
r = gate("profile_table", {"source": "acme", "table": "t"}, "run-off")
check("L1 工具照常放行（不发审批）", r is None, str(r)[:60])

print("\n=== 开启后：每个动作都要 steward 点头 ===\n")
manual(True)
lvl, who = effective("profile_table")
check("L1 提到 L2 且审批人是 steward", lvl == Level.L2 and who == "steward",
      f"{lvl.name}/{who}")
check("L0 不提级（否则模型连发审批那一步都走不到）",
      effective("tool_search")[0] == Level.L0)
check("L3 保留原审批人（手动模式加闸，不改谁拍板）",
      effective("publish_gold") == lookup("publish_gold"),
      str(effective("publish_gold")))
check("L4 仍然是 L4", effective("drop_source_table")[0] == Level.L4)

args = {"source": "acme", "table": "orders"}
r = gate("profile_table", args, "run-manual")
check("L1 动作被挡下", r is not None and r.get("action") == "block",
      (r or {}).get("message", "")[:48])
check("挡下的理由是等审批", "PENDING_APPROVAL" in (r or {}).get("message", ""))
aid = store().pending(action_hash("profile_table", args), "run-manual")
check("审批票落库，审批人是 steward", aid is not None)
if aid:
    row = open_store(readonly=True).db.execute(
        "SELECT approver FROM approvals WHERE id=?", (aid,)).fetchone()
    check("票上写的就是 steward", row and row[0] == "steward", str(row))

# 等后台发信线程
for _ in range(40):
    if any(m.get("kind") == "approval" and m.get("tool") == "profile_table"
           for m in mails()):
        break
    time.sleep(0.1)
check("审批信发给了 steward",
      any(m.get("kind") == "approval" and m.get("to") == "steward@acme.test"
          for m in mails()), str([m.get("to") for m in mails()])[:60])

print("\n=== SQL 走的是另一条路，也必须被扣住 ===\n")
r = gate("sql_query", {"sql": "SELECT 1", "plane": "lake"}, "run-sql")
check("手动模式下轻量 SQL 也要批",
      r is not None and r.get("action") == "block"
      and "PENDING_APPROVAL" in r.get("message", ""), str(r)[:80])
check("轻量 SQL 发给 steward，不是去打扰 owner",
      "steward" in (r or {}).get("message", ""), (r or {}).get("message", "")[:70])
r = gate("sql_query", {"sql": "DROP TABLE x", "plane": "lake"}, "run-sql2")
check("非法 SQL 直接拒，不去打扰人（先过 AST 再要票）",
      r is not None and "SQL_REJECTED" in r.get("message", ""), str(r)[:60])

print("\n=== 提级不能把「唯一的出路」也挡住 ===\n")
# 静默期（出过阶段报告、人还没拍板）里只放 L1 以下的动作，而
# `propose_stage_decision` 正是打破静默的那条路。手动模式把它抬成 L2 之后，
# 若静默期照着提级后的级别判，这一轮就永远出不去 —— 表现是「Agent 不干活了」。
store().append_event("stage", "ROUND_CLOSED", "test")
r = gate("propose_stage_decision",
         {"summary": "bronze 轮结束", "recommend": "start_silver",
          "reason": "接得差不多了"}, "run-quiet")
msg = (r or {}).get("message", "")
check("静默期里提案照样走得到审批（不是被静默期挡死）",
      "ROUND_CLOSED" not in msg and "PENDING_APPROVAL" in msg, msg[:60])

print("\n=== 出站的信被扣住，等 steward 放行 ===\n")
n_before = len(mails())
res = notify.get().send_notice("wang@acme.test", "[数据管家] acme 已接入",
                               "能看到 12 张表，下一步先接哪几张？")
check("send_notice 没有真发出去", res.get("kind") == "held", str(res)[:70])
notices = [m for m in mails() if m.get("kind") == "notice"]
check("待发的信不在收件箱里", not notices, str(notices)[:60])
mail_aid = res.get("approval_id")
check("扣住的信变成一张待批的票", bool(mail_aid))
held = [m for m in mails() if m.get("kind") == "approval"
        and m.get("tool") == "send_notice"]
check("steward 收到审批信（且看得到正文）",
      bool(held) and "先接哪几张" in (held[-1].get("reason") or ""),
      str(held[-1].get("reason", ""))[:50] if held else "无")
check("审批信本身没有被扣（否则套娃死锁）",
      bool(held) and held[-1].get("to") == "steward@acme.test")

# 同一封信重复发不重复打扰
res2 = notify.get().send_notice("wang@acme.test", "[数据管家] acme 已接入",
                                "能看到 12 张表，下一步先接哪几张？")
check("同一封信不重复建票、不重复打扰",
      res2.get("approval_id") == mail_aid
      and len([m for m in mails() if m.get("kind") == "approval"
               and m.get("tool") == "send_notice"]) == len(held))

print("\n=== 人这一侧的进程不受闸门影响 ===\n")
check("hold=False 拿到的是原通道", notify.get(hold=False).name == "outbox")
check("hold=True 拿到的是闸门", notify.get().name.startswith("hold:"))

print("\n=== 批准之后，那封信才真的发出去 ===\n")
sys.path.insert(0, os.path.join(ROOT, "services"))
import approval_callback as cb                               # noqa: E402

with open_store(readonly=False, init_schema=False) as st:
    st.decide(mail_aid, "approve", "steward@acme.test", token_jti="jti-manual-1")
    cb._release_notice(st, {"aid": mail_aid, "d": "approve",
                            "who": "steward@acme.test"})
sent = [m for m in mails() if m.get("kind") == "notice"]
check("批准后信送到了原收件人",
      bool(sent) and sent[-1].get("to") == "wang@acme.test", str(sent)[:70])
kinds = [k for _, k, _ in store().events(mail_aid)]
check("扣住与放行都留痕", "NOTICE_HELD" in kinds and "NOTICE_SENT" in kinds,
      ",".join(kinds))

# 拒绝的那封：什么也不发，但查得到
res3 = notify.get().send_notice("zhou@acme.test", "[数据管家] 周报", "本周……")
drop_aid = res3["approval_id"]
n_notice = len([m for m in mails() if m.get("kind") == "notice"])
with open_store(readonly=False, init_schema=False) as st:
    st.decide(drop_aid, "deny", "steward@acme.test", token_jti="jti-manual-2")
    cb._release_notice(st, {"aid": drop_aid, "d": "deny",
                            "who": "steward@acme.test"})
check("被拒的信不会发出去",
      len([m for m in mails() if m.get("kind") == "notice"]) == n_notice)
check("被拒也留痕（不是丢失）",
      "NOTICE_DROPPED" in [k for _, k, _ in store().events(drop_aid)])

print("\n=== 运维脚本不许把「扣住」印成「已发送」 ===\n")
# 人正是靠那行字判断要不要去催。印「已发送至 X」而信其实还扣着，
# 是本项目第 1 号坑（假绿）搬到了运维输出里。
held_line = notify.sent_line({"kind": "held", "approval_id": "abcdef123"}, "wang@acme.test")
check("扣住时说扣住", "扣" in held_line and "已发送" not in held_line, held_line)
check("真发出去时照旧说已发送",
      notify.sent_line({"kind": "notice"}, "wang@acme.test") == "已发送至 wang@acme.test")

manual(False)
print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
