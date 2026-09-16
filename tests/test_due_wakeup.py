"""到点唤醒（checklist I03.11）。

**时间本身是一种唤醒条件。** 在这之前能推进一条线的只有「有人批了」、
「清洗轮开了」、「WIP 降下来了」——「已批准，等今晚 01:00 的窗口」无处表达：
票一批下来 monitor 立刻就把线拉起来，等于窗口外执行。

这里验两件事，缺一不可：
  · 没到点 → 不出现在任何可推进列表里（`resumable` / `retryable` / monitor 输出）
  · 到点了 → 出现，且 monitor 那一行是机器能直接吃的 continuation

判断在 `runs` 里做（查询侧），不在 monitor 里做 —— 所以 `resume_all()`
那条老路也一样守着窗口。不连 Docker：自带临时 SQLite 治理库。
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(tempfile.mkdtemp(), "due.db")
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]

from datasteward_gate.approvals import open_store          # noqa: E402
import runs                                                # noqa: E402

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def monitor() -> list:
    """跑真正的 monitor 脚本，拿它逐字节的输出。"""
    env = dict(os.environ, DATASTEWARD_DB=DB)
    env.pop("DATASTEWARD_DSN", None)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "resumable.py")],
                       capture_output=True, text=True, env=env)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def kinds_of(lines, run_id):
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("run_id") == run_id:
            out.append(d.get("kind"))
    return out


print("\n=== 到点唤醒（I03.11）===\n")

with open_store(init_schema=True):
    pass

HOUR = 3600
LATER = time.time() + 6 * HOUR
PAST = time.time() - 60

# ---- 1. 已批准 + 等窗口：到点前一步都不能动 ----
rid = runs.create("ingest_table", {"source": "northwind", "table": "shippers"})
with open_store() as st:
    aid, _ = st.request(rid, "hash-win", "ingest_table",
                        '{"source":"northwind","table":"shippers"}', "owner")
runs.suspend(rid, aid, {"stage": "gate"}, "等 owner 批")
with open_store() as st:
    st.decide(aid, "approve", "owner", token_jti="jti-win")

check("批了就能恢复（排期之前的基线）", rid in [r["run_id"] for r in runs.resumable()])

runs.schedule(rid, LATER, "已批准，等今晚的执行窗口")
check("排期之后不算可恢复", rid not in [r["run_id"] for r in runs.resumable()])
check("排期之后 monitor 不报它", not kinds_of(monitor(), rid),
      "、".join(kinds_of(monitor(), rid)))

# 到点：出现，而且带的是「照抄就能调」的参数
runs.schedule(rid, PAST, "窗口到了")
check("到点后重新算可恢复", rid in [r["run_id"] for r in runs.resumable()])
lines = monitor()
check("到点后 monitor 报出来，kind=approved", kinds_of(lines, rid) == ["approved"],
      "、".join(kinds_of(lines, rid)))
row = next((json.loads(l) for l in lines if json.loads(l).get("run_id") == rid), {})
check("那一行带工具名和参数（模型照抄即可）",
      row.get("tool") == "ingest_table"
      and (row.get("args") or {}).get("table") == "shippers", json.dumps(row)[:100])

# ---- 2. 没在等审批、只等时刻的线 → kind=due，不是 blocked_by_wip ----
rid2 = runs.create("check_freshness", {"source": "northwind", "table": "orders"})
runs.suspend(rid2, None, {"stage": "recheck"}, "三天后再看一眼")
check("不排期时按老行为算 WIP 重试",
      kinds_of(monitor(), rid2) == ["blocked_by_wip"], "、".join(kinds_of(monitor(), rid2)))

runs.schedule(rid2, LATER, "三天后再看")
check("排期未到点：既不在 retryable 也不在 due",
      rid2 not in [r["run_id"] for r in runs.retryable()]
      and rid2 not in [r["run_id"] for r in runs.due()])
check("排期未到点：monitor 一行都不报", not kinds_of(monitor(), rid2))

runs.schedule(rid2, PAST, "到点了")
check("到点后进 due()，不进 retryable()",
      rid2 in [r["run_id"] for r in runs.due()]
      and rid2 not in [r["run_id"] for r in runs.retryable()])
check("monitor 把它报成 kind=due", kinds_of(monitor(), rid2) == ["due"],
      "、".join(kinds_of(monitor(), rid2)))

# ---- 3. 同一格内输出逐字节稳定（抖一下就等于每分钟点一次模型）----
a, b = monitor(), monitor()
check("monitor 输出逐字节稳定", a == b)

# ---- 4. 拉起来 / 收口之后排期要清掉，否则每个整点再叫一次 ----
runs.bump_resumed(rid2)
check("恢复后排期被清掉", runs.get(rid2).get("next_action_at") in (None, ""))
runs.schedule(rid2, PAST, "再排一次")
runs.finish(rid2, "done", "做完了")
check("收口后排期被清掉", runs.get(rid2).get("next_action_at") in (None, ""))

# ---- 5. 脏值不能让一条线永远醒不过来 ----
check("next_action_at 是脏值时算「到点」，宁可多醒一次",
      runs.due_now({"next_action_at": "那天晚上"}))
check("没排期一律算到点", runs.due_now({}))

# ---- 6. 状态查询要说清楚「还没到点」 ----
runs.schedule(rid, LATER, "等窗口")
import importlib.util                                      # noqa: E402
PLUGIN = os.path.join(ROOT, ".hermes", "plugins", "data-steward")
spec = importlib.util.spec_from_file_location(
    "ds_plugin_due", os.path.join(PLUGIN, "__init__.py"),
    submodule_search_locations=[PLUGIN])
mod = importlib.util.module_from_spec(spec)
sys.modules["ds_plugin_due"] = mod
spec.loader.exec_module(mod)


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_hook(self, *a):
        pass

    def register_system_prompt_section(self, *a):
        pass

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw


ctx = Ctx()
mod.register(ctx)
out = ctx.tools["get_task_state"]["handler"]({"about": "shippers"})
check("状态里报成 approved_waiting_window", "approved_waiting_window" in out, out[:80])
check("状态里写明到点时刻和「还没到」", "到点再动" in out and "还没到" in out)

print(f"\n{len(ok)} 通过 / {len(bad)} 失败")
if bad:
    print("失败项：" + "、".join(bad))
sys.exit(1 if bad else 0)
