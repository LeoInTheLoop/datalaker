"""L0 平台冒烟（readme 16.0）：证明 datalake 作为独立产品可用。

**这一层不涉及 Agent。** 它是第 2 节「Claw 挂掉，下面仍是完整数据产品」的证据。
Trino/Iceberg/MinIO 本身的正确性由上游保证，这里只验证我们的配置对、且常见用法能跑。

前置：cd infra && docker compose --profile core up -d
跑法：python3 tests/test_lakehouse_smoke.py
"""
import os
import subprocess
import sys

C = "datalaker-trino-1"
ok, bad = [], []


# 加了授权（R3）之后，容器内 CLI 也必须报身份——
# rules.json 里只有 admin/claw/analyst 有权限，默认用户只能读 system。
# 运维与测试脚本用 admin；容器内走 HTTP，免密码。
TRINO_USER = os.environ.get("TRINO_USER", "admin")


def q(sql, fmt="CSV_UNQUOTED"):
    r = subprocess.run(
        ["docker", "exec", C, "trino", "--user", TRINO_USER,
         "--output-format", fmt, "--execute", sql],
        capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[:300])
    return [l for l in r.stdout.strip().split("\n") if l]


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== L0 平台冒烟：datalake 独立可用 ===\n")

# 1. 连接 + catalog
try:
    cats = q("SHOW CATALOGS")
    check("1 Trino 可连接、catalog 齐全",
          "iceberg" in cats and "postgres" in cats, ",".join(cats))
except Exception as e:
    check("1 Trino 可连接、catalog 齐全", False, str(e)[:60])
    print("\n无法连接，后续跳过"); sys.exit(1)

# 准备 schema
q("CREATE SCHEMA IF NOT EXISTS iceberg.smoke")
for t in ("orders", "customers"):
    try:
        q(f"DROP TABLE IF EXISTS iceberg.smoke.{t}")
    except Exception:
        pass

# 2. 建表 → 写入 → 查询
try:
    q("CREATE TABLE iceberg.smoke.customers (id int, name varchar, city varchar)")
    q("INSERT INTO iceberg.smoke.customers VALUES (1,'张三','北京'),(2,'李四','上海'),(3,'王五','广州')")
    n = q("SELECT count(*) FROM iceberg.smoke.customers")[0]
    check("2 建 Iceberg 表 → 写入 → 查询", n == "3", f"{n} 行")
except Exception as e:
    check("2 建 Iceberg 表 → 写入 → 查询", False, str(e)[:70])

# 3. 多表 join —— 最常见的真实用法
try:
    q("CREATE TABLE iceberg.smoke.orders (id int, customer_id int, amount decimal(10,2))")
    q("INSERT INTO iceberg.smoke.orders VALUES (101,1,99.50),(102,1,20.00),(103,2,315.75)")
    rows = q("""SELECT c.name, count(o.id), sum(o.amount)
                FROM iceberg.smoke.customers c JOIN iceberg.smoke.orders o
                  ON c.id = o.customer_id
                GROUP BY c.name ORDER BY c.name""")
    check("3 多表 join + 聚合", len(rows) == 2 and "张三,2,119.50" in rows[0],
          " | ".join(rows))
except Exception as e:
    check("3 多表 join + 聚合", False, str(e)[:70])

# 4. 数据文件确实落在 MinIO 的 lake bucket
try:
    files = q('SELECT file_path FROM iceberg.smoke."customers$files"')
    check("4 数据文件落在 MinIO lake bucket",
          any(f.startswith("s3://lake/") for f in files),
          files[0][:56] if files else "无文件")
except Exception as e:
    check("4 数据文件落在 MinIO lake bucket", False, str(e)[:70])

# 5. Schema evolution：加列后旧数据仍可读
try:
    q("ALTER TABLE iceberg.smoke.customers ADD COLUMN vip boolean")
    rows = q("SELECT name, vip FROM iceberg.smoke.customers WHERE id = 1")
    check("5 Schema evolution（加列后旧数据可读）",
          rows and rows[0].startswith("张三"), rows[0] if rows else "")
except Exception as e:
    check("5 Schema evolution（加列后旧数据可读）", False, str(e)[:70])

# 6. Time travel：按快照查历史版本（审计的基础）
#    自包含：先记住当前快照与行数，写入后再回查，不依赖快照顺序
try:
    before_n = q("SELECT count(*) FROM iceberg.smoke.customers")[0]
    snap = q('SELECT snapshot_id FROM iceberg.smoke."customers$snapshots" '
             'ORDER BY committed_at DESC LIMIT 1')[0]
    q("INSERT INTO iceberg.smoke.customers VALUES (4,'赵六','深圳',true)")
    after_n = q("SELECT count(*) FROM iceberg.smoke.customers")[0]
    old_n = q(f"SELECT count(*) FROM iceberg.smoke.customers FOR VERSION AS OF {snap}")[0]
    check("6 Time travel（历史快照可查）",
          old_n == before_n and after_n != before_n,
          f"写入前 {before_n} → 现在 {after_n}，回查快照得 {old_n}")
except Exception as e:
    check("6 Time travel（历史快照可查）", False, str(e)[:70])

# 7. Postgres 联邦查询 —— 源数据探查依赖它
try:
    n = q("SELECT count(*) FROM postgres.public.orders")[0]
    check("7 Postgres 联邦查询", int(n) >= 0, f"源库 orders {n} 行")
except Exception as e:
    check("7 Postgres 联邦查询", False, str(e)[:70])

# 8. 跨 catalog join —— 「源系统不 join，在 lake 里 join」的兑现证据
try:
    rows = q("""SELECT count(*) FROM iceberg.smoke.customers c
                CROSS JOIN postgres.public.orders p""")
    check("8 跨 catalog join（Iceberg × Postgres）", int(rows[0]) >= 0,
          f"{rows[0]} 行")
except Exception as e:
    check("8 跨 catalog join（Iceberg × Postgres）", False, str(e)[:70])

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if not bad:
    print("→ 这套 lakehouse 可以作为独立产品交付（readme 第 2 节架构底线）")
sys.exit(1 if bad else 0)
