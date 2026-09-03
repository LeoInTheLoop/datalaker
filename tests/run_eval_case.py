"""Eval runner（**体内**）—— 被测系统这一侧的驱动。

它 import services/plugins，因此**故意不放在 `evals/` 下**：
那边一行被测代码都不许进（`evals/test_isolation.py` 断言这一点）。
两边只通过环境变量与文件通信。

契约（由 evals/run.py 设置）：
    输入  EVAL_CASE / EVAL_MANIFEST / EVAL_OUT / EVAL_GATE=on|off
    输出  <EVAL_OUT>/trajectory.jsonl   事件轨迹
          <EVAL_OUT>/findings.json      DQ 检出（结构化，判分器不解析散文）
          <EVAL_OUT>/repairs.json       修复动作（当前恒为空，见文末）

**它绝不读 manifest 里的 `expect` 字段。** 读了就成了照答案答题——
该问不该问必须由被测策略自己判，判分器再拿 ground truth 对。
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in ("", "services", "plugins"):
    sys.path.insert(0, str(ROOT / p) if p else str(ROOT))

OUT = pathlib.Path(os.environ.get("EVAL_OUT", "evals/report/last"))
OUT.mkdir(parents=True, exist_ok=True)
GATE_ON = os.environ.get("EVAL_GATE", "on") != "off"

os.environ.setdefault("DATASTEWARD_DB", str(OUT / "approvals.db"))
os.environ.setdefault("DATASTEWARD_TOKEN_SECRET", "eval")
os.environ.setdefault("REQUIRE_SOURCE_APPROVAL", "1")
os.environ.pop("DATASTEWARD_DSN", None)

import clean, connector, data_tools, lakehouse, notify, sync  # noqa: E402
from trajectory import Trajectory                          # noqa: E402
from datasteward_gate import gate, store                   # noqa: E402
from datasteward_gate.approvals import Store, action_hash  # noqa: E402

case = json.loads(pathlib.Path(os.environ["EVAL_CASE"]).read_text(encoding="utf-8"))
man = json.loads(pathlib.Path(os.environ["EVAL_MANIFEST"]).read_text(encoding="utf-8"))
SRC, SCHEMA = case["source_id"], man.get("schema", "public")
# 采样用 case 的 seed —— 同一个 case 两次跑必须给出同一批样本，否则数字不可比
if case.get("seed") is not None:
    os.environ["CLAW_SAMPLE_SEED"] = str(case["seed"])
APPROVER = "wang@acme.com"

traj = Trajectory(f"{case['case_id']}.gate-{'on' if GATE_ON else 'off'}",
                  out_dir=str(OUT.parent))
admin = Store(os.environ["DATASTEWARD_DB"], readonly=False)
findings_out, repairs_out, bronze_out = [], [], []


class Recorder(notify.Notifier):
    """eval 里不真发信 —— 通道行为不是这轮要测的东西。"""
    name = "recorder"

    def send_approval(self, to, aid, tool, target, reason, approver):
        traj.notify_sent("approval", to, "recorder")
        return {"channel": "recorder"}

    def send_receipt(self, *a, **k):
        return {"channel": "recorder"}

    def send_notice(self, to, subject, body):
        traj.notify_sent("notice", to, "recorder")
        return {"channel": "recorder"}


notify.get = lambda channel=None: Recorder()


def call(tool, args, fn=None, summary=None):
    """走一次完整的准入。gate=off 是**对照组**：直接执行，看会发生什么。"""
    if GATE_ON:
        g = gate(tool, args, "eval")
        if isinstance(g, dict):
            traj.gate_block(tool, args, g.get("message", str(g)))
            return None
    r = fn() if fn else None
    traj.tool_call(tool, args, summary or (str(r)[:120] if r is not None else "ok"))
    return r


def approve(tool, args):
    """模拟人点击批准链接。**票据由独立 Store 落库**，Agent 侧只读。"""
    aid = store().pending(action_hash(tool, args), "eval")
    admin.decide(aid, "approve", APPROVER)
    traj.human_decision("approve", APPROVER, approval_id=aid)
    return aid


# ---------------------------------------------------------------- 1. 接入源
# DSN 由编排方给定（已指向脏 schema，账号仍是只读的 agent_ro）。
# **不在这里自己拼** —— 上一版就是拼错了库，把 northwind 的用例跑到了 olist 上。
dirty_dsn = os.environ.get("EVAL_AGENT_DSN", "")
args = {"source": SRC, "schema": SCHEMA}
if GATE_ON:
    if isinstance(gate("connect_source", args, "eval"), dict):
        traj.gate_block("connect_source", args, "[PENDING_APPROVAL] connect_source")
    aid = approve("connect_source", args)
else:
    aid = "no-approval-control-group"
connector.register_source(SRC, dirty_dsn, approval_id=aid)
traj.tool_call("connect_source", args, f"schema={SCHEMA}")

# ---------------------------------------------------------------- 2. 发现
tables = man.get("tables", [])
traj.tool_call("list_source_tables", {"source": SRC},
               f"{len(data_tools.list_source_tables(SRC)['tables'])} 张表")

for t in tables:
    meta = data_tools.get_table_metadata(SRC, t)
    traj.tool_call("get_table_metadata", {"table": t}, f"{len(meta['columns'])} 列")
    prof = data_tools.profile_table(SRC, t)
    traj.tool_call("profile_table", {"table": t}, f"采样 {prof.get('sampled_rows', 0):,} 行")
    dq = data_tools.run_dq_check(SRC, t)
    traj.tool_call("run_dq_check", {"table": t}, f"{len(dq.get('findings', []))} 项发现")

    for f in dq.get("findings", []):
        findings_out.append({"table": t, "column": f["column"],
                             "issue": f["issue"], "severity": f["severity"]})
        # ↓↓↓ 这段就是「被测策略」：该问还是该自动，由它判，不看 ground truth ↓↓↓
        if f["issue"] == "primary_key_not_unique":
            a = {"table": t, "column": f["column"], "rule": "dedup_by_pk"}
            if GATE_ON:
                approve("apply_cleaning_rule", a)
            call("apply_cleaning_rule", a, summary="按主键去重（确定性变换，可自动）")
        elif f["issue"] == "high_null_rate":
            traj.question(f"{t}.{f['column']}",
                          f"{f['column']} 空值率异常（{f['detail']}）。"
                          f"业务上允许为空吗？还是上游漏采？", approver="steward")
        elif f["issue"] == "constant_column":
            traj.question(f"{t}.{f['column']}",
                          f"{f['column']} 全表同值，疑似废弃字段。可以不接入吗？",
                          options=["接入", "跳过"], approver="steward")

# ---------------------------------------------------------------- 3. 接入
# **真写 bronze**（经 Trino 落 Iceberg），不是记一笔就算数。
# Trino 起不来时如实标 skipped，不假装接入成功。
for t in tables:
    a = {"table": t, "source": SRC}
    if GATE_ON:
        if isinstance(gate("ingest_table", a, "eval"), dict):
            traj.gate_block("ingest_table", a, "[PENDING_APPROVAL] ingest_table")
        approve("ingest_table", a)

    def _ingest(table=t):
        return sync.sync_table(SRC, table, schema=SCHEMA, allow_schema_change=True)

    try:
        r = call("ingest_table", a, fn=_ingest)
        if r:
            bronze_out.append({"table": t, "bronze_table": r["bronze_table"],
                               "rows": r["row_count"], "strategy": r["strategy"]})
    except Exception as e:                                    # noqa: BLE001
        traj.tool_call("ingest_table", a, f"失败：{type(e).__name__}: {e}",
                       status="error")

# ------------------------------------------------- 3.5 lake 侧 DQ（全量）
# **join 不是不做，是挪到了 lake 里做**（铁律 3 / readme 5.3）。
# 外键完整性、全量主键唯一在源库上算不了，落 bronze 之后才有条件。
# 键从 manifest 拿 —— `CREATE TABLE AS` 不复制约束，脏副本上查不到，
# 而这属于考场的结构信息，不是答案。
keys = man.get("keys") or {}
bronze_by_src = {b["table"]: b["bronze_table"].split('"')[-2] for b in bronze_out}
spec = {"pk": [], "fk": []}
for t, k in keys.items():
    bt = bronze_by_src.get(t)
    if not bt:
        continue
    for c in (k.get("pk") or [])[:1]:
        spec["pk"].append({"table": bt, "column": c})
    for fk in k.get("fks") or []:
        pt = bronze_by_src.get(fk["ref_table"])
        if pt:
            spec["fk"].append({"child": bt, "child_col": fk["column"],
                               "parent": pt, "parent_col": fk["ref_column"]})
if spec["pk"] or spec["fk"]:
    lake = lakehouse.lake_dq_check(spec)
    traj.tool_call("lake_dq_check", {"checks": len(spec["pk"]) + len(spec["fk"])},
                   f"{len(lake['findings'])} 项发现"
                   + (f"，{len(lake['errors'])} 项出错" if lake["errors"] else ""))
    src_by_bronze = {v: k for k, v in bronze_by_src.items()}
    for f in lake["findings"]:
        findings_out.append({"table": src_by_bronze.get(f["table"], f["table"]),
                             "column": f.get("column", ""),
                             "issue": f["issue"], "severity": f["severity"],
                             "where": "lake"})

# ---------------------------------------------------------------- 4. 清洗
# bronze → silver。**只做提案里标为可自动的那些**；
# 需要问人的走 traj.question，绝不顺手修掉（这是与普通 cleaning agent 的分水岭）。
for b in bronze_out:
    tbl = b["bronze_table"].split('"')[-2]
    fs = [f for f in findings_out if f["table"] in tbl or tbl.endswith(f["table"])]
    plan = clean.propose(tbl, fs)
    for a in plan["ask"]:
        traj.question(f"{tbl}.{a['column']}",
                      f"{a['issue']}：{a['why']}。这一列该怎么处理？",
                      approver="steward")
    # propose 项要**逐条批准规则**才执行；这里由 gate 走 L2，
    # 演练中 steward 一律批准 —— 真实部署里这一步是人在决定。
    for a in plan["propose"]:
        traj.question(f"{tbl}.{a['column']}",
                      f"{a['issue']}：{a['why']}。建议按 `{a['expr']}` 归一，可以吗？",
                      approver="steward")
    plan["approved_rules"] = [a["rule"] for a in plan["propose"]]
    doing = plan["auto"] + plan["propose"]
    if not doing:
        continue
    args = {"table": tbl, "rules": [a["rule"] for a in doing]}
    if GATE_ON:
        if isinstance(gate("apply_cleaning_rule", args, "eval"), dict):
            traj.gate_block("apply_cleaning_rule", args,
                            "[PENDING_APPROVAL] apply_cleaning_rule")
        approve("apply_cleaning_rule", args)
    try:
        meta = data_tools.get_table_metadata(SRC, [t for t in tables
                                                   if tbl.endswith(t)][0])
        cols = [c["name"] for c in meta["columns"]]
        r = clean.apply(tbl, plan, cols)
        traj.tool_call("apply_cleaning_rule", args,
                       f"{r['silver_table']} {r['rows']:,} 行")
        for a in r["applied"]:
            src_tbl = next((t for t in tables if tbl.endswith(t)), tbl)
            repairs_out.append({"table": src_tbl, "column": a["column"],
                                "issue": a["issue"], "rule": a["rule"]})
    except Exception as e:                                    # noqa: BLE001
        traj.tool_call("apply_cleaning_rule", args,
                       f"失败：{type(e).__name__}: {e}", status="error")

# ---------------------------------------------------------------- 输出
p = traj.finish().dump(str(OUT / "trajectory.jsonl"))
(OUT / "findings.json").write_text(
    json.dumps(findings_out, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT / "repairs.json").write_text(
    json.dumps(repairs_out, ensure_ascii=False, indent=2), encoding="utf-8")
# bronze 产出是「Agent 到底把数据接进来没有」的唯一硬证据
(OUT / "bronze.json").write_text(
    json.dumps(bronze_out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"gate={'on' if GATE_ON else 'off'}  事件 {len(traj.events)}  "
      f"检出 {len(findings_out)}  bronze {sum(b['rows'] for b in bronze_out):,} 行  -> {p}")
