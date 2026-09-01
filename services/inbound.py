"""入站邮件：归属判定与意图分类（readme 10.1–10.3）。

> **归属先用协议，再用判断。** 邮件协议自带线程机制，
> 不要用相似度替代 header——那是把确定性问题变成概率问题，
> 而匹配错了系统还不自知。

四层归属，LLM 排最后且必须标注不确定。
"""
import email.utils
import os
import re

# 自动回复的标准标识（RFC 3834 等）——确定性识别，不用 LLM
AUTO_HEADERS = {
    "auto-submitted": lambda v: v.lower() != "no",
    "precedence": lambda v: v.lower() in ("bulk", "junk", "list", "auto_reply"),
    "x-autoreply": lambda v: True,
    "x-autorespond": lambda v: True,
}

PLUS_RE = re.compile(r"\+ap-([0-9a-f-]{6,})@", re.I)
SUBJ_RE = re.compile(r"\[#([0-9a-f-]{6,})\]", re.I)


def is_auto_reply(headers: dict) -> bool:
    """休假自动回复等。**不重置超时计时器**（readme 10.2）。"""
    low = {k.lower(): (v or "") for k, v in headers.items()}
    for h, pred in AUTO_HEADERS.items():
        if h in low and pred(low[h]):
            return True
    return False


def resolve_item(headers: dict, lookup_by_message_id=None) -> dict:
    """四层归属（readme 10.1）。返回 {item_id, layer, certain}。

    layer 越小越可靠；只有全部失败才交给 LLM，且标记 certain=False。
    """
    low = {k.lower(): (v or "") for k, v in headers.items()}

    # 1. In-Reply-To / References —— RFC 5322，确定性
    refs = []
    for h in ("in-reply-to", "references"):
        refs += re.findall(r"<([^>]+)>", low.get(h, ""))
    if lookup_by_message_id:
        for mid in reversed(refs):          # 最近的引用优先
            item = lookup_by_message_id(mid)
            if item:
                return {"item_id": item, "layer": 1, "certain": True,
                        "via": f"In-Reply-To <{mid[:24]}>"}

    # 2. Gmail threadId（同 provider 内确定）
    if low.get("x-gm-thrid"):
        return {"item_id": None, "layer": 2, "certain": True,
                "thread_id": low["x-gm-thrid"], "via": "gmail threadId"}

    # 3. plus-addressing —— 人手动转发也保留
    for field in ("to", "delivered-to", "x-original-to", "cc"):
        m = PLUS_RE.search(low.get(field, ""))
        if m:
            return {"item_id": m.group(1), "layer": 3, "certain": True,
                    "via": f"plus-address +ap-{m.group(1)[:8]}"}

    # 4. Subject 里的 token —— 兜底，人肉可读
    m = SUBJ_RE.search(low.get("subject", ""))
    if m:
        return {"item_id": m.group(1), "layer": 4, "certain": True,
                "via": f"subject [#{m.group(1)[:8]}]"}

    # 5. 交给 LLM —— 必须标记不确定，且候选集是 open 任务而非全部历史
    return {"item_id": None, "layer": 5, "certain": False,
            "via": "无协议线索，需按 open 任务候选集判断"}


# ---------------------------------------------------------------- 意图分类
INTENTS = ("DECISION", "QUESTION", "DELEGATE", "NOT_MY_SCOPE", "NOISE", "UNCLEAR")

_RULES = [
    ("NOISE", (r"out of office", r"automatic reply", r"自动回复", r"休假", r"年假中")),
    ("DELEGATE", (r"(找|问|联系|转给)\s*[一-龥A-Za-z]{1,10}\s*(吧|看看|处理)?",
                  r"ask\s+\w+", r"forward(ed)?\s+to")),
    ("NOT_MY_SCOPE", (r"不(归|属于)我(管|负责)", r"不是我的", r"not my (job|scope|area)")),
    ("DECISION", (r"^\s*(同意|批准|可以|approve[d]?|ok|yes)\b", r"^\s*(拒绝|不行|deny|no)\b")),
    ("QUESTION", (r"[?？]\s*$", r"^(为什么|什么|哪个|谁|怎么|why|what|which|who|how)")),
]


def classify_intent(body: str, headers: dict | None = None) -> dict:
    """意图分类：**输出是有限枚举 + 置信度，不是自由文本**（readme 10.2）。

    低置信度的默认动作是「问」，不是「猜」——与 5.2 停止点同源。

    ponytail: 规则先行，命中即返回（零 token）；未命中才交模型。
    """
    if headers and is_auto_reply(headers):
        return {"intent": "NOISE", "confidence": 1.0, "by": "header",
                "note": "自动回复：记录但**不重置超时计时器**"}

    text = (body or "").strip()
    if not text:
        return {"intent": "UNCLEAR", "confidence": 0.0, "by": "empty"}

    head = "\n".join(text.splitlines()[:6]).lower()
    for intent, pats in _RULES:
        for p in pats:
            if re.search(p, head, re.I | re.M):
                return {"intent": intent, "confidence": 0.8, "by": f"rule:{p[:18]}"}

    return {"intent": "UNCLEAR", "confidence": 0.3, "by": "no_rule",
            "note": "规则未命中，应交模型复核；仍不确定则回信澄清，不推进"}


def decision_still_requires_click(intent_result: dict) -> bool:
    """即使分类为 DECISION，邮件正文里的「同意」也不作数。

    决定只能来自签名链接的点击（readme 第 9 节）。
    这条让分类错误的代价从「安全事故」降到「体验损失」。
    """
    return True


# ---------------------------------------------------------------- 收信
def _seen_key(mid: str) -> str:
    return f"inbound:{mid}"


def already_processed(store, message_id: str) -> bool:
    """Message-ID 幂等（readme 10.8）：轮询可能抓到同一封两次。"""
    return store.known(_seen_key(message_id), "seen") is not None


def mark_processed(store, message_id: str, outcome: str):
    store.remember(_seen_key(message_id), "seen", outcome, "system:inbound")


def sender_allowed(from_addr: str, store) -> bool:
    """发件人白名单（readme 10.8）。

    只处理角色表中的有效持有人——否则任何人给这个邮箱发信
    就能触发 Agent 动作。这是安全边界，不是洁癖。
    """
    addr = email.utils.parseaddr(from_addr or "")[1].lower()
    if not addr:
        return False
    for role in ("sponsor", "owner", "steward"):
        who = store.resolve_role(role)
        if who and who.lower() == addr:
            return True
    env_allow = {a.strip().lower() for a in
                 os.environ.get("INBOUND_ALLOWLIST", "").split(",") if a.strip()}
    return addr in env_allow


def fetch_unread(limit=20) -> list:
    """通过 Gmail API 拉未读。IMAP 实现同理，接口不变。"""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    creds = Credentials.from_authorized_user_file(
        os.path.join(root, "secrets", "gmail_token.json"),
        ["https://www.googleapis.com/auth/gmail.send",
         "https://www.googleapis.com/auth/gmail.readonly"])
    svc = build("gmail", "v1", credentials=creds)
    res = svc.users().messages().list(userId="me", q="is:unread",
                                      maxResults=limit).execute()
    out = []
    for m in res.get("messages", []):
        full = svc.users().messages().get(userId="me", id=m["id"],
                                          format="metadata").execute()
        hdrs = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
        hdrs["X-Gm-Thrid"] = full.get("threadId", "")
        out.append({"id": m["id"], "headers": hdrs,
                    "snippet": full.get("snippet", "")})
    return out


def process(msg: dict, store, lookup_by_message_id=None) -> dict:
    """处理一封入站邮件：白名单 → 去重 → 归属 → 意图。

    **归属由 header 确定，语义由分类给出**——两件事分开，各用各擅长的。
    """
    h = msg.get("headers", {})
    mid = h.get("Message-ID") or h.get("Message-Id") or msg.get("id", "")
    mid = mid.strip("<>")

    if already_processed(store, mid):
        return {"action": "skip", "why": "duplicate", "message_id": mid}
    if not sender_allowed(h.get("From", ""), store):
        mark_processed(store, mid, "rejected:sender")
        return {"action": "reject", "why": "sender_not_allowed",
                "from": h.get("From", "")}

    attrib = resolve_item(h, lookup_by_message_id)
    intent = classify_intent(msg.get("snippet", ""), h)

    outcome = f"{intent['intent']}@layer{attrib['layer']}"
    mark_processed(store, mid, outcome)
    return {"action": "processed", "message_id": mid,
            "attribution": attrib, "intent": intent,
            "resets_timer": intent["intent"] != "NOISE"}
