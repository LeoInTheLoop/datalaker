"""增量同步与新鲜度断言（readme 6.1）。

覆盖四个坑中的两个可自动验证的：**硬删除**与 **schema 漂移**。
（`updated_at` 不可靠与回填需要更长时间窗口，留给 eval。）
"""
import os, sys, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services"))
import sync

PG = "datalaker-source_pg-1"
ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

def psql(sql, db="northwind"):
    return subprocess.run(["docker", "exec", PG, "psql", "-U", "postgres", "-d", db,
                           "-tAc", sql], capture_output=True, text=True, timeout=60).stdout.strip()

print("\n=== 增量同步与新鲜度 ===\n")

# 建立基线：上一轮可能留下 schema 变更后的状态，先对齐再测
sync.sync_table("northwind", "shippers", allow_schema_change=True)
sync.sync_table("northwind", "orders", watermark_col="order_date",
                allow_schema_change=True)

# 全量刷新
r = sync.sync_table("northwind", "shippers")
n0 = r["row_count"]
chk("全量刷新写入 bronze", n0 > 0, f"{n0} 行")
chk("策略识别为 full_refresh", r["strategy"] == "full_refresh")

# 幂等：再跑一次行数不变（full_refresh 会 DROP 重建）
r2 = sync.sync_table("northwind", "shippers")
chk("全量刷新幂等", r2["row_count"] == n0, f"{r2['row_count']} vs {n0}")

# 新鲜度可见
f = sync.freshness("northwind.shippers")
chk("新鲜度可见", f["known"] and f["age_hours"] is not None, f"age={f['age_hours']}h")
chk("刚同步不算过期", not f["is_stale"])
chk("未同步的资产返回 known=False", not sync.freshness("northwind.__nope__")["known"])

# 增量：水位生效，不重复插入
i1 = sync.sync_table("northwind", "orders", watermark_col="order_date")
i2 = sync.sync_table("northwind", "orders", watermark_col="order_date")
chk("增量策略识别", i1["strategy"] == "incremental_append")
chk("水位生效，不重复插入", i2["row_count"] == i1["row_count"],
    f"{i1['row_count']} -> {i2['row_count']}")
chk("水位已记录", bool(i1["watermark"]), str(i1["watermark"]))

# 坑一：硬删除 —— 增量看不到，全量对账能发现
before = int(psql("SELECT count(*) FROM shippers"))
psql("DELETE FROM shippers WHERE shipper_id=(SELECT max(shipper_id) FROM shippers)")
d = sync.reconcile_deletes("northwind", "shippers", "shipper_id")
chk("硬删除被对账发现（幽灵数据）", d["ghost_rows"] >= 1, f"{d['ghost_rows']} 行")

# 坑二：schema 漂移 —— 必须停下问人，不是自动接受
psql("ALTER TABLE shippers ADD COLUMN IF NOT EXISTS _drift_test varchar(10)")
try:
    sync.sync_table("northwind", "shippers")
    chk("schema 漂移中止同步", False, "竟然继续了")
except sync.SchemaDrift as e:
    chk("schema 漂移中止同步", True, str(e)[:40])
chk("人确认后可继续",
    sync.sync_table("northwind", "shippers", allow_schema_change=True)["row_count"] >= 0)

# 复原
psql("ALTER TABLE shippers DROP COLUMN IF EXISTS _drift_test")
psql(f"INSERT INTO shippers (shipper_id, company_name, phone) "
     f"SELECT {before}, 'Restored', '000' WHERE NOT EXISTS "
     f"(SELECT 1 FROM shippers WHERE shipper_id={before})")
sync.sync_table("northwind", "shippers", allow_schema_change=True)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
