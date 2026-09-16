"""停止点判定（readme 5.2）—— 纯函数，控制流由调用方决定。

**判据只有一条**：下一步会产生「无法无损撤销的后果」，
或需要「业务知识而非技术知识」的判断 —— 停。

停止点绑在**信息边界**上，不绑在时间上：
Agent 靠自己已经拿不到新信息的那一刻，就是该交阶段成果的那一刻。
这与 `docs/handoff/README.md` 里「阶段边界 = 信息边界 = 停止点」是同一条。

它是纯函数（4.5 三层执行形态的第一层）：给定上下文返回该不该停，
不查库、不发信、不改状态。因此可以被 Pipeline 和 Agent loop 共用。
"""
from __future__ import annotations

# 每个停止点：到达条件 + 交付什么 + 问什么。
# **新增停止点 = 加一条数据**，不改判定逻辑 —— 与 policy.py 是同一种写法。
STOP_POINTS = [
    {
        "id": 1, "name": "源清单发现完成",
        "deliverable": "源系统 / 表清单 + 行数 + 更新频率",
        "question": "哪些表优先做？",
        "reason": "接哪张表是业务优先级，不是技术判断",
        "needs": ("tables_discovered", "priority_confirmed"),
        "reached": lambda c: bool(c.get("tables_discovered")) and not c.get("priority_confirmed"),
    },
    {
        "id": 2, "name": "bronze 落地 + profiling 完成",
        "deliverable": "数据质量报告",
        "question": "这些脏数据怎么处理？",
        "reason": "「这个空值合不合法」只有业务知道",
        "needs": ("bronze_tables", "dq_findings", "cleaning_confirmed"),
        "reached": lambda c: (bool(c.get("bronze_tables")) and bool(c.get("dq_findings"))
                              and not c.get("cleaning_confirmed")),
    },
    {
        "id": 3, "name": "清洗规则应用于首批",
        "deliverable": "前后对比样例 100 行",
        "question": "洗成这样对吗？",
        "reason": "清洗改变了数据含义，必须由人复核样例",
        "needs": ("cleaning_applied", "sample_reviewed"),
        "reached": lambda c: bool(c.get("cleaning_applied")) and not c.get("sample_reviewed"),
    },
    {
        "id": 4, "name": "silver 达标待发布",
        "deliverable": "gold 表定义 + 血缘图",
        "question": "批准发布？",
        "reason": "发布是无法无损撤销的 —— 下游一旦引用就改不回来",
        "needs": ("silver_ready", "publish_approved"),
        "reached": lambda c: bool(c.get("silver_ready")) and not c.get("publish_approved"),
    },
    {
        "id": 5, "name": "权限收敛方案生成",
        "deliverable": "建议名单 + 依据",
        "question": "确认这份名单？",
        "reason": "改权限影响真人能不能干活，永远不自动执行",
        "needs": ("permission_proposal", "permission_approved"),
        "reached": lambda c: bool(c.get("permission_proposal")) and not c.get("permission_approved"),
    },
]

# 与停止点无关、但同样必须停的两种情况。
# 它们不是「阶段成果」而是「走不下去了」——分开报，别混进同一个概念。
BLOCKERS = {
    "dq_exhausted": ("修复已用满 3 轮仍未达标",
                     "这是业务规则问题不是技术问题，继续循环只是烧钱（5.3）"),
    "schema_drift": ("源表 schema 变了",
                     "新增字段的业务含义属于业务知识，Agent 不该自行决定怎么用"),
    "unknown_owner": ("找不到这张表的负责人",
                      "没有人能确认口径时，继续做出来的东西不可信"),
}


# 三态，别退成两态：
#   缺键 / 假值 = **查过了，没有这回事**（没人确认过优先级、还没落 bronze）
#   显式 None   = **这次查不出来**（Trino 连不上，不知道 silver 有没有）
# 「查不出来」绝不能算成「没到停止点」—— 那正是「兜底值和一切正常同形」，
# 这个项目已经为它翻过六次车。组装 ctx 的那一方负责区分这两种，
# 判定这一侧只认它写下来的。
def unevaluable(ctx: dict) -> list:
    """哪些停止点这次**判不了**：它要读的事实里有 `None`（查不出来）。

    调用方要么把事实补上，要么如实说这条判不了 —— 不许当成「没到」。
    """
    out = []
    for sp in STOP_POINTS:
        unknown = [k for k in sp.get("needs", ()) if ctx.get(k, False) is None]
        if unknown:
            out.append({"id": sp["id"], "name": sp["name"], "unknown": unknown})
    return out


def next_stop(ctx: dict) -> dict | None:
    """返回当前该停在哪个停止点；没到任何一个则返回 None。

    ctx 由调用方组装 —— 纯函数不去猜状态存在哪。约定的键：
        tables_discovered / priority_confirmed
        bronze_tables / dq_findings / cleaning_confirmed
        cleaning_applied / sample_reviewed
        silver_ready / publish_approved
        permission_proposal / permission_approved
        blockers: list[str]   命中 BLOCKERS 的键
    """
    for b in (ctx.get("blockers") or []):
        if b in BLOCKERS:
            what, why = BLOCKERS[b]
            return {"kind": "blocker", "id": b, "name": what, "reason": why,
                    "deliverable": "已完成部分的阶段成果 + 卡点说明",
                    "question": what + "，请给出处理意见。"}
    for sp in STOP_POINTS:
        if any(ctx.get(k, False) is None for k in sp.get("needs", ())):
            continue          # 查不出来的不算「没到」，见 `unevaluable()`
        if sp["reached"](ctx):
            return {"kind": "stop_point",
                    **{k: v for k, v in sp.items() if k not in ("reached", "needs")}}
    return None


def should_continue(ctx: dict) -> bool:
    """没到停止点才继续。**默认是停，不是走**——拿不准时停下来问人更便宜。"""
    return next_stop(ctx) is None
