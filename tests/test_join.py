"""连表：**源系统禁 join，lake 里随便 join**（铁律 3）。

这条分界不是性能取舍。一条模型生成的多表关联打在生产源库上，即使只读
也可能把它拖死；而 iceberg 是我们自己的地盘，扫爆了也不影响别人。

准入规则 R2 就写全了（`connector.review_sql`：lake 走 trino 方言、只准查
`iceberg.*`、JOIN 不算风险项；source 则 JOIN 先问人）。**缺的一直是工具**——
`sql_query` 在 `policy.py` 里声明了 L1，却从来没有 schema 也没有 handler，
于是 Agent 根本没有办法连表。这一组把那条路接上并钉住。

    python3 tests/test_join.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins"),
                str(ROOT / ".hermes" / "plugins")]

DB = "/tmp/dl_join_test.db"
for suf in ("", "-wal", "-shm"):
    f = pathlib.Path(DB + suf)
    if f.exists():
        f.unlink()
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
os.environ.setdefault("NOTIFY_CHANNEL", "outbox")
os.environ.setdefault("NOTIFY_OUTBOX", "/tmp/dl_join_outbox.jsonl")

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def is_block(r):
    return isinstance(r, dict) and r.get("action") == "block"


from datasteward_gate import gate                              # noqa: E402
from datasteward_gate.policy import POLICY, Level              # noqa: E402
import importlib
T = importlib.import_module("data-steward.tools")                                         # noqa: E402


def _lake_ok():
    """探活走干活同一条路 —— 连不上就整段 SKIP，不是 FAIL。"""
    try:
        import sync
        sync._trino("SELECT 1")
        return True
    except Exception:                                          # noqa: BLE001
        return False


LAKE_OK = _lake_ok()


def _have_bronze(*tables) -> bool:
    """需要的表在不在 bronze 里。**探活走干活同一条路。**

    这一组的 join 断言要真数据。而 bronze 是**共享的、会被清空的** ——
    演练前的 `reset_live.py` 就会把它删干净。依赖「上一次跑留下的表」
    的测试，会在别人清了湖之后莫名其妙地红，而原因跟被测代码毫无关系。
    """
    if not LAKE_OK:
        return False
    try:
        import sync
        have = set(sync._trino(
            "SELECT table_name FROM iceberg.information_schema.tables"
            " WHERE table_schema='bronze'"))
        return all(t in have for t in tables)
    except Exception:                                          # noqa: BLE001
        return False


JOIN_DATA = _have_bronze("northwind__orders", "northwind__customers")

print("\n=== 工具存在，且级别是声明过的（铁律 5）===\n")

chk("sql_query 有 schema 和 handler（此前只有一行级别声明）",
    "sql_query" in T._SCHEMAS and "sql_query" in T._HANDLERS)
chk("describe_asset 同样齐全",
    "describe_asset" in T._SCHEMAS and "describe_asset" in T._HANDLERS)
chk("两个都在 policy.py 里显式声明",
    POLICY.get("sql_query") == (Level.L1, None)
    and POLICY.get("describe_asset") == (Level.L0, None),
    f'{POLICY.get("sql_query")} / {POLICY.get("describe_asset")}')

_man = (ROOT / ".hermes" / "plugins" / "data-steward"
        / "plugin.yaml").read_text(encoding="utf-8")
chk("manifest 也列了（否则 Hermes 那侧对不上）",
    "sql_query" in _man and "describe_asset" in _man)

print("\n=== 源系统：禁关系展开 ===\n")

SRC_JOIN = ("SELECT * FROM orders o JOIN customers c"
            " ON o.customer_id = c.customer_id LIMIT 5")
r = gate("sql_query", {"sql": SRC_JOIN, "plane": "source",
                       "source": "northwind"}, "j-src")
chk("**源库上的 JOIN 先问人**（一条 SQL 就可能把生产库拖死）",
    is_block(r) and "JOIN" in r["message"], r.get("message", "")[:70])

# 注释分隔骗不过 AST —— 它看结构不看字面。
r2 = gate("sql_query", {"sql": "SELECT 1 FROM a/**/JOIN/**/b ON a.x=b.x",
                        "plane": "source", "source": "northwind"}, "j-src2")
chk("注释分隔也拦得住（看 AST，不看关键字）", is_block(r2),
    r2.get("message", "")[:60] if is_block(r2) else str(r2))

print("\n=== lake：可以 join，但只能查 iceberg ===\n")

LAKE_JOIN = ('SELECT o.order_id, c.company_name'
             ' FROM iceberg.bronze."northwind__orders" o'
             ' JOIN iceberg.bronze."northwind__customers" c'
             ' ON o.customer_id = c.customer_id LIMIT 5')
a = {"sql": LAKE_JOIN, "plane": "lake"}
rl = gate("sql_query", a, "j-lake")
chk("**lake 里的 JOIN 直接放行**（不问人，也不算风险）",
    isinstance(rl, dict) and rl.get("action") == "modify", str(rl)[:60])
if isinstance(rl, dict) and rl.get("action") == "modify":
    a.update(rl["args"])

# 跨 catalog 是硬线：lake 模式不许摸外部源。
for bad_sql, why in (
        ('SELECT * FROM postgres.public.orders LIMIT 1', "外部 catalog"),
        ('SELECT * FROM orders LIMIT 1', "未限定表")):
    rb = gate("sql_query", {"sql": bad_sql, "plane": "lake"}, "j-bad")
    chk(f"lake 模式拒绝{why}", is_block(rb), rb.get("message", "")[:60])

if JOIN_DATA:
    out = T._sql_query(a)
    chk("**join 真的跑出了数据**（验收是查得到，不是语法过了）",
        "查到 5 行" in out and "," in out, out[:80])
    # 结果里必须两张表的字段都在 —— 只有一边说明根本没连上。
    chk("结果里两张表的列都在（真的连上了，不是只查了一张）",
        any(ch.isdigit() for ch in out) and any(
            "Vins" in out or "Toms" in out or "Hanari" in out for _ in [0]),
        out[:100])
else:
    why = "lake 连不上" if not LAKE_OK else "bronze 里没有 northwind__orders/customers"
    print(f"  SKIP  join 真的跑出数据（{why}）")
    print(f"  SKIP  结果里两张表的列都在（{why}）")

print("\n=== 怎么连：关系是源库声明的，不是猜的 ===\n")

d = T._describe_asset({"source": "northwind", "table": "orders"})
chk("给出了列与主键", "14 列" in d and "主键 order_id" in d, d[:60])
chk("**给出了外键指向**（猜表名是连错的主要来源）",
    "orders.customer_id → customers.customer_id" in d,
    d[d.find("外键"):d.find("外键") + 60] if "外键" in d else d[:60])
if JOIN_DATA:
    chk("给出了可用的 join 路径，且写成 iceberg 的形式",
        'iceberg.bronze."northwind__orders"' in d and "ON a.customer_id" in d)
    # **只给两边都在 lake 里的**。对端没接进来就提示先接，不是给一条跑不通的。
    #
    # 这里**按湖里实际有什么来验规则**，不写死「employees 不在湖里」。
    # 原先那条硬编码：一次真演练把 northwind__employees 接进来之后就红了 ——
    # 而红的不是规则，是测试自己的前提。测试依赖「某张表不存在」，
    # 迟早会被一次正常的接入推翻。
    import sync as _sync
    in_lake = {t.strip() for t in _sync._trino(
        "SELECT table_name FROM iceberg.information_schema.tables"
        " WHERE table_schema='bronze'")}
    fk_targets = {"customers": "northwind__customers",
                  "employees": "northwind__employees",
                  "shippers": "northwind__shippers"}
    wrong = [t for t, bt in fk_targets.items()
             if (bt in in_lake) != (bt in d)]
    chk("**join 路径里出现的对端，正好是湖里有的那些**",
        not wrong,
        f"对不上：{wrong}；湖里有 {sorted(x for x in fk_targets.values() if x in in_lake)}")
    chk("明说了「在 lake 里 join，不要在源库上关联」",
        "不要在源库上关联" in d)
else:
    for n in ("给出了可用的 join 路径", "对端没接进来的关系不出现",
              "明说了在 lake 里 join"):
        print(f"  SKIP  {n}（bronze 里没有那两张表）")

print("\n=== handler 里没有自己判审批（铁律 1）===\n")

import inspect                                                 # noqa: E402

_src = inspect.getsource(T._sql_query) + inspect.getsource(T._describe_asset)
_leaks = [w for w in ("find_valid", "is_denied", "st.request(", "review_sql")
          if w in _src]
chk("两个 handler 都不自己判准入（那是门禁的事）", not _leaks, str(_leaks))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
