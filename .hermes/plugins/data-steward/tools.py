"""Claw 的工具。

**参数化，不给自由 SQL**（readme 4.6）：每个工具签名固定，
SQL 由代码生成。工具签名是唯一能挂约束的地方 —— 参数校验、级别、
审批人、账本、指纹全挂在它上面，没有签名就没有挂载点。

这个模块被 Hermes 在发现阶段导入（因为 manifest 声明了 `provides_tools`），
所以同样保持 import 轻量：连库放进 handler。
"""
import json
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
    # **标出哪些已经接过了。** 不标的话模型每收到一封信就重新规划一遍，
    # 把接过的表再发一次审批 —— 实测三张表被重复申请，人白点三次链接。
    # 它不是记不住，是**没人告诉它**：清单里只有源系统的表，
    # lake 里已有什么得自己去查，而它没有理由想到要查。
    done = _already_in_lake(src)
    lines = [f"{src} 共 {len(rows)} 张表"
             + (f"，其中 {len(done)} 张已在数据湖里：" if done else "：")]
    for t in rows[:50]:
        n = t.get("approx_rows")
        est = "行数未知" if n is None or n < 0 else f"约 {n:,} 行"
        mark = ""
        if t["table"] in done:
            age = done[t["table"]]
            mark = f"　**已接入**（{age}，要刷新才需要重接）"
        lines.append(f"  · {t['table']}（{est}）{mark}")
    if len(rows) > 50:
        lines.append(f"  …… 另有 {len(rows) - 50} 张未列出")
    if done:
        lines.append("已接入的不用再申请接入 —— 重接只在需要刷新数据时做。")
    return "\n".join(lines)


def _already_in_lake(src: str) -> dict:
    """这个源里哪些表已经落过 bronze，以及多久以前。**读账本，不查 lake**：
    `sync_state` 是同步这件事自己的记录，比反查表名可靠。"""
    import time
    out = {}
    try:
        from datasteward_gate.approvals import open_store
        with open_store(readonly=True, init_schema=False) as st:
            rows = st.db.execute(
                "SELECT asset, last_synced_at FROM sync_state") \
                if hasattr(st.db, "execute") else []
            for asset, ts in rows:
                if not str(asset).startswith(src + "."):
                    continue
                tbl = str(asset).split(".", 1)[1]
                if not ts:
                    out[tbl] = "时间未知"
                    continue
                h = (time.time() - float(ts)) / 3600.0
                out[tbl] = (f"{h:.0f} 小时前同步" if h >= 1
                            else f"{max(1, int(h * 60))} 分钟前同步")
    except Exception:                                        # noqa: BLE001
        pass
    return out


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


def _recently_synced(asset: str, within_h: float = 6.0):
    """这张表是不是刚接过。返回一句人话，或 None。

    **重复接同一张表是模型最常见的浪费**：它每收到一封信就重新规划，
    把接过的表再申请一遍 —— 人白点一次链接，源库白扫一次全表。
    `list_source_tables` 已经在清单里标了「已接入」，但模型不一定去看清单；
    真正要拦住的是**动手那一刻**。
    """
    import time
    try:
        from datasteward_gate.approvals import open_store
        with open_store(readonly=True, init_schema=False) as st:
            row = st.db.execute(
                "SELECT last_synced_at, row_count, schema_hash FROM sync_state"
                " WHERE asset=?", (asset,)).fetchone() \
                if hasattr(st.db, "execute") else None
    except Exception:                                        # noqa: BLE001
        return None
    if not row or not row[0]:
        return None
    age_h = (time.time() - float(row[0])) / 3600.0
    if age_h > within_h:
        return None
    ago = (f"{age_h:.1f} 小时前" if age_h >= 1
           else f"{max(1, int(age_h * 60))} 分钟前")
    return (f"{asset} **{ago}刚接过**（{row[1] or 0:,} 行），没有重接。\n"
            f"要刷新数据用 full_refresh；只是想看内容的话表已经在 "
            f"iceberg.bronze 里了，直接 sql_query（plane=lake）。")


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
    dup = _recently_synced(f"{src}.{tbl}")
    if dup:
        return dup
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
                        silver_table=args.get("silver_table") or None,
                        asset=f"{src}.{tbl}")
    except Exception as e:                                    # noqa: BLE001
        return f"清洗 {bronze} 失败：{type(e).__name__}: {str(e)[:200]}"

    # **给了规则却一条都没匹配上 = 名字写错了，不是「人没批准」。**
    # 两者的返回话术几乎一样（"未执行（没批准这条规则）"），而后果差很多：
    # 前者是洗了等于没洗、看着还成功。实测撞过：模型抄成人类可读的
    # "status / enum_drift"，而规则名是 "enum_drift__status"。
    _all_rules = {a["rule"] for a in plan.get("propose", [])}
    _matched = _all_rules & set(approved)
    if approved and not _matched:
        return (f"**规则名对不上，一条都没执行**（没有洗任何数据）。\n"
                f"你给的：{'、'.join(map(str, approved))}\n"
                f"这张表可用的：{'、'.join(sorted(_all_rules)) or '（没有可执行的规则）'}\n"
                f"规则名要**原样抄** `propose_cleaning` 给出的那个，别改写成"
                f"更好读的形式 —— 它是标识符，不是描述。")

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
                               "description": "要执行的规则名，**原样抄 propose_cleaning 给出的那个**（如 enum_drift__status）。它是标识符不是描述，改写成更好读的形式会一条都匹配不上"},
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


# --------------------------------------------------------------- 溯源
# **「这张表哪来的」必须查得到，不能靠记忆。**
#
# 实测：开一个全新会话问「每张表从哪来、谁批的、按谁的口径洗的」，
# 它答「审批人姓名未随记录留存，无法点名」「口径是谁定的没有任何记录」
# —— 而 approvals 里有 approver、asset_semantics 里写着「王姐，财务负责人」。
# 东西都在库里，**只是没有一个工具去读**。它还把连接账号猜成了
# postgres 超级用户（实际是 fin_reader）—— 查不到就会猜，这比不答更糟。

def _trace_asset(args: dict, **_: Any) -> str:
    """一张表的完整来历（L0，只读治理库）。

    **读的是 `asset_provenance` 这一张台账**，不是跨五张表现拼。
    早先那版拼法要 `args_json LIKE '%表名%'` 去猜哪份审批对应哪张表 ——
    又慢又脆，而且外部系统（审计、BI）根本无从下手。
    """
    from . import _ensure_path
    _ensure_path()
    name = str(args.get("asset") or args.get("table") or "").strip()
    if not name:
        return "错误：需要 asset（如 acme.fin_invoice 或 acme__fin_invoice）。"
    key = name.replace("__", ".", 1) if "__" in name and "." not in name else name

    try:
        import catalog
        from datasteward_gate.approvals import open_store
        # **init_schema=True**：DDL 是幂等的，而存量库可能建于新表之前。
        # 加一张表之后老库里没有它 —— 读工具因此报「no such table」，
        # 看起来像「查不到这张表的来历」，实际是库没升级。两者差很远。
        with open_store(readonly=False, init_schema=True) as st:
            rows = st.provenance(key)
            # 口径原先用 `hasattr(st.db, "execute")` 分支去读，而 psycopg3 的
            # Connection **也有** `.execute` —— 于是 Postgres 走进了 SQLite
            # 那条路，`?` 占位符直接抛错，被外层吞成「读不到治理库」：
            # PG 上这个工具其实一直是全瘫的。改走 catalog 里按后端分派的读法。
            sem = catalog.semantics(key)
            cat = st.catalog(asset=key)
    except Exception as e:                                   # noqa: BLE001
        return f"读不到治理库：{type(e).__name__}: {str(e)[:120]}"

    if not rows and not sem and not cat:
        return (f"# {key}\n\n**无档** —— 台账里没有这张表的任何记录。\n"
                f"湖里若有数据而台账为空，那是缺口本身：没人授权过、"
                f"也没人批准过。**如实报给审计，别猜「大概是示例数据」。**")

    L = [f"# {key} 的来历", "", "| 时间 | 发生了什么 | 谁 | 依据 |",
         "|---|---|---|---|"]
    VERB = {"source_registered": "源接入", "ingested": "入湖",
            "ingested_from_file": "从文件入湖", "cleaned": "清洗",
            "published": "发布 gold", "granted": "开读权限",
            "semantics_defined": "定口径", "refreshed": "全量刷新"}
    for r in rows:
        d = r["detail"] or {}
        extra = (d.get("connection_identity") or d.get("path")
                 or ("规则 " + "、".join(d["approved_rules"])
                     if d.get("approved_rules") else "")
                 or (d.get("result") or "")[:40])
        L.append(f"| {_ts(r['ts'])} | {VERB.get(r['event'], r['event'])}"
                 f"{'：' + str(extra) if extra else ''} | {r['actor'] or '—'} "
                 f"| `{(r['approval_id'] or '')[:8] or '—'}` |")

    if sem:
        # **推断和人确认要分开。** 混着列出来，模型读到的就是一句同样
        # 权威的话 —— 而 `system:` 开头的那些只是外键推断出来的。
        L += ["", "## 口径"]
        for s_ in sorted(sem, key=lambda x: x["key"]):
            tag = catalog.LABEL.get(s_["status"], s_["status"])
            verb = "确认" if s_["status"] == "confirmed" else "推断"
            L.append(f"- `{s_['key']}`［{tag}］{str(s_['value'])[:120]}"
                     f"\n  —— **{s_['by']}** {verb}（{_ts(s_['at'])}）")
    else:
        L += ["", "## 口径", "- **无档**：没有人定过这张表的口径"]

    obs = [r for r in cat if r["status"] == "observed"]
    other = [r for r in cat if r["status"] != "observed"]
    if obs:
        at = max(r["observed_at"] for r in obs)
        L += ["", f"## 结构档案［观测］采于 {_ts(at)}",
              f"- {len(obs)} 条观测事实在档（列、主键、外键）。"
              f"看内容用 `describe_asset`，它不回源库。"]
    if other:
        L += ["", "## 推断与否定（**留着是为了下次少猜同一个错**）"]
        for r in other:
            L.append(f"- [{r['kind']}/{r['key']}]"
                     f"［{catalog.LABEL.get(r['status'], r['status'])}］"
                     f"{json.dumps(r['value'], ensure_ascii=False)[:100]}"
                     f" —— {r['actor']}（{_ts(r['observed_at'])}）")

    L += ["", "> 台账（append-only）与档案都只报查得到的，"
          "**查不到就写「无档」** —— 不猜、不补编。"
          "［观测］源库读到的　［推断］待验证　［已确认］人拍过板。"]
    return "\n".join(L)


def _ts(v) -> str:
    import time
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(v)))
    except Exception:                                        # noqa: BLE001
        return str(v or "时间未知")


def _dsn_identity(dsn: str) -> str:
    """从连接串里取**身份**，扔掉口令。审计要知道用谁连的，不需要密码。"""
    try:
        rest = str(dsn).split("://", 1)[1]
        cred, host = rest.split("@", 1)
        return f"{cred.split(':', 1)[0]}@{host}"
    except Exception:                                        # noqa: BLE001
        return "（连接串解析不出）"


_SCHEMAS.update({
    "trace_asset": {
        "name": "trace_asset",
        "description": (
            "一张表的完整来历：源从哪来、谁给的连接、用什么账号、谁批准的接入、"
            "洗过什么、按谁定的口径、文件来源。**要跟人交代数据出处时用它** —— "
            "别凭记忆答，记忆跨不过会话。"
        ),
        "parameters": {"type": "object", "properties": {
            "asset": {"type": "string",
                      "description": "资产名，如 acme.fin_invoice；"
                                     "bronze 表名 acme__fin_invoice 也认"}},
            "required": ["asset"]},
    },
})


# --------------------------------------------------------------- 口径沉淀
# **问过的不再问**（readme 5.7）。人回了口径，就得落进 `asset_semantics`，
# 否则下一轮、下一个会话、换一个部署，同一个问题还要再问一遍 ——
# 重复问同一件事是最快失去信任的方式。
#
# 实测踩过：`define_semantics` 在 policy.py 里声明了 L2，**但从来没有实现**。
# 模型于是退而求其次，把口径记进了 Hermes 自己的 `memory`（它刚好被放行）
# —— 看着像记住了，项目的知识库里一条都没有。换个 Agent、换台机器就全丢。

def _define_semantics(args: dict, **_: Any) -> str:
    """把人确认过的口径沉淀下来（L2，Steward 确认）。"""
    from . import _ensure_path
    _ensure_path()
    asset = str(args.get("asset") or "").strip()
    # **key 必填，不给默认值。** 带默认值的参数会让「给了 semantics」和
    # 「没给」算成两个不同的动作（指纹算的是原始参数，看不到默认值），
    # 于是同一条口径被反复发审批、把 steward 的 WIP 占满。实测踩过。
    key = str(args.get("key") or "").strip()
    value = str(args.get("value") or "").strip()
    by = str(args.get("confirmed_by") or "").strip()
    if not asset or not value or not key:
        return ("错误：需要 asset（如 acme.fin_monthly.region）、"
                "key（这条口径叫什么，如 null_meaning / normalize_rule）"
                "与 value（口径本身）。**key 要明确写出来** —— "
                "同一列可能有好几条口径，含糊的名字以后查不出来。")
    if not by:
        return ("错误：需要 confirmed_by —— **口径必须记是谁定的**。"
                "没有出处的口径下次没人认账，等于没定。")
    # 枚举**要结构化地给**，不要只写在自然语言里。
    # 原先只有 value 一段话（「统一成全大写，枚举只允许 PAID / PENDING /
    # UNPAID / VOID」），下游验收得拿正则 `\b[A-Z][A-Z_]{2,}\b` 去抠 ——
    # 换个写法（小写枚举、中文值）就抠不出来，而那时断言会**静默跳过**
    # 而不是报错。让机器去解析自然语言，是这类「看着通过了」的来源。
    allowed = args.get("allowed_values")
    if isinstance(allowed, str):
        allowed = [x.strip() for x in allowed.replace("、", ",").split(",")
                   if x.strip()]
    if allowed is not None and not isinstance(allowed, list):
        return "错误：allowed_values 要给字符串数组，如 [\"PAID\", \"VOID\"]。"

    try:
        from datasteward_gate import store
        st = store()
        old = st.known(asset, key)
        st.remember(asset, key, value, by, source_item=args.get("source_item"))
        if allowed:
            import catalog
            catalog.confirm(asset, "constraint", "allowed_values",
                            [str(x) for x in allowed], actor=by,
                            evidence={"from": key, "source_item":
                                      args.get("source_item") or ""})
    except Exception as e:                                   # noqa: BLE001
        return f"沉淀 {asset}.{key} 失败：{type(e).__name__}: {str(e)[:200]}"
    if old and old.get("value") != value:
        return (f"{asset}.{key} 的口径已更新：\n"
                f"  原：{old['value'][:80]}（{old.get('confirmed_by')}）\n"
                f"  新：{value[:80]}（{by}）\n"
                f"**口径改了，之前按旧口径洗过的数据要重洗** —— 别忘了这件事。")
    tail = (f" 枚举已结构化登记：{'、'.join(map(str, allowed))}"
            f"（下游按它校验，不再从这句话里抠）。" if allowed else "")
    return (f"已记下 {asset}.{key} = {value[:100]}（{by} 确认）。{tail}"
            f"以后不会再就这一条问人。")


_SCHEMAS.update({
    "define_semantics": {
        "name": "define_semantics",
        "description": (
            "把某人确认过的业务口径记下来，以后不再问第二遍。"
            "**人在信里回了口径就该调它** —— 只记在对话里，换个会话就没了。"
        ),
        "parameters": {"type": "object", "properties": {
            "asset": {"type": "string",
                      "description": "口径针对什么，如 acme.fin_monthly.region"},
            "key": {"type": "string",
                    "description": "这条口径叫什么，**必填**：如 null_meaning"
                                   "（空值什么意思）、normalize_rule（怎么归一）、"
                                   "deprecated（废弃列）"},
            "value": {"type": "string", "description": "口径本身，一句话说清"},
            "confirmed_by": {"type": "string",
                             "description": "谁确认的（邮箱或姓名）。必填"},
            "allowed_values": {"type": "array", "items": {"type": "string"},
                               "description": "口径限定了取值范围时**必须给**，"
                                              "如 [\"PAID\",\"PENDING\",\"VOID\"]。"
                                              "只写在 value 那句话里不算 —— "
                                              "下游要靠它校验清洗结果"},
            "source_item": {"type": "string",
                            "description": "依据的那份审批/提问 id，可空"}},
            "required": ["asset", "key", "value", "confirmed_by"]},
    },
})


# --------------------------------------------------------------- 连表
# **源系统禁 join，lake 里随便 join**（铁律 3）。这条分界不是性能取舍：
# 一条模型生成的多表关联打在生产源库上，即使只读也可能把它拖死；
# 而 iceberg 是我们自己的地盘，扫爆了也不影响别人。
#
# 门禁那一侧（`_sql_guard` → `connector.review_sql`）已经把规则写全了：
# plane=lake 走 trino 方言、只准查 `iceberg.*`、JOIN 不算风险项；
# plane=source 则 JOIN / 无过滤聚合 / 大 LIMIT 一律先问人。
# **缺的一直是工具本身** —— `sql_query` 在 policy.py 里声明了 L1，
# 但从来没有 schema 也没有 handler，于是 Agent 根本没有办法连表。

def _sql_query(args: dict, **_: Any) -> str:
    """执行一条已经过门禁准入的 SQL（L1；内容级风险由 `_sql_guard` 判）。"""
    from . import _ensure_path
    _ensure_path()
    sql = str(args.get("sql") or "").strip()
    plane = str(args.get("plane") or "source").lower()
    if not sql:
        return "错误：需要 sql。"
    if plane not in ("source", "lake"):
        return f"错误：plane 只能是 source 或 lake，收到 {plane!r}。"

    # 门禁放行时会把重写过的 SQL（补了 LIMIT 等）合并回参数，并打上
    # `_sql_gate_approved`。**这里不重新判一遍**——两处实现同一规则必然漂移，
    # 那正是 gate 与 Connector 各有一份关键字判断时出过的事。
    try:
        if plane == "lake":
            import sync
            rows = sync._trino(sql)
            n = len(rows)
            head = rows[:20]
            body = "\n".join("  " + str(r) for r in head)
            more = f"\n  …… 共 {n} 行，只显示前 20 行" if n > 20 else ""
            return f"查到 {n} 行：\n{body}{more}" if n else "查到 0 行。"
        src = str(args.get("source") or args.get("source_id") or "").strip()
        if not src:
            return "错误：plane=source 时需要 source（源系统 id）。"
        import connector
        r = connector.query(src, sql, purpose=str(args.get("purpose") or ""),
                            approved=bool(args.get("_sql_gate_approved")))
        rows = r.get("rows") or []
        head = rows[:20]
        body = "\n".join("  " + str(x) for x in head)
        more = (f"\n  …… 共 {len(rows)} 行，只显示前 20 行"
                if len(rows) > 20 else "")
        return f"查到 {len(rows)} 行：\n{body}{more}" if rows else "查到 0 行。"
    except Exception as e:                                   # noqa: BLE001
        return f"查询失败：{type(e).__name__}: {str(e)[:250]}"


def _describe_asset(args: dict, **_: Any) -> str:
    """一张表的结构、负责人、口径 + **怎么和别的表连**（L0，只读）。

    **先读档案，读不到才回源库。** 以前每调一次就是三趟源库往返
    （列、外键、lake 清单），新会话等于从零开始 —— 而「这张表长什么样」
    是上一次已经问过的事。现在 `services/catalog.py` 把观测事实存下来，
    这里默认读它，并**报出快照采于什么时候**：省往返的代价是答案有延迟，
    把延迟藏起来就变成了撒谎。要确认源库现在是不是这样，传 `refresh=true`。

    每一条都标出是［观测］、［推断］还是［已确认］。R5 实测过一次
    「查不到它就会猜」，猜出来的答案比不答更糟 —— 标注是这个工具的正事。

    join 路径只在 lake 侧给：源系统禁关系展开（铁律 3），
    在源库上提示「你可以这样 join」等于鼓励它去踩那条线。
    """
    from . import _ensure_path
    _ensure_path()
    src = str(args.get("source") or "").strip()
    tbl = str(args.get("table") or "").strip()
    if not (src and tbl):
        return "错误：需要 source 与 table。"
    refresh = str(args.get("refresh") or "").lower() in ("1", "true", "yes")

    try:
        import catalog
        snap, hit_source = catalog.snapshot(src, tbl, refresh=refresh)
    except Exception as e:                                   # noqa: BLE001
        return f"读 {src}.{tbl} 的结构失败：{type(e).__name__}: {str(e)[:200]}"
    if not snap:
        return (f"读不到 {src}.{tbl} 的结构：档案里没有，源库也没采到。"
                f"**别猜** —— 先确认这张表存在、且这个源已经接入。")

    out = [catalog.render(snap, hit_source=hit_source)]

    # join 路径：**只给已经落进 lake 的那些**。没落进来的先接进来再说。
    # 这一段查的是 Trino（我们自己的湖），不是源库。
    try:
        import sync
        have = set(sync._trino(
            "SELECT table_name FROM iceberg.information_schema.tables"
            " WHERE table_schema='bronze'"))
    except Exception:                                        # noqa: BLE001
        have = set()
    me = f"{src}__{tbl}"
    paths = []
    for t, c, rt, rc in (snap.get("foreign_keys") or []):
        a, b = f"{src}__{t.split('.')[-1]}", f"{src}__{rt.split('.')[-1]}"
        if a in have and b in have:
            paths.append(f'  iceberg.bronze."{a}" a JOIN iceberg.bronze."{b}" b'
                         f'  ON a.{c} = b.{rc}')
    if paths:
        out.append("\n## 可用的 join（两边都已在 lake 里）")
        out += paths[:8]
        out.append("**在 lake 里 join，不要在源库上关联**（源系统禁关系展开）。")
    elif me not in have:
        out.append(f"\n这张表还没接进 lake（bronze 里没有 {me}），"
                   f"要连表得先 ingest_table。")
    else:
        out.append("\n暂时没有两边都在 lake 里的 join 路径 —— "
                   "对端表还没接进来。")
    return "\n".join(out)


_SCHEMAS.update({
    "sql_query": {
        "name": "sql_query",
        "description": (
            "执行一条只读 SQL。**plane=lake 时可以 JOIN、聚合**（在我们自己的"
            "数据湖里，表名形如 iceberg.bronze.\"源__表\"）；plane=source 是"
            "外部源系统，**禁止跨表关联**，重读会先问人。要连表就用 lake。"
        ),
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "只读 SQL"},
            "plane": {"type": "string", "enum": ["source", "lake"],
                      "description": "source=外部源系统（禁 join）；"
                                     "lake=已复制进来的表（可 join）"},
            "source": {"type": "string",
                       "description": "plane=source 时的源系统 id"},
            "purpose": {"type": "string",
                        "description": "bulk 表示批量抽取，受低峰窗口约束"}},
            "required": ["sql", "plane"]},
    },
    "describe_asset": {
        "name": "describe_asset",
        "description": (
            "一张表的列、类型、主键、外键、负责人和已定的口径，以及"
            "**怎么和别的表连**。要 join 或者要问人之前先看这个。"
            "**默认读档案，不打扰源库**，返回里会写明快照采于何时；"
            "每条都标了［观测］／［推断］／［已确认］—— 标着推断的别当结论。"
        ),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "源系统 id"},
            "table": {"type": "string", "description": "表名"},
            "refresh": {"type": "boolean",
                        "description": "重新回源库采一次。**只在怀疑源库结构"
                                       "变了时才用** —— 平时读档就够，"
                                       "每次都 refresh 等于没有档案"}},
            "required": ["source", "table"]},
    },
})


# --------------------------------------------------------------- 接一个新源
# **真实世界里源是这样来的**：人在邮件里写一串连接信息，或者 IT 发个配置
# 文件过来。不是脚本往清单里塞一行 —— 那是演的。
#
# 这个工具是 L3（sponsor 批）。它是**唯一**能让一个源变得可用的路：
# 批准之后凭证进 `source_secrets`（Agent 读不到），可见性进 `source_grants`
# （门禁读它）。没批准就没有数据源 —— `register_source` 拒绝无 approval_id
# 的注册，这条从 R1 就写死了，只是此前 Agent 没有工具能走到它。

def _dsn_from_approval(source_id: str) -> str:
    """从**已批准**的那条 connect_source 审批里取回连接串。

    只认已经有 approve 决定的那条 —— 未决的审批里也存着参数，
    但那还不是「人同意了」。
    """
    try:
        from datasteward_gate import store
        st = store()
        rows = st.db.execute(
            "SELECT a.args_json FROM approvals a JOIN decisions d"
            " ON d.approval_id = a.id WHERE a.tool_name='connect_source'"
            " AND d.decision='approve' ORDER BY d.decided_at DESC LIMIT 20"
        ).fetchall() if hasattr(st.db, "execute") else []
        for (aj,) in rows:
            d = json.loads(aj)
            if d.get("source_id") == source_id and d.get("dsn"):
                return str(d["dsn"])
    except Exception:                                        # noqa: BLE001
        pass
    return ""


def _connect_source(args: dict, **_: Any) -> str:
    """把人给的连接信息注册成一个可用的源（L3，sponsor 审批）。"""
    from . import _ensure_path
    _ensure_path()
    sid = str(args.get("source_id") or "").strip()
    dsn = str(args.get("dsn") or "").strip()
    if not sid:
        return "错误：需要 source_id。"
    if not dsn:
        # **恢复时不必重新给连接串。** 被拦下那次的参数已经在审批记录里；
        # 让模型再念一遍 dsn 意味着密码要第二次进上下文（还得指望它
        # 记对）。这里从**已批准的那条审批**里取回来 —— 顺带也就
        # 不需要在 monitor 的输出里写它了。
        dsn = _dsn_from_approval(sid)
    if not dsn:
        return ("错误：需要 dsn。它是对方给你的连接串 —— 邮件正文里那一行，"
                "或者附件里的配置。**拿不到就问人要，不要自己编。**")
    if "://" not in dsn:
        return (f"错误：{dsn[:40]!r} 不像连接串。应当形如 "
                f"postgresql://用户:口令@主机:端口/库名。"
                f"**拿不到就说拿不到**，不要自己编一个。")

    # 走到这里说明门禁已经放行 = 票据是真的。**把那张票找出来当依据** ——
    # `register_source` 拒绝没有 approval_id 的注册，而这个依据必须是
    # 真发生过的那次批准，不是随手编一个 id。
    try:
        from datasteward_gate import store
        st = store()
        row = st.db.execute(
            "SELECT a.id FROM approvals a JOIN decisions d ON d.approval_id=a.id"
            " WHERE a.tool_name='connect_source' AND d.decision='approve'"
            " ORDER BY d.decided_at DESC LIMIT 1").fetchone() \
            if hasattr(st.db, "execute") else None
        aid = row[0] if row else ""
    except Exception:                                        # noqa: BLE001
        aid = ""
    if not aid:
        return ("错误：找不到这次接入的批准记录。注册数据源必须有审批依据 —— "
                "没有批准就没有数据源。")

    try:
        import connector
        r = connector.register_source(
            sid, dsn, approval_id=aid, kind=str(args.get("kind") or "postgres"),
            description=str(args.get("description") or ""),
            by=str(args.get("given_by") or "邮件"))
    except Exception as e:                                   # noqa: BLE001
        return f"注册 {sid} 失败：{type(e).__name__}: {str(e)[:200]}"

    # **不要把连接串回显给模型。** 它已经在上下文里出现过一次（是参数），
    # 但没有理由再出现第二次 —— 每多一次就多一次被写进日志、被带进
    # 下一轮 prompt 的机会。
    tables, err = [], ""
    try:
        tables = connector.list_tables(sid)
    except Exception as e:                                   # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:120]}"

    # **接成功或失败，都要给人一个交代 + 下一步。**
    # 只把结果 return 给模型的话，人那边什么都看不到 —— 而这条线是他批的，
    # 他有权知道批完之后到底连上没有。失败尤其要说：连不上多半是
    # 连接信息不对，而只有他能给新的。
    _notify_connect(sid, args.get("given_by"), tables, err)

    if err:
        return (f"源 {sid} 注册了（依据审批 {aid[:8]}），**但连不上**：{err}。"
                f"已把情况回给对方，等新的连接信息。不要反复重试。")
    return (f"已接入源 {sid}（依据审批 {aid[:8]}），能看到 {len(tables)} 张表。"
            f"凭证已存放，之后我只用 source_id 提交查询，不再持有连接串。"
            f"已把结果和下一步回给对方。")


def _notify_connect(sid, given_by, tables, err):
    """把接入结果回给人。**发不出去要说出来**，不吞。"""
    try:
        import notify
        st_ = None
        try:
            from datasteward_gate import store
            st_ = store()
        except Exception:                                    # noqa: BLE001
            pass
        to = _resolve_to(st_, "owner") or given_by or ""
        if err:
            body = (f"{sid} 的接入审批已经通过，但按这份连接信息**连不上**：\n\n"
                    f"  {err}\n\n"
                    f"下一步：麻烦确认一下账号/口令/网络是否可达，"
                    f"再把新的连接信息发我。在收到之前我不会反复重试。")
            subj = f"[数据管家] {sid} 接入失败，需要新的连接信息"
        else:
            # `list_tables` 返回 (表名, 行数估算) 的元组列表。
            # 行数是**估算**，-1 表示未知 —— 别把 -1 印成「-1 行」。
            def _one(t):
                if isinstance(t, (tuple, list)) and len(t) >= 2:
                    n = t[1]
                    return f"{t[0]}（约 {n:,} 行）" if isinstance(n, int) and n >= 0 \
                        else f"{t[0]}（行数未知）"
                return str(t)
            names = "、".join(_one(t) for t in tables[:12])
            body = (f"{sid} 已经接进来了，能看到 {len(tables)} 张表：\n\n"
                    f"  {names}{'……' if len(tables) > 12 else ''}\n\n"
                    f"下一步：告诉我先接哪几张（接哪张表优先是业务判断，"
                    f"不是技术判断）。每张表的接入我会单独发审批给负责人。")
            subj = f"[数据管家] {sid} 已接入，共 {len(tables)} 张表"
        notify.get().send_notice(to, subj, body)
    except Exception:                                        # noqa: BLE001
        pass                     # 通知失败不改变已经完成的注册


_SCHEMAS.update({
    "connect_source": {
        "name": "connect_source",
        "description": (
            "把某人给你的连接信息注册成一个可用的数据源。**这是接入一个新源的"
            "唯一入口** —— 在此之前你碰不到它。需要负责人批准。"
            "连接串来自对方的邮件正文或附件；拿不到就问，不要自己编。"
        ),
        "parameters": {"type": "object", "properties": {
            "source_id": {"type": "string",
                          "description": "给这个源起的 id，如 acme、northwind"},
            "dsn": {"type": "string",
                    "description": "对方给的连接串，如 "
                                   "postgresql://user:pass@host:5432/db。"
                                   "**恢复一次被拦下的接入时不用再给** —— "
                                   "它已经在那份审批里了"},
            "kind": {"type": "string", "description": "源类型，默认 postgres"},
            "given_by": {"type": "string", "description": "谁给的（邮箱或姓名）"},
            "description": {"type": "string", "description": "这个源是干什么的"}},
            # **dsn 不是必填**：恢复时它已经在审批记录里了，让模型再念一遍
            # 等于密码第二次进上下文（还得指望它记对）。schema 写成 required
            # 的话 Hermes 在调用前就拒了 —— handler 里那段取回逻辑根本跑不到。
            "required": ["source_id"]},
    },
})


# --------------------------------------------------------------- 阶段提案
# 周报从**播报**改成**提案**（acme_full_v2.md §3）：一轮做完了，
# 下一步该做什么是人的决策点，不是 Agent 自行续摊。
#
# 这个工具是 L1 —— 和 `propose_cleaning` 完全同构：**提案本身不改任何东西**，
# 所以它不需要审批（否则「发问」这件事自己也要先被批一次，套娃）。
# 真正的门在别处：没有被批准的 `start_silver`，`apply_cleaning_rule`
# 一律被门禁拒。引导在 skill 里，强制在门禁里，两层别混。

def _stage_options():
    """三个选项在 `policy.py` 里 —— 一处定义，工具 / 令牌 / 门禁三处读。"""
    from . import _ensure_path
    _ensure_path()
    from datasteward_gate.policy import STAGE_KEYS, STAGE_OPTIONS
    return STAGE_OPTIONS, STAGE_KEYS


def _propose_stage_decision(args: dict, **_: Any) -> str:
    """把阶段小结变成一条三选一的提问，等人拍板（L1：只发问，不改东西）。"""
    from . import _ensure_path
    _ensure_path()
    summary = str(args.get("summary") or "").strip()
    recommend = str(args.get("recommend") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not summary:
        return "错误：需要 summary（这一轮的小结，人靠它做判断）。"
    stage_options, stage_keys = _stage_options()
    if recommend not in stage_keys:
        return (f"错误：recommend 必须是 {sorted(stage_keys)} 之一，"
                f"收到 {recommend!r}。**必须给建议** —— 只把三个选项摆出来"
                f"而不说该选哪个，等于把判断推回给人。")
    if not reason:
        return "错误：需要 reason —— 没有理由的建议没法被反驳，也就没法被采纳。"

    opts = [dict(o, recommended=(o["key"] == recommend)) for o in stage_options]
    decider = str(args.get("decider") or "sponsor").strip()
    try:
        from datasteward_gate import store
        st = store()
        qid, created = st.ask(
            run_id=str(args.get("run_id") or "stage"),
            asset="__stage__",
            question=f"{summary}\n\n下一步三选一。我建议「{recommend}」，因为{reason}",
            options=opts, approver=decider,
            evidence=reason)
    except Exception as e:                                    # noqa: BLE001
        return f"发起阶段提案失败：{type(e).__name__}: {str(e)[:200]}"

    if not created:
        return (f"这一轮的提案已经发出去过了（id={qid[:8]}），还没有人拍板。"
                f"**不要重复打扰** —— 等回复，或者去做别的不受阻塞的事。")

    # 落库了还得**发出去**。只落库不发信的话，提案在表里躺着、人从不知道，
    # 而 Agent 这边看起来「已经问过了」—— 又一次静默。
    sent = _send_stage_mail(qid, decider, summary, recommend, reason, opts)
    return (f"阶段提案已发给 {decider}（id={qid[:8]}，{sent}），三个选项、建议"
            f"「{recommend}」。**正文写同意不算数，要点链接。**"
            f"在拍板之前不要自行开始下一轮。")


def _resolve_to(st_, role: str) -> str:
    """角色名 → 邮箱。**解析不出来时不要把角色名当邮箱用。**

    实测撞过：阶段提案发给了字面量 "sponsor"，于是躺在一个叫 sponsor 的
    收件箱里，谁也看不到 —— 提案发出去了、没人收到，而日志一切正常。
    `_notify_async` 那边早就按 `MAIL_<ROLE>` 兜底了，这里漏了同一步。
    """
    import notify
    who = (st_.resolve_role(role) if st_ else None) or ""
    if "@" in who:
        return who
    for key in (f"MAIL_{role.upper().replace(':', '_')}",
                "MAIL_SPONSOR", "MAIL_OWNER"):
        v = notify.cfg(key, "")
        if "@" in v:
            return v
    return ""


def _send_stage_mail(qid, decider, summary, recommend, reason, opts) -> str:
    """把提案连同三枚一次性链接发出去。发不出去要**说出来**，不吞。"""
    try:
        import notify
        st_ = None
        try:
            from datasteward_gate import store
            st_ = store()
        except Exception:                                    # noqa: BLE001
            pass
        to = _resolve_to(st_, decider)
        lines = [summary, "", f"我的建议：{recommend} —— {reason}", "",
                 "请点其中一个链接（正文回复不算数）："]
        for o, url in notify.choice_links(qid, decider, opts):
            mark = "（我建议这个）" if o.get("recommended") else ""
            lines.append(f'- {o["label"]}{mark}\n  {url}')
        notify.get().send_notice(to, "[数据管家] 阶段提案：下一步怎么走",
                                 "\n".join(lines))
        return "已发出"
    except Exception as e:                                   # noqa: BLE001
        return f"**但没发出去**：{type(e).__name__}: {str(e)[:80]}"


_SCHEMAS.update({
    "propose_stage_decision": {
        "name": "propose_stage_decision",
        "description": (
            "一轮做完时把小结变成给负责人的**提案**：三选一（继续追未完成 / "
            "开始清洗轮 / 放弃剩下的），必须附上你的建议和理由。"
            "只发问，不改任何东西 —— 阶段转换本身是人的决策点。"
        ),
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string",
                        "description": "这一轮的小结：接了多少、卡在谁那里、"
                                       "发现了什么问题。人靠它做判断"},
            "recommend": {"type": "string",
                          "enum": sorted(_stage_options()[1]),
                          "description": "你建议选哪个。必须给"},
            "reason": {"type": "string", "description": "为什么建议这个"},
            "decider": {"type": "string",
                        "description": "谁拍板，角色名，默认 sponsor"},
            "run_id": {"type": "string", "description": "任务线 id，可空"}},
            "required": ["summary", "recommend", "reason"]},
    },
})


_HANDLERS = {
    "trace_asset": _trace_asset,
    "define_semantics": _define_semantics,
    "sql_query": _sql_query,
    "describe_asset": _describe_asset,
    "connect_source": _connect_source,
    "propose_stage_decision": _propose_stage_decision,
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
            toolset="data-steward",
            schema=fn,
            handler=_HANDLERS[name],
            description=fn["description"],
            emoji="\U0001f5c3",  # card file box
        )
