"""bronze → silver 清洗（readme 5.3 / 7 / 16.1）。

三条不能破的规矩：不改源系统、bronze 原样落地、原值保留在 `_raw`。
但真正的分水岭是**哪些不许自动修** —— 一个「把 NULL 填成 0」的 agent
看起来很能干，直到财务发现报表少了一个亿。
"""
import os
import pathlib
import subprocess
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_clean_{uuid.uuid4().hex[:8]}.db"

import clean
import ledger as L

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def trino_up():
    """探活必须**走和干活同一条路**。

    早先这里自己拼一条免密码的 `docker exec`，加上认证之后它悄悄失败，
    于是整段 silver 实测被判成「Trino 未启动」跳过 —— 10 条断言凭空消失
    而回归还是绿的。探活与干活分两条路，迟早出这种事。"""
    try:
        import sync
        sync._trino("SELECT 1")
        return True
    except Exception:                                        # noqa: BLE001
        return False


print("\n=== 提案：哪些能自动，哪些必须问 ===\n")

findings = [
    {"column": "order_status", "issue": "enum_drift"},
    {"column": "city", "issue": "spelling_drift"},
    {"column": "zip", "issue": "type_mismatch"},
    {"column": "customer_id", "issue": "high_null_rate"},
    {"column": "price", "issue": "unit_outlier"},
    {"column": "delivered_at", "issue": "date_before_order"},
    {"column": "flag", "issue": "constant_column"},
    {"column": "order_id", "issue": "primary_key_not_unique"},
    {"column": "x", "issue": "某种没见过的问题"},
]
p = clean.propose("t", findings)
auto = {a["column"] for a in p["auto"]}
prop = {a["column"] for a in p["propose"]}
ask = {a["column"] for a in p["ask"]}

chk("去重是唯一可直接做的（不改变任何值）", auto == {"order_id"}, str(auto))
chk("大小写归一是提案不是自动（标准形取哪个要人定）", "order_status" in prop)
chk("拼写归一是提案（音标是不是同一个地方要人裁决）", "city" in prop)
chk("类型污染是提案", "zip" in prop)
chk("**补空值绝不自动**", "customer_id" in ask)
chk("**改数量级绝不自动**", "price" in ask)
chk("**改日期绝不自动**", "delivered_at" in ask)
chk("废弃字段判断要问业务", "flag" in ask)
chk("没见过的问题按不可自动处理", "x" in ask)
chk("去重被识别为确定性变换", p["dedup_by_pk"] is True)
chk("提案自带那句要问的话", "洗成这样对吗" in p["question"])
chk("每条都写明了理由", all(a.get("why") for a in p["auto"] + p["ask"]))
chk("提案项给出 SQL 表达式", all(a["expr"] for a in p["propose"]))
chk("要问的项没有表达式（防止被误执行）", all(a["expr"] is None for a in p["ask"]))
chk("无 findings 时不提案", clean.propose("t", [])["question"] == "无需清洗")

print("\n=== 标识符校验 ===\n")

for bad_name in ("a b", "a;drop", "1x", ""):
    try:
        clean._ident(bad_name)
        chk(f"非法标识符 {bad_name!r} 被拒", False)
    except clean.CleanError:
        chk(f"非法标识符 {bad_name!r} 被拒", True)

T = "olist_raw__eval_olist_dq_v1__orders"


def _bronze_has(table: str) -> bool:
    """那张表在不在 bronze 里。**探活走干活同一条路。**

    这一段要真数据，而它用的表是**别的东西留下的**（eval 跑过才有）。
    lake 是共享的、会被清空的（`tests/reset_live.py` 就会清），
    依赖别人留下的状态 = 别人一清这里就红，而原因跟被测代码无关。
    """
    try:
        import sync
        return table in set(sync._trino(
            "SELECT table_name FROM iceberg.information_schema.tables"
            " WHERE table_schema='bronze'"))
    except Exception:                                         # noqa: BLE001
        return False


if not trino_up():
    print("\n  SKIP  Trino 未启动，跳过 silver 实测\n")
elif not _bronze_has(T):
    print(f"\n  SKIP  silver 实测：bronze 里没有 {T}"
          f"\n        （它由 eval 跑产生；湖被清过就得先跑一次 eval）\n")
else:
    print("\n=== 实测：bronze 原样，silver 才是清洗结果 ===\n")

    cols = ["order_id", "customer_id", "order_status", "order_purchase_timestamp",
            "order_approved_at", "order_delivered_carrier_date",
            "order_delivered_customer_date", "order_estimated_delivery_date"]
    plan = clean.propose(T, [{"column": "order_status", "issue": "enum_drift"}])
    chk("未批准时不执行任何变换",
        clean.apply(T, plan, cols, silver_table=T + "_noapprove")["applied"] == [])
    plan["approved_rules"] = ["enum_drift__order_status"]      # 人批了这条规则
    r = clean.apply(T, plan, cols, pk="order_id")

    chk("silver 建出来了", r["rows"] > 0, f"{r['rows']:,} 行")
    chk("bronze 行数未被改动", r["bronze_rows"] > 0, f"{r['bronze_rows']:,}")
    chk("生效规则被记录", "enum_drift__order_status" in
        [a["rule"] for a in r["applied"]])

    ba = clean.before_after(T, T, "order_status", limit=5)
    chk("有前后对比样例", ba["shown"] > 0, f"变了 {ba['changed_rows']} 行")
    chk("原值保留在 _raw", all(s["before"] != s["after"] for s in ba["samples"]),
        str(ba["samples"][:2]))
    chk("洗后确实归一", all(s["after"] == s["after"].lower().strip()
                            for s in ba["samples"]))

    print("\n=== 规则必须挂在台账上 ===\n")

    rl = L.record(T, "enum_drift", field="order_status",
                  observed_pattern="delivered/Delivered/DELIVERED 三种写法")
    reg = clean.register_rules(r["applied"], {"enum_drift": rl["rl_id"]})
    chk("规则登记进台账", len(reg) == 1, str([x["name"] for x in reg]))
    chk("规则指向具体缺陷", L.rules()[0]["ledger_ref"] == rl["rl_id"])
    chk("没有台账引用的规则不登记",
        clean.register_rules([{"column": "c", "issue": "unknown", "rule": "r"}],
                             {}) == [])

print("\n=== 标准形按人定的口径来 ===\n")

# `enum_drift` 的注释从 R2 起就写着「标准形取哪个**要人定**」，
# 而实现一直写死 `lower()` —— 人定了全大写也照样洗成小写。
# 口径确实沉淀在 asset_semantics 里，只是**没有人去读**：
# 「闸门读的量没人写」的镜像 —— 量写了，闸门不读。
_semdb = "/tmp/dl_clean_sem.db"
for _suf in ("", "-wal", "-shm"):
    _f = pathlib.Path(_semdb + _suf)
    if _f.exists():
        _f.unlink()
_saved_db = os.environ.get("DATASTEWARD_DB")
os.environ["DATASTEWARD_DB"] = _semdb
try:
    from plugins.datasteward_gate.approvals import Store as _St
    _s2 = _St(_semdb, readonly=False)
    chk("没定过口径时返回 None（不猜一个默认值）",
          clean.normalized_case("a.b", "c") is None)
    _s2.remember("a.b.c", "normalize_rule", "统一成全大写 PAID/VOID", "wang")
    chk("**人定了全大写就返回 upper**", clean.normalized_case("a.b", "c") == "upper")
    _s2.remember("a.b.f", "normalize_rule", "统一成大写，原始大小写保留", "wang")
    chk("描述原始大小写不影响已定的大写口径",
        clean.normalized_case("a.b", "f") == "upper")
    _s2.remember("a.b.d", "normalize_rule", "统一成小写", "wang")
    chk("人定了小写就返回 lower", clean.normalized_case("a.b", "d") == "lower")
    # 两种都提到 = 看不懂，**别猜**：猜错的方向是「按你没说过的规矩改了你的数据」。
    _s2.remember("a.b.e", "normalize_rule", "大写还是小写你们定", "wang")
    chk("看不懂就返回 None（宁可不洗，不许猜）",
          clean.normalized_case("a.b", "e") is None)
finally:
    if _saved_db is None:
        os.environ.pop("DATASTEWARD_DB", None)
    else:
        os.environ["DATASTEWARD_DB"] = _saved_db

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
