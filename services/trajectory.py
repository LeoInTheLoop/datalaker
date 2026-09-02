"""轨迹记录（结构借鉴同级项目 skillEval 的 contracts/trajectory.py）。

**为什么需要**：断言只能回答「机制对不对」，回答不了「Agent 做对了没有」——
它是不是绕了远路、该问人的地方问了没有、被拒后有没有换个方式硬试。

与 skillEval 的差异在**事件类型**：那边是通用 agent 的 tool_call / file_write，
这里是数据治理领域的门禁、挂起、人回复、升级、放弃。

两份记录各司其职：
- `events` 表   运行时状态，用于崩溃恢复（readme 4.1）
- `trajectory.jsonl`  可观察事件投影，用于审计与评估
"""
import json
import os
import pathlib
import time
import uuid

EVENT_TYPES = (
    "tool_call",        # 调用了工具
    "gate_block",       # 被门禁拦下（PENDING / DENIED / L4 / BUDGET / WIP / NOT_DECLARED）
    "notify_sent",      # 发出通知
    "inbound",          # 收到人的回复
    "human_decision",   # 人点击了批准/拒绝
    "escalation",       # 超时升级
    "abandoned",        # 优雅放弃
    "question",         # 向人提问
    "memory_hit",       # 命中已沉淀的知识（问过的不再问）
)


class Trajectory:
    """一次运行的轨迹。

    用法：
        traj = Trajectory("discovery-olist")
        traj.tool_call("list_source_tables", {"source": "olist_raw"}, "9 张表")
        traj.gate_block("ingest_table", {...}, "PENDING_APPROVAL", approval_id=...)
        traj.dump()
    """

    def __init__(self, case_id: str, run_id: str | None = None,
                 out_dir: str = "outputs"):
        self.case_id = case_id
        self.run_id = run_id or f"{case_id}.{uuid.uuid4().hex[:8]}"
        self.out_dir = out_dir
        self.events: list[dict] = []
        self.started_ms = int(time.time() * 1000)
        self.status = "running"
        self.error_kind = None

    # ------------------------------------------------------------ 记录
    def _add(self, event_type: str, name: str, **kw) -> dict:
        assert event_type in EVENT_TYPES, f"未知事件类型 {event_type}"
        ev = {
            "step_index": len(self.events) + 1,
            "event_type": event_type,
            "name": name,
            "tool_name": kw.pop("tool_name", None),
            "arguments": kw.pop("arguments", None),
            "result_summary": kw.pop("result_summary", None),
            "status": kw.pop("status", "ok"),
            "timestamp_ms": int(time.time() * 1000),
            "metadata": kw,
        }
        self.events.append(ev)
        return ev

    def tool_call(self, tool, args=None, summary=None, status="ok", **kw):
        return self._add("tool_call", tool, tool_name=tool, arguments=args,
                         result_summary=summary, status=status, **kw)

    def gate_block(self, tool, args, reason, **kw):
        """被门禁拦下。**这是最该被记录的一类**——
        它说明 Agent 想做什么、以及为什么没做成。"""
        return self._add("gate_block", reason.split("]")[0].strip("[") or "BLOCK",
                         tool_name=tool, arguments=args,
                         result_summary=reason[:160], status="blocked", **kw)

    def question(self, asset, text, options=None, approver=None, **kw):
        return self._add("question", asset, result_summary=text[:160],
                         approver=approver, options=options, **kw)

    def notify_sent(self, kind, to, channel="email", **kw):
        return self._add("notify_sent", kind, result_summary=f"{channel} -> {to}",
                         to=to, channel=channel, **kw)

    def inbound(self, from_addr, intent, layer=None, accepted=True, **kw):
        return self._add("inbound", intent, result_summary=f"from {from_addr}",
                         status="ok" if accepted else "rejected",
                         from_addr=from_addr, attribution_layer=layer, **kw)

    def human_decision(self, decision, approver, approval_id=None, **kw):
        return self._add("human_decision", decision, result_summary=f"by {approver}",
                         approver=approver, approval_id=approval_id, **kw)

    def escalation(self, level, approver, age_days, **kw):
        return self._add("escalation", f"L{level}", result_summary=f"{approver} 等待 {age_days:.1f} 天",
                         level=level, approver=approver, **kw)

    def abandoned(self, approver, tool, **kw):
        return self._add("abandoned", tool, result_summary=f"{approver} 超时未响应",
                         status="abandoned", approver=approver, **kw)

    def memory_hit(self, asset, key, value, **kw):
        """命中已沉淀的知识——**没问就是最好的问**（readme 5.7）。"""
        return self._add("memory_hit", f"{asset}.{key}",
                         result_summary=str(value)[:120], **kw)

    # ------------------------------------------------------------ 输出
    def finish(self, status="success", error_kind=None):
        self.status = status
        self.error_kind = error_kind
        return self

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "status": self.status,
            "error_kind": self.error_kind,
            "duration_ms": int(time.time() * 1000) - self.started_ms,
            "event_count": len(self.events),
            "events": self.events,
        }

    def dump(self, path: str | None = None) -> str:
        p = pathlib.Path(path or os.path.join(self.out_dir, f"{self.run_id}",
                                              "trajectory.jsonl"))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(self.to_dict(), ensure_ascii=False) + "\n")
        return str(p)

    # ------------------------------------------------------------ 查询
    def tools_used(self) -> list:
        return [e["tool_name"] for e in self.events
                if e["event_type"] == "tool_call" and e["tool_name"]]

    def blocked_tools(self) -> list:
        return [e["tool_name"] for e in self.events if e["event_type"] == "gate_block"]

    def counts(self) -> dict:
        c = {}
        for e in self.events:
            c[e["event_type"]] = c.get(e["event_type"], 0) + 1
        return c
