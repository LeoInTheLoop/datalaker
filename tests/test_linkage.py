"""闭环 C：关联产生价值。

**验收（R6 §3 C 原文）**：挑两套**没有显式外键**的系统，完成实体对应确认，
回答一个**单表答不了**的问题，并核验答案。

用的两张表：`acme.crm_customer`（40 行，客户）与 `acme.crm_contact`
（55 行，对接人）。**它们之间没有任何外键约束** —— 已在测试里现验，
不是靠记忆。

这一组要证的不是「join 能跑通」，而是三件更难的事：

1. **证据能收集，但证据分不出对错。** `customer_id = customer_id`（对）
   和 `customer_id = id`（错）的值域重叠率**都是 1.0**。分得开它们的是
   孤儿行与连接形状，而那也只是把差别摆出来，不是判定。
2. **错的连法答出来的数看着一样正常。** 两种连法回答「有对接人的客户数」
   都是 40，「上海的」都是 24 —— 从答案本身看不出连错了。
   所以门禁必须挡在「用未确认的连接回答问题」这一步。
3. **被否定的假设留着。** 下次再猜同一对表时，先看「上次为什么说不行」。

    python3 tests/test_linkage.py

Trino / bronze 连不上就整组 SKIP（探活走干活同一条路）。
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

DB = "/tmp/dl_linkage_test.db"
for suf in ("", "-wal", "-shm"):
    f = pathlib.Path(DB + suf)
    if f.exists():
        f.unlink()
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


A, B = "acme.crm_customer", "acme.crm_contact"
TA = 'iceberg.bronze."acme__crm_customer"'
TB = 'iceberg.bronze."acme__crm_contact"'

# --- 探活：这两张表得真在 bronze 里，且真的有行 --------------------------
try:
    import sync
    na = int(sync._trino(f"SELECT count(*) FROM {TA}")[0])
    nb = int(sync._trino(f"SELECT count(*) FROM {TB}")[0])
    if not (na and nb):
        raise RuntimeError(f"bronze 里没有数据（{na}/{nb}）")
except Exception as e:                                        # noqa: BLE001
    print(f"\n  SKIP  bronze 里没有 crm_customer / crm_contact（{type(e).__name__}: "
          f"{str(e)[:80]}）—— 先跑一次接入\n")
    sys.exit(0)

import catalog                                                # noqa: E402
import connector                                              # noqa: E402
import linkage                                                # noqa: E402
from datasteward_gate import gate, _link_not_confirmed        # noqa: E402
from datasteward_gate.approvals import open_store             # noqa: E402

print(f"\n=== 起点：两张表在 bronze（{na} / {nb} 行），且**没有外键** ===\n")

fks = connector.list_foreign_keys("acme")
rel = [f for f in fks if "crm_customer" in str(f) and "crm_contact" in str(f)]
chk("**这两张表之间确实没有外键**（不是靠记忆，是现查的）", not rel, str(rel)[:90])

catalog.observe("acme", "crm_customer")
catalog.observe("acme", "crm_contact")
snap = catalog.lookup("acme", "crm_contact")
chk("两张表都建了档（连表要先有档，不回源库猜列）",
    bool(snap and snap.get("columns")), str(len((snap or {}).get("columns") or [])))

print("\n=== 一、候选发现：只在 lake 侧扫，产出带证据的假设 ===\n")

got = linkage.candidates(A, B)
cands, unprobed = got["pairs"], got["unprobed"]
by = {f'{c["a"]}={c["b"]}': c for c in cands}
chk("找到了候选", len(cands) >= 2, str(list(by)))
# 粗筛只按类型兼容 + 预算，**不按名字像不像排除** ——
# `买家编号` / `buyer_id` 这类跨系统对应过不了任何字符串相似度阈值，
# 而那正是跨系统最常见的形状。名字只用来决定先探谁。
chk("粗筛不看名字：低相似度的对也被探了",
    any(c["name_score"] < 0.5 for c in cands + unprobed)
    or got["planned"] == got["probed"],
    f'planned={got["planned"]} probed={got["probed"]} '
    f'最低分={min([c["name_score"] for c in cands] or [1])}')
chk("超预算的对报出来而不是丢掉",
    got["planned"] == got["probed"] + len(unprobed),
    f'{got["planned"]} = {got["probed"]} + {len(unprobed)}')
# **名字不像的对也要被探。** 旧代码有一道 `name_score < 0.5 就跳过`，
# 它省钱，但那是一次判定伪装成成本过滤 —— `买家编号` / `buyer_id`
# 这类跨系统对应过不了任何字符串相似度阈值，而那正是最常见的形状。
# 在计划层数：旧阈值会放行几对，现在实际探了几对。
_ca, _cb = linkage._columns(A), linkage._columns(B)
_plan = [linkage._name_score(a[0], b[0])
         for a in _ca if linkage._type_family(a[1])
         for b in _cb if linkage._type_family(b[1]) == linkage._type_family(a[1])]
_old_gate = sum(1 for n in _plan if n >= 0.5)
chk("**名字不像的对现在也探了**（旧阈值会静默跳过它们）",
    got["probed"] > _old_gate,
    f'旧阈值只放行 {_old_gate} 对，现在探了 {got["probed"]} 对')
chk("丢弃只发生在证据上（值域不重叠），不发生在名字上",
    got["dropped"] > 0 and all(c["rate"] >= 0.05 for c in cands if "rate" in c),
    f'按证据丢了 {got["dropped"]} 对')

# 指名探查：模型判断某两列可能同实体时的出口，绕开预算
named = linkage.candidates(A, B, pairs_only=[("customer_id", "id")])
chk("probe_pairs 能指名探一对（模型判定的出口）",
    len(named["pairs"]) == 1 and named["pairs"][0]["b"] == "id",
    str([(c["a"], c["b"]) for c in named["pairs"]]))
chk("指名时不受预算影响", not named["unprobed"])

right = by.get("customer_id=customer_id")
wrong = by.get("customer_id=id")
chk("对的那条在候选里", bool(right))
chk("**错的那条也在候选里**（它长得一样像，不该被偷偷藏掉）", bool(wrong))

if right and wrong:
    chk("**两条的值域重叠率都是 1.0** —— 证据本身分不出对错",
        right["rate"] == 1.0 and wrong["rate"] == 1.0,
        f'对={right["rate"]} 错={wrong["rate"]}')
    chk("孤儿值分得开：对的两侧都不孤",
        right["a_orphans"] == 0 and right["b_orphans"] == 0,
        f'{right["a_orphans"]}/{right["b_orphans"]}')
    chk("**孤儿值分得开：错的那条有 15 个值连不上**",
        wrong["b_orphans"] == 15, str(wrong["b_orphans"]))
    chk("连接形状也分得开（N 条对接人挂 1 个客户 vs 硬凑成 1:1）",
        right["cardinality"] == "1:N" and wrong["cardinality"] == "1:1",
        f'对={right["cardinality"]} 错={wrong["cardinality"]}')
    chk("证据翻成了人话（人要据此判断，不能只给数字）",
        "15 个值连不上" in wrong["note"] and "不孤" in right["note"],
        wrong["note"][:60])
    chk("**排序把孤儿少的排前面**（排序不是判定，只决定人先看哪条）",
        cands[0]["b"] == "customer_id", str([c["b"] for c in cands]))

print("\n=== 候选落进 inferred 层，一条都不算数 ===\n")

r_ok = linkage.propose(A, B, "customer_id", "customer_id", right or {"x": 1})
r_no = linkage.propose(A, B, "customer_id", "id", wrong or {"x": 1})
K_OK, K_NO = r_ok["key"], r_no["key"]
chk("两条候选都记下了，状态是 inferred",
    {x["status"] for x in linkage.links(A)} == {"inferred"},
    str({x["key"]: x["status"] for x in linkage.links(A)}))
chk("**推断必须带证据**（没证据的推断没法复核）",
    all(x["evidence"] for x in linkage.links(A)),
    str(list((linkage.links(A)[0]["evidence"] or {}).keys()))[:80])

try:
    catalog.record_inference(A, "link", "no_evidence", {"x": 1}, None)
    chk("空证据被拒", False, "居然记下了")
except ValueError:
    chk("空证据被拒（record_inference 硬性要求 evidence）", True)

print("\n=== 二、门禁：没确认的连接，不许拿去回答问题（铁律 1）===\n")

with open_store(readonly=False, init_schema=True) as st:
    m = _link_not_confirmed(st, {"asset": A, "link_key": K_OK})
    chk("**inferred 的连接被挡住**", bool(m) and "NOT_CONFIRMED" in m, str(m)[:90])
    chk("拦截信息说清了为什么（推断连得出结果，但没人担保）",
        "没人担保" in (m or ""), str(m)[:110])
    m2 = _link_not_confirmed(st, {"asset": A, "link_key": "acme.x:a=b"})
    chk("不存在的连接被挡住", bool(m2) and "LINK_UNKNOWN" in m2, str(m2)[:70])
    m3 = _link_not_confirmed(st, {"asset": A})
    chk("**不说用了哪条连接也被挡住**（跨表结论要说得清凭什么连）",
        bool(m3) and "LINK_REQUIRED" in m3, str(m3)[:70])

    class Broken:
        def catalog(self, **kw):
            raise RuntimeError("db down")
    m4 = _link_not_confirmed(Broken(), {"asset": A, "link_key": K_OK})
    chk("**档案读不出来时 fail-closed**（读不到 ≠ 批过了）",
        bool(m4) and "UNVERIFIABLE" in m4, str(m4)[:70])

print("\n=== 二·2、走真闸门（不是直接调检查函数）===\n")

# 上面几条调的是 `_link_not_confirmed` 本身。**那证明不了它被接上了。**
# 这个项目摔过的坑正是这个形状：量写了，闸门不读。
g = gate("answer_with_link",
         {"question": "q", "sql": f"SELECT count(*) FROM {TA}",
          "asset": A, "link_key": K_OK}, task_id="t-link")
chk("**真闸门也挡住了 inferred 的连接**",
    isinstance(g, dict) and g.get("action") == "block"
    and "LINK_NOT_CONFIRMED" in str(g.get("message")), str(g)[:90])

print("\n=== 三、人否定错的那条：留着，带原因 ===\n")

linkage.refute(A, K_NO, "wang@acme.com",
               "contact.id 是它自己的主键，不是客户号；按它连会漏掉 15 条对接人")
st_rows = {x["key"]: x["status"] for x in linkage.links(A)}
chk("错的那条成了 refuted", st_rows.get(K_NO) == "refuted", str(st_rows))
chk("**被否定的假设没有被删**（下次别再猜同一个错）",
    any(x["key"] == K_NO for x in linkage.links(A)), str(list(st_rows)))
with open_store(readonly=False, init_schema=True) as st:
    m = _link_not_confirmed(st, {"asset": A, "link_key": K_NO})
    chk("**否定过的连接再用会被挡，且告诉你上次为什么说不行**",
        bool(m) and "REFUTED" in m and "主键" in m, str(m)[:120])

print("\n=== 四、人确认对的那条 ===\n")

linkage.confirm(A, K_OK, "wang@acme.com", note="crm_contact.customer_id 就是客户号")
st_rows = {x["key"]: x["status"] for x in linkage.links(A)}
chk("对的那条成了 confirmed", st_rows.get(K_OK) == "confirmed", str(st_rows))
with open_store(readonly=False, init_schema=True) as st:
    chk("**门禁放行了**", _link_not_confirmed(st, {"asset": A, "link_key": K_OK}) is None)
with open_store(readonly=True, init_schema=False) as st:
    hist = st.catalog(A, kind="link", current_only=False, limit=99)
chk("**确认没有把推断删掉**（系统猜过什么、人为什么改，都还查得到）",
    {r["status"] for r in hist} == {"inferred", "confirmed", "refuted"},
    str([(r["key"][-22:], r["status"]) for r in hist]))

# plane 由门禁钉死成 lake：模型写 source 也没用，否则这个工具就成了
# 绕过 `plane=source` 那些护栏的旁路（铁律 3）。
g2 = gate("answer_with_link",
          {"question": "q", "sql": f"SELECT count(*) FROM {TA}",
           "asset": A, "link_key": K_OK, "plane": "source",
           "source": "acme"}, task_id="t-link")
chk("**闸门把 plane 钉死成 lake**（模型给 source 也改回来）",
    isinstance(g2, dict) and g2.get("action") == "modify"
    and g2["args"].get("plane") == "lake", str(g2)[:110])
chk("放行时没把工具自己的参数擦掉（link_key 还在）",
    isinstance(g2, dict) and g2["args"].get("link_key") == K_OK,
    str(sorted((g2 or {}).get("args", {}))))
chk("SQL 也过了同一条 AST 准入（gate 会把它重写回来）",
    isinstance(g2, dict) and "select" in g2["args"].get("sql", "").lower(),
    str(g2["args"].get("sql"))[:70])

# **未声明的工具默认拒绝**（铁律 5）—— 三个新工具都得在 policy 里。
from datasteward_gate.policy import POLICY, Level              # noqa: E402
for t in ("propose_link", "confirm_link", "answer_with_link"):
    chk(f"{t} 已在 policy 里显式声明级别（铁律 5）", t in POLICY,
        str(POLICY.get(t)))
chk("**确认连接是 L2、要人批**（不是 L1 自己拍板）",
    POLICY.get("confirm_link") == (Level.L2, "steward"),
    str(POLICY.get("confirm_link")))
chk("找候选是 L1（只提假设，不该也要审批 —— 那是套娃）",
    POLICY.get("propose_link")[0] == Level.L1)

print("\n=== 五、回答一个单表答不了的问题 ===\n")

Q = "哪个城市的客户对接人最多？"
SQL = (f'SELECT lower(c.city) AS city, count(*) AS contacts'
       f' FROM {TA} c JOIN {TB} t ON c.customer_id = t.customer_id'
       f' GROUP BY lower(c.city) ORDER BY 2 DESC, 1 LIMIT 5')

# **先证明它真的单表答不了**：两张表各自都缺一半。
chk("**contact 表里没有 city**（所以单看它答不了「哪个城市」）",
    "city" not in {c[0] for c in (catalog.lookup("acme", "crm_contact")
                                  or {}).get("columns", [])})
cols_a = {c[0] for c in (catalog.lookup("acme", "crm_customer") or {}).get("columns", [])}
cols_b = {c[0] for c in (catalog.lookup("acme", "crm_contact") or {}).get("columns", [])}
chk("**customer 表里没有对接人**（所以单看它也答不了「几个对接人」）",
    not any("contact" in c for c in cols_a), str(sorted(cols_a)))

# 复核走另一条路径：不 join，用 IN 子查询按城市数对接人。
VERIFY = (f"SELECT count(*) FROM {TB} t WHERE t.customer_id IN"
          f" (SELECT customer_id FROM {TA} WHERE lower(city) = 'shanghai')")
top = sync._trino(SQL)
res = linkage.answer(Q, SQL, [A, B], actor="claw",
                     verify_sql=VERIFY, expect=None)
chk("答出来了", res.get("rows", 0) > 0, str(res.get("result"))[:80])
chk("**答案记了它依赖哪一份数据**（源变了才知道结论该不该复核）",
    set(res["depends_on"]) == {A, B}, str(res["depends_on"])[:120])
chk("**依赖里带得上行数**（台账没记也要从湖里现查，不能只说「不明」）",
    all(v.get("lake_rows") for v in res["depends_on"].values()),
    str(res["depends_on"])[:130])
chk("**复核跑了另一条路径**", res["verification"].get("checked"),
    str(res["verification"])[:90])

print("\n=== 六、核验答案：错的连法答出来的数看着一样正常 ===\n")

WRONG_SQL = SQL.replace("t.customer_id", "t.id")
w = sync._trino(WRONG_SQL)
chk("错的连法也跑得通、也有结果（**跑通不等于答对**）", bool(w), str(w)[:70])

n_right = int(sync._trino(
    f"SELECT count(DISTINCT c.customer_id) FROM {TA} c JOIN {TB} t"
    f" ON c.customer_id = t.customer_id")[0])
n_wrong = int(sync._trino(
    f"SELECT count(DISTINCT c.customer_id) FROM {TA} c JOIN {TB} t"
    f" ON c.customer_id = t.id")[0])
chk("**「有对接人的客户数」两种连法答案一样**（计数题分不出来）",
    n_right == n_wrong == 40, f"{n_right} vs {n_wrong}")

chk("**但按城市分组就分出来了**（错的连法给出不同分布）",
    top != w, f"对={top[:2]} 错={w[:2]}")

# 真正的核验：抽样回到源库逐行对。**这才是「核验答案」，
# 不是「SQL 跑通了」。**
sample = sync._trino(
    f'SELECT t.id, t.customer_id, c.name FROM {TB} t JOIN {TA} c'
    f' ON t.customer_id = c.customer_id ORDER BY t.id LIMIT 5')
# **源库不许 JOIN**（铁律 3）—— 核验必须绕开它，不是绕过它。
try:
    connector.query("acme", "SELECT t.id FROM crm_contact t"
                            " JOIN crm_customer c ON t.customer_id=c.customer_id")
    chk("源库拒绝 JOIN（铁律 3）", False, "居然放行了")
except Exception as e:                                        # noqa: BLE001
    chk("**核验不能靠在源库上 JOIN**（铁律 3 挡住了，这是对的）",
        type(e).__name__ == "QueryApprovalRequired", type(e).__name__)

# 两张表**各自单表**取回来，配对在这边做。这样既守住铁律 3，
# 又真的回到了源头 —— 核验的价值全在「不复用被核验的那条路径」。
def _rows(sql):
    r = connector.query("acme", sql).get("rows") or []
    return [r_ if isinstance(r_, str) else ",".join(
        "" if x is None else str(x) for x in r_) for r_ in r]

pairs = {}
for line in _rows("SELECT customer_id, name FROM crm_customer"):
    cid, _, nm = line.partition(",")
    pairs[cid] = nm
expect_rows = []
for line in _rows("SELECT id, customer_id FROM crm_contact ORDER BY id LIMIT 5"):
    i, _, cid = line.partition(",")
    expect_rows.append(f"{i},{cid},{pairs.get(cid)}")
chk("**抽样回源库逐行对得上**（湖里连出来的配对，源库认）",
    bool(expect_rows) and list(sample) == expect_rows,
    f"湖={sample[:2]} 源={expect_rows[:2]}")

print("\n=== 七、结论进台账：哪天源变了，查得到它基于哪份数据 ===\n")

with open_store(readonly=True, init_schema=False) as st:
    prov = [p for p in st.provenance(A, limit=200) if p["event"] == "answered"]
chk("答案写进了资产台账", bool(prov), str(len(prov)))
if prov:
    d = prov[-1]["detail"]
    chk("台账里记着问题本身", Q[:8] in str(d.get("question")), str(d.get("question"))[:50])
    chk("**台账里记着当时的数据版本**", bool(d.get("depends_on")),
        str(d.get("depends_on"))[:100])
    chk("台账里记着复核结果", bool(d.get("verification")), str(d.get("verification"))[:80])

print("\n=== 八、复核对不上时，如实说，不把结果吞掉 ===\n")

bad_res = linkage.answer("故意对不上的复核", f"SELECT count(*) FROM {TA}",
                         [A], verify_sql=f"SELECT count(*) FROM {TB}",
                         expect="99999")
chk("**复核不通过要标出来**", bad_res["verification"].get("ok") is False,
    str(bad_res["verification"])[:90])
chk("但结果本身还在（是「这个数没验过」，不是「查不出来」）",
    bad_res.get("rows", 0) > 0, str(bad_res.get("result"))[:40])

bad2 = linkage.answer("复核语句本身写错了", f"SELECT count(*) FROM {TA}",
                      [A], verify_sql="SELECT * FROM no_such_table_xyz")
chk("**复核语句自己挂了，也不能算「验过了」**",
    bad2["verification"].get("checked") is False
    and "error" in bad2["verification"], str(bad2["verification"])[:90])

print("\n=== 八·2、工具自己的输出（闸门验过了，工具还得说得对）===\n")

# 这两条都是**实跑时崩过 / 说错过**的分支，不是补充覆盖率。
import importlib.util                                          # noqa: E402
import types                                                   # noqa: E402

_pkg = types.ModuleType("ds_tools_pkg")
_pkg.__path__ = [str(ROOT / ".hermes" / "plugins" / "data-steward")]
_pkg._ensure_path = lambda: None
sys.modules["ds_tools_pkg"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "ds_tools_pkg.tools", ROOT / ".hermes" / "plugins" / "data-steward" / "tools.py")
TOOLS = importlib.util.module_from_spec(_spec)
sys.modules["ds_tools_pkg.tools"] = TOOLS
_spec.loader.exec_module(TOOLS)

txt = TOOLS._answer_with_link({
    "question": Q, "sql": SQL, "asset": A, "link_key": K_OK, "assets": [A, B],
    "verify_sql": VERIFY, "expect": "22"})
chk("**依赖那行报的是湖里现查的行数**（台账没记时别说「行数 None」）",
    "40 行" in txt and "55 行" in txt and "None" not in txt,
    txt.split("依赖的数据")[-1][:110])
chk("复核通过说通过", "✅" in txt, txt[-160:][:60])

_orig = linkage.overlap
linkage.overlap = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("trino busy"))
try:
    out = TOOLS._propose_link({"asset_a": A, "asset_b": B, "record": "false"})
finally:
    linkage.overlap = _orig
chk("**证据算不出来时工具不崩**（崩过一次：候选里没有 shared 这个键）",
    "算不出来" in out, out[:120])
chk("而且说的是「算不出来」，不是「没这条候选」"
    "（后者会让一次查询失败看起来像一个结论）",
    "重试" in out and "没找到" not in out, out[:120])

print("\n=== 九、跨系统真连不上时，不硬连 ===\n")

try:
    catalog.observe("olist_raw", "customers")
    cross = linkage.candidates("acme.crm_customer", "olist_raw.customers")["pairs"]
    chk("**两个 customer_id 值域毫不重叠 → 一条候选都不给**",
        not cross, str(cross)[:100])
except Exception as e:                                        # noqa: BLE001
    chk("跨系统对照（olist_raw 不在时跳过）", True, f"skip: {type(e).__name__}")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
