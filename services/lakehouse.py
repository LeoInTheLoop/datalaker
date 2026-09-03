"""Lakehouse 查询入口。

这里处理已经复制进 datalake 的表。它和外部源系统不是一个风险等级：
在 `iceberg` 上 join / 聚合是正常分析；通过 `postgres` / `northwind`
catalog 查源表则仍然是外部源负载，不能混进 lake 模式。
"""
import csv
import io
import os
import subprocess
import time

import connector


TRINO_C = os.environ.get("TRINO_CONTAINER", "datalaker-trino-1")
TRINO_USER = os.environ.get("TRINO_READ_USER", os.environ.get("TRINO_WRITE_USER", "claw"))
TRINO_TIMEOUT = int(os.environ.get("TRINO_QUERY_TIMEOUT", "180"))


class LakehouseError(RuntimeError):
    pass


def query(sql: str, purpose: str = "ad_hoc", approved: bool = False) -> dict:
    """查询已入湖数据。

    只允许读 `iceberg.*`。写 lake 的动作必须走 `ingest_table` /
    `apply_cleaning_rule` / `publish_gold` 这类显式工具，不能塞进自由 SQL。
    """
    review = connector.review_sql(sql, approved=approved, plane="lake")
    if review.action == "reject":
        raise connector.QueryRejected(review.message)
    if review.action == "needs_approval":
        raise connector.QueryApprovalRequired(review.message)

    t0 = time.time()
    r = subprocess.run(
        ["docker", "exec", TRINO_C, "trino", "--user", TRINO_USER,
         "--output-format", "CSV_HEADER", "--execute", review.sql],
        capture_output=True, text=True, timeout=TRINO_TIMEOUT)
    if r.returncode != 0:
        raise LakehouseError((r.stderr or r.stdout).strip()[:300])

    lines = [l for l in r.stdout.splitlines()
             if l and not l.startswith("Picked up JAVA_TOOL_OPTIONS")]
    if not lines:
        return {"plane": "lake", "columns": [], "rows": [],
                "row_count": 0, "duration_ms": round((time.time() - t0) * 1000, 1),
                "purpose": purpose}

    reader = csv.reader(io.StringIO("\n".join(lines)))
    parsed = list(reader)
    columns = parsed[0] if parsed else []
    rows = [tuple(row) for row in parsed[1:]]
    return {"plane": "lake", "columns": columns, "rows": rows,
            "row_count": len(rows), "duration_ms": round((time.time() - t0) * 1000, 1),
            "purpose": purpose}


# ---------------------------------------------------------------- lake 侧 DQ
# readme 5.3：**把全量扫描从别人的库搬到自己的库**。
# 主键唯一性、外键完整性、跨表矛盾必须全量算，而源库禁 join（铁律 3），
# 因此它们只能在 bronze 落地之后于 lake 上做。这是「join 不是不做，
# 是挪到了 lake 里做」的兑现处。
BRONZE = "iceberg.bronze"


def _t(name):
    return f'{BRONZE}."{name}"'


def _row1(sql):
    """取第一行并按列拆开。

    `sync._trino` 返回的是 CSV **行**列表 —— 一行两列拿到的是 `"a,b"` 一个元素，
    不是两个。直接解包会 `not enough values to unpack`，而那个异常被
    `lake_dq_check` 吃掉记成「出错」，表面上看是「检查跑了但没发现」。
    """
    import sync
    rows = sync._trino(sql)
    if not rows:
        return []
    return [x.strip() for x in rows[0].split(",")]


def pk_unique_full(bronze_table: str, pk: str) -> dict:
    """全量主键唯一性。源上只能采样，这里是唯一能给出确定结论的地方。"""
    n, d = (int(x) for x in _row1(
        f'SELECT count(*), count(DISTINCT "{pk}") FROM {_t(bronze_table)}'))
    if n == d:
        return {"issue": None, "rows": n, "distinct": d, "passed": True}
    return {"issue": "pk_unique_full", "severity": "high", "passed": False,
            "rows": n, "distinct": d,
            "detail": f"{n:,} 行只有 {d:,} 个不同主键，重复 {n - d:,} 行"}


def broken_foreign_key(child_table: str, child_col: str,
                       parent_table: str, parent_col: str) -> dict:
    """外键完整性。**这就是那个被挪到 lake 里做的 join。**"""
    bad, total = (int(x) for x in _row1(
        f'SELECT count(*) FILTER (WHERE p."{parent_col}" IS NULL), count(*) '
        f'FROM {_t(child_table)} c LEFT JOIN {_t(parent_table)} p '
        f'ON c."{child_col}" = p."{parent_col}" '
        f'WHERE c."{child_col}" IS NOT NULL'))
    rate = 1 - (bad / total) if total else 1.0
    return {"issue": "broken_foreign_key" if bad else None,
            "severity": "high" if bad else None, "passed": bad == 0,
            "orphans": bad, "checked": total, "integrity": round(rate, 4),
            "detail": (f"{bad:,}/{total:,} 行的 {child_col} 指向不存在的 "
                       f"{parent_table}.{parent_col}（完整率 {rate:.2%}）"
                       if bad else "外键完整")}


def total_mismatch(head_table: str, head_key: str, head_total: str,
                   line_table: str, line_key: str, line_amount: str,
                   tolerance: float = 0.01) -> dict:
    """跨表矛盾：头表金额 ≠ 明细求和。同样是 lake 上的 join。"""
    bad, total = (int(x) for x in _row1(
        f'SELECT count(*) FILTER (WHERE abs(h.t - l.s) > {tolerance}), count(*) FROM '
        f'(SELECT "{head_key}" k, CAST("{head_total}" AS double) t '
        f' FROM {_t(head_table)}) h '
        f'JOIN (SELECT "{line_key}" k, sum(CAST("{line_amount}" AS double)) s '
        f' FROM {_t(line_table)} GROUP BY 1) l ON h.k = l.k'))
    return {"issue": "total_mismatch" if bad else None,
            "severity": "high" if bad else None, "passed": bad == 0,
            "mismatched": bad, "checked": total,
            "detail": (f"{bad:,}/{total:,} 单的 {head_total} 与明细求和不符"
                       if bad else "头表与明细一致")}


def lake_dq_check(spec: dict) -> dict:
    """按声明跑一组 lake 侧检查。

    spec 形如：
        {"pk": [{"table": "northwind__orders", "column": "order_id"}],
         "fk": [{"child": "...", "child_col": "...",
                 "parent": "...", "parent_col": "..."}],
         "totals": [{...}]}
    """
    findings, errors = [], []

    def _run(fn, label, table, column, *a, **k):
        """`table` / `column` 显式传 —— 从 label 里反解会丢列名，
        调用方就只能猜，实测猜错过一次（把 products 的发现记成了 customers 的列）。"""
        try:
            r = fn(*a, **k)
        except Exception as e:                               # noqa: BLE001
            errors.append(f"{label}: {type(e).__name__}: {str(e)[:120]}")
            return
        if r.get("issue"):
            findings.append({"check": label, "table": table, "column": column, **r})

    for x in spec.get("pk", []):
        _run(pk_unique_full, f"pk:{x['table']}", x["table"], x["column"],
             x["table"], x["column"])
    for x in spec.get("fk", []):
        _run(broken_foreign_key, f"fk:{x['child']}.{x['child_col']}",
             x["child"], x["child_col"],
             x["child"], x["child_col"], x["parent"], x["parent_col"])
    for x in spec.get("totals", []):
        _run(total_mismatch, f"total:{x['head']}", x["head"], x["head_total"],
             x["head"], x["head_key"], x["head_total"], x["line"],
             x["line_key"], x["line_amount"])

    return {"findings": findings, "errors": errors,
            "passed": not findings and not errors,
            "note": "全量扫描在自己的地盘上做 —— 源库只采样（readme 5.3）"}
