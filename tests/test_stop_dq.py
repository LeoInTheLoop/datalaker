"""停止点判定（5.2）与 DQ 门槛量化 / 收敛（5.3）。

这两条是 R2 顺延下来的欠账：在它们之前，Agent「拦得住、问得对、催得动」，
但不知道**什么时候该停下来交阶段成果**，也不知道**什么叫够干净了**。
"""
import json
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services"))
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_stopdq_{uuid.uuid4().hex[:8]}.db"

import data_tools as D
import memory as M
import stop_points as S

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== 停止点判定（5.2）===\n")

chk("空上下文不停（还没开始，没什么可交的）", S.next_stop({}) is None)

s1 = S.next_stop({"tables_discovered": True})
chk("发现完源清单就停", s1 and s1["id"] == 1, s1 and s1["name"])
chk("停止点带交付物", s1 and "清单" in s1["deliverable"])
chk("停止点带问题", s1 and s1["question"].endswith("？"))
chk("确认优先级后放行", S.next_stop({"tables_discovered": True,
                                     "priority_confirmed": True}) is None)

s2 = S.next_stop({"tables_discovered": True, "priority_confirmed": True,
                  "bronze_tables": ["orders"], "dq_findings": [{"x": 1}]})
chk("bronze + profiling 完成后停", s2 and s2["id"] == 2, s2 and s2["name"])

s4 = S.next_stop({"tables_discovered": True, "priority_confirmed": True,
                  "silver_ready": ["orders"]})
chk("发布前必停（无法无损撤销）", s4 and s4["id"] == 4)
chk("理由点明不可撤销", s4 and "撤销" in s4["reason"])

s5 = S.next_stop({"permission_proposal": True})
chk("权限方案必停", s5 and s5["id"] == 5)

b = S.next_stop({"blockers": ["dq_exhausted"], "tables_discovered": True})
chk("卡点优先于停止点", b and b["kind"] == "blocker", b and b["id"])
chk("未知卡点被忽略而非崩溃", S.next_stop({"blockers": ["nonsense"]}) is None)
chk("should_continue 与 next_stop 一致",
    S.should_continue({"tables_discovered": True}) is False)

print("\n=== DQ 门槛量化（5.3）===\n")

asset = f"test.tbl_{uuid.uuid4().hex[:6]}"
th = D.get_thresholds(asset)
chk("默认门槛齐全", {"null_rate_max", "pk_unique_min", "format_conformance_min",
                     "fk_integrity_min"} <= set(th))
chk("未确认的门槛标了出来", th["_confirmed"] is False)
chk("主键唯一性门槛是 100%", th["pk_unique_min"] == 1.0)
chk("空值率门槛是 5%", th["null_rate_max"] == 0.05)

p = D.propose_thresholds(asset)
chk("提案不直接生效", p["confirmed"] is False)
chk("提案带可问的问题", "？" in p["question"])

from plugins.datasteward_gate.approvals import Store

st = Store(os.environ["DATASTEWARD_DB"], readonly=False)
st.remember(asset, D.THRESHOLD_KEY, json.dumps({"null_rate_max": 0.2}), "wang@acme.com")
th2 = D.get_thresholds(asset)
chk("Owner 确认后门槛生效", th2["null_rate_max"] == 0.2, str(th2["null_rate_max"]))
chk("确认状态被记录", th2["_confirmed"] is True)
chk("未覆盖的项仍用默认值", th2["pk_unique_min"] == 1.0)

print("\n=== 收敛：3 轮用尽后退出自动处理 ===\n")

a2 = f"test.conv_{uuid.uuid4().hex[:6]}"
chk("初始未用尽", not M.is_exhausted(a2, "high_null_rate"))
for _ in range(M.MAX_ATTEMPTS):
    M.record_attempt(a2, "high_null_rate")
chk(f"{M.MAX_ATTEMPTS} 轮后用尽", M.is_exhausted(a2, "high_null_rate"))
chk("裁决只有三种", set(D.VERDICTS) == {"pass_to_silver", "propose_fix",
                                        "needs_human_review"})

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
