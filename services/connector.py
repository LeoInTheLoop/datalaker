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


def _admit(sql: str) -> str:
    """语句准入。

    ponytail: R1 仍是关键字判断，与 plugin 侧的 _sql_guard 同源。
    TODO(R3): 统一换成 sqlglot 解析 AST，两处共用一个实现。
    """
    low = " ".join(sql.lower().split())
    head = low.split(" ", 1)[0] if low else ""
    if head not in ("select", "show", "describe", "explain"):
        raise QueryRejected("仅允许 SELECT / SHOW / DESCRIBE")
    if " join " in low:
        raise QueryRejected("源系统上不允许 join —— 请分别抽取后在 lake 中关联")
    if head == "select" and " limit " not in low:
        sql = sql.rstrip("; ") + f" LIMIT 1000"
    return sql


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
