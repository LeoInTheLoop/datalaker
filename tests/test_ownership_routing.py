"""审批人解析：**先看人确认过的归属，再猜表名**（readme 10.4）。

这条是 LLM 在环演练逼出来的。northwind / olist 的表没有域前缀，
于是全都落到兜底角色，而那个人**正确地回了「这不是我管的表」**——连着好几天。
兜底本身没错，错在它曾经是唯一的依据：人一旦告诉过我们归属，就不该再靠表名去猜。
"""
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
DB = f"/tmp/dl_own_{uuid.uuid4().hex[:8]}.db"
os.environ["DATASTEWARD_DB"] = DB

from datasteward_gate import resolve_approver_role
from plugins.datasteward_gate.approvals import Store

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


st = Store(DB, readonly=False)
st.assign_role("owner", "catchall@acme.com", "boot", "兜底")
st.assign_role("owner:fin", "wang@acme.com", "boot", "")
st.assign_role("owner:crm", "li@acme.com", "boot", "")
st.assign_role("owner:orders", "sun@acme.com", "boot", "")

print("\n=== ② 表名前缀（原有行为，不能破）===\n")

chk("前缀能推断出具体角色",
    resolve_approver_role(st, "owner", {"table": "fin_monthly"}) == "owner:fin")
chk("推断不出时回退兜底（宁可发给上一级）",
    resolve_approver_role(st, "owner", {"table": "shippers"}) == "owner")
chk("具体角色无人持有时也回退",
    resolve_approver_role(st, "owner", {"table": "hr_headcount"}) == "owner")
chk("已经是具体角色的原样返回",
    resolve_approver_role(st, "owner:fin", {"table": "anything"}) == "owner:fin")
chk("没有角色要求时不乱造", resolve_approver_role(st, None, {"table": "x"}) is None)

print("\n=== ① 记忆层的归属优先于表名 ===\n")

# 表名前缀会把它推给 owner:orders，但人确认过它归 CRM
st.remember("northwind.orders", "ownership", "owner:crm", "boss@acme.com")
got = resolve_approver_role(st, "owner", {"table": "orders", "source": "northwind"})
chk("人确认过的归属压过表名推断", got == "owner:crm", got)
chk("没记归属的表仍走推断",
    resolve_approver_role(st, "owner", {"table": "orders",
                                        "source": "other"}) == "owner:orders")

st.remember("olist_raw.sellers", "ownership", "owner:fin", "boss@acme.com")
chk("无前缀可推的表也能靠归属找到人",
    resolve_approver_role(st, "owner", {"table": "sellers",
                                        "source": "olist_raw"}) == "owner:fin")

print("\n=== 记的是人不是角色时 ===\n")

# 归属记成邮箱：不能凭空造一个没人持有的角色，否则通知发不出去
st.remember("northwind.products", "ownership", "someone@acme.com", "boss@acme.com")
got = resolve_approver_role(st, "owner", {"table": "products",
                                          "source": "northwind"})
chk("角色不存在时回退而不是造一个空角色", got == "owner", got)
st.assign_role("owner:products", "someone@acme.com", "boss@acme.com", "")
got2 = resolve_approver_role(st, "owner", {"table": "products",
                                           "source": "northwind"})
chk("角色被指派之后才用它", got2 == "owner:products", got2)

print("\n=== 坏输入不许把门禁搞崩 ===\n")

chk("空 args 安全", resolve_approver_role(st, "owner", {}) == "owner")
chk("怪表名安全",
    resolve_approver_role(st, "owner", {"table": "a.b.c d;e"}) == "owner")


class Broken:
    def known(self, *a):
        raise RuntimeError("库挂了")

    def resolve_role(self, *a):
        raise RuntimeError("库挂了")


chk("存储异常时回退而不是抛出（门禁不能因此崩）",
    resolve_approver_role(Broken(), "owner", {"table": "fin_x"}) == "owner")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
