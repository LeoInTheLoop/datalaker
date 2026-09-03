"""数据工具（readme 4.6 支柱二）。

默认走参数化工具；模型生成 SQL 只能进入 `sql_query()`，先过 gate /
Connector 的 AST preflight。理由有二：

1. 安全：AST 准入只挡语法，挡不住语义。参数化从根上不给自由度
2. 可读：审批邮件里 `ingest_table(FIN.monthly)` 比一段 SQL 清楚得多，
   而审批人每周只有 2–3 小时，读不懂就只能盲批

所有源系统访问都经 Connector Service（第 8 节），受只读、负载准入、
LIMIT 注入、串行队列、时间窗口约束。已入湖数据走 `plane=lake`，
只能查询 lake catalog。

分层（readme 4.5）：本模块是**纯函数层**，不注册为工具的部分直接被
Pipeline 调用；注册为工具的部分（见 policy.py）才暴露给模型。
"""
import os
import re

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 「看起来是空」的常见写法。真实数据里这些比 NULL 更常见
BLANK_TOKENS = "('null', 'none', 'n/a', 'na', 'nan', '-', '--', " \
               "'未知', '无', '空', 'unknown', 'not available')"


class BadIdentifier(ValueError):
    """表名/列名不合法。参数化的第一道关：标识符必须是纯标识符。"""


def _ident(name, what="标识符"):
    if not name or not IDENT.match(str(name)):
        raise BadIdentifier(f"{what}不合法: {name!r}（只允许字母、数字、下划线）")
    return name


def _q(source_id, sql, purpose):
    import connector
    return connector.query(source_id, sql, purpose=purpose)


def sql_query(source_id: str | None = None, sql: str = "", plane: str = "source",
              purpose: str = "ad_hoc",
              _sql_gate_approved: bool = False) -> dict:
    """模型生成 SQL 的执行入口。

    `_sql_gate_approved` 只应由 Hermes `pre_tool_call` 注入；模型自己传入时
    gate 会剥掉。真正的源系统连接仍在 Connector 里。
    """
    p = (plane or "source").lower()
    if p not in ("source", "lake"):
        import connector
        raise connector.QueryRejected(f"未知 SQL plane: {plane!r}")
    if p == "lake":
        import lakehouse
        return lakehouse.query(sql, purpose=purpose,
                               approved=(_sql_gate_approved is True))

    import connector
    return connector.query(source_id, sql, purpose=purpose,
                           approved=(_sql_gate_approved is True))


# ---------------------------------------------------------------- L0 发现
def list_source_tables(source_id: str) -> dict:
    """列出源系统的表与行数估算。L0：只读元数据，不碰数据。"""
    import connector
    return {"source": source_id,
            "tables": [{"table": a, "approx_rows": b}
                       for a, b in connector.list_tables(source_id)]}


def get_table_metadata(source_id: str, table: str) -> dict:
    """列、类型、是否可空、主键。L0。

    走 Connector 的**元数据通道**：系统目录查询天然要 join，
    但数据量极小；不给业务护栏开豁免，而是把这类查询也参数化。
    """
    _ident(table, "表名")
    import connector
    d = connector.describe_table(source_id, table)
    return {"table": table,
            "columns": [{"name": c, "type": t, "nullable": n == "YES"}
                        for c, t, n in d["columns"]],
            "primary_key": d["primary_key"]}


# ---------------------------------------------------------------- L1 画像
def profile_table(source_id: str, table: str, sample_rows: int = 50000) -> dict:
    """数据画像。

    **源上只采样**（readme 5.3）：分布类统计对采样不敏感，
    而全量扫描的代价由别人的生产库承担。
    主键唯一性、跨字段一致性这类必须全量的，落 bronze 后在 lake 里算。
    """
    _ident(table, "表名")
    meta = get_table_metadata(source_id, table)
    cols = [c["name"] for c in meta["columns"]]
    if not cols:
        return {"table": table, "error": "无列信息"}

    # 一次查询算完所有列，而不是每列一次 —— 减少对源库的往返
    # bronze 原样落地意味着「空」有多种形式：NULL、空串、以及各类占位符。
    # 只算 IS NULL 会严重低估——真实脏数据里空串和「未知」比 NULL 更常见。
    parts = ["count(*) AS _n"]
    for c in cols:
        _ident(c, "列名")
        parts.append(f'count("{c}") AS "{c}__nonnull"')
        parts.append(f'count(DISTINCT "{c}") AS "{c}__distinct"')
        parts.append(
            f'count(*) FILTER (WHERE "{c}" IS NULL '
            f'OR btrim("{c}"::text) = \'\' '
            f'OR lower(btrim("{c}"::text)) IN {BLANK_TOKENS}) AS "{c}__blank"')
    # **无序 LIMIT 有系统性偏差**：Postgres 读的是堆的前段，
    # 而 UPDATE 过的行会被追加到堆尾。R5 实测：向 99441 行的表注入 8000 个 NULL
    # （占 8%），LIMIT 50000 的采样窗口里一个都没命中，DQ 完全检不出。
    # 真实源库里「最近改过的行」恰恰是最该看的那批。
    # TABLESAMPLE 是块级随机，堆尾同样有机会被选中（readme 5.3 明确提到它）。
    sub, how = _sample_clause(source_id, table, sample_rows)
    sql = f'SELECT {", ".join(parts)} FROM {sub} _s'
    try:
        r = _q(source_id, sql, purpose="profiling")
    except Exception:                                        # noqa: BLE001
        # 方言不支持 TABLESAMPLE 或准入不放行时退回原路径，但**如实标注**
        how = "limit_fallback"
        r = _q(source_id, f'SELECT {", ".join(parts)} FROM '
                          f'(SELECT * FROM "{table}" LIMIT {int(sample_rows)}) _s',
               purpose="profiling")
    row = r["rows"][0]
    names = r["columns"]
    v = dict(zip(names, row))
    n = int(v["_n"]) or 1

    out = []
    for c in cols:
        nonnull = int(v[f"{c}__nonnull"])
        distinct = int(v[f"{c}__distinct"])
        blank = int(v.get(f"{c}__blank", 0) or 0)
        out.append({
            "column": c,
            "null_rate": round(1 - nonnull / n, 4),      # 严格 NULL
            "blank_rate": round(blank / n, 4),           # NULL + 空串 + 占位符
            "distinct": distinct,
            "distinct_rate": round(distinct / n, 4) if n else 0,
            "is_constant": distinct <= 1 and n > 1,
            "looks_unique": distinct == n and n > 1,
        })
    return {"table": table, "sampled_rows": n, "sample_limit": sample_rows,
            "sampling": how, "columns": out}


# 低于这个行数用逐行采样（BERNOULLI），以上用块级（SYSTEM）
BERNOULLI_MAX_ROWS = int(os.environ.get("CLAW_BERNOULLI_MAX_ROWS", "2000000"))


def _sample_clause(source_id, table, sample_rows):
    """取样子句。表比样本还小就整表扫，没必要绕。"""
    approx = 0
    try:
        for t, n in __import__("connector").list_tables(source_id):
            if t == table:
                approx = int(n or 0)
                break
    except Exception:                                        # noqa: BLE001
        approx = -1
    if approx > 0 and approx <= sample_rows:
        return f'(SELECT * FROM "{table}")', "full"
    if approx > 0:
        # 不放大比例、也不再截断：TABLESAMPLE 取 65% 的块之后再 LIMIT 5 万，
        # 等于「随机选块 + 只读前段」，偏差原封不动地回来了。
        # 让比例本身决定样本量，LIMIT 只当安全上限（一般不生效）。
        pct = max(0.1, min(100.0, 100.0 * sample_rows / approx))
        # **SYSTEM 是整块取舍，BERNOULLI 是逐行。**
        # UPDATE 过的行会聚在堆尾的少数几个块里，SYSTEM 一旦没选中那几块，
        # 整批注入全漏 —— 实测 customers 表的两类注入就是这样一起消失的。
        # BERNOULLI 要全表扫，但十万行量级代价可忽略；
        # 真正的大表交给 SYSTEM，那时块级偏差换来的扫描节省才划算。
        method = "BERNOULLI" if approx <= BERNOULLI_MAX_ROWS else "SYSTEM"
        # **REPEATABLE 让采样可复现。** 没有它，同一份数据两次画像会给出
        # 不同的 DQ 结论，跨 run 的数字就不可比 —— 而可复现是发布的前提。
        seed = os.environ.get("CLAW_SAMPLE_SEED", "")
        rep = f" REPEATABLE ({int(seed)})" if seed.strip().lstrip("-").isdigit() else ""
        return (f'(SELECT * FROM "{table}" TABLESAMPLE {method} ({pct:.3f}){rep} '
                f'LIMIT {int(sample_rows * 2)})',
                f"{method.lower()}_{pct:.2f}pct"
                + ("_seeded" if rep else "_unseeded"))
    return f'(SELECT * FROM "{table}" LIMIT {int(sample_rows)})', "limit"


def _approx_rows(source_id, table) -> int:
    """行数估算。拿不到返回 -1（未知），**不返回 0** —— 0 会被当成空表。"""
    try:
        for t, n in __import__("connector").list_tables(source_id):
            if t == table:
                return int(n or -1)
    except Exception:                                        # noqa: BLE001
        pass
    return -1


def sample_values(source_id: str, table: str, columns: list,
                  sample_rows: int = 20000) -> dict:
    """取一批样本行的原始值，供 `dq_rules` 的内容型规则使用。

    走 Connector 的**画像取样入口**（参数化，不是自由 SQL）。
    自由 SQL 那条路走不通：不写 LIMIT 会被夹到 1000 行，
    显式写大 LIMIT 又要走审批 —— 而那道护栏防的是
    「模型生成的大结果进上下文」，这里两条都不成立：
    SQL 由代码拼、只传表名列名，取回的行只喂给纯函数算结论（readme 13.1）。

    **不给护栏开豁免，改成参数化入口** —— 与 R3 决策 22 同一条原则。
    """
    import connector
    for c in columns:
        _ident(c, "列名")
    _ident(table, "表名")

    approx = _approx_rows(source_id, table)
    pct, method = None, "BERNOULLI"
    if approx > sample_rows:
        pct = max(0.1, min(100.0, 100.0 * sample_rows / approx))
        method = "BERNOULLI" if approx <= BERNOULLI_MAX_ROWS else "SYSTEM"
    raw = os.environ.get("CLAW_SAMPLE_SEED", "")
    seed = int(raw) if raw.strip().lstrip("-").isdigit() else None

    r = connector.sample_rows(source_id, table, columns, pct=pct, seed=seed,
                              method=method, cap=sample_rows)
    by_col = {c: [] for c in columns}
    idx = {name: i for i, name in enumerate(r["columns"])}
    for row in r["rows"]:
        for c in columns:
            by_col[c].append(row[idx[c]] if c in idx else None)
    how = f"{method.lower()}_{pct:.2f}pct" if pct else "full"
    return {"table": table, "sampling": how + ("_seeded" if seed else ""),
            "rows": len(r["rows"]), "values": by_col}


DEFAULT_THRESHOLDS = {
    "null_rate_max": 0.05,          # 关键字段空值率上限
    "pk_unique_min": 1.0,           # 主键唯一性必须 100%
    "format_conformance_min": 0.95,  # 格式合规率
    "fk_integrity_min": 0.99,        # 外键引用完整性在 lake 上算，避免压源库
}


def run_dq_check(source_id: str, table: str, thresholds: dict | None = None) -> dict:
    """按门槛判定，产出**结论**而非数据行。

    「够干净了」必须量化，否则永远不会结束（readme 5.3）。
    返回的 findings 是给模型看的结论，不含任何原始值——
    这既是合规要求，也让 token 消耗低一个数量级（13.1）。
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    prof = profile_table(source_id, table)
    if "error" in prof:
        return prof
    meta = get_table_metadata(source_id, table)
    pk = set(meta["primary_key"])

    findings = []
    for c in prof["columns"]:
        if c["column"] in pk and not c["looks_unique"]:
            findings.append({"column": c["column"], "issue": "primary_key_not_unique",
                             "severity": "high",
                             "detail": f"主键列去重后 {c['distinct']} < 采样 {prof['sampled_rows']}"})
        # 用 blank_rate 判定：bronze 里空串与占位符和 NULL 是一回事
        rate = max(c.get("blank_rate", 0), c["null_rate"])
        if rate > th["null_rate_max"]:
            how = ("空值" if c["null_rate"] >= c.get("blank_rate", 0)
                   else "空值/占位符")
            findings.append({"column": c["column"], "issue": "high_null_rate",
                             "severity": "medium",
                             "detail": f"{how}率 {rate:.1%} 超过阈值 {th['null_rate_max']:.0%}"})
        if c["is_constant"]:
            findings.append({"column": c["column"], "issue": "constant_column",
                             "severity": "low", "detail": "全表同一个值，可能是废弃字段"})

    # --- 内容型规则（dq_rules，纯函数）---
    # 只有这三条聚合规则时，十类缺陷里有六类注入了也测不到（R5 首轮实测）。
    # 内容型规则要看原始值，因此单取一次样本 —— 仍是单表 SELECT。
    content = []
    try:
        import dq_rules
        cand = [c["column"] for c in prof["columns"]
                if not c["is_constant"] and not c["looks_unique"]]
        if cand:
            sv = sample_values(source_id, table, cand[:40])
            for col, vals in sv["values"].items():
                content += dq_rules.check_column(col, vals)
        # 同表跨列的日期比较：不是 join，可以在源上算
        dcols = [c["column"] for c in prof["columns"]
                 if "date" in c["column"] or "timestamp" in c["column"]]
        if len(dcols) >= 2 and cand:
            sv2 = sample_values(source_id, table, dcols[:4])
            base = dcols[0]
            for other in dcols[1:]:
                r = dq_rules.date_before(
                    list(zip(sv2["values"][base], sv2["values"][other])),
                    base, other)
                if r:
                    content.append({"column": other, **r})
    except Exception as e:                                   # noqa: BLE001
        content.append({"column": "*", "issue": "content_rules_failed",
                        "severity": "low",
                        "detail": f"{type(e).__name__}: {str(e)[:120]}"})
    findings += content

    passed = not any(f["severity"] == "high" for f in findings)
    return {"table": table, "passed": passed, "thresholds": th,
            "findings": findings, "sampled_rows": prof["sampled_rows"],
            "sampling": prof.get("sampling"),
            "rules_applied": ["high_null_rate", "primary_key_not_unique",
                              "constant_column"] + sorted(dq_rules.RULES)
                             + ["date_before_order"]}


# ------------------------------------------------------------ DQ 门槛与收敛
# readme 5.3：「够干净了」必须量化，否则永远不会结束。
# 阈值默认值由 Agent 提案、Owner 确认 —— 确认后沉淀进 asset_semantics，
# 下次直接用，不重复问（5.7）。
THRESHOLD_KEY = "dq_thresholds"


def get_thresholds(asset: str) -> dict:
    """取该资产的 DQ 门槛。没人确认过就用默认值，并标明「未经确认」。"""
    import json as _j
    from plugins.datasteward_gate.approvals import open_store
    try:
        with open_store(readonly=True, init_schema=False) as st:
            v = st.known(asset, THRESHOLD_KEY)
    except Exception:                                        # noqa: BLE001
        v = None
    if not v:
        return {**DEFAULT_THRESHOLDS, "_confirmed": False}
    raw = v["value"] if isinstance(v, dict) else (
        v[0] if isinstance(v, (list, tuple)) else v)
    try:
        return {**DEFAULT_THRESHOLDS, **_j.loads(raw), "_confirmed": True}
    except Exception:                                        # noqa: BLE001
        return {**DEFAULT_THRESHOLDS, "_confirmed": False}


def propose_thresholds(asset: str, overrides: dict | None = None) -> dict:
    """给 Owner 的门槛提案。**只提案不生效**——确认走审批链路。"""
    th = {**DEFAULT_THRESHOLDS, **(overrides or {})}
    return {"asset": asset, "thresholds": th, "confirmed": False,
            "question": f"{asset} 的数据质量门槛定成这样可以吗？"
                        f"达标即进 silver，不达标则最多修 3 轮后转人工。"}


VERDICTS = ("pass_to_silver", "propose_fix", "needs_human_review")


def dq_verdict(source_id: str, table: str, thresholds: dict | None = None) -> dict:
    """把 DQ 结论变成**一个可执行的裁决**，而不是一堆 findings。

    三种结果，对应 readme 5.3 的收敛条件：

        pass_to_silver      达标，进 silver
        propose_fix         未达标但还有修复轮次
        needs_human_review  3 轮用尽仍未达标 —— 业务规则问题，退出自动处理

    第三种是这套设计的关键：**不收敛时必须退出，而不是继续烧钱**。
    """
    import memory as _m
    asset = f"{source_id}.{table}"
    th = thresholds or get_thresholds(asset)
    dq = run_dq_check(source_id, table, {k: v for k, v in th.items()
                                         if not k.startswith("_")})
    if "error" in dq:
        return {"asset": asset, "verdict": "needs_human_review",
                "reason": dq["error"], "thresholds": th}

    unmet = [f for f in dq["findings"] if f["severity"] in ("high", "medium")]
    exhausted = [f["issue"] for f in unmet if _m.is_exhausted(asset, f["issue"])]

    if not unmet:
        verdict, reason = "pass_to_silver", "全部门槛达标"
    elif exhausted:
        verdict = "needs_human_review"
        reason = (f"{exhausted} 已修满 {_m.MAX_ATTEMPTS} 轮仍未达标——"
                  f"这是业务规则问题，不再自动处理")
    else:
        verdict, reason = "propose_fix", f"{len(unmet)} 项未达标，仍有修复轮次"

    return {"asset": asset, "verdict": verdict, "reason": reason,
            "thresholds": {k: v for k, v in th.items() if not k.startswith("_")},
            "thresholds_confirmed": bool(th.get("_confirmed")),
            "unmet": unmet, "exhausted": exhausted,
            "passed": verdict == "pass_to_silver",
            "sampled_rows": dq.get("sampled_rows")}
