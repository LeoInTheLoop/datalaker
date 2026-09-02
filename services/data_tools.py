"""数据工具（readme 4.6 支柱二）。

**全部参数化，不接受自由 SQL** —— SQL 由代码生成。理由有二：

1. 安全：AST 准入只挡语法，挡不住语义。参数化从根上不给自由度
2. 可读：审批邮件里 `ingest_table(FIN.monthly)` 比一段 SQL 清楚得多，
   而审批人每周只有 2–3 小时，读不懂就只能盲批

所有源系统访问都经 Connector Service（第 8 节），受只读、禁 join、
LIMIT 注入、串行队列、时间窗口约束。

分层（readme 4.5）：本模块是**纯函数层**，不注册为工具的部分直接被
Pipeline 调用；注册为工具的部分（见 policy.py）才暴露给模型。
"""
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
    sql = (f'SELECT {", ".join(parts)} FROM '
           f'(SELECT * FROM "{table}" LIMIT {int(sample_rows)}) _s')
    r = _q(source_id, sql, purpose="profiling")
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
            "columns": out}


DEFAULT_THRESHOLDS = {
    "null_rate_max": 0.05,      # 关键字段空值率上限
    "pk_unique_min": 1.0,       # 主键唯一性必须 100%
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

    passed = not any(f["severity"] == "high" for f in findings)
    return {"table": table, "passed": passed, "thresholds": th,
            "findings": findings, "sampled_rows": prof["sampled_rows"]}
