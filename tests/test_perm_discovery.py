"""权限现状发现（readme 11.2）：数据库侧 + SaaS 侧，只读只建议。"""
import os
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_perm_{uuid.uuid4().hex[:8]}.db"
os.environ["SAAS_PROVIDER"] = "mock"

import ledger as L
import perm_discovery as P
import saas as S

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== SaaS 侧（这个规模公司的重点）===\n")

SRC = "acme_crm"
S.attest_readonly(SRC, "boss@acme.com", "Profile=Integration_ReadOnly，工单 ACME-1183")
inv = S.dump_permissions(SRC)
r = P.scan_saas(SRC, inv, now=time.mktime((2026, 9, 3, 0, 0, 0, 0, 0, 0)))
kinds = {f["issue_type"] for f in r["findings"]}

chk("扫出发现", len(r["findings"]) > 0, str(len(r["findings"])))
# fixture 里 modify_all 只给了 System Administrator，规则**正确地不报**。
# 单独造一个越界输入来验证规则本身会响 —— 不写恒真断言充数。
chk("管理员带 Modify All Data 不误报", "modify_all_on_non_admin" not in kinds)
bad_inv = {**inv, "object_access": [{"profile_or_set": "Standard User",
                                     "object": "Account", "modify_all": True}]}
chk("非管理员带 Modify All Data 被抓",
    "modify_all_on_non_admin" in {f["issue_type"]
                                  for f in P.scan_saas(SRC, bad_inv)["findings"]})
chk("非管理员的 View All 被抓", "view_all_on_non_admin" in kinds, str(sorted(kinds)))
chk("离职者账号仍 Active 被抓", "stale_active_account" in kinds)
stale = [f for f in r["findings"] if f["issue_type"] == "stale_active_account"]
chk("带管理员的停滞账号判为 high",
    any(f["severity"] == "high" and "zhou" in f["subject"] for f in stale),
    str([(f["subject"], f["severity"]) for f in stale]))
chk("外部域账号被抓", "external_account_privileged" in kinds)
ext = [f for f in r["findings"] if f["issue_type"] == "external_account_privileged"]
chk("指向的正是外包账号", any("vendor.com" in f["subject"] for f in ext))
chk("组织级默认过宽被抓", "org_wide_open" in kinds)
chk("每条发现都带可执行建议", all(f.get("suggestion") for f in r["findings"]))
chk("**不编造访问频率**", all(f["access_evidence"] in ("unknown",)
                              or "last_login" in f["access_evidence"]
                              for f in r["findings"]))
chk("说明了为什么不判定「从不访问」", "未采集" in r["note"])
chk("停用的账号不报（孙实习 active=false）",
    not any("sun@acme.com" in f["subject"] for f in stale))

print("\n=== 数据库侧 ===\n")

d = P.scan_database("northwind")
chk("能读到授权清单", d["grants"] > 0, str(d["grants"]))
chk("能读到角色清单", d["roles"] > 0, str(d["roles"]))
dk = {f["issue_type"] for f in d["findings"]}
chk("超级用户被抓", "superuser" in dk, str(sorted(dk)))
chk("敏感表判定用命名约定", P._sensitive("fin_invoice") and not P._sensitive("shippers"))
chk("同样不编造访问频率", "未采集" in d["note"])
# 只观测：扫描全程只走元数据通道，账本里不该出现任何非 SELECT
import connector as C
recent = [x for x in C.LEDGER if x["source"] == "northwind"]
chk("扫描全程只有只读查询",
    all(x["status"] == "OK" and "select" in x["sql"].lower()
        for x in recent[-6:]),
    str([x["status"] for x in recent[-6:]]))

print("\n=== 写进台账，走同一套整改闭环 ===\n")

before = len(L.all_items())
out = P.to_ledger(r, owner_role="owner:crm")
chk("发现全部入账", len(L.all_items()) - before == len(out), str(len(out)))
chk("类别标为 permission",
    all(L.get(x["rl_id"])["category"] == "permission" for x in out))
chk("动作栏写明未改源系统",
    all("仅观测" in L.get(x["rl_id"])["action_taken"] for x in out))
chk("建议有主人和期限",
    all(L.get(x["rl_id"])["owner_role"] == "owner:crm"
        and L.get(x["rl_id"])["due_at"] for x in out))
chk("状态推进到 proposed",
    all(L.get(x["rl_id"])["status"] == "proposed" for x in out))

again = P.to_ledger(r, owner_role="owner:crm")
chk("重复扫描是复发累加而非重复条目",
    len(L.all_items()) == before + len(out), str(len(L.all_items())))
chk("复发次数上去了",
    L.get(again[0]["rl_id"])["recurrences"] == 2,
    str(L.get(again[0]["rl_id"])["recurrences"]))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
