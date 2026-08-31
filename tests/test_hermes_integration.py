"""R1 第 1 步：在真实 Hermes 里验证 pre_tool_call 能 block。

这是全项目最后一个停留在纸面的核心假设。

跑法（需 Hermes 的 3.13 环境，见 docs/handoff/R1.md）：
    HERMES=/path/to/hermes-agent \
    $HERMES/.venv-h/bin/python tests/test_hermes_integration.py
"""
import os
import sys

DL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES = os.environ.get("HERMES", "")
if HERMES:
    sys.path.insert(0, HERMES)
# Hermes 自己也有一个顶层 `plugins` 包 —— 直接把我们的 plugins 目录入路径，
# 以 `datasteward_gate` 顶层名导入，避开同名包冲突。
sys.path.insert(0, os.path.join(DL, "plugins"))

os.environ.setdefault("DATASTEWARD_DB", "/tmp/dl_hermes.db")
for suf in ("", "-wal", "-shm"):
    f = os.environ["DATASTEWARD_DB"] + suf
    if os.path.exists(f):
        os.remove(f)

from hermes_cli import plugins as P
from datasteward_gate import gate

ok, bad = [], []
def check(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== Hermes 真实集成：pre_tool_call ===\n")

# 0. 契约常量核对
check("pre_tool_call 在 VALID_HOOKS 中", "pre_tool_call" in P.VALID_HOOKS)
check("pre_tool_call 被标记 fail-closed",
      "pre_tool_call" in P._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS)

# 1. 把治理 gate 注册进 Hermes 的 hook 表
mgr = P.get_plugin_manager()
mgr._hooks.setdefault("pre_tool_call", []).append(gate)
check("gate 已注册进 Hermes", gate in mgr._hooks["pre_tool_call"])

# 2. L0 工具：应放行（block_message 为 None）
msg, mod = P._dispatch_pre_tool_call_hooks("get_table_metadata", {"table": "orders"},
                                           task_id="run-hermes")
check("L0 工具被放行", msg is None, f"msg={msg}")

# 3. L3 工具无票据：Hermes 必须给出 block_message
msg, mod = P._dispatch_pre_tool_call_hooks("ingest_table", {"table": "FIN.monthly"},
                                           task_id="run-hermes")
check("L3 工具被 Hermes 拦截", msg is not None, (msg or "")[:52])
check("拦截消息含 PENDING_APPROVAL", "PENDING_APPROVAL" in (msg or ""))

# 4. L4：永不自动
msg4, _ = P._dispatch_pre_tool_call_hooks("drop_source_table", {"table": "orders"},
                                          task_id="run-hermes")
check("L4 被拦截", msg4 is not None and "L4" in msg4, (msg4 or "")[:40])

# 5. modify：SQL 无 LIMIT 时 Hermes 应回传改写后的 args
msg5, mod5 = P._dispatch_pre_tool_call_hooks("sql_query", {"sql": "SELECT * FROM orders"},
                                             task_id="run-hermes")
check("modify 被 Hermes 接受", mod5 is not None and "LIMIT 1000" in mod5.get("sql", ""),
      str(mod5)[:56])
check("modify 不产生 block", msg5 is None, f"msg={msg5}")

# 6. join 被拒
msg6, _ = P._dispatch_pre_tool_call_hooks(
    "sql_query", {"sql": "SELECT * FROM a JOIN b ON a.id=b.id"}, task_id="run-hermes")
check("源系统 join 被拦截", msg6 is not None and "join" in msg6.lower(), (msg6 or "")[:44])

# 7. gate 抛异常时，Hermes 侧仍拿到 block（fail closed 端到端）
import datasteward_gate as G
_orig = G.store
try:
    G.store = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    msg7, _ = P._dispatch_pre_tool_call_hooks("ingest_table", {"table": "Z"},
                                              task_id="run-hermes")
    check("组件异常 -> Hermes 侧仍 block", msg7 is not None and "GATE_ERROR" in msg7,
          (msg7 or "")[:44])
finally:
    G.store = _orig

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
