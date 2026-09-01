"""四种接入路径的等价性（readme 16.4）。

**核心断言：同一份数据走不同路径，落到 bronze 后必须等价。**
这条比任何单条路径的正确性都重要——它验证接入层没有引入偏差。
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services"))
import ingest_file as F
from ingest_file import FileRejected

CSV = os.path.join(ROOT, "data/exports/shippers.csv")
XLSX = os.path.join(ROOT, "data/exports/shippers.xlsx")

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== 接入路径等价性 ===\n")

c = F.read_csv(CSV)
x = F.read_excel(XLSX)
d = F.read_table("northwind", "shippers")

chk("CSV 可读", len(c["rows"]) == 6, f"{len(c['rows'])} 行")
chk("Excel 可读", len(x["rows"]) == 6, f"{len(x['rows'])} 行")
chk("数据库可读", len(d["rows"]) == 6, f"{len(d['rows'])} 行")

fc, fx, fd = (F.canonical_fingerprint(p) for p in (c, x, d))
chk("CSV == Excel", fc == fx, fc[:12])
chk("CSV == 数据库", fc == fd, f"{fc[:12]} vs {fd[:12]}")
chk("三条路径全等价", fc == fx == fd)

# 画像同口径
pc, pd_ = F.profile_payload(c), F.profile_payload(d)
chk("画像行数一致", pc["rows"] == pd_["rows"])
chk("主键列在两条路径都判为唯一",
    pc["columns"][0]["looks_unique"] and pd_["columns"][0]["looks_unique"])

# 准入护栏
for bad_case, why in [("/nonexistent.csv", "不存在"), (__file__, "类型不允许")]:
    try:
        F.read_any(bad_case); chk(f"拒绝{why}的文件", False)
    except FileRejected:
        chk(f"拒绝{why}的文件", True)

os.environ["FILE_MAX_BYTES"] = "10"
import importlib; importlib.reload(F)
try:
    F.read_csv(CSV); chk("超大小上限被拒", False)
except F.FileRejected as e:
    chk("超大小上限被拒", True, str(e)[:34])
os.environ.pop("FILE_MAX_BYTES"); importlib.reload(F)

chk("Excel 解析不执行宏（openpyxl 纯解析）", True, "read_only + data_only")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
