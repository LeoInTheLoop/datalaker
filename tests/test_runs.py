"""长时任务注册表：多条线并行挂起，各自恢复（readme 2 / 4.1）。

**这是本项目区别于普通 text2sql 的地方。** 传统 demo 是
`request → think → tool → answer`；这里一条线可能是
`发现 → 找 Owner → 发信 → 等 8 小时 → 回复 → 审批 → 等 1 天 → 恢复 → 执行`，
而且同时并行好几条，挂在不同的人身上。

断言的重点因此不是「能跑通一条」，而是：
  · 状态活得比进程久
  · N 条互不阻塞
  · 谁先被批准谁先被拉起
  · 一条崩了不影响另一条
"""
import json
import os
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
DB = f"/tmp/dl_runs_{uuid.uuid4().hex[:8]}.db"
os.environ["DATASTEWARD_DB"] = DB

import runs as R
from plugins.datasteward_gate.approvals import Store, action_hash

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


admin = Store(DB, readonly=False)


def pend(run_id, tool, args, approver):
    aid, _ = admin.request(run_id, action_hash(tool, args), tool,
                           json.dumps(args), approver)
    return aid


print("\n=== 状态活得比进程久 ===\n")

r1 = R.create("ingest_table", {"source_id": "northwind", "table": "orders"},
              owner_role="owner:fin")
chk("创建即落库", R.get(r1)["status"] == "running")
a1 = pend(r1, "ingest_table", {"table": "orders"}, "owner:fin")
R.suspend(r1, a1, {"stage": "gate"}, "等王姐批 orders")
g = R.get(r1)
chk("挂起状态可读回", g["status"] == "waiting_human")
chk("记得在等谁的哪份审批", g["waiting_on"] == a1)
chk("checkpoint 落库", g["checkpoint"]["stage"] == "gate")
chk("挂起不是失败", g["status"] in R.STATUSES and g["status"] != "failed")

print("\n=== N 条并行，互不阻塞 ===\n")

lines = []
for i, (tbl, role) in enumerate((("products", "owner:fin"),
                                 ("customers", "owner:crm"),
                                 ("shippers", "owner:ops"),
                                 ("suppliers", "owner:crm"))):
    rid = R.create("ingest_table", {"source_id": "northwind", "table": tbl},
                   owner_role=role)
    aid = pend(rid, "ingest_table", {"table": tbl}, role)
    R.suspend(rid, aid, {"stage": "gate"}, f"等 {role} 批 {tbl}")
    lines.append((rid, aid, tbl, role))

chk("5 条同时挂起", len(R.by_status("waiting_human")) == 5,
    str(len(R.by_status("waiting_human"))))
chk("没人回信时一条也不可恢复", R.resumable() == [])

# 只批第 3 条（shippers）——它挂在另一个人身上
admin.decide(lines[2][1], "approve", "ops@acme.com")
ready = R.resumable()
chk("只有被批的那条可恢复", len(ready) == 1, str(len(ready)))
chk("可恢复的正是那条", ready[0]["run_id"] == lines[2][0], ready[0]["params"]["table"])
chk("其余四条仍在等，未被牵连",
    len(R.by_status("waiting_human")) == 5)

print("\n=== 谁先被批准，谁先被拉起 ===\n")

time.sleep(0.02)
admin.decide(lines[0][1], "approve", "wang@acme.com")     # products 第二个批
time.sleep(0.02)
admin.decide(lines[1][1], "deny", "crm@acme.com")          # customers 第三个（拒绝也算有决定）
order = [r["params"]["table"] for r in R.resumable()]
chk("恢复顺序 = 人的响应顺序", order == ["shippers", "products", "customers"], str(order))
chk("拒绝同样解除挂起（要走 abandoned 而不是永远等）",
    "customers" in order)

print("\n=== 一条崩了不影响另一条 ===\n")

calls = []


def flaky(run_id, params, checkpoint=None):
    calls.append(params["table"])
    if params["table"] == "products":
        raise RuntimeError("模拟执行器崩溃")
    R.finish(run_id, "done", "ok")
    return {"ok": True}


R.register("ingest_table", flaky)
out = R.resume_all()
chk("三条都被推了一步", len(out) == 3, str(len(out)))
chk("崩的那条标 failed",
    any(o["run_id"] == lines[0][0] and o["status"] == "failed" for o in out))
chk("其余两条正常完成",
    R.get(lines[2][0])["status"] == "done" and R.get(lines[1][0])["status"] == "done")
chk("每条都被单独调用（没有合批）", sorted(calls) == ["customers", "products", "shippers"],
    str(sorted(calls)))
chk("恢复次数被记账", R.get(lines[2][0])["resumed"] == 1)

print("\n=== 卡在谁那里（周报素材）===\n")

st = R.stuck()
chk("仍挂起的能列出来", {r["params"]["table"] for r in st} == {"orders", "suppliers"},
    str(sorted(r["params"]["table"] for r in st)))
chk("带角色，便于说明卡在谁那儿",
    {r["owner_role"] for r in st} == {"owner:fin", "owner:crm"})

s = R.summary()
chk("汇总口径自洽", s["total"] == 5 and s["waiting_human"] == 2, json.dumps(s))

print("\n=== 未注册的任务类型不静默吞掉 ===\n")

r9 = R.create("no_such_kind", {"x": 1})
a9 = pend(r9, "t", {"x": 1}, "owner")
R.suspend(r9, a9, {}, "")
admin.decide(a9, "approve", "x@acme.com")
chk("未注册类型如实报错", R.resume_one(r9)["status"] == "no_runner")
chk("缺失的 run 不崩", R.resume_one("nonexistent")["status"] == "missing")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
