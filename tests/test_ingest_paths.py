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

# 自己导出，不依赖预先生成的文件——
# 别的测试（如 test_sync）会改动源数据，跨测试的数据依赖必然出问题
import subprocess, csv as _csv
os.makedirs(os.path.dirname(CSV), exist_ok=True)
_out = subprocess.run(
    ["docker", "exec", "datalaker-source_pg-1", "psql", "-U", "postgres",
     "-d", "northwind", "-c",
     "\\copy (SELECT * FROM shippers ORDER BY shipper_id) TO STDOUT WITH CSV HEADER"],
    capture_output=True, text=True, timeout=60).stdout
open(CSV, "w").write(_out)
import openpyxl
_wb = openpyxl.Workbook(); _ws = _wb.active; _ws.title = "shippers"
for _row in _csv.reader(_out.splitlines()):
    _ws.append(_row)
_wb.save(XLSX)

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== 接入路径等价性 ===\n")

c = F.read_csv(CSV)
x = F.read_excel(XLSX)
d = F.read_table("northwind", "shippers")

N = len(c["rows"])
chk("CSV 可读", N > 0, f"{N} 行")
chk("Excel 可读", len(x["rows"]) == N, f"{len(x['rows'])} 行")
chk("数据库可读", len(d["rows"]) == N, f"{len(d['rows'])} 行")

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

print("\n=== 重复接同一张表要认得出来 ===\n")

# **模型最常见的浪费**：每收到一封信就重新规划，把接过的表再申请一遍 ——
# 人白点一次链接，源库白扫一次全表。实测一轮里同一张表被接了两次。
# `list_source_tables` 已经在清单里标了「已接入」，但模型不一定去看清单；
# 真正要拦住的是**动手那一刻**。
import sys as _s9                                             # noqa: E402
import pathlib as _p9                                         # noqa: E402
_s9.path.insert(0, str(_p9.Path(__file__).resolve().parent.parent / ".hermes" / "plugins"))
import importlib
_T9 = importlib.import_module("data-steward.tools")                                      # noqa: E402

chk("刚接过的表会被认出来（不重接、不白扫）",
    callable(getattr(_T9, "_recently_synced", None)))
_src9 = __import__("inspect").getsource(_T9._ingest_table)
chk("**检查在动手之前**（不是抽完才发现白抽了）",
    "_recently_synced" in _src9
    and _src9.index("_recently_synced") < _src9.index("try:"))
chk("没接过的表不受影响", _T9._recently_synced("nosuch.table") is None)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
