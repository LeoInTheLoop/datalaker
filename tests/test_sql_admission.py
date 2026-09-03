"""SQL 准入断言（readme 8.1）：AST 解析，不是关键字匹配。

关键字匹配挡不住的手法，这里逐个验证。
gate 与 Connector **共用同一实现**——两处各写一套必然漂移。
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "services"))
sys.path.insert(0, os.path.join(ROOT, "plugins"))
import connector as C

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

CASES = [
    ("正常单表 SELECT",              "SELECT id FROM orders", True),
    ("source 显式 JOIN 不直放",       "SELECT * FROM a JOIN b ON 1=1", False),
    ("source 注释分隔 JOIN 不直放",   "SELECT * FROM a/**/JOIN/**/b ON 1=1", False),
    ("source 子查询 JOIN 不直放",     "SELECT * FROM (SELECT * FROM a JOIN b ON 1=1) x", False),
    ("source 隐式逗号 JOIN 不直放",   "SELECT * FROM a, b", False),
    ("多语句注入",                   "SELECT 1; DROP TABLE orders", False),
    ("UPDATE 伪装",                  "UPDATE orders SET x=1", False),
    ("CTE 内 DELETE",                "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", False),
    ("语法错误一律拒绝",             "SELECT FROM WHERE ((", False),
    ("空语句",                       "   ", False),
]
print("\n=== SQL 准入（AST）===\n")
for name, sql, should_pass in CASES:
    try:
        C._admit(sql); passed = True
    except C.QueryRejected:
        passed = False
    chk(name, passed == should_pass)

review = C.review_sql("SELECT id FROM orders LIMIT 10000", model_generated=True)
chk("大返回量 SELECT 不是默认放行而是要审批",
    review.action == "needs_approval" and "10,000" in review.message,
    review.message)
chk("模型生成无过滤聚合需审批",
    C.review_sql("SELECT count(*) FROM orders",
                 model_generated=True).action == "needs_approval")
chk("内部参数化聚合不走模型 SQL 审批",
    C.review_sql("SELECT count(*) FROM orders").action in ("allow", "modify"))
chk("集合查询需审批",
    C.review_sql("SELECT id FROM a UNION SELECT id FROM b").action == "needs_approval")
lake_join = C.review_sql(
    'SELECT * FROM iceberg.bronze."northwind__orders" o '
    'JOIN iceberg.bronze."northwind__customers" c ON o.customer_id=c.customer_id',
    plane="lake", model_generated=True)
chk("lake 已复制表允许 JOIN 分析",
    lake_join.action in ("allow", "modify") and "JOIN" in lake_join.sql.upper(),
    lake_join.action)
chk("lake 模式不能偷查外部 catalog",
    C.review_sql("SELECT * FROM postgres.public.orders",
                 plane="lake", model_generated=True).action == "reject")
chk("lake 模式必须显式写 iceberg catalog",
    C.review_sql("SELECT * FROM orders",
                 plane="lake", model_generated=True).action == "reject")
chk("source 模式不能查 lake catalog",
    C.review_sql('SELECT * FROM iceberg.bronze."northwind__orders"',
                 plane="source", model_generated=True).action == "reject")
try:
    C._admit("SELECT id FROM orders LIMIT 10000")
    chk("Connector 直执行大返回量也不会绕过审批", False)
except C.QueryApprovalRequired:
    chk("Connector 直执行大返回量也不会绕过审批", True)

danger = C.review_sql("DELETE FROM orders", model_generated=True)
chk("破坏性 SQL 默认拒绝，不发审批", danger.action == "reject",
    danger.message)

chk("无 LIMIT 自动注入", "LIMIT" in C._admit("SELECT id FROM orders").upper())
chk("注释含 join 字样不误伤（看结构不看字面）",
    "LIMIT" in C._admit("SELECT id FROM orders /* join later */").upper())
chk("已有 LIMIT 不重复注入",
    C._admit("SELECT id FROM orders LIMIT 5").upper().count("LIMIT") == 1)

# gate 侧复用同一实现
from datasteward_gate import _sql_guard
r = _sql_guard({"sql": "SELECT * FROM a JOIN b ON 1=1"})
chk("gate 侧 join 进入审批态", r and r.get("action") == "block"
    and "SQL_APPROVAL_REQUIRED" in r.get("message", ""),
    (r or {}).get("message", "")[:60])
r2 = _sql_guard({"sql": "SELECT id FROM orders"})
chk("gate 侧自动注入 LIMIT", r2 and r2.get("action") == "modify")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
