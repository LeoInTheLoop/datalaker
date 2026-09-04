"""入站邮件断言（readme 10.1–10.3、10.8）。

核心：**归属由 header 确定，语义由分类给出**——两件事分开。
"""
import os, sys, uuid
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "/tmp/dl_inb.db"
for suf in ("", "-wal", "-shm"):
    if os.path.exists(DB + suf): os.remove(DB + suf)
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["INBOUND_ALLOWLIST"] = "owner@corp.com"
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "services"))
import inbound as I
from plugins.datasteward_gate.approvals import open_store

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== 入站：归属 + 意图 + 防线 ===\n")
st = open_store(readonly=False)
look = lambda mid: "item-1" if mid == "known@x" else None

# 归属四层，layer 越小越可靠
for h, want, name in [
    ({"In-Reply-To": "<known@x>"}, 1, "In-Reply-To"),
    ({"X-Gm-Thrid": "t123"}, 2, "Gmail threadId"),
    ({"To": "claw+ap-44cf3410@gmail.com"}, 3, "plus-address"),
    ({"Subject": "Re: [#44cf3410] 请批准"}, 4, "subject token"),
    ({"From": "x@y.z"}, 5, "无协议线索"),
]:
    r = I.resolve_item(h, look)
    chk(f"归属 L{want}：{name}", r["layer"] == want, f"layer={r['layer']}")
chk("只有第 5 层标记为不确定",
    I.resolve_item({"From": "a@b.c"}, look)["certain"] is False)
chk("转发后 plus-address 仍有效（人手动转发保留 To）",
    I.resolve_item({"To": "claw+ap-abc123@x.com"}, look)["item_id"] == "abc123")

def AR(addr, serv="mx.corp.com"):
    """收件服务器盖的验真结果。

    真实邮件必然带这一条（服务器跑完 SPF/DKIM/DMARC 后 prepend）。
    测试也必须带 —— 不带的话，测的是一个生产里根本不存在的配置。
    """
    d = addr.rpartition("@")[2]
    return [f"{serv}; spf=pass smtp.mailfrom={addr};"
            f" dkim=pass header.d={d}; dmarc=pass header.from={d}"]


# 意图：有限枚举
for body, want in [("同意，请执行", "DECISION"), ("这个字段什么意思？", "QUESTION"),
                   ("去问张三吧", "DELEGATE"), ("这不归我管", "NOT_MY_SCOPE"),
                   ("嗯", "UNCLEAR")]:
    chk(f"意图 {want}", I.classify_intent(body)["intent"] == want,
        I.classify_intent(body)["intent"])
chk("意图输出在枚举内", all(I.classify_intent(b)["intent"] in I.INTENTS
                            for b in ("x", "同意", "?", "")))

# 自动回复：确定性识别，且不重置计时器
auto = I.classify_intent("我在休假", {"Auto-Submitted": "auto-replied"})
chk("自动回复由 header 确定性识别", auto["intent"] == "NOISE" and auto["by"] == "header")
msg = {"id": "m1", "headers": {"Message-ID": "<a1@x>", "From": "owner@corp.com",
                               "Precedence": "bulk"}, "snippet": "自动回复",
       "auth_results": AR("owner@corp.com")}
r = I.process(msg, st, look)
chk("NOISE 不重置超时计时器", r["resets_timer"] is False)

# 防线
chk("陌生发件人被拒",
    I.process({"id": "m2", "headers": {"Message-ID": "<a2@x>", "From": "stranger@evil.com"},
               "snippet": "hi"}, st, look)["action"] == "reject")
dup = {"id": "m3", "headers": {"Message-ID": "<a3@x>", "From": "owner@corp.com"},
       "snippet": "同意", "auth_results": AR("owner@corp.com")}
chk("首次处理", I.process(dup, st, look)["action"] == "processed")
chk("Message-ID 幂等去重", I.process(dup, st, look)["action"] == "skip")

# ---- 冒充：`From:` 在白名单里，但这封信不是那个域发出来的 ----
# 这是白名单唯一真正要防的攻击。只比对 From 的话它 100% 打得穿：
# From 是发件人自己写的，投递过程不认证它。
spoof = {"id": "sp1", "headers": {"Message-ID": "<sp1@x>",
                                  "From": "owner@corp.com"},
         "snippet": "同意，都批了"}
r = I.process(spoof, st, look)
chk("冒充白名单里的人 → 拒收（没有验真结果，fail closed）",
    r["action"] == "reject" and r["why"] == "sender_not_authenticated",
    r.get("detail", ""))

# 攻击者自己在正文里塞一条 Authentication-Results。服务器写的那条排第一，
# 塞进来的排在后面 —— 取第一条就不会被骗。
r = I.process({"id": "sp2", "headers": {"Message-ID": "<sp2@x>",
                                        "From": "owner@corp.com"},
               "snippet": "同意",
               "auth_results": ["mx.corp.com; spf=fail smtp.mailfrom=evil.com;"
                                " dkim=none; dmarc=fail header.from=corp.com",
                                "mx.corp.com; dmarc=pass header.from=corp.com"]},
              st, look)
chk("正文里伪造的验真结果不算数（只认服务器 prepend 的第一条）",
    r["action"] == "reject", r.get("detail", ""))

# 别的域通过了验真，也不能代表 corp.com
r = I.process({"id": "sp3", "headers": {"Message-ID": "<sp3@x>",
                                        "From": "owner@corp.com"},
               "snippet": "同意",
               "auth_results": ["mx.corp.com; spf=pass smtp.mailfrom=a@evil.com;"
                                " dkim=pass header.d=evil.com"]},
              st, look)
chk("验真通过但域不对齐 → 仍然拒收", r["action"] == "reject",
    r.get("detail", ""))

ok_auth, why = I.sender_authenticated(
    "owner@corp.com",
    ["mx.corp.com; dkim=pass header.d=mail.corp.com"])
chk("子域算对齐（mail.corp.com 代表 corp.com）", ok_auth, why)

chk("没有 From 域时不通过",
    I.sender_authenticated("", AR("owner@corp.com"))[0] is False)

chk("正文里的「同意」仍需点击才作数",
    I.decision_still_requires_click({"intent": "DECISION"}) is True)

# 附件路径：准入 + 溯源（不联网，用本地文件模拟已保存的附件）
import shutil
tmpd = "/tmp/dl_att"; os.makedirs(tmpd, exist_ok=True)
src_csv = os.path.join(ROOT, "data/exports/shippers.csv")
if os.path.exists(src_csv):
    dst = os.path.join(tmpd, "shippers.csv"); shutil.copy(src_csv, dst)
    r = I.ingest_attachment({"filename": "shippers.csv", "path": dst,
                             "status": "saved", "source_message_id": "<mail-9@x>"}, st)
    chk("附件可解析", r["ok"] and r["rows"] > 0, f"{r.get('rows')} 行")
    chk("附件记录溯源（哪封邮件来的）",
        "mail-9@x" in (st.known("attachment.shippers.csv", "provenance") or {}).get("value", ""))
    chk("附件指纹与文件路径一致", len(r["fingerprint"]) == 64)
chk("被拒的附件不进入解析",
    I.ingest_attachment({"filename": "x.exe", "status": "rejected:type"}, st)["ok"] is False)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
