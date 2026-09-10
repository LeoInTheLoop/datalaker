"""Connector Service —— 唯一的数据出入口（readme 第 8 节）。

架构位置比功能完整度更重要：**Agent 不持有 DSN**，只能提交 {source_id, sql}。
排队与限流因此是架构性强制，不是纪律。

ponytail: R1 做成进程内模块。DSN 只在本模块读取，工具函数拿不到。
需要跨进程隔离时（R3）再包一层 HTTP，调用方接口不变。
"""
import json
import os
import pathlib
import sys
import threading
import time
from dataclasses import dataclass
from typing import Literal

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _env():
    # Docker deliberately does not mount the developer's .env into the Agent
    # container.  Process environment is the runtime contract; the local file
    # remains a convenience fallback for scripts launched from the repository.
    d = dict(os.environ)
    path = ROOT / ".env"
    if not path.exists():
        return d
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d.setdefault(k, v)
    return d


_E = _env()

# ---------------------------------------------------------------------------
# 数据源注册（readme 8 凭证层）
#
# **Agent 天生不该持有任何库的凭证。** 硬编码 _SOURCES 等于它一上线
# 就能连所有库——现实中第一步是问人「公司有哪些系统」，
# 再走 L3 审批开只读账号，凭证进 secret store，最后才注册进来。
#
# 这里保留一份「引导源」用于开发与测试；生产应当为空，
# 全部经 register_source() 在审批通过后动态注册。
# ---------------------------------------------------------------------------
# **引导源可以整组关掉。** 只要它非空，Agent 不注册也连得上 ——
# 于是「源是人给的」就还是一句口号：eval 里的 trap 碰不碰得到、
# 发现覆盖率是不是真的靠问人问出来的，全都失真。
# 生产必须为空（这一节开头就写着）；eval 跑真链路时也必须关。
_NO_BOOTSTRAP = os.environ.get("CLAW_NO_BOOTSTRAP_SOURCES", "") in ("1", "true", "yes")

_BOOTSTRAP = {} if _NO_BOOTSTRAP else {
    "olist": _E.get("SOURCE_DSN", ""),
    "northwind": _E.get("NORTHWIND_DSN", "")
                 or (_E.get("SOURCE_DSN", "").replace("/olist", "/northwind")),
    "acme": _E.get("ACME_DSN", "")
            or (_E.get("SOURCE_DSN", "").replace("/olist", "/acme")),
    "olist_raw": _E.get("OLIST_DSN", "")
                 or (_E.get("SOURCE_DSN", "").replace("/olist", "/olist_raw")),
    # 文件 / SaaS 导出的暂存区（readme 8.2）。它不是源系统，
    # 但 Agent 对它同样只读 —— 写入由 export_ingest 用独立账号做。
    "stage": _E.get("STAGE_DSN", "")
             or (_E.get("SOURCE_DSN", "").replace("/olist", "/stage")),
}

_REGISTERED: dict = {}


def register_source(source_id: str, dsn: str, approval_id: str | None = None,
                    kind: str = "postgres", description: str = "",
                    by: str = "", persist: bool = True):
    """审批通过后注册一个数据源。

    `approval_id` 是这条注册的依据——**没有批准就没有数据源**。

    **落库，不只落进程内存。** 早先只写 `_REGISTERED` 这个 dict：
    Hermes 每次会话是新进程，上一轮批准注册的源下一轮就不认了 ——
    表现是「明明批过了，还是说没注册」。凭证进 `source_secrets`
    （Agent 读不到那张表），可读的只有 `source_grants` 里的 source_id。
    """
    if not approval_id and os.environ.get("REQUIRE_SOURCE_APPROVAL", "1") != "0":
        raise ConnectorError(
            f"注册数据源 {source_id} 需要审批依据（approval_id）——"
            f"Agent 不能自行给自己开数据源")
    _REGISTERED[source_id] = dsn
    _LOCKS.setdefault(source_id, threading.Lock())
    if persist:
        _persist_source(source_id, dsn, approval_id or "", kind, by)
    return {"source_id": source_id, "kind": kind, "approval_id": approval_id,
            "description": description}


def _persist_source(source_id, dsn, approval_id, kind, by):
    """凭证进 `source_secrets`，**可见性**进 `source_grants`。

    两张表分开是刻意的：门禁要知道「这个源被人给过」（读 grants），
    但不该、也不需要看到连接串（secrets 那张 Agent 侧无权限）。
    """
    import sys as _s
    _s.path.insert(0, str(ROOT / "plugins"))
    from datasteward_gate.approvals import open_store
    approval_id = str(approval_id or "")
    # The steward schema is provisioned by the database owner in the demo/
    # deployment.  Running DDL here under agent_role makes an otherwise valid
    # approved registration fail with "permission denied for table" before the
    # connector ever tests the supplied account.
    with open_store(readonly=False, init_schema=False) as st:
        st.put_source_secret(source_id, dsn, approval_id, by or "connect_source",
                             kind=kind)
        st.grant_source(source_id, by or "connect_source",
                        f"经批准注册（approval={approval_id[:8]}）")


def _load_registered(source_id: str) -> str:
    """从库里取已注册的连接串。**只有本模块调它。**

    工具函数拿不到 DSN —— 那是这个模块存在的全部理由（见文件开头）。
    """
    try:
        import sys as _s
        _s.path.insert(0, str(ROOT / "plugins"))
        from datasteward_gate.approvals import open_store, rows_of
        with open_store(readonly=False, init_schema=False) as st:
            if os.environ.get("DATASTEWARD_DSN") and hasattr(st.db, "cursor"):
                # The Agent role has no SELECT on source_secrets.  The
                # owner-controlled function returns only the approved secret
                # for this source to the Connector path.
                with st.db.cursor() as cur:
                    cur.execute("SELECT dsn FROM datasteward_get_source_secret(%s)",
                                (source_id,))
                    rows = cur.fetchall()
            else:
                rows = rows_of(st, "SELECT dsn FROM source_secrets WHERE source_id = {0}",
                               (source_id,))
            return rows[0][0] if rows else ""
    except Exception:                                        # noqa: BLE001
        pass
    return ""


def known_sources() -> list:
    return sorted(set(_BOOTSTRAP) | set(_REGISTERED))


def _dsn(source_id: str) -> str:
    # 顺序：本进程注册过的 → 库里注册过的（跨会话）→ 引导源（开发用，可关）
    dsn = (_REGISTERED.get(source_id) or _load_registered(source_id)
           or _BOOTSTRAP.get(source_id, ""))
    if not dsn:
        raise ConnectorError(
            f"数据源 {source_id} 未注册。Agent 不持有未经批准的凭证——"
            f"先问人有哪些系统，再走审批开只读账号。")
    return dsn


# 每个源一把锁：concurrency = 1，串行执行（readme 8.1 并发与排队）
_LOCKS = {sid: threading.Lock() for sid in _BOOTSTRAP}

MAX_SCAN_ROWS = int(_E.get("CONNECTOR_MAX_SCAN_ROWS", "5000000"))
STATEMENT_TIMEOUT_MS = int(_E.get("CONNECTOR_STATEMENT_TIMEOUT_MS", "30000"))
DEFAULT_LIMIT = int(_E.get("CONNECTOR_DEFAULT_LIMIT", "1000"))
SAFE_RESULT_ROWS = int(_E.get("CONNECTOR_SAFE_RESULT_ROWS", str(DEFAULT_LIMIT)))
MAX_RESULT_ROWS = int(_E.get("CONNECTOR_MAX_RESULT_ROWS", "100000"))
APPROVAL_SCAN_ROWS = int(_E.get("CONNECTOR_APPROVAL_SCAN_ROWS", "100000"))
LAKE_CATALOGS = {
    x.strip().lower()
    for x in _E.get("CONNECTOR_LAKE_CATALOGS", "iceberg").split(",")
    if x.strip()
}

# 负载记账（readme 8.1）：本周对该源造成了多少负载
LEDGER: list[dict] = []


class ConnectorError(Exception):
    pass


class QueryRejected(ConnectorError):
    """未通过准入。Agent 应当改写查询而非重试。"""


class QueryApprovalRequired(QueryRejected):
    """查询不是默认拒绝，但需要人确认后才能打到源系统。"""


@dataclass(frozen=True)
class SQLReview:
    action: Literal["allow", "modify", "needs_approval", "reject"]
    sql: str
    reasons: tuple[str, ...] = ()
    message: str = ""
    approver_role: str = "owner"


def _write_classes(exp):
    names = ("Insert", "Update", "Delete", "Drop", "Create", "Alter",
             "TruncateTable", "Merge")
    return tuple(cls for cls in (getattr(exp, n, None) for n in names) if cls)


def _read_roots(exp):
    names = ("Select", "Show", "Describe", "Union", "Except", "Intersect")
    return tuple(cls for cls in (getattr(exp, n, None) for n in names) if cls)


def _set_classes(exp):
    names = ("Union", "Except", "Intersect")
    return tuple(cls for cls in (getattr(exp, n, None) for n in names) if cls)


def _limit_value(tree) -> int | None:
    """只认固定数字 LIMIT；表达式/参数化 LIMIT 交给人工确认。"""
    limit = tree.args.get("limit")
    if not limit:
        return None
    expr = getattr(limit, "expression", None)
    if expr is None:
        expr = getattr(limit, "args", {}).get("expression")
    if expr is None:
        return None
    try:
        if getattr(expr, "is_int", False):
            return int(expr.this)
        return int(expr)
    except Exception:
        return None


def _has_implicit_multi_from(tree, exp) -> bool:
    for scope in tree.find_all(exp.From):
        n = 1 if getattr(scope, "this", None) is not None else 0
        n += len(getattr(scope, "expressions", None) or [])
        if n > 1:
            return True
    return False


def _outer_from(tree):
    """外层 SELECT 自己的 FROM。sqlglot 30 把键名从 `from` 改成了 `from_`，
    两个都试 —— 只试一个的后果是这条判断在某个版本上静默失效。"""
    return tree.args.get("from") or tree.args.get("from_")


def _scan_bounded(tree, exp, cap: int) -> bool:
    """外层聚合的**每一个输入关系**都被字面量 LIMIT 限死了吗。

    为什么要有这条：`review_sql` 的「无过滤聚合可能触发全表扫描」看的是
    外层有没有 WHERE。但下面这种写法扫描量其实已经封顶了 ——

        SELECT status, COUNT(*)
        FROM (SELECT status FROM fin_invoice LIMIT 200) t
        GROUP BY status

    内层 `LIMIT 200` 把输入基数钉死在 200 行，外层聚合最多也就看这 200 行。
    live eval 实测撞到：模型写出了这种**正确的有界查询**，照样被要求审批
    （R6 §13）。误判方向是安全的，但它教模型「写对了也要等人」。

    **只认这一种形状，不做基数估算。** 每个 source 必须是带字面量 LIMIT
    的子查询，且那个 LIMIT 不超过「要审批的扫描量」阈值。任何一个拿不准
    —— 表达式 LIMIT、裸表、CTE、集合运算 —— 一律返回 False 走原路。
    宁可多问一次，不可放过一次全表扫。
    """
    f = _outer_from(tree)
    if f is None:
        return False
    sources = []
    if getattr(f, "this", None) is not None:
        sources.append(f.this)
    sources += list(getattr(f, "expressions", None) or [])
    for j in tree.args.get("joins") or []:
        if getattr(j, "this", None) is None:
            return False
        sources.append(j.this)
    if not sources:
        return False
    for src in sources:
        if not isinstance(src, exp.Subquery):
            return False
        inner = src.this
        if not isinstance(inner, exp.Select):
            return False
        n = _limit_value(inner)
        if n is None or n > cap:
            return False
    return True


def _parse_read_sql(sql: str, dialect: str = "postgres"):
    import sqlglot
    from sqlglot import exp

    raw = (sql or "").strip()
    if not raw:
        return None, exp, "空语句"

    try:
        stmts = sqlglot.parse(raw, read=dialect)
    except Exception as e:
        return None, exp, f"SQL 无法解析，拒绝执行：{str(e)[:80]}"

    stmts = [st for st in stmts if st is not None]
    if len(stmts) != 1:
        return None, exp, f"只允许单条语句，收到 {len(stmts)} 条"

    tree = stmts[0]
    if not isinstance(tree, _read_roots(exp)):
        return None, exp, (
            f"仅允许 SELECT / SHOW / DESCRIBE，收到 {type(tree).__name__.upper()}")

    for node in tree.walk():
        if isinstance(node, _write_classes(exp)):
            return None, exp, f"语句中包含写操作 {type(node).__name__.upper()}"

    return tree, exp, ""


def _table_scope_error(tree, exp, plane: str, dialect: str) -> str:
    tables = list(tree.find_all(exp.Table))
    if plane == "lake":
        unqualified = []
        external = []
        for t in tables:
            catalog = (t.catalog or "").lower()
            rendered = t.sql(dialect=dialect)
            if not catalog:
                unqualified.append(rendered)
            elif catalog not in LAKE_CATALOGS:
                external.append(rendered)
        if external:
            return ("lake 模式只允许查询已复制到 datalake 的表，发现外部 catalog："
                    f"{', '.join(external[:3])}")
        if unqualified:
            return ("lake 模式必须显式写出 lake catalog，例如 "
                    "`iceberg.bronze.table`，不能使用未限定表："
                    f"{', '.join(unqualified[:3])}")
    else:
        cataloged = [t.sql(dialect=dialect) for t in tables if t.catalog]
        if cataloged:
            return ("source 模式不允许三段式 catalog 查询；外部源只能经 "
                    "Connector 当前 source_id 执行，lake 表请使用 plane='lake'："
                    f"{', '.join(cataloged[:3])}")
    return ""


def review_sql(sql: str, *, approved: bool = False,
               model_generated: bool = False,
               plane: Literal["source", "lake"] = "source") -> SQLReview:
    """SQL 执行前审查：默认放行、需要审批、默认拒绝分开。

    这里解决的是「模型生成的 SQL 不能直接打源系统」：

    - 写入/DDL/多语句：默认拒绝
    - 可能明显放大源系统负载的读：先问人
    - 轻量读：放行；无 LIMIT 时自动补默认 LIMIT

    底层仍用 `sqlglot` 看 AST，不用正则。
    """
    if plane not in ("source", "lake"):
        return SQLReview("reject", sql, message=f"未知 SQL plane: {plane!r}")

    dialect = "trino" if plane == "lake" else "postgres"
    tree, exp, err = _parse_read_sql(sql, dialect=dialect)
    if err:
        return SQLReview("reject", sql, message=err)

    err = _table_scope_error(tree, exp, plane, dialect)
    if err:
        return SQLReview("reject", sql, message=err)

    reasons: list[str] = []
    if plane == "source" and (list(tree.find_all(exp.Join))
                              or _has_implicit_multi_from(tree, exp)):
        reasons.append("包含 JOIN/多表关联，可能放大源系统扫描与锁等待")
    if plane == "source" and isinstance(tree, _set_classes(exp)):
        reasons.append("包含集合查询，可能触发多路扫描")

    if isinstance(tree, exp.Select):
        limit = tree.args.get("limit")
        limit_value = _limit_value(tree)
        if limit is None:
            tree = tree.limit(DEFAULT_LIMIT)
        elif limit_value is None:
            reasons.append("LIMIT 不是固定数字，无法静态判断返回规模")
        elif limit_value > MAX_RESULT_ROWS:
            return SQLReview(
                "reject", tree.sql(dialect=dialect),
                message=(f"请求返回 {limit_value:,} 行，超过硬上限 "
                         f"{MAX_RESULT_ROWS:,}，不会发起审批。"))
        elif plane == "source" and limit_value > SAFE_RESULT_ROWS:
            reasons.append(
                f"请求返回 {limit_value:,} 行，超过默认安全上限 {SAFE_RESULT_ROWS:,}")

        # 排序与聚合是同一条判断的两个兄弟：担心的都是「输入有多大」。
        # 输入已经被字面量 LIMIT 钉死时两条一起放过 —— 同一个 AST 事实，
        # 同一个 helper，不是新机制。live 实测模型写的正是
        # `... FROM (SELECT ... LIMIT 2000) t GROUP BY ... ORDER BY n DESC`：
        # 聚合那条放过了，排序这条照样拦，于是它还是等审批。
        if (plane == "source" and model_generated and tree.args.get("order")
                and not tree.args.get("where")
                and not _scan_bounded(tree, exp, APPROVAL_SCAN_ROWS)):
            reasons.append("无过滤排序可能触发大表排序")
        if (plane == "source" and model_generated and not tree.args.get("where")
                and (tree.args.get("group") or list(tree.find_all(exp.AggFunc)))
                and not _scan_bounded(tree, exp, APPROVAL_SCAN_ROWS)):
            reasons.append("无过滤聚合可能触发全表扫描")

    rewritten = tree.sql(dialect=dialect)
    if reasons and not approved:
        return SQLReview("needs_approval", rewritten, tuple(reasons),
                         "；".join(reasons), "owner")
    if rewritten.strip() != (sql or "").strip():
        return SQLReview("modify", rewritten)
    return SQLReview("allow", rewritten)


def _admit(sql: str, *, approved: bool = False,
           plane: Literal["source", "lake"] = "source") -> str:
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
    review = review_sql(sql, approved=approved, plane=plane)
    if review.action == "reject":
        raise QueryRejected(review.message)
    if review.action == "needs_approval":
        raise QueryApprovalRequired(review.message)
    return review.sql


def _estimate(cur, sql: str) -> float:
    """先 EXPLAIN 估算扫描行数，超过审批阈值问人，超过硬上限拒绝。"""
    def rows(plan):
        own = float(plan.get("Plan Rows", 0) or 0)
        child = max((rows(p) for p in plan.get("Plans", []) or []), default=0.0)
        return max(own, child)

    try:
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        plan = cur.fetchone()[0]
        return rows(plan[0]["Plan"])
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


def query(source_id: str, sql: str, purpose: str = "",
          approved: bool = False) -> dict:
    """Agent 访问源系统的唯一入口。

    purpose='bulk' 的查询受低峰时间窗口约束；探查类不受限。
    """
    import psycopg

    if purpose == "bulk" and not _in_bulk_window():
        w = f"{_E.get('BULK_WINDOW_START')}-{_E.get('BULK_WINDOW_END')}"
        _record(source_id, sql, purpose, 0, 0, -1, f"REJECTED: 非低峰窗口({w})")
        raise QueryRejected(
            f"批量抽取只在 {w} 执行。当前不在窗口内，任务已排队至下个窗口。")

    dsn = _dsn(source_id)

    t0 = time.time()
    try:
        sql = _admit(sql, approved=approved)
    except QueryApprovalRequired as e:
        _record(source_id, sql, purpose, 0, time.time() - t0, -1,
                f"PENDING_APPROVAL: {e}")
        raise
    except QueryRejected as e:
        # 被拒的查询同样入账 —— 「Agent 尝试过什么危险操作」是审计的一部分
        _record(source_id, sql, purpose, 0, time.time() - t0, -1, f"REJECTED: {e}")
        raise

    with _LOCKS.setdefault(source_id, threading.Lock()):                       # 串行，排队无法绕过
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
                est = _estimate(cur, sql)
                if est > MAX_SCAN_ROWS:
                    _record(source_id, sql, purpose, 0, time.time() - t0, est,
                            f"REJECTED: 预估 {est:.0f} 行超限")
                    raise QueryRejected(
                        f"预估扫描 {est:.0f} 行，超过上限 {MAX_SCAN_ROWS}。请缩小范围或走增量。")
                if est > APPROVAL_SCAN_ROWS and not approved:
                    _record(source_id, sql, purpose, 0, time.time() - t0, est,
                            f"PENDING_APPROVAL: 预估 {est:.0f} 行")
                    raise QueryApprovalRequired(
                        f"预估扫描 {est:.0f} 行，超过默认安全上限 "
                        f"{APPROVAL_SCAN_ROWS}。需要 Owner 确认后执行。")
                cur.execute(sql)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchall()

    dur = time.time() - t0
    _record(source_id, sql, purpose, len(rows), dur, est, "OK")
    return {"columns": cols, "rows": rows, "row_count": len(rows),
            "duration_ms": round(dur * 1000, 1)}


_PERSIST_WARNED = False


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
    落库失败不能影响查询本身，所以仍然不往上抛。

    **但不能不出声。** 这里原先漏写 `ts` 列（表上 NOT NULL 且无默认），
    每次 INSERT 都 NotNullViolation，被 `except: pass` 吞掉 —— 于是
    「Agent 跑过哪些 SQL」这条审计轨整个不存在，而且看起来一切正常：
    工具返回成功、事件记 TOOL_ok、只有台账是空的。R6 真实演练里
    14 次 sql_query 成功、台账 0 行才发现（自增 id 已经到 76）。
    静默的兜底会把「坏了」和「没事发生」变成同一个样子。
    """
    dsn = _E.get("STEWARD_AGENT_DSN") or _E.get("DATASTEWARD_DSN", "")
    if not dsn:
        return
    try:
        import psycopg
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as c:
            c.execute(
                "INSERT INTO query_ledger (ts, source_id, purpose, sql_text, rows_out,"
                " duration_ms, est_rows, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (rec["ts"], rec["source"], rec["purpose"], rec["sql"], rec["rows"],
                 rec["duration_ms"], rec["est_rows"], rec["status"]))
    except Exception as exc:                                # noqa: BLE001
        global _PERSIST_WARNED
        if not _PERSIST_WARNED:
            _PERSIST_WARNED = True
            print(f"query_ledger 落库失败，SQL 审计轨将缺失：{type(exc).__name__}: "
                  f"{str(exc).strip()[:200]}", file=sys.stderr)


def _usage_store(readonly=True):
    """走 Store，不再自己连 DSN。

    原来这里直接 `psycopg.connect(DSN)`，于是**没有 DSN 时整段静默失效**——
    本地 SQLite 上跑，账本永远是空的，预算兜底（5.6）等于没有。
    Store 已经把两个后端都实现了，用它就不必维护第二份。
    """
    import sys as _s
    _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "plugins"))
    from datasteward_gate.approvals import open_store
    return open_store(readonly=readonly, init_schema=not readonly)


def record_usage(model, prompt_tokens=0, output_tokens=0, cost_usd=0.0,
                 run_id="", purpose=""):
    """记录一次 LLM 调用。

    成本不可见就无法设上限。这张表是 `ops/claw-status.py` 和
    第 5.6 节预算兜底的数据来源。
    """
    try:
        with _usage_store(readonly=False) as st:
            st.record_usage(model, prompt_tokens, output_tokens, cost_usd,
                            run_id, purpose)
    except Exception:
        pass


def today_usage():
    """今日用量。预算兜底与状态面板共用。"""
    import datetime as _dt
    midnight = _dt.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        with _usage_store() as st:
            return st.today_usage(midnight)
    except Exception:
        return {"calls": 0, "tokens": 0, "cost_usd": 0.0}


# ---------------------------------------------------------------------------
# 元数据查询：与业务查询分开
#
# 系统目录（pg_catalog / information_schema）查询天然要 join，
# 但数据量只有几十行，不构成负担；它走固定参数化通道，不走模型 SQL。
#
# 不给护栏开 purpose 豁免（Agent 可以声称自己在做 discovery），
# 而是**把元数据查询也参数化**：SQL 写死在这里，调用方只能传表名，
# 且表名先过标识符校验。这与 4.6「参数化优于自由 SQL」是同一条原则。
# ---------------------------------------------------------------------------
_IDENT = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _meta_exec(source_id: str, sql: str, args=(), purpose="metadata"):
    import psycopg
    t0 = time.time()
    with _LOCKS.setdefault(source_id, threading.Lock()):
        with psycopg.connect(_dsn(source_id), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
                cur.execute(sql, args)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchall()
    _record(source_id, sql, purpose, len(rows), time.time() - t0, -1, "OK")
    return {"columns": cols, "rows": rows, "row_count": len(rows)}


def list_tables(source_id: str) -> list:
    """表清单与行数估算。SQL 固定，无参数。

    两处曾经出错，都不是小事：

    1. **不限 schema** —— `pg_stat_user_tables` 覆盖所有 schema，
       同名表会重复出现，别的 schema（比如 eval 的脏副本）会污染源清单。
       现在只看 `current_schema()`，也就是连接串里 search_path 指到的那个。
    2. **只看 `n_live_tup`** —— 它由统计收集器维护，容器重启后归零。
       而「单表扫描可用行数估算」是铁律 3 的前提，估算给 0 等于前提失效。
       `pg_class.reltuples` 存在系统目录里，重启不丢，用它兜底。
       都拿不到时返回 -1 **表示未知**，而不是 0 —— 0 会被当成空表。
    """
    r = _meta_exec(source_id,
                   "SELECT c.relname,"
                   " GREATEST(COALESCE(s.n_live_tup, 0), COALESCE(c.reltuples, -1))"
                   " FROM pg_class c"
                   " JOIN pg_namespace n ON n.oid = c.relnamespace"
                   " LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid"
                   " WHERE c.relkind = 'r' AND n.nspname = current_schema()"
                   " ORDER BY 2 DESC, 1")
    return [(a, int(b) if b is not None and int(b) >= 0 else -1)
            for a, b in r["rows"]]


def validate_read_access(source_id: str, tables=None) -> list:
    """确认注册的账号至少能读出一张表的列定义。

    PostgreSQL 的 information_schema 允许已登录但没有表权限的账号看到
    很少或没有列；只用 ``list_tables`` 做接入探针会把这种账号误判成可用，
    直到同步阶段才拼出空列 DDL。接入时就把失败说清楚，避免后续产生假成功。
    """
    tables = list_tables(source_id) if tables is None else tables
    for table, _ in tables:
        meta = describe_table(source_id, table)
        if meta.get("columns"):
            return tables
    raise ConnectorError(
        f"账号已登录，但 {source_id} 没有任何可读表的 SELECT 权限")


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
    out = {"columns": cols["rows"], "primary_key": [r[0] for r in pk["rows"]]}
    _catalog_observed(source_id, table, out)
    return out


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
    fks = [tuple(row) for row in r["rows"]]
    _catalog_fks(source_id, fks)
    return fks


# 采集即建档（R6 闭环 A）。**观测事实只从这里进档案** ——
# 把落档挂在 Connector 上而不是某个工具上，是因为读结构的路径不止一条
# （describe_asset / get_table_metadata / profile_table / sync 都会读），
# 挂在工具上就会漏，而漏掉的那次正是「新会话查不到」的来源。
#
# 与 `_persist` 同一条原则：**落档失败不能影响查询本身**，整体吞掉异常。
# 但吞掉之后档案就是旧的 —— 所以读档那侧必须报出快照时刻（catalog.render）。
def _catalog_observed(source_id, table, meta):
    try:
        import catalog
        catalog.record_observed(source_id, table, meta)
    except Exception:                                        # noqa: BLE001
        pass


def _catalog_fks(source_id, fks):
    try:
        import catalog
        catalog.record_foreign_keys(source_id, fks)
    except Exception:                                        # noqa: BLE001
        pass


def load_report(source_id: str | None = None) -> dict:
    """负载记账 —— 进周报交给 DBA。"""
    rs = [r for r in LEDGER if source_id is None or r["source"] == source_id]
    return {
        "queries": len(rs),
        "rejected": sum(1 for r in rs if r["status"].startswith("REJECTED")),
        "pending_approval": sum(1 for r in rs
                                if r["status"].startswith("PENDING_APPROVAL")),
        "rows_returned": sum(r["rows"] for r in rs),
        "total_ms": round(sum(r["duration_ms"] for r in rs), 1),
        "slowest": max((r for r in rs), key=lambda r: r["duration_ms"], default=None),
    }


# ---------------------------------------------------------------- 权限元数据
# 与 `_meta_exec` 同一条分界：**元数据通道允许关系展开**（readme 8.2）。
# 系统目录上的 join 量小、低频，且禁掉只会逼出更贵的 N+1。
# SQL 全部写死在这里，调用方传不了任意语句。
def list_grants(source_id: str) -> list:
    """谁对哪些表有什么权限。只读、只观测（铁律 4）。"""
    r = _meta_exec(source_id,
                   "SELECT grantee, table_name, privilege_type, is_grantable"
                   " FROM information_schema.role_table_grants"
                   " WHERE table_schema = current_schema()"
                   " ORDER BY grantee, table_name, privilege_type")
    return [{"grantee": a, "table": b, "privilege": c, "grantable": d == "YES"}
            for a, b, c, d in r["rows"]]


def list_roles(source_id: str) -> list:
    """角色现状：能不能登录、是不是超级用户、继承了谁。"""
    r = _meta_exec(source_id,
                   "SELECT r.rolname, r.rolsuper, r.rolcanlogin, r.rolbypassrls,"
                   " COALESCE(string_agg(m.rolname, ','), '')"
                   " FROM pg_roles r"
                   " LEFT JOIN pg_auth_members am ON am.member = r.oid"
                   " LEFT JOIN pg_roles m ON m.oid = am.roleid"
                   " WHERE left(r.rolname, 3) <> 'pg_'"   # psycopg 会把 % 当占位符
                   " GROUP BY r.rolname, r.rolsuper, r.rolcanlogin, r.rolbypassrls"
                   " ORDER BY r.rolname")
    return [{"role": a, "superuser": b, "can_login": c, "bypass_rls": d,
             "member_of": [x for x in (e or "").split(",") if x]}
            for a, b, c, d, e in r["rows"]]


# ---------------------------------------------------------------- 画像取样
# **不给护栏开豁免，改成参数化入口** —— 与 R3 决策 22（元数据查询）同一条原则。
#
# 内容型 DQ 规则（enum 漂移、拼写漂移、类型污染…）必须看原始值，
# 而 `query()` 会把无 LIMIT 的语句夹到 1000 行、显式大 LIMIT 又要审批。
# 那道护栏防的是**模型自由生成的大结果进上下文**，这里两条都不成立：
#
#   · SQL 由代码拼，只传表名与列名，且都过标识符校验
#   · 取回的行**不进模型上下文**，只喂给纯函数算结论（readme 13.1）
#
# 因此单开一个入口，自带独立上限，而不是让调用方传 approved=True。
PROFILE_SAMPLE_ROWS = int(_E.get("CONNECTOR_PROFILE_SAMPLE_ROWS", "50000"))


def sample_rows(source_id: str, table: str, columns: list,
                pct: float | None = None, seed: int | None = None,
                method: str = "BERNOULLI", cap: int = 20000) -> dict:
    """按比例取样若干列的原始值。仍是单表 SELECT，禁 join 一条不放松。"""
    import psycopg

    if not _IDENT.match(table or ""):
        raise QueryRejected(f"表名不合法：{table!r}")
    for c in columns:
        if not _IDENT.match(c or ""):
            raise QueryRejected(f"列名不合法：{c!r}")
    if method not in ("BERNOULLI", "SYSTEM"):
        raise QueryRejected(f"未知采样方式：{method}")
    cap = max(1, min(int(cap), PROFILE_SAMPLE_ROWS))

    cols = ", ".join(f'"{c}"' for c in columns)
    samp = ""
    if pct and 0 < pct < 100:
        rep = f" REPEATABLE ({int(seed)})" if seed is not None else ""
        samp = f" TABLESAMPLE {method} ({pct:.3f}){rep}"
    sql = f'SELECT {cols} FROM "{table}"{samp} LIMIT {cap}'

    t0 = time.time()
    with _LOCKS.setdefault(source_id, threading.Lock()):
        with psycopg.connect(_dsn(source_id), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
                est = _estimate(cur, sql)
                if est > MAX_SCAN_ROWS:
                    _record(source_id, sql, "profiling", 0, time.time() - t0, est,
                            f"REJECTED: 预估 {est:.0f} 行超限")
                    raise QueryRejected(
                        f"画像取样预估扫描 {est:.0f} 行，超过上限 {MAX_SCAN_ROWS}")
                cur.execute(sql)
                names = [d.name for d in cur.description]
                rows = cur.fetchall()
    _record(source_id, sql, "profiling", len(rows), time.time() - t0, est, "OK")
    return {"columns": names, "rows": rows, "row_count": len(rows)}
