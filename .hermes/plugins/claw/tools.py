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

def _scan_permissions(args: dict, **_: Any) -> str:
    """扫源系统的权限现状。**只观测、只建议，绝不改**（铁律 4）。"""
    src = str(args.get("source") or "").strip()
    if not src:
        return "错误：需要 source。"
    from . import _ensure_path
    _ensure_path()
    try:
        import perm_discovery
        kind = str(args.get("kind") or "database")
        r = (perm_discovery.scan_saas(src) if kind == "saas"
             else perm_discovery.scan_database(src))
    except Exception as e:                                    # noqa: BLE001
        return f"扫描 {src} 权限失败：{type(e).__name__}: {str(e)[:160]}"
    fs = r.get("findings") or []
    if not fs:
        return f"{src}：没有发现明显的权限问题。{r.get('note', '')}"
    lines = [f"{src} 权限现状 {len(fs)} 项发现（只观测，未做任何变更）："]
    for f in fs[:12]:
        lines.append(f"  · [{f['severity']}] {f['subject']} —— {f['detail']}")
        if f.get("suggestion"):
            lines.append(f"      建议：{f['suggestion']}")
    lines.append(r.get("note", ""))
    return "\n".join(x for x in lines if x)


def _check_lake_quality(args: dict, **_: Any) -> str:
    """在 lake 上做全量检查。**这就是那个被挪进来的 join**（铁律 3）。"""
    from . import _ensure_path
    _ensure_path()
    tbl = str(args.get("bronze_table") or "").strip()
    pk = str(args.get("pk") or "").strip()
    if not tbl:
        return "错误：需要 bronze_table。"
    spec = {"pk": [{"table": tbl, "column": pk}] if pk else [], "fk": []}
    child_col = str(args.get("fk_column") or "")
    parent = str(args.get("fk_parent_table") or "")
    parent_col = str(args.get("fk_parent_column") or "")
    if child_col and parent and parent_col:
        spec["fk"] = [{"child": tbl, "child_col": child_col,
                       "parent": parent, "parent_col": parent_col}]
    if not (spec["pk"] or spec["fk"]):
        return "错误：至少要给 pk，或者一组外键（fk_column/fk_parent_table/fk_parent_column）。"
    try:
        import lakehouse
        r = lakehouse.lake_dq_check(spec)
    except Exception as e:                                    # noqa: BLE001
        return f"lake 侧检查失败：{type(e).__name__}: {str(e)[:160]}"
    if r["errors"]:
        return "检查出错：" + "；".join(r["errors"][:3])
    if not r["findings"]:
        return f"{tbl}：全量检查通过（主键唯一 / 外键完整）。"
    return "\n".join([f"{tbl} 全量检查 {len(r['findings'])} 项："]
                      + [f"  · [{f['severity']}] {f['issue']} —— {f['detail']}"
                         for f in r["findings"]])


def _check_freshness(args: dict, **_: Any) -> str:
    """这张表的数据有多旧、超没超 SLA。"""
    from . import _ensure_path
    _ensure_path()
    asset = str(args.get("asset") or "").strip()
    if not asset:
        return "错误：需要 asset（形如 northwind.orders）。"
    try:
        import sync
        r = sync.freshness(asset)
    except Exception as e:                                    # noqa: BLE001
        return f"查新鲜度失败：{type(e).__name__}: {e}"
    if not r.get("known"):
        return f"{asset} 还没同步过 —— 没有新鲜度可言。"
    stale = "**已超 SLA**" if r.get("is_stale") else "在 SLA 内"
    return (f"{asset}：策略 {r.get('strategy')}，"
            f"距上次同步 {r.get('age_hours', 0):.1f} 小时，{stale}。")


_SCHEMAS.update({
    "scan_permissions": {
        "name": "scan_permissions",
        "description": (
            "扫描一个源系统的权限现状，找出过度授权、外部账号、停滞的管理员账号等。"
            "**只观测只建议，绝不修改任何权限** —— 改错的爆炸半径比数据大。"
        ),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "源系统 id"},
            "kind": {"type": "string", "enum": ["database", "saas"],
                     "description": "数据库还是 SaaS，默认 database"}},
            "required": ["source"]},
    },
    "check_lake_quality": {
        "name": "check_lake_quality",
        "description": (
            "对已落 bronze 的表做**全量**检查：主键唯一性、外键完整性。"
            "源库上只能采样、且不允许跨表关联，这类结论只有在数据湖里才算得准。"
        ),
        "parameters": {"type": "object", "properties": {
            "bronze_table": {"type": "string", "description": "bronze 表名，如 northwind__orders"},
            "pk": {"type": "string", "description": "主键列名"},
            "fk_column": {"type": "string", "description": "外键列，可空"},
            "fk_parent_table": {"type": "string", "description": "父表 bronze 名，可空"},
            "fk_parent_column": {"type": "string", "description": "父表主键列，可空"}},
            "required": ["bronze_table"]},
    },
    "check_freshness": {
        "name": "check_freshness",
        "description": "查一张已接入的表数据有多旧、有没有超过新鲜度 SLA。",
        "parameters": {"type": "object", "properties": {
            "asset": {"type": "string", "description": "形如 northwind.orders"}},
            "required": ["asset"]},
    },
})

# --------------------------------------------------------------- M3 收尾四件
# 这四个都在门禁的 L2/L3 上。**handler 里一行审批判断都没有** ——
# 被拦时 Hermes 根本走不到这里。写了就是把限制散进业务代码（铁律 1）。

def _apply_cleaning_rule(args: dict, **_: Any) -> str:
    """按已批准的规则把 bronze 洗成 silver（L2，Steward 确认）。"""
    from . import _ensure_path
    _ensure_path()
    src = str(args.get("source") or "").strip()
    tbl = str(args.get("table") or "").strip()
    if not (src and tbl):
        return "错误：需要 source 与 table。"
    bronze = str(args.get("bronze_table") or f"{src}__{tbl}").strip()
    approved = args.get("approved_rules") or []
    if isinstance(approved, str):
        approved = [x.strip() for x in approved.split(",") if x.strip()]
    try:
        import clean, data_tools, sync
        dq = data_tools.run_dq_check(src, tbl)
        plan = clean.propose(bronze, dq.get("findings") or [])
        plan["approved_rules"] = approved
        cols = sync.lake_columns("bronze", bronze)
        if not cols:
            return (f"iceberg.bronze.{bronze} 不存在 —— 先把表接进来"
                    f"（ingest_table）再洗。")
        r = clean.apply(bronze, plan, cols, pk=args.get("pk") or None,
                        silver_table=args.get("silver_table") or None)
    except Exception as e:                                    # noqa: BLE001
        return f"清洗 {bronze} 失败：{type(e).__name__}: {str(e)[:200]}"

    # **未被批准的提案要说出来**，否则「洗完了」会被读成「都处理干净了」。
    skipped = [a["rule"] for a in plan.get("propose", [])
               if a["rule"] not in set(approved)]
    ask = [f'{a["column"]}/{a["issue"]}' for a in plan.get("ask", [])]
    out = [f'{r["silver_table"]}：{r["rows"]:,} 行'
           f'（bronze {r["bronze_rows"]:,}，去重 {r["deduped"]}）。'
           f'生效 {len(r["applied"])} 条规则，原值保留在 <列>_raw。']
    if skipped:
        out.append(f"未执行（没批准这条规则）：{'、'.join(skipped)}")
    if ask:
        out.append(f"**仍需你给口径，我没动**：{'、'.join(ask)}")
    return "\n".join(out)


def _publish_gold(args: dict, **_: Any) -> str:
    """silver → gold（L3，Owner 审批）。"""
    from . import _ensure_path
    _ensure_path()
    st = str(args.get("silver_table") or "").strip()
    if not st:
        return "错误：需要 silver_table。"
    cols = args.get("columns") or None
    if isinstance(cols, str):
        cols = [x.strip() for x in cols.split(",") if x.strip()]
    try:
        import publish
        r = publish.publish_gold(st, gold_table=args.get("gold_table") or None,
                                 columns=cols)
    except Exception as e:                                    # noqa: BLE001
        return f"发布 {st} 失败：{type(e).__name__}: {str(e)[:240]}"
    lines = [f'{r["gold_table"]}：{r["rows"]:,} 行，分类 {r["classification"]}，'
             f'来源 {r["upstream"]}。']
    if r["dropped_raw"]:
        lines.append(f'清洗前的原值列没有发布：{"、".join(r["dropped_raw"])}')
    lines.append(f'遮蔽列：{"、".join(r["masked_columns"]) or "（无）"}。'
                 f'{r["note"]}')
    return "\n".join(lines)


def _grant_read(args: dict, **_: Any) -> str:
    """把一张 gold 表的读权限开给某个人（L3，Owner 审批）。"""
    from . import _ensure_path
    _ensure_path()
    who = str(args.get("principal") or "").strip()
    asset = str(args.get("asset") or "").strip()
    if not (who and asset):
        return "错误：需要 principal 与 asset（形如 gold.customer_360）。"
    try:
        import policy_sync
        policy_sync.grant(who, asset, role=str(args.get("role") or "analyst"),
                          by=str(args.get("granted_by") or "owner"))
        r = policy_sync.sync([asset], dry_run=False)
    except Exception as e:                                    # noqa: BLE001
        return f"授权失败：{type(e).__name__}: {str(e)[:200]}"
    d = r["diff"]
    return (f'{asset} 已开给 {who}：策略新增 {len(d["added"])} 条、'
            f'变更 {len(d["changed"])} 条，写入 {r["written"]}。'
            f'能看到哪些列由分类决定，不由这次授权决定。{r["note"]}')


def _ingest_export(args: dict, **_: Any) -> str:
    """把 SaaS 导出的附件落进 bronze（L3，Owner 审批）。"""
    from . import _ensure_path
    _ensure_path()
    src = str(args.get("saas_source") or "").strip()
    tbl = str(args.get("table") or "").strip()
    if not (src and tbl):
        return "错误：需要 saas_source 与 table。"
    try:
        import export_ingest
        r = export_ingest.ingest_export(src, tbl, path=args.get("path") or None,
                                        pk=args.get("pk") or None)
    except Exception as e:                                    # noqa: BLE001
        return f"接入导出件 {src}.{tbl} 失败：{type(e).__name__}: {str(e)[:200]}"
    out = [f'{r.get("bronze_table", tbl)}：{r.get("bronze_rows", 0):,} 行。']
    d = r.get("deletions") or {}
    if d.get("deleted_count"):
        out.append(f'这次快照里少了 {d["deleted_count"]} 个主键 —— '
                   f"全量快照能看见删除，增量在 updated_at 水位线上看不见。")
    if not r.get("column_mapping_confirmed"):
        out.append("列名映射还没人确认过 —— 导出的列名是显示标签，"
                   "业务方改个显示名列名就变，需要 confirm_column_mapping。")
    return "\n".join(out)


_SCHEMAS.update({
    "apply_cleaning_rule": {
        "name": "apply_cleaning_rule",
        "description": (
            "按清洗提案把 bronze 洗成 silver。**要 Steward 批准。**"
            "只执行确定性的那批，加上你在 approved_rules 里点名批准的规则；"
            "需要人给口径的那些原样带过去，不会自己填。原值一律留在 <列>_raw。"
        ),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "源系统 id"},
            "table": {"type": "string", "description": "源表名"},
            "bronze_table": {"type": "string",
                             "description": "bronze 表名，默认 <source>__<table>"},
            "approved_rules": {"type": "array", "items": {"type": "string"},
                               "description": "已获批准的规则名（propose_cleaning 里给出的）"},
            "pk": {"type": "string", "description": "主键列，用于按主键去重"},
            "silver_table": {"type": "string", "description": "目标 silver 表名，默认同名"}},
            "required": ["source", "table"]},
    },
    "publish_gold": {
        "name": "publish_gold",
        "description": (
            "把一张 silver 表发布到 gold 供外部查询。**要 Owner 审批。**"
            "前提是这张 gold 资产已经有分类 —— 没有分类就推不出遮蔽规则。"
            "清洗前的原值列（<列>_raw）不会被发布。"
        ),
        "parameters": {"type": "object", "properties": {
            "silver_table": {"type": "string", "description": "silver 表名"},
            "gold_table": {"type": "string", "description": "目标 gold 表名，默认同名"},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": "要发布的列，默认全部（不含 _raw）"}},
            "required": ["silver_table"]},
    },
    "grant_read": {
        "name": "grant_read",
        "description": (
            "把一张已发布资产的读权限开给某个人。**要 Owner 审批。**"
            "只决定「谁能进来」；看得到哪些列由该表的分类决定。"
        ),
        "parameters": {"type": "object", "properties": {
            "principal": {"type": "string", "description": "被授权的人（用户名或邮箱）"},
            "asset": {"type": "string", "description": "形如 gold.customer_360"},
            "role": {"type": "string", "enum": ["analyst", "owner"],
                     "description": "以什么角色读，默认 analyst"},
            "granted_by": {"type": "string", "description": "批准人"}},
            "required": ["principal", "asset"]},
    },
    "ingest_export": {
        "name": "ingest_export",
        "description": (
            "把 SaaS 定时报表导出的附件落进 bronze。**要 Owner 审批。**"
            "全量快照 —— 所以能看见「这次没了的记录」，这是增量同步做不到的。"
        ),
        "parameters": {"type": "object", "properties": {
            "saas_source": {"type": "string", "description": "SaaS 源 id，如 salesforce"},
            "table": {"type": "string", "description": "对象名，如 Account"},
            "path": {"type": "string", "description": "附件路径，默认取最近一次暂存"},
            "pk": {"type": "string", "description": "不可变主键列，用于识别删除"}},
            "required": ["saas_source", "table"]},
    },
})


_HANDLERS = {
    "apply_cleaning_rule": _apply_cleaning_rule,
    "publish_gold": _publish_gold,
    "grant_read": _grant_read,
    "ingest_export": _ingest_export,
    "scan_permissions": _scan_permissions,
    "check_lake_quality": _check_lake_quality,
    "check_freshness": _check_freshness,
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
