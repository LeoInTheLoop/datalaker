"""发布到 gold（readme 7 停止点 4）。

两条前置条件是这一步的全部意义，其余只是又复制一份表：

  1. 没分类不发布 —— 没有分类就推不出遮蔽规则，analyst 读到的是明文
  2. `_raw` 不出去 —— 那是清洗前的原值，跟着发布等于清洗白做

前两组不连 Trino（判定发生在建表之前）；建表那组要 core 起着。
"""
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_pub_{uuid.uuid4().hex[:8]}.db"

import policy_sync as PS
import publish

# 建表：`classifications` 是只读打开的，空库里连 asset_semantics 都没有。
# 真实环境里库早就在了；测试得自己先把它建出来，否则第一组测的是
# 「表不存在」而不是「没分类」。
with PS._store(readonly=False):
    pass

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def fails(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return None
    except Exception as e:                                    # noqa: BLE001
        return str(e)


print("\n=== 没分类不发布 ===\n")

msg = fails(publish.publish_gold, "cust_clean")
chk("未分类的表被挡在建表之前", msg is not None and "分类" in msg, (msg or "")[:44])
chk("说清楚为什么（不是「参数错误」）", msg is not None and "明文" in msg)

msg = fails(publish.publish_gold, "a-b")
chk("非法表名被拒", msg is not None and "非法" in msg, (msg or "")[:40])

print("\n=== _raw 不出去 ===\n")

PS.classify("gold.cust_clean", "PII", "wang@acme.com")
PS.classify_column("gold.cust_clean", "phone", "PII", "wang@acme.com")

# 显式点名 _raw 列 —— 这是最容易发生的泄漏：模型看到 silver 里有这列，
# 以为「都发出去更完整」。默认不带已经挡住一半，点名也必须挡住。
msg = fails(publish.publish_gold, "cust_clean", columns=["id", "phone_raw"])
chk("显式点名 _raw 列被拒", msg is not None and "原值列" in msg,
    (msg or "")[:50])
chk("这道闸门不依赖 Trino（否则它挂了这里就假绿）",
    msg is not None and "Trino" not in msg and "Error" not in msg)

print("\n=== 建表（要 Trino） ===\n")

live = False
try:
    import sync
    sync._trino("SELECT 1")
    live = True
except Exception as e:                                        # noqa: BLE001
    print(f"  SKIP  Trino 未启动：{str(e)[:60]}")

if live:
    T = "pub_smoke"
    sync._trino("CREATE SCHEMA IF NOT EXISTS iceberg.silver")
    sync._trino(f'DROP TABLE IF EXISTS iceberg.silver."{T}"')
    sync._trino(f'CREATE TABLE iceberg.silver."{T}" (id int, phone varchar,'
                ' phone_raw varchar, city varchar)')
    sync._trino(f"INSERT INTO iceberg.silver.\"{T}\" VALUES"
                " (1,'13800001111','138-0000-1111','北京'),"
                " (2,'13900002222','139 0000 2222','上海')")
    PS.classify(f"gold.{T}", "PII", "wang@acme.com")
    PS.classify_column(f"gold.{T}", "phone", "PII", "wang@acme.com")

    r = publish.publish_gold(T)
    chk("发布成功且行数一致", r["rows"] == 2, f'{r["rows"]} 行')
    chk("默认不带 _raw 列", "phone_raw" not in r["columns"], str(r["columns"]))
    chk("明确报告丢掉了哪些原值列", r["dropped_raw"] == ["phone_raw"],
        str(r["dropped_raw"]))
    cols = sync.lake_columns("gold", T)
    chk("gold 表里确实没有 _raw（不只是返回值里没有）",
        all(not c.endswith("_raw") for c in cols), str(cols))
    chk("报告了哪些列会被遮蔽", r["masked_columns"] == ["phone"],
        str(r["masked_columns"]))
    chk("记了血缘（停止点 4 的交付物）",
        r["upstream"] == f"iceberg.silver.{T}", r["upstream"])
    with PS._store() as st:
        v = st.known(f"gold.{T}", "lineage:upstream")
    chk("血缘落进了库，不只在返回值里", bool(v))

    # 分类是发布的前置条件，那么「发布完了」应该能直接推出策略
    pol = PS.generate([f"gold.{T}"])
    mine = [t for t in pol["tables"] if t.get("table") == T]
    chk("发布后立刻能推出遮蔽规则（分类先行的兑现）",
        any("columns" in t for t in mine), str(len(mine)))

    sync._trino(f'DROP TABLE IF EXISTS iceberg.gold."{T}"')
    sync._trino(f'DROP TABLE IF EXISTS iceberg.silver."{T}"')

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
