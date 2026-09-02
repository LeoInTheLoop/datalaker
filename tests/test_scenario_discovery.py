"""端到端场景：从零发现到接入（tests/scenarios/discovery.json）。

不是单元测试——这是把 11 幕业务剧本跑一遍，其中 **3 处故意的假信息**，
验证整条链路在真实交互下是否仍然安全。

邮件不真发：notifier 换成记录器。数据库是真的（acme，6 张表带真实缺陷）。
"""
import json, os, sys, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "/tmp/dl_scenario.db"
for suf in ("", "-wal", "-shm"):
    if os.path.exists(DB + suf): os.remove(DB + suf)
os.environ.update(DATASTEWARD_DB=DB, DATASTEWARD_TOKEN_SECRET="scen",
                  PER_PERSON_WIP_LIMIT="5", GLOBAL_WIP_LIMIT="20",
                  INBOUND_ALLOWLIST="")
os.environ.pop("DATASTEWARD_DSN", None)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services"))
sys.path.insert(0, os.path.join(ROOT, "plugins"))

import data_tools, inbound, notify
from datasteward_gate import gate, store
from datasteward_gate.approvals import Store, action_hash

SCEN = json.load(open(os.path.join(ROOT, "tests/scenarios/discovery.json")))
SRC = SCEN["source"]
ok, bad, log = [], [], []


def chk(act, n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  [{act:>2}] {n}" + (f"  ·  {d}" if d else ""))


# 邮件不真发：记录下来即可
class Recorder(notify.Notifier):
    name = "recorder"
    sent = []
    def send_approval(self, to, aid, tool, target, reason, approver):
        self.sent.append(("approval", to, tool, target)); return {"channel": "recorder"}
    def send_receipt(self, to, aid, decision, tool, target, approver):
        self.sent.append(("receipt", to, decision, target)); return {"channel": "recorder"}
    def send_notice(self, to, subject, body):
        self.sent.append(("notice", to, subject)); return {"channel": "recorder"}


REC = Recorder()
notify.get = lambda channel=None: REC

st = store()
admin = Store(DB, readonly=False)
print(f"\n=== {SCEN['name']} ===")
print(f"    {SCEN['premise']}\n")

# ---- 幕 1：发现 ----
tables = data_tools.list_source_tables(SRC)["tables"]
chk(1, "扫描到源系统的表", len(tables) >= 6, f"{len(tables)} 张")
chk(1, "此时没有任何 owner 信息", st.resolve_role("owner:fin") is None,
    "从零开始，只知道 Sponsor")

# ---- 幕 2-3：问 Sponsor，拿到转介 ----
admin.assign_role("sponsor", SCEN["roles"]["sponsor"], "bootstrap", "初始指派")
qid, _ = st.ask("scen", f"{SRC}", SCEN["acts"][1]["question"],
                [{"key": "who", "label": "请告知联系人"}], "sponsor")
chk(2, "已向 Sponsor 发起提问", bool(qid))

reply = SCEN["acts"][2]["inbound"]
msg = {"id": "m3", "headers": {**reply["headers"], "Message-ID": "<r3@x>",
                               "From": reply["from"]}, "snippet": reply["body"]}
r = inbound.process(msg, st, lookup_by_message_id=lambda m: qid if m == "ask-1@claw" else None)
chk(3, "Sponsor 回复被接受（在白名单内）", r["action"] == "processed", r.get("why", ""))
chk(3, "意图识别为转介", r["intent"]["intent"] == "DELEGATE", r["intent"]["intent"])

# 按转介内容登记角色（真实实现里由 LLM 抽取人名+邮箱，这里直接用剧本给定值）
admin.assign_role("owner:fin", SCEN["roles"]["owner:fin"], SCEN["roles"]["sponsor"], "boss 转介")
admin.assign_role("owner:crm", SCEN["roles"]["owner:crm"], SCEN["roles"]["sponsor"], "boss 转介")
chk(3, "角色已登记且可解析", st.resolve_role("owner:fin") == SCEN["roles"]["owner:fin"])

# ---- 幕 4：⚠️ 陌生人冒充 ----
atk = SCEN["acts"][3]["inbound"]
r = inbound.process({"id": "m4", "headers": {**atk["headers"], "Message-ID": "<r4@x>",
                                             "From": atk["from"]},
                     "snippet": atk["body"]}, st)
chk(4, "⚠️ 陌生人冒充 owner 被拒", r["action"] == "reject" and r["why"] == "sender_not_allowed",
    atk["from"])

# ---- 幕 5-6：问王姐，她认领两张、不认识第三张 ----
q2, _ = st.ask("scen", f"{SRC}.fin", SCEN["acts"][4]["question"],
               [{"key": "list", "label": "请列出属于财务的表"}], "owner:fin")
chk(5, "向 owner:fin 发起提问", bool(q2))
chk(5, "在办未超 WIP 上限", st.open_count("owner:fin") <= 5, f"{st.open_count('owner:fin')} 件")

w = SCEN["acts"][5]["inbound"]
r = inbound.process({"id": "m6", "headers": {**w["headers"], "Message-ID": "<r6@x>",
                                             "From": w["from"]}, "snippet": w["body"]}, st)
chk(6, "王姐（角色持有人）的回复被接受", r["action"] == "processed")
st.remember(f"{SRC}.legacy_export", "ownership", "无人认领：财务表示未见过",
            SCEN["roles"]["owner:fin"])
chk(6, "无人认领的表被记录", st.known(f"{SRC}.legacy_export", "ownership") is not None)

# ---- 幕 7：⚠️ 正文说「我同意」 ----
fake = SCEN["acts"][6]["inbound"]
r = inbound.process({"id": "m7", "headers": {**fake["headers"], "Message-ID": "<r7@x>",
                                             "From": fake["from"]}, "snippet": fake["body"]}, st)
chk(7, "正文被分类为 DECISION", r["intent"]["intent"] == "DECISION")
chk(7, "⚠️ 但正文不作数，仍需点击", inbound.decision_still_requires_click(r["intent"]))
args_fin = {"table": "fin_monthly", "source": SRC}
g = gate("ingest_table", args_fin, "scen")
chk(7, "⚠️ 门禁仍然拦住（只认票据不认文本）",
    isinstance(g, dict) and "PENDING_APPROVAL" in g.get("message", ""))

# ---- 幕 8：真正点击批准 ----
aid = st.pending(action_hash("ingest_table", args_fin), "scen")
chk(8, "审批请求已落库", aid is not None)
admin.decide(aid, "approve", st.resolve_role("owner:fin"))
chk(8, "批准后放行", gate("ingest_table", args_fin, "scen") is None)
chk(8, "审批邮件确实发给了当前角色持有人",
    any(s[0] == "approval" and s[1] == SCEN["roles"]["owner:fin"] for s in REC.sent),
    str([s[1] for s in REC.sent if s[0] == "approval"]))

# ---- 幕 9：⚠️ 拿财务票据接 CRM 的 PII 表 ----
g = gate("ingest_table", {"table": "crm_customer", "source": SRC}, "scen")
chk(9, "⚠️ 越权接入被拦（参数指纹绑定）",
    isinstance(g, dict) and g.get("action") == "block", (g or {}).get("message", "")[:40])

# ---- 幕 10：中途换人 ----
pend_before = st.open_count("owner:fin")
admin.assign_role("owner:fin", SCEN["acts"][9]["to"], SCEN["roles"]["sponsor"], "王姐转岗")
chk(10, "角色指向新人", st.resolve_role("owner:fin") == SCEN["acts"][9]["to"])
chk(10, "未决事项数量不变（跟角色不跟人）", st.open_count("owner:fin") == pend_before,
    f"{pend_before} → {st.open_count('owner:fin')}")
chk(10, "历史决定未被改写",
    admin.db.execute("SELECT count(*) FROM decisions").fetchone()[0] >= 1)

# ---- 幕 11：无人认领的表 ----
g = gate("ingest_table", {"table": "legacy_export", "source": SRC}, "scen")
chk(11, "无人认领的表仍需审批，不自行接入",
    isinstance(g, dict) and g.get("action") == "block")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
print(f"发出的通知: {len(REC.sent)} 条（未真实投递）")
sys.exit(1 if bad else 0)
