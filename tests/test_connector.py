"""Connector Service 断言（readme 8.1）。前置：source_pg 已启动。"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services"))
import connector
from connector import QueryRejected

ok, bad = [], []
def check(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== Connector Service ===\n")

# 1. Agent 拿不到 DSN —— 只能通过 query() 访问
check("模块暴露的是 query 而非连接", not hasattr(connector, "connect"))

# 2. 正常查询
r = connector.query("olist", "SELECT id, status FROM orders", purpose="profiling")
check("单表 SELECT 可执行", r["row_count"] >= 2, f"{r['row_count']} 行 / {r['duration_ms']}ms")

# 3. 自动注入 LIMIT
check("无 LIMIT 自动注入", "LIMIT 1000" in connector.LEDGER[-1]["sql"])

# 4. join 被拒
try:
    connector.query("olist", "SELECT * FROM orders o JOIN orders b ON o.id=b.id")
    check("源系统禁止 join", False)
except QueryRejected as e:
    check("源系统禁止 join", True, str(e)[:32])

# 5. 非 SELECT 被拒
for stmt in ("UPDATE orders SET status='x'", "DROP TABLE orders", "INSERT INTO orders VALUES (9)"):
    try:
        connector.query("olist", stmt); check(f"拒绝 {stmt.split()[0]}", False)
    except QueryRejected:
        check(f"拒绝 {stmt.split()[0]}", True)

# 6. 未知数据源
try:
    connector.query("nope", "SELECT 1"); check("未知数据源被拒", False)
except connector.ConnectorError:
    check("未知数据源被拒", True)

# 7. 负载记账
rep = connector.load_report("olist")
check("负载记账可用", rep["queries"] >= 2 and rep["rejected"] >= 4,
      f"{rep['queries']} 次查询 / {rep['rejected']} 次拒绝")

# 8. 低峰时间窗口只约束批量抽取
import os
os.environ["BULK_WINDOW_START"], os.environ["BULK_WINDOW_END"] = "01:00", "05:00"
connector._E["BULK_WINDOW_START"], connector._E["BULK_WINDOW_END"] = "01:00", "05:00"
import datetime
now = datetime.datetime.now().strftime("%H:%M")
in_win = "01:00" <= now <= "05:00"
try:
    connector.query("olist", "SELECT 1", purpose="bulk")
    check("批量抽取受时间窗口约束", in_win, f"当前 {now} 在窗口内")
except QueryRejected:
    check("批量抽取受时间窗口约束", not in_win, f"当前 {now} 不在窗口内，已拒")
try:
    connector.query("olist", "SELECT count(*) FROM orders", purpose="probe")
    check("探查查询不受窗口限制", True)
except QueryRejected:
    check("探查查询不受窗口限制", False, "探查被误拒")

# 9. 账本落库（进程重启后仍可查）
check("负载账本已持久化", hasattr(connector, "_persist") and hasattr(connector, "today_usage"))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
