"""硬 turn：测试窗口一定会停（CLAUDE.md「Gate 定边界，max_turn 定窗口」）。

产品要求是「一直推进」，所以一次演练不会自己收口。要测「面对这个现状，
它下一步干什么」，就得有个外部硬停：数够 N 次就不让再动手，然后人去看
这 N 步做了什么。

四件事：
  · 没设 `CLAW_MAX_TURN` 时行为完全不变（生产默认不限）
  · 一次 turn = 一次进 gate 的工具调用，**被 block 的也算**
  · 到线之后一律不放行，连读元数据也不放
  · **数不出来就停** —— 计数坏了不能变成「随便跑」

不连 Docker：自带临时 SQLite 治理库。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DATASTEWARD_DB"] = os.path.join(tempfile.mkdtemp(), "turn.db")
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_TOKEN_SECRET"] = "test-secret"
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]

from datasteward_gate import gate                            # noqa: E402
from datasteward_gate.approvals import open_store, rows_of   # noqa: E402

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def call(tool="get_table_metadata", args=None):
    return gate(tool, args or {"source": "acme", "table": "orders"}, task_id="t1")


def blocked(r):
    return isinstance(r, dict) and r.get("action") == "block"


def code(r):
    m = str((r or {}).get("message") or "")
    return m[1:m.index("]")] if m.startswith("[") and "]" in m else ""


def turns(scope="win-1"):
    with open_store(readonly=True, init_schema=False) as st:
        return rows_of(st, "SELECT count(*) FROM events WHERE run_id={0} AND kind={0}",
                       (scope, "TURN"))[0][0]


print("\n=== 硬 turn（测试窗口）===\n")

with open_store(init_schema=True):
    pass

# ---- 1. 不设就不限：生产默认行为一个字不变 ----
os.environ.pop("CLAW_MAX_TURN", None)
os.environ.pop("CLAW_TURN_SCOPE", None)
for _ in range(5):
    r = call()
check("没设 CLAW_MAX_TURN 时 L0 工具照常放行", not blocked(r), code(r))
check("没设时一条 turn 记录都不写", turns("default") == 0)

# ---- 2. 设了就数，到线就停 ----
os.environ["CLAW_MAX_TURN"] = "3"
os.environ["CLAW_TURN_SCOPE"] = "win-1"
results = [call() for _ in range(5)]
check("前 3 次放行", not any(blocked(r) for r in results[:3]),
      "；".join(code(r) for r in results[:3]))
check("第 4 次起被 TURN_LIMIT 挡住",
      all(blocked(r) and code(r) == "TURN_LIMIT" for r in results[3:]),
      "；".join(code(r) for r in results[3:]))
check("只记了 3 次 turn（到线后不再累加）", turns() == 3, str(turns()))
check("拒绝消息说清楚这是外部停止点，不要重试",
      "不是你做错了什么" in str(results[4]["message"])
      and "不要重试" in str(results[4]["message"]))

# ---- 3. 被别的规则 block 的那一次也算一个 turn ----
os.environ["CLAW_TURN_SCOPE"] = "win-2"
os.environ["CLAW_MAX_TURN"] = "2"
r1 = call("drop_source_table", {"source": "acme", "table": "orders"})   # L4，必被拒
check("L4 动作被拒", blocked(r1) and code(r1) == "L4", code(r1))
r2 = call()
check("被拒的那次也占了一个 turn", not blocked(r2) and turns("win-2") == 2, str(turns("win-2")))
r3 = call()
check("于是第 3 次就到线了", blocked(r3) and code(r3) == "TURN_LIMIT", code(r3))

# ---- 4. 到线之后连读元数据都不放行 ----
check("到线后 L0 只读工具同样被挡",
      blocked(call("describe_asset", {"source": "acme", "table": "orders"})))

# ---- 5. 数不出来就停，不能变成「随便跑」 ----
os.environ["CLAW_TURN_SCOPE"] = "win-3"
os.environ["DATASTEWARD_DB"] = "/nonexistent-dir/nope.db"
import datasteward_gate                                      # noqa: E402
datasteward_gate._local.__dict__.pop("store", None)
r = call()
check("计数不可用时按停止处理（fail-closed）",
      blocked(r) and code(r) == "TURN_LIMIT", code(r))

print(f"\n{len(ok)} 通过 / {len(bad)} 失败")
if bad:
    print("失败项：" + "、".join(bad))
sys.exit(1 if bad else 0)
