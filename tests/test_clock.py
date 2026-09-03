"""时间引擎：把「等 12 天」压缩成几秒，验证升级链路真的走到底。

**不改被测代码**：升级读的是 `now() - created_at`，把 created_at 往前推
与真的等了那么久完全等价。因此这里跑的是**生产路径**，不是测试分支。
"""
import json
import os
import subprocess
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins"),
                os.path.join(ROOT, "evals")]
os.environ.pop("DATASTEWARD_DSN", None)
DB = f"/tmp/dl_clock_{uuid.uuid4().hex[:8]}.db"
os.environ["DATASTEWARD_DB"] = DB
os.environ["NOTIFY_CHANNEL"] = "outbox"
os.environ["NOTIFY_OUTBOX"] = f"/tmp/dl_clock_outbox_{uuid.uuid4().hex[:6]}.jsonl"

import clock
from plugins.datasteward_gate.approvals import Store, action_hash

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def escalate():
    """跑真实的升级脚本 —— 不是在测试里重写一遍它的逻辑。"""
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "escalate.py")],
                       capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ})
    return r.stdout + r.stderr


st = Store(DB, readonly=False)
ids = {}
for who in ("owner:hr", "owner:fin", "owner:crm"):
    aid, _ = st.request(f"run-{who}", action_hash("ingest_table", {"t": who}),
                        "ingest_table", json.dumps({"t": who}), who)
    ids[who] = aid

print("\n=== 时钟推进 ===\n")

a0 = clock.ages(DB)
chk("三条都在等", len(a0) == 3, str(len(a0)))
chk("初始年龄接近 0", all(x["age_days"] < 0.01 for x in a0))

clock.advance(4, db=DB)
a1 = {x["id"]: x for x in clock.ages(DB)}
chk("推进 4 天后年龄正确", 3.9 < a1[ids["owner:hr"]]["age_days"] < 4.1,
    f"{a1[ids['owner:hr']]['age_days']:.2f}")

out = escalate()
a2 = {x["id"]: x for x in clock.ages(DB)}
chk("T+3d 触发第一级（催办）", a2[ids["owner:hr"]]["level"] >= 1,
    f"L{a2[ids['owner:hr']]['level']}")

print("\n=== 一路推到放弃 ===\n")

# 只让 hr 那条继续等；另外两条先批掉，验证「已决的不受时钟影响」
st.decide(ids["owner:fin"], "approve", "wang@acme.com")

# 从这里开始只推 hr 那条 —— 模拟「别人在正常节奏里，只有吴总监不回」
ONLY = [ids["owner:hr"]]
clock.advance(3, db=DB, only_ids=ONLY)      # hr 累计 7 天
escalate()
a3 = {x["id"]: x for x in clock.ages(DB)}
chk("T+6d 升到第二级", a3[ids["owner:hr"]]["level"] >= 2,
    f"L{a3[ids['owner:hr']]['level']}")

clock.advance(3, db=DB, only_ids=ONLY)      # hr 累计 10 天
escalate()
a4 = {x["id"]: x for x in clock.ages(DB)}
chk("T+9d 升到第三级", a4[ids["owner:hr"]]["level"] >= 3,
    f"L{a4[ids['owner:hr']]['level']}")

clock.advance(3, db=DB, only_ids=ONLY)      # hr 累计 13 天
escalate()
left = {x["id"] for x in clock.ages(DB)}
chk("T+12d 优雅放弃（退出活跃队列）", ids["owner:hr"] not in left)
row = st.db.execute("SELECT abandoned_at FROM approvals WHERE id=?",
                    (ids["owner:hr"],)).fetchone()
chk("放弃被标记而非删除", row and row[0] is not None)
ev = st.db.execute("SELECT count(*) FROM events WHERE run_id=? AND kind='ABANDONED'",
                   (ids["owner:hr"],)).fetchone()[0]
chk("event log 留痕", ev >= 1, str(ev))

print("\n=== 已决的历史不被篡改 ===\n")

d = st.db.execute("SELECT count(*) FROM decisions WHERE approval_id=?",
                  (ids["owner:fin"],)).fetchone()[0]
chk("已批准的决定还在", d == 1)
chk("已决事项不再出现在待办里", ids["owner:fin"] not in left)
chk("只推指定的那条，其余不受影响", ids["owner:crm"] in left)
crm_age = next(x["age_days"] for x in clock.ages(DB) if x["id"] == ids["owner:crm"])
chk("未被推进的那条年龄停在 4 天", 3.9 < crm_age < 4.1, f"{crm_age:.2f}")
chk("它也还没被升到放弃层级",
    next(x["level"] for x in clock.ages(DB) if x["id"] == ids["owner:crm"]) <= 1)

print("\n=== 时钟只有 eval 侧动得了 ===\n")

ro = Store(DB, readonly=True)
try:
    ro.decide(ids["owner:crm"], "approve", "x@acme.com")
    chk("Agent 侧连接写不了决定", False, "竟然写成功了")
except PermissionError:
    chk("Agent 侧连接写不了决定", True)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
