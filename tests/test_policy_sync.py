"""Policy Sync（readme 11.3）：**策略绑 tag，不绑表名。**

新表接入只要打好 tag 就自动继承策略——几百张表逐表配一遍不可能维护，
这是权限治理能规模化的唯一方式。

活体验证（两账号查同表 PII 遮蔽）需要 Trino，设 `POLICY_SYNC_E2E=1` 才跑：
它要重启容器，放进每次回归太慢。默认跑的是生成逻辑本身。
"""
import json
import os
import subprocess
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_ps_{uuid.uuid4().hex[:8]}.db"
RULES = f"/tmp/dl_rules_{uuid.uuid4().hex[:6]}.json"

import policy_sync as PS

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


BASE = {"catalogs": [{"user": "admin", "allow": "all"}],
        "schemas": [{"user": "admin", "owner": True}],
        "tables": [{"user": "admin", "privileges": ["SELECT", "INSERT"]}]}
json.dump(BASE, open(RULES, "w"))

print("\n=== 打标 ===\n")

try:
    PS.classify("gold.t", "Secret", "x")
    chk("未知分类被拒", False)
except ValueError:
    chk("未知分类被拒", True)

PS.classify("gold.customer_360", "PII", "wang@acme.com")
PS.classify_column("gold.customer_360", "phone", "PII", "wang@acme.com")
PS.classify_column("gold.customer_360", "email", "PII", "wang@acme.com")
PS.classify("gold.orders_summary", "Internal", "wang@acme.com")

c = PS.classifications(["gold.customer_360", "gold.orders_summary", "gold.nope"])
chk("表级分类可读回", c["gold.customer_360"]["level"] == "PII")
chk("列级分类可读回", set(c["gold.customer_360"]["columns"]) == {"phone", "email"})
chk("未打标的表不出现", "gold.nope" not in c)

print("\n=== 由 tag 生成策略 ===\n")

r = PS.generate(["gold.customer_360", "gold.orders_summary"], base=BASE)
gen = [t for t in r["tables"] if "_reason" in t]
chk("PII 列生成遮蔽规则", len(gen) == 1, str(len(gen)))
chk("遮蔽的正是被标的列",
    {x["name"] for x in gen[0]["columns"]} == {"phone", "email"})
chk("Internal 表不生成限制", all(t.get("table") != "orders_summary" for t in gen))
chk("手工配的基础规则被保留",
    any(t.get("user") == "admin" for t in r["tables"]))
chk("生成的规则带可复核理由", "_reason" in gen[0] and "遮蔽" in gen[0]["_reason"])

PS.classify("gold.hr_salary", "Confidential", "wang@acme.com")
r2 = PS.generate(["gold.hr_salary"], base=BASE)
g2 = [t for t in r2["tables"] if "_reason" in t][0]
chk("整表敏感但未标列 → analyst 完全不可见", g2["privileges"] == [])
chk("理由写明了为什么", "未标列" in g2["_reason"])

print("\n=== 写盘：Trino 严格校验，说明字段必须剥掉 ===\n")

PS.write(r, RULES)
w = json.load(open(RULES))
chk("落盘后没有下划线字段",
    all(not k.startswith("_") for t in w["tables"] for k in t))
why = json.load(open(RULES.replace(".json", ".why.json")))
chk("理由落到旁路文件", len(why) == 1 and "遮蔽" in why[0]["reason"])
chk("旁路文件指明是哪张表", why[0]["table"] == "customer_360")

print("\n=== 变更必须可复核 ===\n")

d = PS.diff(BASE, r)
chk("新增规则被列出", len(d["added"]) == 1)
chk("没有误删", d["removed"] == [])
s = PS.sync(["gold.customer_360"], path=RULES, dry_run=True)
chk("默认 dry_run（策略会让人干不了活）", s["dry_run"] is True)
chk("dry_run 不落盘", s["written"] is None)
chk("提示需要重载", "重载" in s["note"] or "生效" in s["note"])

if os.environ.get("POLICY_SYNC_E2E") == "1":
    print("\n=== 活体：两账号查同表 ===\n")

    def q(user):
        r = subprocess.run(
            ["docker", "exec", "datalaker-trino-1", "trino", "--user", user,
             "--execute", "SELECT phone FROM iceberg.gold.customer_360 LIMIT 1"],
            capture_output=True, text=True, timeout=120)
        return r.stdout
    chk("claw 看到真实值", "138" in q("claw"))
    chk("analyst 被遮蔽", "***" in q("analyst"))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
