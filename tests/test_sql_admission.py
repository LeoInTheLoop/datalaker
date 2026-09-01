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
    ("显式 join",                    "SELECT * FROM a JOIN b ON 1=1", False),
    ("注释分隔 join",                "SELECT * FROM a/**/JOIN/**/b ON 1=1", False),
    ("子查询里的 join",              "SELECT * FROM (SELECT * FROM a JOIN b ON 1=1) x", False),
    ("隐式逗号 join",                "SELECT * FROM a, b", False),
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

chk("无 LIMIT 自动注入", "LIMIT" in C._admit("SELECT id FROM orders").upper())
chk("注释含 join 字样不误伤（看结构不看字面）",
    "LIMIT" in C._admit("SELECT id FROM orders /* join later */").upper())
chk("已有 LIMIT 不重复注入",
    C._admit("SELECT id FROM orders LIMIT 5").upper().count("LIMIT") == 1)

# gate 侧复用同一实现
from datasteward_gate import _sql_guard
r = _sql_guard({"sql": "SELECT * FROM a JOIN b ON 1=1"})
chk("gate 侧同样拦住 join", r and r.get("action") == "block", (r or {}).get("message", "")[:32])
r2 = _sql_guard({"sql": "SELECT id FROM orders"})
chk("gate 侧自动注入 LIMIT", r2 and r2.get("action") == "modify")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
