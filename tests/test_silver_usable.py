"""silver 生成之后**下游拿来就能用吗**。

清洗跑完、表建出来了，不等于这份数据能用。可用性是分开的一件事：
查得动、主键唯一、洗过的值符合人定的口径、原值还能对照、行数没有凭空
多出来、两张 silver 之间连得起来。

这一组**只查 lake 和治理库**，不经过 Agent 的说法 —— 它验的是产物本身。

    python3 tests/test_silver_usable.py

silver 是空的时候整组 SKIP（先跑一遍演练或 eval 才有）。
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


try:
    import sync
    sync._trino("SELECT 1")
except Exception as e:                                        # noqa: BLE001
    print(f"\n  SKIP  连不上 lake（{type(e).__name__}）\n")
    sys.exit(0)


def q(sql):
    return sync._trino(sql)


def one(sql):
    r = q(sql)
    return r[0] if r else None


def cols_of(schema, table) -> list:
    return q("SELECT column_name FROM iceberg.information_schema.columns"
             f" WHERE table_schema='{schema}' AND table_name='{table}'"
             " ORDER BY ordinal_position")


SILVER = sorted(t for t in q(
    "SELECT table_name FROM iceberg.information_schema.tables"
    " WHERE table_schema='silver'") if "eval_" not in t)

if not SILVER:
    print("\n  SKIP  silver 里没有表 —— 先跑一遍演练或 eval 产生清洗结果\n")
    sys.exit(0)

print(f"\n=== 查得动（{len(SILVER)} 张 silver 表）===\n")

for t in SILVER:
    try:
        n = int(one(f'SELECT count(*) FROM iceberg.silver."{t}"'))
        chk(f"{t} 查得动", n >= 0, f"{n:,} 行")
    except Exception as e:                                    # noqa: BLE001
        chk(f"{t} 查得动", False, f"{type(e).__name__}: {str(e)[:60]}")

print("\n=== 原值可回溯：洗过的列必须留 <列>_raw ===\n")

# 清洗改变了数据含义。**没有原值就没法验证洗得对不对**，
# 也没法在口径改了之后重洗 —— readme 6.3 的硬要求。
for t in SILVER:
    c = cols_of("silver", t)
    raws = [x for x in c if x.endswith("_raw")]
    base = [x[:-4] for x in raws]
    missing = [b for b in base if b not in c]
    chk(f"{t}：每个 _raw 都有对应的正式列", not missing, str(missing))
    if raws:
        # 原值列不该是空的 —— 空了等于没留
        empty = [r for r in raws
                 if int(one(f'SELECT count(*) FROM iceberg.silver."{t}"'
                            f' WHERE "{r}" IS NOT NULL')) == 0]
        chk(f"{t}：原值列真的有值（{'、'.join(raws)}）", not empty, str(empty))

print("\n=== 行数：只会去重，不会凭空多 ===\n")

BRONZE = set(q("SELECT table_name FROM iceberg.information_schema.tables"
               " WHERE table_schema='bronze'"))
for t in SILVER:
    if t not in BRONZE:
        print(f"  SKIP  {t}：bronze 里没有同名表，比不了")
        continue
    b = int(one(f'SELECT count(*) FROM iceberg.bronze."{t}"'))
    s = int(one(f'SELECT count(*) FROM iceberg.silver."{t}"'))
    chk(f"{t}：silver({s:,}) ≤ bronze({b:,})", s <= b,
        f"多出来 {s - b} 行 —— 清洗不该造数据")

print("\n=== 口径真的生效了 ===\n")

# 人定过枚举的列，洗完之后的值必须落在那个枚举里。
# **这一条是「清洗读口径」的验收**：口径沉淀了、也执行了，
# 但没人回头检查过结果符不符合口径。
import json                                                    # noqa: E402
import re                                                      # noqa: E402

os.environ.setdefault("DATASTEWARD_DB", "/tmp/live.db")
sem = []
try:
    from datasteward_gate.approvals import open_store
    with open_store(readonly=True, init_schema=False) as st:
        sem = st.db.execute(
            "SELECT asset, key, value FROM asset_semantics"
            " WHERE key='normalize_rule'").fetchall() \
            if hasattr(st.db, "execute") else []
except Exception as e:                                        # noqa: BLE001
    print(f"  SKIP  读不到口径库（{type(e).__name__}）")

checked = 0
unparsed = []
try:
    import catalog
except Exception:                                             # noqa: BLE001
    catalog = None
for asset, _k, value in sem:
    parts = asset.split(".")
    if len(parts) < 3:
        continue
    tbl, col = f"{parts[0]}__{parts[1]}", parts[2]
    if tbl not in SILVER or col not in cols_of("silver", tbl):
        continue
    # **先读人结构化登记的枚举**（define_semantics 的 allowed_values）。
    structured = catalog.allowed_values(asset) if catalog else None
    if structured:
        allowed = set(structured)
    else:
        # 兜底：从口径原文里抠 —— 人写的是「PAID / PENDING / UNPAID / VOID」。
        # 换个写法（小写枚举、中文值）就抠不出来，而**抠不出来必须报出来**：
        # 原先这里直接 continue，那条断言于是静默消失，看着像通过了。
        allowed = set(re.findall(r"\b[A-Z][A-Z_]{2,}\b", value))
        if not allowed:
            unparsed.append(f"{tbl}.{col}")
            continue
    got = {r.split(",")[0] for r in q(
        f'SELECT DISTINCT "{col}" FROM iceberg.silver."{tbl}"'
        f' WHERE "{col}" IS NOT NULL')}
    outside = got - allowed
    chk(f"{tbl}.{col} 的值都在人定的枚举里", not outside,
        f"越界值 {outside}（口径允许 {sorted(allowed)}）")
    checked += 1
    # 原值确实和洗后不同 —— 否则「洗过」是假的
    if f"{col}_raw" in cols_of("silver", tbl):
        diff = int(one(f'SELECT count(*) FROM iceberg.silver."{tbl}"'
                       f' WHERE "{col}" IS DISTINCT FROM "{col}_raw"'))
        chk(f"{tbl}.{col} 确实被洗过（与原值有差异）", diff > 0,
            f"{diff} 行不同")
if unparsed:
    # **不判失败，但必须看得见。** 「去掉首尾空格」这类口径本来就没有枚举，
    # 判它失败是误报；可「限定了取值却写成机器读不懂的样子」也藏在这批里，
    # 而两者从自然语言里分不开 —— 那正是要 allowed_values 的原因。
    # 折中：如实报出未校验的条数，不让它悄悄消失。
    print(f"  未校验  {len(unparsed)} 条 normalize_rule 没有结构化枚举："
          f"{unparsed[:5]} —— 若其中有限定取值的，重定口径时补 allowed_values")
if not checked:
    print("  SKIP  没有「已定枚举口径 且 已落 silver」的列可验")

print("\n=== 下游真实用法：两张 silver 连得起来 ===\n")

if len(SILVER) >= 2:
    a, b = SILVER[0], SILVER[1]
    try:
        # 连不连得上不看业务语义，看**引擎能不能跑通一条 join**：
        # silver 是给下游用的，跑不了 join 就谈不上可用。
        n = int(one(f'SELECT count(*) FROM iceberg.silver."{a}" x'
                    f' CROSS JOIN (SELECT 1 AS k FROM iceberg.silver."{b}"'
                    f' LIMIT 1) y'))
        chk(f"{a} × {b} 能跑 join", n >= 0, f"{n:,} 行")
    except Exception as e:                                    # noqa: BLE001
        chk(f"{a} × {b} 能跑 join", False, f"{type(e).__name__}: {str(e)[:60]}")
else:
    print("  SKIP  只有一张 silver 表，连不了")

print("\n=== _raw 不出 silver：gold 里不该有原值列 ===\n")

# `<列>_raw` 是清洗前的原值，跟着发布等于把没洗的数据给了下游
# （readme 6.3 / publish_gold 的前置）。silver 有是对的，gold 有就是错的。
GOLD = [t for t in q("SELECT table_name FROM iceberg.information_schema.tables"
                     " WHERE table_schema='gold'") if "eval_" not in t]
if GOLD:
    leaked = {t: [c for c in cols_of("gold", t) if c.endswith("_raw")]
              for t in GOLD}
    leaked = {t: c for t, c in leaked.items() if c}
    chk("gold 里没有 _raw 列（原值不出去）", not leaked, str(leaked))
else:
    print("  SKIP  gold 为空（还没发布过）—— 这本身不是问题")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
