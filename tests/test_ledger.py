"""Remediation Ledger 与整改闭环（readme 7 / 7.1）。

断言的重点是**闭环**而不是记录：
只在 lake 里清洗等于给源头的缺陷付永久利息，
所以真正要验证的是「建议 → 采纳 → 源头修好 → 规则退役」这条链。
"""
import os
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_ledger_{uuid.uuid4().hex[:8]}.db"

import ledger as L

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== 记录与复发聚合 ===\n")

r = L.record("CRM.customers", "格式不一致", field="phone",
             observed_pattern="12% 的值含中文字符",
             action_taken="正则提取数字段，原值存 _raw 列", rows_cleaned=73000)
rl = r["rl_id"]
chk("首次登记", r["new"] and r["recurrences"] == 1, rl)
chk("id 有前缀便于引用", rl.startswith("RL-"))

for _ in range(16):
    L.record("CRM.customers", "格式不一致", field="phone", rows_cleaned=73000)
x = L.get(rl)
chk("同一缺陷聚合而非追加", len(L.all_items()) == 1, str(len(L.all_items())))
chk("复发次数累加", x["recurrences"] == 17, str(x["recurrences"]))
chk("累计行数累加", x["rows_cleaned"] == 73000 * 17, f"{x['rows_cleaned']:,}")
chk("不同字段是不同条目",
    L.record("CRM.customers", "格式不一致", field="email")["new"])

ev = L.evidence(rl)
chk("证据把债务量化成行数与次数", "17 次" in ev and "1,241,000" in ev, ev.split(chr(10))[3].strip())

print("\n=== 权限类与数据类同表 ===\n")

p = L.record("FIN.monthly_report", "过度授权", category="permission",
             observed_pattern="全公司 187 人可读，近 90 天仅 6 人访问")
chk("权限问题进同一张台账", L.get(p["rl_id"])["category"] == "permission")
chk("两类可分别统计",
    len([x for x in L.all_items() if x["category"] == "permission"]) == 1)

print("\n=== 建议要有主人和期限 ===\n")

try:
    L.propose(rl, "  ", "owner:crm")
    chk("空建议被拒", False)
except ValueError:
    chk("空建议被拒", True)

pr = L.propose(rl, "CRM 表单增加输入校验", "owner:crm", due_days=14)
chk("建议有负责人", pr["owner_role"] == "owner:crm")
chk("建议有期限", pr["due_at"] and pr["due_at"] > time.time())
chk("状态推进到 proposed", pr["status"] == "proposed")
chk("未到期不算超期", L.overdue() == [])
chk("到期后进超期清单", len(L.overdue(now=time.time() + 15 * 86400)) == 1)

print("\n=== 拒绝是合法结局，但要留痕 ===\n")

try:
    L.reject(p["rl_id"], "wang@acme.com", "")
    chk("拒绝必须给理由", False)
except ValueError:
    chk("拒绝必须给理由", True)

rj = L.reject(p["rl_id"], "wang@acme.com", "监管要求全员可查，确实允许")
chk("拒绝被记录", rj["status"] == "rejected")
chk("理由留痕", "监管" in rj["reject_reason"])

print("\n=== 规则必须标注它在补哪个洞 ===\n")

try:
    L.add_rule("orphan_rule", "RL-NOTEXIST", "x")
    chk("没有台账引用的规则不许登记", False)
except ValueError:
    chk("没有台账引用的规则不许登记", True)

L.add_rule("normalize_phone_cn", rl, "CRM.customers.phone / 格式不一致",
           retire_when="源系统上线输入校验后")
chk("规则登记成功", len(L.rules()) == 1)
chk("规则挂在具体台账条目上", L.rules()[0]["ledger_ref"] == rl)

print("\n=== 闭环：源头修好 → 规则可退役 ===\n")

chk("源头没修时不可退役", L.retirable() == [])
L.accept(rl, "li@acme.com")
chk("采纳后仍未修完不可退役", L.retirable() == [])
L.mark_fixed(rl, "li@acme.com")
chk("源头修好后规则可退役", [r["name"] for r in L.retirable()] == ["normalize_phone_cn"])

m0 = L.metrics()
L.retire("normalize_phone_cn")
m1 = L.metrics()
chk("活跃规则数下降（成功指标）", m1["active_rules"] < m0["active_rules"],
    f"{m0['active_rules']} -> {m1['active_rules']}")
chk("退役数上升", m1["retired_rules"] == 1)
chk("采纳率可算", m1["adoption_rate"] is not None, str(m1["adoption_rate"]))
chk("复发总数进指标", m1["total_recurrences"] >= 17, str(m1["total_recurrences"]))
chk("指标里点明了方向", "下降" in m1["note"])

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
