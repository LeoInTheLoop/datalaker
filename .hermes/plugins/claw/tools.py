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


_HANDLERS = {"list_source_tables": _list_source_tables}


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
