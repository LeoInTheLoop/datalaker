"""资产档案：**认知可复用 —— 不回源库也说得清一张表**（R6 闭环 A）。

验收判据不是「档案表建出来了」，而是这两条：

  1. **源库断了照样能答。** 第二阶段把 `_meta_exec` 换成直接抛异常
     （比「数一数访问了几次」硬：访问不了还能答，才叫真的没依赖它），
     `describe_asset` 仍要给出结构、负责人、口径。
  2. **三层分得开。** 推断、人确认、被否定的假设各自成行；人一确认，
     原来的推断**仍然查得到** —— 「系统曾经猜错过什么」是下次少犯错的依据。
     R6 之前它们混在 `asset_semantics` 里，`UNIQUE(asset,key)` 让确认
     直接覆盖推断。

需要 docker core（源库 northwind）。

    ./.venv/bin/python tests/test_catalog.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins"),
                str(ROOT / ".hermes" / "plugins")]

DB = "/tmp/dl_catalog_test.db"
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


import catalog                                                # noqa: E402
import connector                                              # noqa: E402
import memory as M                                            # noqa: E402
from datasteward_gate.approvals import open_store              # noqa: E402

# 工具模块**必须当包内模块导入** —— tools.py 里有 `from . import _ensure_path`，
# 直接 `import tools` 会撞「relative import with no known parent package」。
import importlib                                              # noqa: E402
T = importlib.import_module("data-steward.tools")             # noqa: E402

SRC, TBL = "northwind", "orders"
ASSET = f"{SRC}.{TBL}"

print("\n=== 阶段 1：第一次采集就建档（源库可达）===\n")

try:
    connector.describe_table(SRC, TBL)
    connector.list_foreign_keys(SRC)
    reachable = True
except Exception as e:                                        # noqa: BLE001
    reachable = False
    print(f"  源库连不上（{type(e).__name__}），本轮只能跑离线部分")

if reachable:
    st = open_store(readonly=True)
    rows = st.catalog(asset=ASSET)
    kinds = {(r["kind"], r["key"]): r for r in rows if r["status"] == "observed"}
    chk("采集即落档：列在档", ("schema", "columns") in kinds,
        f"{len(kinds.get(('schema', 'columns'), {}).get('value') or [])} 列")
    chk("采集即落档：主键在档", ("schema", "primary_key") in kinds)
    chk("采集即落档：外键在档", ("foreign_keys", "declared") in kinds,
        str(len(kinds.get(("foreign_keys", "declared"), {}).get("value") or [])))
    chk("观测行的 actor 是采集方而非模型",
        all(r["actor"].startswith("connector:")
            for r in rows if r["status"] == "observed"))
    chk("每条观测都带依据",
        all(r["evidence"] for r in rows if r["status"] == "observed"))

    # 幂等：源库没变，再采一次不该多出行。
    # 巡检（闭环 B）每天跑一遍，不幂等就是一年 365 行垃圾。
    n0 = len(st.catalog(asset=ASSET, current_only=False))
    connector.describe_table(SRC, TBL)
    connector.list_foreign_keys(SRC)
    chk("内容没变就不写新行（幂等）",
        len(st.catalog(asset=ASSET, current_only=False)) == n0, f"仍 {n0} 行")

print("\n=== 阶段 2：源库断了，档案还答得出来 ===\n")

# 把元数据通道换成直接抛异常。**比数访问次数硬**：
# 「没访问」可能只是这次凑巧走了缓存；「访问不了还能答」才是不依赖它。
_real_meta = connector._meta_exec
touched = []


def _dead(*a, **k):
    touched.append(a[:2])
    raise RuntimeError("源库不可达（本测试故意切断）")


connector._meta_exec = _dead

if reachable:
    snap = catalog.lookup(SRC, TBL)
    chk("断源后仍读得到结构", bool(snap and snap["columns"]),
        f"{len(snap['columns'])} 列" if snap else "无")
    chk("快照带观测时刻", bool(snap and snap.get("observed_at")))
    chk("读档一次都没碰源库", not touched, str(touched))

    out = T._describe_asset({"source": SRC, "table": TBL})
    chk("describe_asset 断源可用", "列" in out and "错误" not in out[:20],
        out.splitlines()[0][:60])
    chk("输出报出快照时刻（延迟不藏着）", "档案快照" in out and "采于" in out)
    chk("输出标了［观测］", "［观测］" in out)
    chk("describe_asset 全程没碰源库", not touched, str(touched))

    # 「源库声明了没有外键」和「我们没采到外键」是两个结论，不能混。
    connector._meta_exec = _real_meta
    free = catalog.observe(SRC, "us_states")
    connector._meta_exec = _dead
    chk("观测到「没有外键」会显式记一条空的", free.get("foreign_keys") == [],
        repr(free.get("foreign_keys")))
    chk("输出说的是「源库没有声明」，不是「没采到」",
        "源库没有声明" in catalog.render(free))
    chk("确实采到外键的表不受影响",
        bool(catalog.lookup(SRC, TBL)["foreign_keys"]))
    # 只被别的表引用、自己没被 describe 过的表：FK 在档但没有结构 ——
    # **算无档**。半份档案不该冒充完整的一份。
    chk("只有外键没有结构的表算无档", catalog.lookup(SRC, "shippers") is None)

# 无档的表：**必须说不知道，不许猜**（R5 实测过它把连接账号猜成超级用户）
none_snap = catalog.lookup(SRC, "no_such_table_xyz")
chk("无档就是无档，不编", none_snap is None)
out2 = T._describe_asset({"source": SRC, "table": "no_such_table_xyz"}) \
    if reachable else "失败"
chk("无档且源库不可达时如实报错，不编结构",
    "失败" in out2 and "列：" not in out2, out2[:60])

connector._meta_exec = _real_meta

print("\n=== 阶段 3：三层不互相覆盖 ===\n")

st = open_store(readonly=False)

# 推断
iid = catalog.record_inference(
    ASSET, "link", "cross_system", {"to": "acme.fin_invoice", "on": "customer_id"},
    evidence={"overlap": 0.87, "sample": ["ALFKI", "ANATR"]}, actor="model")
chk("推断可落档", bool(iid))

try:
    catalog.record_inference(ASSET, "link", "no_evidence", {"x": 1},
                             evidence=None, actor="model")
    chk("无依据的推断被拒", False)
except ValueError:
    chk("无依据的推断被拒", True)

# 人确认 —— 值和推断不同，模拟「人改了机器的猜测」
cid = catalog.confirm(ASSET, "link", "cross_system",
                      {"to": "acme.fin_invoice", "on": "cust_no"},
                      actor="wang@acme.com", evidence={"ticket": "appr-123"})
chk("确认可落档", bool(cid))

cur = st.catalog(asset=ASSET, kind="link")
cur_cross = [r for r in cur if r["key"] == "cross_system"]
chk("当前有效的只剩确认那条", len(cur_cross) == 1
    and cur_cross[0]["status"] == "confirmed",
    str([r["status"] for r in cur_cross]))

allr = st.catalog(asset=ASSET, kind="link", current_only=False)
old = [r for r in allr if r["id"] == iid]
chk("**推断没被删**，仍查得到", len(old) == 1, str(old[0]["status"]) if old else "没了")
chk("推断被标成「被谁取代」", bool(old and old[0]["superseded_by"] == cid),
    str(old[0]["superseded_by"]) if old else "")
chk("人改过的地方看得出来（推断值 ≠ 确认值）",
    bool(old) and old[0]["value"].get("on") != cur_cross[0]["value"].get("on"),
    f"{old[0]['value'].get('on')} → {cur_cross[0]['value'].get('on')}" if old else "")

# 否定的假设留痕
rid = catalog.refute(ASSET, "link", "bad_guess", "两边的 id 不是同一个实体",
                     actor="wang@acme.com")
chk("被否定的假设留痕", bool(rid))
chk("否定行查得到",
    any(r["status"] == "refuted" for r in st.catalog(asset=ASSET, kind="link")))

print("\n=== 阶段 4：观测层模型写不了 ===\n")

# 真正的边界不在数据库权限（采集与 Agent 共用一个账号），在**工具签名**：
# 没有任何工具接受 status 参数，也没有任何工具能写 observed。
T2 = T

props = {}
for name, sc in T2._SCHEMAS.items():
    props[name] = set((sc.get("parameters") or {}).get("properties") or {})
chk("没有工具接受 status 参数",
    not [n for n, p in props.items() if "status" in p],
    str([n for n, p in props.items() if "status" in p]))
# 观测层的唯一入口是 `catalog.record_observed`，而它只由 connector 调。
# **静态查一遍**：工具模块里不该出现写 observed 的调用 —— 光靠「现在没写」
# 是靠不住的，得让下一个人加进去时这条测试就红。
_src = (ROOT / ".hermes" / "plugins" / "data-steward" / "tools.py").read_text()
chk("工具模块里没有写档案的调用（读可以，写不行）",
    "record_observed" not in _src and "catalog_put" not in _src,
    "record_observed" if "record_observed" in _src else "catalog_put")
_conn_src = (ROOT / "services" / "connector.py").read_text()
chk("观测层的写入方只有 connector", "record_observed" in _conn_src)

try:
    st.catalog_put(ASSET, "schema", "columns", [], "made_up", "model")
    chk("未知状态被拒", False)
except ValueError:
    chk("未知状态被拒", True)

print("\n=== 阶段 5：口径的出处会跟着更新 ===\n")

# R6 交接点名的 bug：`remember()` 的 UPSERT 只更新 value/confirmed_by，
# **source_item 留着上一次的** —— 新口径配旧出处，追溯时指向一份
# 根本没提过这条口径的审批。
st.remember("x.y", "revenue", "按开票", "zhao@acme.com", source_item="appr-1")
st.remember("x.y", "revenue", "按收款", "wang@acme.com", source_item="appr-2")
row = st.db.execute("SELECT value, confirmed_by, source_item FROM asset_semantics"
                    " WHERE asset='x.y' AND key='revenue'").fetchone()
chk("口径更新了", row[0] == "按收款", str(row[0]))
chk("出处跟着更新（不再是新口径配旧出处）", row[2] == "appr-2", str(row[2]))

print("\n=== 阶段 6：口径区分推断与人确认 ===\n")

st.remember(ASSET, "ownership", "owner:ops", "boss@acme.com")
st.assign_role("owner:ops", "sun@acme.com", "boot", "")
own = catalog.ownership(ASSET)
chk("人确认过的归属标 confirmed", own["status"] == "confirmed", str(own))
chk("归属能解析到具体的人", own["person"] == "sun@acme.com", str(own["person"]))

st.remember(f"{ASSET}.freight", "null_meaning", "空表示未产生运费",
            "wang@acme.com")
sem = {s["key"]: s for s in catalog.semantics(ASSET)}
chk("人定的口径标 confirmed",
    sem.get("freight.null_meaning", {}).get("status") == "confirmed",
    str(sem.get("freight.null_meaning", {}).get("status")))

if reachable:
    M.infer_asset_links(SRC)
    links = M.related(SRC, TBL)
    chk("外键推断仍可取回", bool(links), f"{len(links)} 条")
    infer_rows = [r for r in st.catalog(asset=ASSET, kind="link")
                  if r["key"] == "fk_derived"]
    chk("外键推断落的是 inferred 层，不是 asset_semantics",
        bool(infer_rows) and infer_rows[0]["status"] == "inferred",
        str(infer_rows[0]["status"]) if infer_rows else "无")
    legacy = st.known(ASSET, "links")
    chk("不再往 asset_semantics 写推断", legacy is None, str(legacy))

    out = T2._describe_asset({"source": SRC, "table": TBL})
    chk("输出里推断与确认分得开",
        "［推断］" in out and "［已确认］" in out,
        "推断✓" if "［推断］" in out else "推断✗")
    chk("负责人进了输出", "sun@acme.com" in out)
    chk("口径进了输出", "未产生运费" in out)

    tr = T2._trace_asset({"asset": ASSET})
    chk("trace_asset 也标类别", "［已确认］" in tr and "推断" in tr,
        tr.splitlines()[0][:40])

print("\n=== 阶段 7：枚举口径结构化，不靠正则从人话里抠 ===\n")

# R6 §6.2 的欠账：`test_silver_usable` 拿 `\\b[A-Z][A-Z_]{2,}\\b` 从
# 「统一成全大写，枚举只允许 PAID / PENDING / UNPAID / VOID」里抠枚举 ——
# 换个写法（小写、中文值）就抠不出来，而那时断言**静默跳过**而不是报错。
COL = "acme.fin_invoice.status"
chk("没登记时返回 None（而不是空列表）", catalog.allowed_values(COL) is None)

r = T._define_semantics({
    "asset": COL, "key": "normalize_rule",
    "value": "统一成全大写；取值只有这四种",
    "confirmed_by": "wang@acme.com",
    "allowed_values": ["PAID", "PENDING", "UNPAID", "VOID"]})
chk("带 allowed_values 能定口径", "已记下" in r, r[:50])
chk("回执点明枚举已结构化登记", "结构化登记" in r, r[:80])
chk("枚举读得回来",
    catalog.allowed_values(COL) == ["PAID", "PENDING", "UNPAID", "VOID"],
    str(catalog.allowed_values(COL)))
# 取值范围是**某一列**的事。问表名时前缀匹配会随便返回某列的枚举 ——
# 那比返回 None 糟得多（拿错列的枚举去校验，还会「通过」）。
chk("问表名不会串到某一列的枚举",
    catalog.allowed_values("acme.fin_invoice") is None,
    str(catalog.allowed_values("acme.fin_invoice")))

# 小写、中文值 —— 正则一个都抠不出来，结构化字段照样读得回
COL2 = "acme.fin_invoice.channel"
T._define_semantics({"asset": COL2, "key": "normalize_rule",
                     "value": "渠道只有这三种", "confirmed_by": "wang@acme.com",
                     "allowed_values": ["线上", "线下", "代理"]})
import re as _re
chk("正则确实抠不出中文枚举（这就是要结构化的原因）",
    not _re.findall(r"\b[A-Z][A-Z_]{2,}\b", "渠道只有这三种"))
chk("中文枚举照样读得回", catalog.allowed_values(COL2) == ["线上", "线下", "代理"],
    str(catalog.allowed_values(COL2)))

# 枚举改了：旧的仍查得到（与三层同一条规矩）
T._define_semantics({"asset": COL2, "key": "normalize_rule",
                     "value": "加了「其它」", "confirmed_by": "boss@acme.com",
                     "allowed_values": ["线上", "线下", "代理", "其它"]})
hist = [r for r in st.catalog(asset=COL2, kind="constraint", current_only=False)]
chk("枚举改过之后旧版本仍查得到", len(hist) == 2, f"{len(hist)} 版")
chk("当前只有一版有效",
    len([r for r in hist if r["superseded_by"] is None]) == 1)

r2 = T._define_semantics({"asset": COL2, "key": "x", "value": "y",
                          "confirmed_by": "a@b.c", "allowed_values": "不是数组"})
chk("allowed_values 给字符串也认（逗号/顿号分隔）", "已记下" in r2, r2[:40])
chk("字符串形式解析成列表",
    catalog.allowed_values(COL2) == ["不是数组"], str(catalog.allowed_values(COL2)))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    for b in bad:
        print("  FAILED:", b)
raise SystemExit(1 if bad else 0)
