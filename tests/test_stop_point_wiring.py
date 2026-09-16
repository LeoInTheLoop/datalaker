"""停止点接进运行路径（checklist I08.09）。

`services/stop_points.py` 从 R4 写好之后**只有测试引用**，没有任何运行路径
调用它 —— 「预算到线就停」那一半有人管（门禁），「信息边界到了、该交阶段
成果」这一半没人管。这里验的就是后半：monitor 能不能自己发现该停了。

三件事：
  · 局面到了 → monitor 报一行，带交付物和要问的问题
  · 人拍过了 → 不再报（不反复打扰）
  · 事实查不出来 → 判不了，**不许当成「没到」**

不连 Docker：自带临时 SQLite 治理库；Trino 本来就连不上，正好用来验第三条。
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(tempfile.mkdtemp(), "stop.db")
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins"),
                os.path.join(ROOT, "ops")]

from datasteward_gate.approvals import open_store          # noqa: E402
import stop_points                                         # noqa: E402

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def monitor() -> list:
    env = dict(os.environ, DATASTEWARD_DB=DB)
    env.pop("DATASTEWARD_DSN", None)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "resumable.py")],
                       capture_output=True, text=True, env=env)
    return [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]


def stops(lines):
    return [d for d in lines if d.get("kind") in ("stop_point", "blocker")]


def sql(*stmts):
    with open_store() as st:
        for s in stmts:
            st.db.execute(s)
        st.db.commit()


print("\n=== 停止点接线（I08.09）===\n")

with open_store(init_schema=True):
    pass

check("空库什么都不报", not stops(monitor()))

# ---- bronze 落了、DQ 有发现、没人拍过怎么处理 → 停止点 2 ----
now = time.time()
sql(f"INSERT INTO sync_state (asset, strategy, last_synced_at) "
    f"VALUES ('northwind.orders', 'full', {now})",
    "INSERT INTO remediation_ledger (rl_id, source_table, issue_type, category,"
    f" status, first_seen, last_seen) VALUES ('RL-1', 'northwind.orders',"
    f" 'null_rate', 'data', 'open', {now}, {now})")
hit = stops(monitor())
check("bronze + DQ 发现且无人拍板 → 报停止点", len(hit) == 1, json.dumps(hit)[:80])
if hit:
    check("报的是停止点 2（脏数据怎么处理）", hit[0].get("stop_id") == "2",
          hit[0].get("name", ""))
    check("带上交付物和要问的问题",
          bool(hit[0].get("deliverable")) and bool(hit[0].get("question")))
    check("不带 waited —— 提案发一次就够，不每小时催",
          "waited" not in hit[0], json.dumps(hit[0])[:60])

check("同一局面下输出逐字节稳定", monitor() == monitor())

# ---- 人拍了 start_silver → 这一停止点过去了 ----
with open_store() as st:
    aid = st.ask("run-x", "northwind.orders", "这些脏数据怎么处理？",
                 [{"key": "start_silver", "label": "开始清洗轮"}], "steward")
    st.decide(aid if isinstance(aid, str) else aid[0], "answered", "steward",
              token_jti="jti-stage", chosen="start_silver")
check("人拍板之后不再报同一个停止点",
      not [d for d in stops(monitor()) if d.get("stop_id") == "2"])

# ---- 卡点优先于停止点 ----
sql("INSERT INTO asset_catalog (asset, kind, key, value, status, actor, observed_at)"
    f" VALUES ('northwind.orders', 'review', 'schema_changed', '列变了', 'observed',"
    f" 'system:metadata_watch', {now})")
hit = stops(monitor())
check("源结构变了 → 报卡点", any(d.get("kind") == "blocker" for d in hit),
      json.dumps(hit)[:80])

# ---- 三态：查不出来 ≠ 没到 ----
check("silver_ready=None 时停止点 4 判不了",
      any(u["id"] == 4 for u in stop_points.unevaluable({"silver_ready": None})))
check("判不了的不会被 next_stop 当成「没到」而放过",
      stop_points.next_stop({"silver_ready": None, "publish_approved": False}) is None)
check("同样的局面、事实查得出来时照常报",
      (stop_points.next_stop({"silver_ready": True, "publish_approved": False}) or {})
      .get("id") == 4)

# ---- 盯住还没有事实来源的键 ----
import resumable                                            # noqa: E402
observed = set(resumable._observe()[0])
needed = {k for sp in stop_points.STOP_POINTS for k in sp.get("needs", ())}
check("还没有来源的键正好是记在案的那两个",
      needed - observed == {"priority_confirmed", "sample_reviewed"},
      f"实际缺 {sorted(needed - observed)}")
check("Trino 连不上时 silver_ready 写 None，不写 False",
      resumable._observe()[0].get("silver_ready") is None)

print(f"\n{len(ok)} 通过 / {len(bad)} 失败")
if bad:
    print("失败项：" + "、".join(bad))
sys.exit(1 if bad else 0)
