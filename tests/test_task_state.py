"""任务线状态查询（第一批 A5 的「查状态」那一半）。

**状态是确定性代码算的**：库里的 `runs.status` + `decisions` 里有没有决定
→ 一套统一词表。模型只读这份，不靠回忆。

这里验的是映射本身和「查不到就说查不到」，不验门禁 —— 那是 B 组的事。
不连 Docker：自带一个临时 SQLite 治理库。
"""
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, ".hermes", "plugins", "data-steward")

os.environ["DATASTEWARD_DB"] = os.path.join(tempfile.mkdtemp(), "task_state.db")
os.environ.pop("DATASTEWARD_DSN", None)
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]

from datasteward_gate.approvals import open_store          # noqa: E402
import runs                                                # noqa: E402

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "ds_plugin_state", os.path.join(PLUGIN, "__init__.py"),
        submodule_search_locations=[PLUGIN])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ds_plugin_state"] = mod
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
    return ctx.tools, mod


print("\n=== 任务线状态（A5）===\n")

with open_store(init_schema=True):
    pass
tools, mod = load_tool()
check("get_task_state 已注册", "get_task_state" in tools)
if "get_task_state" not in tools:
    sys.exit(1)
call = tools["get_task_state"]["handler"]

# 未声明的工具默认被拒 —— 所以新工具必须同步进策略表
from datasteward_gate.policy import POLICY                  # noqa: E402
check("已登记进策略表且是只读级别",
      POLICY.get("get_task_state", (None, None))[0] == 0,
      str(POLICY.get("get_task_state")))

check("没有任务线时如实说没有", "没有在办的任务线" in call({}))

# --- 等人批 ---
rid = runs.create("ingest_table", {"source": "northwind", "table": "shippers"})
with open_store() as st:
    aid, _ = st.request(rid, "hash-a", "ingest_table",
                        '{"source":"northwind","table":"shippers"}', "owner")
runs.suspend(rid, aid, {"stage": "gate", "tool": "ingest_table"}, "等 owner 批")
out = call({"about": "shippers"})
check("已申请未批 → pending_approval", "pending_approval" in out, out[:60])
check("报出等谁、从什么时候开始等", "owner" in out and "自 " in out)
check("提醒不要重复申请", "不要重复申请" in out)
check("Scope 不在这张表里，说清楚", "Scope 不在这张表里" in out)

# --- 批准了但还没被拉起来 ---
with open_store() as st:
    st.decide(aid, "approve", "owner", token_jti="jti-approve")
check("已批准未恢复 → approved_ready", "approved_ready" in call({"about": "shippers"}))

# --- 批准了但在等执行窗口 ---
runs.set_checkpoint(rid, {"stage": "window", "tool": "ingest_table"}, "等低峰窗口")
check("已批准等窗口 → approved_waiting_window",
      "approved_waiting_window" in call({"about": "shippers"}))

# --- 被否决 ---
rid_d = runs.create("ingest_table", {"source": "northwind", "table": "salaries"})
with open_store() as st:
    aid_d, _ = st.request(rid_d, "hash-d", "ingest_table",
                          '{"source":"northwind","table":"salaries"}', "owner")
    runs.suspend(rid_d, aid_d, {"stage": "gate"}, "")
    st.decide(aid_d, "deny", "owner", token_jti="jti-deny")
check("被否决 → denied", "denied" in call({"about": "salaries"}))

# --- 排队（没在等人） ---
rid_w = runs.create("ingest_table", {"source": "northwind", "table": "orders"})
runs.suspend(rid_w, None, {"stage": "wip"}, "并发上限")
check("被并发上限挡住 → waiting_capacity",
      "waiting_capacity" in call({"about": "orders"}))

# --- 完成与失败默认不出现在在办清单里 ---
rid_done = runs.create("profile_table", {"source": "northwind", "table": "products"})
runs.finish(rid_done, "done", "画像完成")
rid_fail = runs.create("profile_table", {"source": "northwind", "table": "regions"})
runs.finish(rid_fail, "failed", "源库超时")
live = call({})
check("默认只列在办的", "products" not in live and "regions" not in live)
allout = call({"include_done": True})
check("include_done 之后 done / failed 都在", "done" in allout and "failed" in allout)

check("指定不存在的 run_id 时如实说没有", "没有 run_id=" in call({"run_id": "nope-1"}))

# --- 映射不认得的状态时不能装作正常 ---
from importlib import import_module                         # noqa: E402
tmod = import_module("ds_plugin_state.tools")
check("未覆盖的库状态原样报出，不兜底成「正常」",
      "未归类状态" in tmod._task_state({"status": "zombie"}, None))

print(f"\n{len(ok)} 通过 / {len(bad)} 失败")
if bad:
    print("失败项：" + "、".join(bad))
sys.exit(1 if bad else 0)
