"""Claw 的工具。

**参数化，不给自由 SQL**（readme 4.6）：每个工具签名固定，
SQL 由代码生成。工具签名是唯一能挂约束的地方 —— 参数校验、级别、
审批人、账本、指纹全挂在它上面，没有签名就没有挂载点。

这个模块被 Hermes 在发现阶段导入（因为 manifest 声明了 `provides_tools`），
所以同样保持 import 轻量：连库放进 handler。
"""
from typing import Any

_SCHEMAS = {
    "list_source_tables": {
        "name": "list_source_tables",
        "description": (
            "列出某个已接入源系统里的表，以及每张表的行数估算。"
            "这是发现阶段的第一步：先知道有哪些表，再决定问谁、接哪张。"
            "只读元数据，不碰数据内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "源系统 id，例如 northwind、olist_raw、acme",
                },
            },
            "required": ["source"],
        },
    },
}


def _list_source_tables(args: dict, **_: Any) -> str:
    src = str(args.get("source") or "").strip()
    if not src:
        return "错误：需要 source 参数（源系统 id）。"
    from . import _ensure_path
    _ensure_path()
    try:
        import data_tools
        r = data_tools.list_source_tables(src)
    except Exception as e:                                    # noqa: BLE001
        # 如实报错，不编造表清单 —— 拿不到就说拿不到
        return f"读取 {src} 的表清单失败：{type(e).__name__}: {e}"

    rows = r.get("tables") or []
    if not rows:
        return f"{src} 里没有可见的表（可能是权限，也可能确实是空的）。"
    lines = [f"{src} 共 {len(rows)} 张表："]
    for t in rows[:50]:
        n = t.get("approx_rows")
        est = "行数未知" if n is None or n < 0 else f"约 {n:,} 行"
        lines.append(f"  · {t['table']}（{est}）")
    if len(rows) > 50:
        lines.append(f"  …… 另有 {len(rows) - 50} 张未列出")
    return "\n".join(lines)


def _profile_table(args: dict, **_: Any) -> str:
    src = str(args.get("source") or "").strip()
    tbl = str(args.get("table") or "").strip()
    if not (src and tbl):
        return "错误：需要 source 与 table。"
    from . import _ensure_path
    _ensure_path()
    try:
        import data_tools
        d = data_tools.run_dq_check(src, tbl)
    except Exception as e:                                    # noqa: BLE001
        return f"画像 {src}.{tbl} 失败：{type(e).__name__}: {e}"
    if "error" in d:
        return f"画像 {src}.{tbl} 失败：{d['error']}"
    fs = d.get("findings") or []
    head = (f"{tbl}：采样 {d.get('sampled_rows', 0):,} 行"
            f"（{d.get('sampling', '?')}），{len(fs)} 项发现")
    if not fs:
        return head + "。质量门槛全部达标。"
    lines = [head + "："]
    for f in fs[:12]:
        lines.append(f"  · {f['column']} / {f['issue']}（{f['severity']}）"
                     f" {f.get('detail', '')}")
    return "\n".join(lines)


def _ingest_table(args: dict, **_: Any) -> str:
    """接入一张表到 bronze。**L3 —— 门禁会先拦下它。**

    这个 handler 只有在门禁放行后才会被调用；被拦时 Hermes 根本不会走到这里。
    所以这里不需要（也不该）自己判断有没有审批 —— 那样限制就散进业务代码了。
    """
    src = str(args.get("source") or "").strip()
    tbl = str(args.get("table") or "").strip()
    if not (src and tbl):
        return "错误：需要 source 与 table。"
    from . import _ensure_path
    _ensure_path()
    try:
        import sync
        r = sync.sync_table(src, tbl)
    except Exception as e:                                    # noqa: BLE001
        return f"接入 {src}.{tbl} 失败：{type(e).__name__}: {str(e)[:200]}"
    return (f"{src}.{tbl} 已落 {r['bronze_table']}："
            f"{r['row_count']:,} 行，策略 {r['strategy']}。")


_SCHEMAS.update({
    "profile_table": {
        "name": "profile_table",
        "description": (
            "对一张源表做画像并按质量门槛判定，返回**结论**而不是数据行。"
            "源上只采样，全量检查落 bronze 后在 lake 里做。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源系统 id"},
                "table": {"type": "string", "description": "表名"},
            },
            "required": ["source", "table"],
        },
    },
    "ingest_table": {
        "name": "ingest_table",
        "description": (
            "把一张源表接入数据湖的 bronze 层。**这是需要负责人审批的动作**——"
            "调用后如果返回待审批，就说明已经替你发出了审批请求，"
            "不要重试，去做别的不受阻塞的事。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源系统 id"},
                "table": {"type": "string", "description": "表名"},
            },
            "required": ["source", "table"],
        },
    },
})

def _get_table_metadata(args: dict, **_: Any) -> str:
    src, tbl = str(args.get("source") or ""), str(args.get("table") or "")
    if not (src and tbl):
        return "错误：需要 source 与 table。"
    from . import _ensure_path
    _ensure_path()
    try:
        import data_tools
        m = data_tools.get_table_metadata(src, tbl)
    except Exception as e:                                    # noqa: BLE001
        return f"读取 {src}.{tbl} 结构失败：{type(e).__name__}: {e}"
    pk = "、".join(m.get("primary_key") or []) or "（无主键）"
    cols = "\n".join(f"  · {c['name']} {c['type']}"
                      + ("" if c.get("nullable", True) else " NOT NULL")
                      for c in m["columns"][:40])
    return f"{tbl} 主键：{pk}\n列（{len(m['columns'])} 个）：\n{cols}"


def _propose_cleaning(args: dict, **_: Any) -> str:
    """由 DQ 结论生成清洗提案。**只提案不执行**（L1）。"""
    src, tbl = str(args.get("source") or ""), str(args.get("table") or "")
    if not (src and tbl):
        return "错误：需要 source 与 table。"
    from . import _ensure_path
    _ensure_path()
    try:
        import clean, data_tools
        dq = data_tools.run_dq_check(src, tbl)
        plan = clean.propose(tbl, dq.get("findings") or [])
    except Exception as e:                                    # noqa: BLE001
        return f"生成 {src}.{tbl} 的清洗提案失败：{type(e).__name__}: {e}"

    def _fmt(items, label):
        if not items:
            return ""
        body = "\n".join(f"  · {i['column']} / {i['issue']} —— {i['why']}"
                          for i in items[:10])
        return f"\n{label}：\n{body}"

    return (plan["question"]
            + _fmt(plan["auto"], "可直接做（确定性，不改变任何值）")
            + _fmt(plan["propose"], "建议这样归一，需要你批准规则")
            + _fmt(plan["ask"], "**必须你给口径，我不会自己动**"))


def _record_finding(args: dict, **_: Any) -> str:
    """把一条发现记进整改台账（L1）。复发是累加不是新增条目。"""
    from . import _ensure_path
    _ensure_path()
    try:
        import ledger
        r = ledger.record(
            str(args.get("source_table") or ""),
            str(args.get("issue_type") or ""),
            field=args.get("field"),
            category=str(args.get("category") or "data"),
            observed_pattern=str(args.get("observed") or ""),
            rows_cleaned=int(args.get("rows") or 0))
    except Exception as e:                                    # noqa: BLE001
        return f"记台账失败：{type(e).__name__}: {e}"
    kind = "首次登记" if r["new"] else f"复发累加到第 {r['recurrences']} 次"
    return f"{r['rl_id']}：{kind}。复发次数本身就是推动源头整改最有力的证据。"


_SCHEMAS.update({
    "get_table_metadata": {
        "name": "get_table_metadata",
        "description": "看一张源表的列、类型、是否可空与主键。只读元数据。",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "源系统 id"},
            "table": {"type": "string", "description": "表名"}},
            "required": ["source", "table"]},
    },
    "propose_cleaning": {
        "name": "propose_cleaning",
        "description": (
            "对一张表给出清洗提案，分三档：可直接做的、建议归一但要人批准规则的、"
            "**必须问人给口径的**。只提案不执行 —— 补空值、改数量级、改日期"
            "这类永远不会自动做。"
        ),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "源系统 id"},
            "table": {"type": "string", "description": "表名"}},
            "required": ["source", "table"]},
    },
    "record_finding": {
        "name": "record_finding",
        "description": (
            "把一条数据或权限问题记进整改台账，用于反向推动源系统修复。"
            "同一缺陷再次出现是复发累加，不是新增条目。"
        ),
        "parameters": {"type": "object", "properties": {
            "source_table": {"type": "string", "description": "如 CRM.customers"},
            "issue_type": {"type": "string", "description": "如 格式不一致 / 过度授权"},
            "field": {"type": "string", "description": "字段名，可空"},
            "category": {"type": "string", "enum": ["data", "permission"]},
            "observed": {"type": "string", "description": "观察到的现象"},
            "rows": {"type": "integer", "description": "本次影响行数"}},
            "required": ["source_table", "issue_type"]},
    },
})

_HANDLERS = {
    "list_source_tables": _list_source_tables,
    "get_table_metadata": _get_table_metadata,
    "profile_table": _profile_table,
    "propose_cleaning": _propose_cleaning,
    "record_finding": _record_finding,
    "ingest_table": _ingest_table,
}


def register_tools(ctx) -> None:
    for name, fn in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="claw",
            schema=fn,
            handler=_HANDLERS[name],
            description=fn["description"],
            emoji="\U0001f5c3",  # card file box
        )
