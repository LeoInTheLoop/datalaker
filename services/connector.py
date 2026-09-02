"""Connector Service —— 唯一的数据出入口（readme 第 8 节）。

架构位置比功能完整度更重要：**Agent 不持有 DSN**，只能提交 {source_id, sql}。
排队与限流因此是架构性强制，不是纪律。

ponytail: R1 做成进程内模块。DSN 只在本模块读取，工具函数拿不到。
需要跨进程隔离时（R3）再包一层 HTTP，调用方接口不变。
"""
import json
import pathlib
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _env():
    d = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


_E = _env()

# source_id -> DSN。**只有本模块读取这个表**
_SOURCES = {
    "olist": _E.get("SOURCE_DSN", ""),
    "northwind": _E.get("NORTHWIND_DSN", "")
                 or (_E.get("SOURCE_DSN", "").replace("/olist", "/northwind")),
    "acme": _E.get("ACME_DSN", "")
            or (_E.get("SOURCE_DSN", "").replace("/olist", "/acme")),
}

# 每个源一把锁：concurrency = 1，串行执行（readme 8.1 并发与排队）
_LOCKS = {sid: threading.Lock() for sid in _SOURCES}

MAX_SCAN_ROWS = int(_E.get("CONNECTOR_MAX_SCAN_ROWS", "5000000"))
STATEMENT_TIMEOUT_MS = int(_E.get("CONNECTOR_STATEMENT_TIMEOUT_MS", "30000"))

# 负载记账（readme 8.1）：本周对该源造成了多少负载
LEDGER: list[dict] = []


class ConnectorError(Exception):
    pass


class QueryRejected(ConnectorError):
    """未通过准入。Agent 应当改写查询而非重试。"""


DEFAULT_LIMIT = int(_E.get("CONNECTOR_DEFAULT_LIMIT", "1000"))


def _admit(sql: str) -> str:
    """语句准入：**AST 解析，不是关键字匹配**（readme 8.1）。

    关键字匹配挡不住的，AST 能挡：

    | 绕过手法 | 关键字判断 | AST |
    |---|---|---|
    | `SELECT 1; DROP TABLE x` 多语句 | ❌ 只看第一个词 | ✅ 解析出 2 条 |
    | `SELECT/**/1 FROM a JOIN b` 注释分隔 | ❌ 匹配不到 " join " | ✅ 看结构 |
    | `SELECT * FROM (SELECT..JOIN..)` 子查询 | ❌ | ✅ 遍历整棵树 |
    | `WITH x AS (DELETE..) SELECT` CTE 写操作 | ❌ 头部是 with | ✅ |

    反过来，注释里出现 "join" 这个词不再误伤——**看结构不看字面**。
    """
    import sqlglot
    from sqlglot import exp

    raw = (sql or "").strip()
    if not raw:
        raise QueryRejected("空语句")

    try:
        stmts = sqlglot.parse(raw, read="postgres")
    except Exception as e:
        raise QueryRejected(f"SQL 无法解析，拒绝执行：{str(e)[:80]}")

    stmts = [st for st in stmts if st is not None]
    if len(stmts) != 1:
        raise QueryRejected(f"只允许单条语句，收到 {len(stmts)} 条")

    tree = stmts[0]
    if not isinstance(tree, (exp.Select, exp.Show, exp.Describe)):
        raise QueryRejected(
            f"仅允许 SELECT / SHOW / DESCRIBE，收到 {type(tree).__name__.upper()}")

    # 整棵树里不允许出现写操作（含 CTE、子查询内部）
    WRITE = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
             exp.Alter, exp.TruncateTable, exp.Merge)
    for node in tree.walk():
        if isinstance(node, WRITE):
            raise QueryRejected(f"语句中包含写操作 {type(node).__name__.upper()}")

    # join 检查遍历整棵树 —— 子查询与 CTE 里的 join 同样拦下
    if list(tree.find_all(exp.Join)):
        raise QueryRejected("源系统上不允许 join —— 请分别抽取后在 lake 中关联")
    # 逗号连接的隐式 join：FROM a, b
    for scope in tree.find_all(exp.From):
        if len(scope.expressions) > 1:
            raise QueryRejected("源系统上不允许 join（FROM 多表）")

    if isinstance(tree, exp.Select) and not tree.args.get("limit"):
        tree = tree.limit(DEFAULT_LIMIT)

    return tree.sql(dialect="postgres")


def _estimate(cur, sql: str) -> float:
    """先 EXPLAIN 估算扫描行数，超阈值直接拒绝（readme 8.1 单查询护栏）。"""
    try:
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        plan = cur.fetchone()[0]
        rows = plan[0]["Plan"].get("Plan Rows", 0)
        return float(rows)
    except Exception:
        return -1.0        # 估不出来不阻断，但记账时标记


def _in_bulk_window() -> bool:
    """批量抽取是否在允许的时间窗口内（readme 8.1 时间窗口）。"""
    import datetime
    start = _E.get("BULK_WINDOW_START", "")
    end = _E.get("BULK_WINDOW_END", "")
    if not (start and end):
        return True
    now = datetime.datetime.now().strftime("%H:%M")
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end          # 跨零点


def query(source_id: str, sql: str, purpose: str = "") -> dict:
    """Agent 访问源系统的唯一入口。

    purpose='bulk' 的查询受低峰时间窗口约束；探查类不受限。
    """
    import psycopg

    if purpose == "bulk" and not _in_bulk_window():
        w = f"{_E.get('BULK_WINDOW_START')}-{_E.get('BULK_WINDOW_END')}"
        _record(source_id, sql, purpose, 0, 0, -1, f"REJECTED: 非低峰窗口({w})")
        raise QueryRejected(
            f"批量抽取只在 {w} 执行。当前不在窗口内，任务已排队至下个窗口。")

    if source_id not in _SOURCES:
        raise ConnectorError(f"未知数据源: {source_id}")
    dsn = _SOURCES[source_id]
    if not dsn:
        raise ConnectorError(f"数据源 {source_id} 未配置 DSN")

    t0 = time.time()
    try:
        sql = _admit(sql)
    except QueryRejected as e:
        # 被拒的查询同样入账 —— 「Agent 尝试过什么危险操作」是审计的一部分
        _record(source_id, sql, purpose, 0, time.time() - t0, -1, f"REJECTED: {e}")
        raise

    with _LOCKS[source_id]:                       # 串行，排队无法绕过
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
                est = _estimate(cur, sql)
                if est > MAX_SCAN_ROWS:
                    _record(source_id, sql, purpose, 0, time.time() - t0, est,
                            f"REJECTED: 预估 {est:.0f} 行超限")
                    raise QueryRejected(
                        f"预估扫描 {est:.0f} 行，超过上限 {MAX_SCAN_ROWS}。请缩小范围或走增量。")
                cur.execute(sql)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchall()

    dur = time.time() - t0
    _record(source_id, sql, purpose, len(rows), dur, est, "OK")
    return {"columns": cols, "rows": rows, "row_count": len(rows),
            "duration_ms": round(dur * 1000, 1)}


def _record(source_id, sql, purpose, rows, dur, est, status):
    rec = {
        "ts": time.time(), "source": source_id, "purpose": purpose,
        "sql": sql[:200], "rows": rows, "duration_ms": round(dur * 1000, 1),
        "est_rows": est, "status": status,
    }
    LEDGER.append(rec)
    _persist(rec)


def _persist(rec):
    """落库。

    内存里的 LEDGER 进程一重启就没了 —— 记了等于没记。
    运维监控必须独立于被监控对象：Agent 挂掉时这些数据仍要可查。
    落库失败不能影响查询本身，故整体吞掉异常。
    """
    dsn = _E.get("STEWARD_AGENT_DSN") or _E.get("DATASTEWARD_DSN", "")
    if not dsn:
        return
    try:
        import psycopg
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as c:
            c.execute(
                "INSERT INTO query_ledger (source_id, purpose, sql_text, rows_out,"
                " duration_ms, est_rows, status) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (rec["source"], rec["purpose"], rec["sql"], rec["rows"],
                 rec["duration_ms"], rec["est_rows"], rec["status"]))
    except Exception:
        pass


def record_usage(model, prompt_tokens=0, output_tokens=0, cost_usd=0.0,
                 run_id="", purpose=""):
    """记录一次 LLM 调用。

    成本不可见就无法设上限。这张表是 `ops/claw-status.sh` 和
    第 5.6 节预算兜底的数据来源。
    """
    dsn = _E.get("STEWARD_AGENT_DSN") or _E.get("DATASTEWARD_DSN", "")
    if not dsn:
        return
    try:
        import psycopg
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as c:
            c.execute(
                "INSERT INTO usage_ledger (run_id, model, prompt_tokens,"
                " output_tokens, cost_usd, purpose) VALUES (%s,%s,%s,%s,%s,%s)",
                (run_id, model, prompt_tokens, output_tokens, cost_usd, purpose))
    except Exception:
        pass


def today_usage():
    """今日用量。预算兜底与状态面板共用。"""
    dsn = _E.get("STEWARD_AGENT_DSN") or _E.get("DATASTEWARD_DSN", "")
    if not dsn:
        return {"calls": 0, "tokens": 0, "cost_usd": 0.0}
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT count(*), coalesce(sum(prompt_tokens+output_tokens),0),"
                    " coalesce(sum(cost_usd),0) FROM usage_ledger"
                    " WHERE ts >= date_trunc('day', now())")
                n, tok, cost = cur.fetchone()
                return {"calls": n, "tokens": int(tok), "cost_usd": float(cost)}
    except Exception:
        return {"calls": 0, "tokens": 0, "cost_usd": 0.0}


# ---------------------------------------------------------------------------
# 元数据查询：与业务查询分开
#
# 系统目录（pg_catalog / information_schema）查询天然要 join，
# 但数据量只有几十行，不构成负担——用「禁 join」拦它是误伤。
#
# 不给护栏开 purpose 豁免（Agent 可以声称自己在做 discovery），
# 而是**把元数据查询也参数化**：SQL 写死在这里，调用方只能传表名，
# 且表名先过标识符校验。这与 4.6「参数化优于自由 SQL」是同一条原则。
# ---------------------------------------------------------------------------
_IDENT = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _meta_exec(source_id: str, sql: str, args=(), purpose="metadata"):
    import psycopg
    if source_id not in _SOURCES:
        raise ConnectorError(f"未知数据源: {source_id}")
    t0 = time.time()
    with _LOCKS.setdefault(source_id, threading.Lock()):
        with psycopg.connect(_SOURCES[source_id], connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
                cur.execute(sql, args)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchall()
    _record(source_id, sql, purpose, len(rows), time.time() - t0, -1, "OK")
    return {"columns": cols, "rows": rows, "row_count": len(rows)}


def list_tables(source_id: str) -> list:
    """表清单与行数估算。SQL 固定，无参数。"""
    r = _meta_exec(source_id,
                   "SELECT relname, n_live_tup FROM pg_stat_user_tables "
                   "ORDER BY n_live_tup DESC")
    return [(a, int(b)) for a, b in r["rows"]]


def describe_table(source_id: str, table: str) -> dict:
    """列定义与主键。表名是唯一参数，且必须是纯标识符。"""
    if not _IDENT.match(table or ""):
        raise QueryRejected(f"表名不合法: {table!r}")
    cols = _meta_exec(source_id,
                      "SELECT column_name, data_type, is_nullable "
                      "FROM information_schema.columns "
                      "WHERE table_schema='public' AND table_name=%s "
                      "ORDER BY ordinal_position", (table,))
    pk = _meta_exec(source_id,
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid "
                    "AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary",
                    (table,))
    return {"columns": cols["rows"], "primary_key": [r[0] for r in pk["rows"]]}


def list_foreign_keys(source_id: str) -> list:
    """外键关系。走元数据通道，SQL 固定无参数。"""
    # 用 pg_catalog 而非 information_schema：
    # 后者的视图按当前用户权限过滤，只读账号看不到约束定义
    r = _meta_exec(source_id, """
        SELECT c.conrelid::regclass::text  AS tbl,
               a.attname                   AS col,
               c.confrelid::regclass::text AS ref_tbl,
               af.attname                  AS ref_col
        FROM pg_constraint c
        JOIN pg_attribute a  ON a.attrelid  = c.conrelid  AND a.attnum  = c.conkey[1]
        JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = c.confkey[1]
        WHERE c.contype = 'f'
          AND c.connamespace = 'public'::regnamespace
    """)
    return [tuple(row) for row in r["rows"]]


def load_report(source_id: str | None = None) -> dict:
    """负载记账 —— 进周报交给 DBA。"""
    rs = [r for r in LEDGER if source_id is None or r["source"] == source_id]
    return {
        "queries": len(rs),
        "rejected": sum(1 for r in rs if r["status"].startswith("REJECTED")),
        "rows_returned": sum(r["rows"] for r in rs),
        "total_ms": round(sum(r["duration_ms"] for r in rs), 1),
        "slowest": max((r for r in rs), key=lambda r: r["duration_ms"], default=None),
    }
