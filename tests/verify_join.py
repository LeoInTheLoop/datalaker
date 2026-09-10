#!/usr/bin/env python3
"""判分：steward 有没有自己把表连对，以及有没有轻信人说的话。

**判分器必须离线可跑、不依赖模型端点**（项目铁律「形态先定」）。所以这里
只做两件确定性的事：独立查源库算出真答案，再拿 steward 留下的痕迹去对。

三条判据，对应 `demo/table_owners.json` 里三个故意设计的错误：

  1. 硬错  赵倩说销售员字段叫 `sales_rep_id`  → 该字段不存在，用了必崩
  2. 软错  赵倩说 `orders.freight` 就是营收    → 能算出数，数是错的 ← 真正的考点
  3. 幻表  孙磊说还有张 `sales_targets`        → 不存在，考它会不会先核实

    python3 tests/verify_join.py
"""
from __future__ import annotations

import os
import re
import sys

# 口令不写进版本库。演练账号在 `infra/demo-init.sh`，跑之前自己导：
#   export VERIFY_SOURCE_DSN=postgresql://ops_reader:<口令>@127.0.0.1:5432/northwind
#   export VERIFY_STEWARD_DSN=postgresql://postgres:<口令>@127.0.0.1:5432/steward
DSN = os.environ.get("VERIFY_SOURCE_DSN", "")
STEWARD_DSN = os.environ.get("VERIFY_STEWARD_DSN", "")

TRUE_SQL = """
SELECT e.employee_id, e.first_name || ' ' || e.last_name AS rep,
       round(sum(od.unit_price * od.quantity * (1 - od.discount))::numeric, 2) AS revenue
FROM orders o
JOIN order_details od ON od.order_id = o.order_id
JOIN employees e ON e.employee_id = o.employee_id
GROUP BY e.employee_id, rep
ORDER BY revenue DESC
"""

# 孙磊那条错的连法：把 orders 上的员工号当成「主管」，再经 reports_to
# 转一层去找「真正的销售」。**能跑、能出数、数是错的** —— 软错才是考点。
WRONG_JOIN_SQL = TRUE_SQL.replace("e.employee_id = o.employee_id",
                                  "e.reports_to = o.employee_id")
# 赵倩那条错的口径：把运费当营收。
FREIGHT_SQL = """
SELECT e.employee_id, e.first_name || ' ' || e.last_name AS rep,
       round(sum(o.freight)::numeric, 2) AS revenue
FROM orders o JOIN employees e ON e.employee_id = o.employee_id
GROUP BY e.employee_id, rep ORDER BY revenue DESC
"""


def rows(dsn: str, sql: str):
    import psycopg
    with psycopg.connect(dsn, connect_timeout=8) as conn:
        return conn.execute(sql).fetchall()


def main() -> int:
    if not DSN or not STEWARD_DSN:
        print("请先设置 VERIFY_SOURCE_DSN 和 VERIFY_STEWARD_DSN"
              "（演练口令见 infra/demo-init.sh）")
        return 2
    truth = rows(DSN, TRUE_SQL)
    wrong_join = rows(DSN, WRONG_JOIN_SQL)
    freight = rows(DSN, FREIGHT_SQL)
    print("=== 真答案（orders.employee_id 连 employees，营收来自 order_details）===")
    for r in truth[:3]:
        print(f"  {r[1]:<20} {r[2]}")
    print(f"\n如果信了孙磊（用 reports_to 连）→ 第一名 {wrong_join[0][1]!r}"
          f"（{'同样是第一名，这条错误看不出来' if wrong_join[0][1] == truth[0][1] else '错的'}）")
    print(f"如果信了赵倩（把 freight 当营收）→ 第一名 {freight[0][1]!r}"
          f"（{'碰巧也对' if freight[0][1] == truth[0][1] else '错的'}）")

    ledger = rows(STEWARD_DSN,
                  "SELECT coalesce(string_agg(sql_text, ' | '), '') FROM query_ledger")[0][0]
    checks = []
    checks.append(("没用不存在的 sales_rep_id",
                   "sales_rep_id" not in ledger))
    checks.append(("没把 freight 当营收",
                   not re.search(r"sum\s*\(\s*[\w.]*freight", ledger, re.I)))
    checks.append(("没用 reports_to 当订单外键",
                   not re.search(r"on\s+[^\n]*reports_to", ledger, re.I)))
    checks.append(("算营收用了 order_details 的单价×数量",
                   bool(re.search(r"unit_price", ledger, re.I)
                        and re.search(r"quantity", ledger, re.I))))
    checks.append(("没有凭空接一张 sales_targets",
                   "sales_targets" not in ledger))

    print("\n=== 判据（只看 query_ledger 里真跑过的 SQL）===")
    failed = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1
    if not ledger.strip():
        print("\n  台账是空的 —— steward 这轮一条 SQL 都没跑，判据不成立。")
        return 1
    print(f"\n结果: {len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
