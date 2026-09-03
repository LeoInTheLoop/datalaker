"""数据工具断言（readme 4.6 支柱二）。前置：northwind 已导入。"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services"))
import connector
import data_tools as D
from data_tools import BadIdentifier

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== 数据工具（默认参数化；模型 SQL 走受审入口）===\n")

t = D.list_source_tables("northwind")
chk("列出源表", len(t["tables"]) >= 10, f"{len(t['tables'])} 张")

m = D.get_table_metadata("northwind", "orders")
chk("元数据：列与主键", len(m["columns"]) == 14 and m["primary_key"] == ["order_id"],
    f"{len(m['columns'])} 列 pk={m['primary_key']}")

p = D.profile_table("northwind", "orders")
chk("画像：采样统计", p["sampled_rows"] == 830, f"{p['sampled_rows']} 行")
oid = next(c for c in p["columns"] if c["column"] == "order_id")
chk("主键被识别为唯一", oid["looks_unique"])

d = D.run_dq_check("northwind", "orders")
chk("DQ 判定通过（无 high）", d["passed"])
chk("DQ 抓到高空值率列",
    any(f["issue"] == "high_null_rate" for f in d["findings"]),
    next((f["column"] for f in d["findings"] if f["issue"] == "high_null_rate"), ""))
chk("DQ 结论不含原始数据行",
    all("value" not in str(f).lower() or "null_rate" in str(f) for f in d["findings"]))

# 参数化：标识符注入被挡
for evil in ("orders; DROP TABLE x", "orders'--", "a b", ""):
    try:
        D.get_table_metadata("northwind", evil)
        chk(f"标识符注入被拒: {evil[:18]!r}", False, "竟然通过")
    except (BadIdentifier, Exception) as e:
        chk(f"标识符注入被拒: {evil[:18]!r}", True, type(e).__name__)

try:
    D.sql_query(source_id="northwind", plane="mars", sql="SELECT 1")
    chk("未知 SQL plane 被拒", False)
except connector.QueryRejected:
    chk("未知 SQL plane 被拒", True)

# 元数据通道不绕过业务 SQL gate
try:
    connector.query("northwind", "SELECT * FROM orders o JOIN customers c ON 1=1")
    chk("业务 JOIN 需审批", False)
except connector.QueryRejected:
    chk("业务 JOIN 需审批", True)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
