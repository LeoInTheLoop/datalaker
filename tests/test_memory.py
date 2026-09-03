"""记忆断言（readme 4.6 支柱三）+ 迭代上限（5.3）。"""
import json, os, sys, uuid
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services"))

# **测试自己准备数据。**（R3 handoff 的教训：跨测试的数据依赖必然出问题）
# 审批偏好统计原先读的是 ~/.datalaker/approvals.db —— 那里有没有已决记录
# 取决于之前跑过什么，于是这条断言时红时绿。
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_memory_{uuid.uuid4().hex[:8]}.db"

from plugins.datasteward_gate.approvals import Store, action_hash
_seed = Store(os.environ["DATASTEWARD_DB"], readonly=False)
for i, dec in enumerate(("approve", "approve", "deny")):
    aid, _ = _seed.request(f"seed-{i}", action_hash("ingest_table", {"t": i}),
                           "ingest_table", json.dumps({"t": i}), "owner")
    _seed.decide(aid, dec, "wang@acme.com")

import memory as M

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== 记忆：资产关联 + 审批偏好 + 迭代上限 ===\n")

r = M.infer_asset_links("northwind")
chk("从外键推断关联", r["total_links"] >= 20, f"{r['tables_with_links']} 表 {r['total_links']} 条")
ls = M.related("northwind", "orders")
chk("orders 关联可取回", {"customers", "employees"} <= {l["to"] for l in ls},
    str([l["to"] for l in ls][:4]))
chk("只用确定性信号（外键）", all(l["type"] in ("foreign_key", "referenced_by") for l in ls))
chk("未知资产返回空而非报错", M.related("northwind", "no_such_table") == [])

p = M.approver_profile("owner")
chk("审批偏好可统计", p["decided"] > 0, f"已决 {p['decided']}")
chk("给出可执行提示", isinstance(p["hints"], list))
chk("证据级别有档位", M.suggest_evidence_level("owner") in ("brief", "standard", "detailed"),
    M.suggest_evidence_level("owner"))

asset = f"test.tbl_{uuid.uuid4().hex[:6]}"
chk("初始未用尽配额", not M.is_exhausted(asset, "high_null_rate"))
for i in range(1, 4):
    n = M.record_attempt(asset, "high_null_rate")
    chk(f"第 {i} 次尝试计数正确", n == i, str(n))
chk("3 轮后配额用尽", M.is_exhausted(asset, "high_null_rate"))
chk("其他问题不受影响", not M.is_exhausted(asset, "fk_broken"))
ex = M.exhausted_issues(asset)
chk("转人工清单可列出", len(ex) == 1 and ex[0]["issue"] == "high_null_rate", str(ex))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
