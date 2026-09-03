"""Pipeline：接入一张表（readme 4.5 / 4.6 支柱四）。

**步骤固定，控制流由代码决定**——这是它与 Agent loop 的根本区别。
Pipeline 内部也调模型（assess 节点），但不会自己决定「要不要顺便看看另一张表」。

```
profile ──► assess ──► propose ──► gate ──┬─► PENDING：正常结束（不是挂起等待）
（纯函数）  （LLM）    （纯函数）           └─► APPROVED：apply ──► END
```

### 与 harness 的边界（readme 4.6 决策 19）

| 归 LangGraph | 归 harness |
|---|---|
| 节点流转、条件边 | 跨天的人工审批挂起 |
| 单次执行的 checkpoint | 审批状态的权威来源（approvals 表） |
| | 工具准入（pre_tool_call gate） |

**不使用 LangGraph 的 `interrupt`**：它为「同一 run 内暂停等输入」设计，
而我们的挂起是跨天、跨进程、带签名令牌的。
gate 返回 PENDING 后 Pipeline **正常结束**，进程可以死；
审批链接被点击后由 scheduler 重新拉起，从 checkpoint 续跑。
"""
import os
import sys
from typing import Any, Literal, TypedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services"))
sys.path.insert(0, os.path.join(ROOT, "plugins"))

from langgraph.graph import END, StateGraph

import data_tools


class State(TypedDict, total=False):
    run_id: str
    source_id: str
    watermark_col: str
    allow_schema_change: bool
    bronze: dict
    table: str
    profile: dict
    dq: dict
    proposal: dict
    gate: dict
    status: Literal["pending_approval", "denied", "ingested", "blocked"]
    message: str


# ---------------------------------------------------------------- 节点
def node_profile(s: State) -> State:
    """纯函数：源上采样画像。不调模型。"""
    prof = data_tools.profile_table(s["source_id"], s["table"])
    dq = data_tools.run_dq_check(s["source_id"], s["table"])
    return {"profile": prof, "dq": dq}


def node_assess(s: State) -> State:
    """判断发现的问题该怎么处理。

    这是 Pipeline 里**唯一需要模型**的一步。
    送进上下文的只有 DQ 结论，不含任何原始数据行（readme 13.1）。

    ponytail: 无模型端点时退化为规则判定——
    Pipeline 的其余步骤不因模型不可用而停摆（4.5 部分降级）。
    """
    findings = s["dq"].get("findings", [])
    high = [f for f in findings if f["severity"] == "high"]
    needs_human = [f for f in findings if f["issue"] in
                   ("high_null_rate", "constant_column")]
    return {"proposal": {
        "blocking": bool(high),
        "needs_human": [f["column"] for f in needs_human],
        "summary": (f"{len(findings)} 项发现，其中 {len(high)} 项阻断性"
                    if findings else "未发现质量问题"),
    }}


def node_propose(s: State) -> State:
    """纯函数：把结论组织成审批请求的载荷。"""
    p = s["proposal"]
    reason = p["summary"]
    if p["needs_human"]:
        reason += f"；以下列需你确认是否业务上合法：{', '.join(p['needs_human'])}"
    return {"proposal": {**p, "reason": reason}}


def node_gate(s: State) -> State:
    """调 gate。**Pipeline 不是安全边界**——节点里调工具照样要过门禁。"""
    from datasteward_gate import gate
    r = gate("ingest_table", {"table": s["table"], "source": s["source_id"]},
             s.get("run_id", "pipeline"))
    if r is None:
        return {"gate": {"allowed": True}}
    msg = r.get("message", "")
    if "PENDING_APPROVAL" in msg:
        return {"gate": {"allowed": False}, "status": "pending_approval",
                "message": msg}
    if "DENIED" in msg:
        return {"gate": {"allowed": False}, "status": "denied", "message": msg}
    return {"gate": {"allowed": False}, "status": "blocked", "message": msg}


def node_apply(s: State) -> State:
    """落 bronze —— **真写**，经 Trino 以 `claw` 身份操作 Iceberg。

    在此之前这个节点只是「记结果」，于是整条 Pipeline 跑完 lake 里什么都没有。
    接上 `sync.sync_table` 之后，`iceberg.bronze` 里能查到真实行数，
    「Agent 能不能把数据接进来」才第一次有了可验证的答案。

    写入失败**不假装成功**：状态标 `ingest_failed`，消息带原因。
    """
    import sync

    try:
        r = sync.sync_table(s["source_id"], s["table"],
                            watermark_col=s.get("watermark_col"),
                            allow_schema_change=s.get("allow_schema_change", False))
    except Exception as e:                                    # noqa: BLE001
        return {"status": "ingest_failed",
                "message": f"{s['table']} 写入 bronze 失败：{type(e).__name__}: {e}"}
    return {"status": "ingested", "bronze": r,
            "message": f"{s['table']} 已落 {r['bronze_table']}"
                       f"（{r['row_count']:,} 行，策略 {r['strategy']}）"}


def route_after_gate(s: State) -> str:
    """条件边：这正是「迟早会有分支」的地方（4.6 选型理由）。"""
    return "apply" if s.get("gate", {}).get("allowed") else END


# ---------------------------------------------------------------- 图
def build():
    g = StateGraph(State)
    g.add_node("profile", node_profile)
    g.add_node("assess", node_assess)
    g.add_node("propose", node_propose)
    g.add_node("gate", node_gate)
    g.add_node("apply", node_apply)
    g.set_entry_point("profile")
    g.add_edge("profile", "assess")
    g.add_edge("assess", "propose")
    g.add_edge("propose", "gate")
    g.add_conditional_edges("gate", route_after_gate, {"apply": "apply", END: END})
    g.add_edge("apply", END)
    return g


def run(source_id: str, table: str, run_id: str = "pipeline", checkpointer=None):
    """跑一次。返回终态——**不阻塞等待人工**。"""
    app = build().compile(checkpointer=checkpointer)
    cfg = {"configurable": {"thread_id": f"{run_id}:{source_id}:{table}"}}
    return app.invoke({"run_id": run_id, "source_id": source_id, "table": table},
                      config=cfg)


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "northwind"
    tbl = sys.argv[2] if len(sys.argv) > 2 else "orders"
    out = run(src, tbl)
    print(f"status : {out.get('status')}")
    print(f"message: {(out.get('message') or '')[:120]}")
    if out.get("dq"):
        print(f"DQ     : passed={out['dq']['passed']}, "
              f"{len(out['dq']['findings'])} 项发现")


# ---------------------------------------------------------------- 长时任务适配
def as_run(run_id: str, params: dict, checkpoint: dict | None = None) -> dict:
    """把这条 Pipeline 接进任务注册表（`services/runs.py`）。

    Pipeline 本身**不阻塞等待人工** —— 它跑到门禁就正常结束。
    这里负责把「结束的原因」翻译成 run 的状态：

        pending_approval → waiting_human（记下在等哪份审批）
        ingested         → done
        denied           → abandoned（不会就同一动作重复发起）
        其他             → failed

    审批落库后由 `runs.resume_one` 重新调进来，Pipeline 从头跑一遍：
    profiling 是幂等的，而 gate 这次会放行。**幂等比 checkpoint 便宜**。
    """
    import runs
    from datasteward_gate import store
    from datasteward_gate.approvals import action_hash

    src, tbl = params["source_id"], params["table"]
    s = run(src, tbl, run_id=run_id)
    status, msg = s.get("status", ""), s.get("message", "")

    if status == "pending_approval":
        aid = store().pending(action_hash("ingest_table",
                                          {"table": tbl, "source": src}), run_id)
        runs.suspend(run_id, aid, {"stage": "gate", "table": tbl}, msg)
    elif "WIP_LIMIT" in msg:
        # **被 WIP 挡回来不是失败**，是「别人待办太多，等等再说」。
        # 记成 failed 的话这条线就永远起不来了。
        runs.suspend(run_id, None, {"stage": "wip", "table": tbl}, msg)
    elif status == "ingested":
        runs.finish(run_id, "done", msg)
    elif status == "denied":
        runs.finish(run_id, "abandoned", msg)
    else:
        runs.finish(run_id, "failed", msg or status)
    return s


def _register():
    import runs
    runs.register("ingest_table", as_run)


try:                                     # 导入即注册，恢复器不必知道有哪些 Pipeline
    _register()
except Exception:                        # noqa: BLE001  —— 注册失败不该拖垮导入
    pass
