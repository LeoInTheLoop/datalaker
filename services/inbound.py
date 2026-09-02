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

# DECISION_NEG 归并回 DECISION —— 对外只暴露枚举里的六类
_ALIAS = {"DECISION_NEG": "DECISION"}

# 英文规则不是中文的直译——办公邮件有自己的说法：
# 批准说 LGTM / go ahead，转介说 loop in / check with，
# 推脱说 not my wheelhouse。按中文模式翻译过去会漏掉大半。
_RULES = [
    ("NOISE", (r"\bout of office\b", r"\booo\b", r"automatic(ally)? repl",
               r"auto-?repl", r"\bon (leave|vacation|holiday|pto)\b",
               r"currently away", r"will be back on",
               r"自动回复", r"休假", r"年假中", r"不在办公室")),
    # 「找运营负责人对一下」也是转介——职位描述同样算，不要求紧跟人名。
    # 这类转介**不是终点而是中间态**：知道要转，但不知道转给谁。
    ("DELEGATE", (r"(找|问|联系|转给|对接)\s*[一-龥A-Za-z]{1,12}",
                  # 英文办公邮件的转介说法，与中文并不对应
                  r"\bloop(ing)? in\b", r"\bcc'?(ing|ed)?\b\s+\w+",
                  r"\b(reach out|check|speak|talk|connect|sync) (to|with)\s+\w+",
                  r"\bask\s+\w+", r"\bforward(ed|ing)?\s+to\b",
                  r"\b\w+\s+owns\s+(this|that|it)\b",
                  r"\bdefer(ring)? to\b", r"\bbest person (is|would be)\b",
                  # 英文常用「X should be able to / X would know」表示转介，
                  # 并不出现 ask/loop in 这类动词
                  r"\b\w+\s+(should|would|can|could)\s+(be able to|know|have|help)\b",
                  r"\b(is|are) the right (person|people|team|contact)\b",
                  r"\b(try|talk to|ping)\s+[A-Z]\w+",
                  r"\byou'?ll need to (ask|check|contact)\b")),
    ("NOT_MY_SCOPE", (r"不(归|属于)我(管|负责)", r"不是我的", r"我不管",
                      r"\bnot my (job|scope|area|team|call|remit|wheelhouse|table)\b",
                      r"\b(i (don'?t|do not) own)\b", r"\bwrong person\b",
                      r"\boutside (my|our) (scope|remit|area)\b",
                      r"\bnot (the )?owner\b")),
    # 中英文分开：\b 词边界在中文字符之间不成立，
    # 「我同意接入」里「同意」后面跟中文，加 \b 会漏掉
    ("DECISION", (r"(我?同意|批准了?|可以的?|没问题|通过了?)",
                  # LGTM/SGTM/+1/ship it 是最常见的批准说法，approve 反而少见
                  r"\b(approve[d]?|approval granted|agreed?)\b",
                  r"\b(lgtm|sgtm)\b", r"\+1\b",
                  r"\b(go ahead|green ?light|ship it|sounds good|fine by me)\b",
                  r"\b(ok(ay)? to (proceed|go)|please proceed)\b")),
    ("DECISION_NEG", (r"(拒绝|不行|不同意|不批|先别)",
                      r"\b(deny|denied|reject(ed)?)\b",
                      r"\b(hold off|not yet|let'?s not|no ?go|stand down)\b")),
    ("QUESTION", (r"[?？]\s*$",
                  r"^(为什么|什么|哪个|谁|怎么|能否|是否)",
                  r"^\s*(why|what|which|who|whom|how|when|where)\b",
                  r"^\s*(can|could|would|should|do|does|did|is|are|any) (you|we|this|that|i)\b",
                  r"\b(clarify|explain|not sure what)\b")),
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
                return {"intent": _ALIAS.get(intent, intent), "confidence": 0.8,
                        "by": f"rule:{p[:18]}"}

    return {"intent": "UNCLEAR", "confidence": 0.3, "by": "no_rule",
            "note": "规则未命中，应交模型复核；仍不确定则回信澄清，不推进"}


# 转介目标抽取：有邮箱才算「解析完成」
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TARGET = re.compile(r"(?:找|问|联系|转给|对接)\s*([一-龥A-Za-z][一-龥A-Za-z0-9_.]{0,15})")
# 英文：loop in Sarah / check with the ops lead / Mike owns this
_TARGET_EN = re.compile(
    r"(?:loop(?:ing)? in|cc'?(?:ing|ed)?|reach out to|check with|speak (?:to|with)|"
    r"talk to|ask|forward(?:ed|ing)? to|defer(?:ring)? to|connect (?:to|with))\s+"
    r"((?:the\s+)?[A-Za-z][\w.'-]*(?:\s+(?:lead|owner|team|manager|admin|head))?)",
    re.I)
_OWNS_EN = re.compile(r"\b((?:the\s+)?[A-Z][\w.'-]+)\s+owns\s+(?:this|that|it)\b")
# 职位描述而非人名 —— 与中文的「负责人」同类，都需要澄清
_VAGUE_EN = ("lead", "owner", "team", "manager", "head", "admin", "someone",
             "somebody", "person", "department", "dept")
# 代词不是转介目标：「Mike owns this, ask him」里该抓的是 Mike 不是 him
_PRONOUNS = {"him", "her", "them", "me", "us", "you", "it", "he", "she",
             "they", "this", "that", "these", "those", "someone", "somebody"}
# 泛指不是具体目标：「ask each team」问不出「Who is each team?」这种话
_QUANTIFIERS = ("each", "every", "all", "any", "both", "various", "several",
                "respective", "relevant", "individual")
# 「X should be able to / X would know」里的 X
_ABLE_EN = re.compile(
    r"\b((?:the\s+)?[A-Za-z][\w.'-]*(?:\s+(?:lead|owner|team|manager|admin|head))?)"
    r"\s+(?:should|would|can|could)\s+(?:be able to|know|have|help)", re.I)


def _is_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")
# 抽取会连着后面的动词一起吞进来（「运营负责人对一下」），在这里截断
_TAIL = re.compile(
    r"(对一下|对下|看一下|看下|问一下|确认|核实|看看|处理|聊聊|沟通|了解|问问|要一下|拿一下|同步|跟进|核对).*$")
_LEAD = re.compile(r"^(一下|下|个|的)")


def parse_delegation(body: str) -> dict:
    """解析转介：转给谁、有没有联系方式。

    **DELEGATE 是中间态不是终点。**
    「找王姐 wang@acme.com」可以直接联系；
    「你找运营负责人对一下」只知道要转、不知道转给谁——必须追问。
    把这两种混为一谈，链路就在这里断了。
    """
    text = body or ""
    emails = _EMAIL.findall(text)
    m = _TARGET.search(text)
    if m:
        raw = _LEAD.sub("", _TAIL.sub("", m.group(1)))
    else:
        # 「X owns this」比「ask him」更明确，先试它
        raw = ""
        mo = _OWNS_EN.search(text) or _ABLE_EN.search(text)
        if mo:
            raw = mo.group(1).strip()
        else:
            for cand in _TARGET_EN.finditer(text):
                c = cand.group(1).strip().rstrip(".,;:!?")
                head = c.lower().split()[0] if c else ""
                if c.lower() not in _PRONOUNS and head not in _QUANTIFIERS:
                    raw = c
                    break
        raw = raw.rstrip(".,;:!?")
    raw = raw.rstrip("的地得，,。 ")
    raw = re.sub(r"(吧|呢|啊|哦|嘛|了|一下)+$", "", raw)
    # 「负责人」「那边」这类是职位/指代，不是人名
    low = raw.lower()
    if low and low.split()[0] in _QUANTIFIERS:
        raw, low = "", ""
    # 是职位/指代（不知道是谁），还是人名（知道是谁但没邮箱）——问法不同
    is_role = bool(raw) and (
        any(w in raw for w in ("负责人", "那边", "他们", "同事", "部门", "团队",
                               "这张表", "该表"))
        or low.startswith("the ")
        or any(w == low or low.endswith(" " + w) for w in _VAGUE_EN))
    return {"raw": raw, "email": emails[0] if emails else None,
            "resolved": bool(emails), "is_role": is_role,
            "cjk": _is_cjk(text),
            "vague": (is_role or not raw) and not emails}


def _q_lang(body: str, zh: str, en: str) -> str:
    """按对方的语言回话。用中文回英文邮件是很扎眼的失礼。"""
    return zh if _is_cjk(body) else en


def next_action(intent_result: dict, body: str = "") -> dict:
    """意图之后：**接下来做什么**。

    分类只回答「这是什么」，不回答「怎么办」——
    而 Agent 需要的是后者。低置信度的默认动作是问，不是猜（5.2）。
    """
    it = intent_result.get("intent")

    if it == "DELEGATE":
        d = parse_delegation(body)
        if d["resolved"]:
            return {"action": "contact_new_person", "target": d["email"],
                    "note": f"转介到 {d['raw'] or d['email']}，可直接联系"}
        cjk = d.get("cjk", True)
        who = d["raw"] or ("你提到的负责人" if cjk else "the person you mentioned")
        if "这张表" in who or "该表" in who:
            who = "这张表的负责人"

        # 知道是谁只是缺邮箱 vs 连是谁都不知道 —— 问法不同，
        # 且**按对方的语言问**，不要用中文回英文邮件
        if not d["raw"]:
            q = ("方便告诉我具体该找谁吗？给个邮箱我直接联系。" if cjk else
                 "Could you point me to the specific person and their email? "
                 "I'll follow up directly.")
        elif d.get("is_role"):
            q = (f"{who}是哪位？方便给个邮箱吗？我直接联系确认。" if cjk else
                 f"Who is {who}? Could you share their email so I can follow up directly?")
        else:
            q = (f"能给一下{who}的邮箱吗？我直接联系确认。" if cjk else
                 f"Could you share {who}'s email? I'll follow up with them directly.")
        return {"action": "clarify_who", "target": who, "question": q,
                "note": "转介目标未解析——这是新的一次提问，计入 WIP"}

    if it == "NOT_MY_SCOPE":
        return {"action": "find_other_owner",
                "question": _q_lang(body, "了解。那这块应该找谁？", "Understood. Who should I talk to about this instead?"),
                "note": "不再催这个人（10.2）"}

    if it == "QUESTION":
        return {"action": "answer_then_reask",
                "note": "先回答对方的问题，再重新发起审批"}

    if it == "DECISION":
        return {"action": "await_click",
                "note": "正文表态不作数，等待签名链接被点击（第 9 节）"}

    if it == "NOISE":
        return {"action": "ignore_keep_timer",
                "note": "记录但不重置超时计时器（10.2）"}

    return {"action": "clarify_intent",
            "question": _q_lang(body, "抱歉没太看懂，能再说明一下吗？", "Sorry, I didn't quite follow. Could you clarify?"),
            "note": "低置信度默认动作是问，不是猜"}


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
    # 角色名是动态的（owner:FIN / steward:CRM），不能硬编码枚举——
    # 要认的是「此刻谁持有任何角色」
    try:
        if addr in store.current_holders():
            return True
    except Exception:
        pass
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


# ---------------------------------------------------------------- 邮件附件
def fetch_attachments(message_id: str, out_dir: str) -> list:
    """取附件（readme 16.4）。

    这是唯一一条「数据从外部主动进来」的通道，安全要求最高：
    发件人白名单在 `process()` 已过；这里再过类型与大小，
    并把来源 Message-ID 记进结果供溯源。

    **不执行宏**：解析交给 ingest_file（openpyxl 纯解析）。
    """
    import base64
    import pathlib
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    import ingest_file

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    creds = Credentials.from_authorized_user_file(
        os.path.join(root, "secrets", "gmail_token.json"),
        ["https://www.googleapis.com/auth/gmail.send",
         "https://www.googleapis.com/auth/gmail.readonly"])
    svc = build("gmail", "v1", credentials=creds)
    full = svc.users().messages().get(userId="me", id=message_id).execute()

    saved = []
    pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
    for part in full.get("payload", {}).get("parts", []):
        fn = part.get("filename")
        if not fn:
            continue
        ext = pathlib.Path(fn).suffix.lower()
        if ext not in ingest_file.ALLOWED_EXT:
            saved.append({"filename": fn, "status": "rejected:type"})
            continue
        body = part.get("body", {})
        size = int(body.get("size") or 0)
        if size > ingest_file.MAX_BYTES:
            saved.append({"filename": fn, "status": "rejected:size", "bytes": size})
            continue
        att_id = body.get("attachmentId")
        if not att_id:
            continue
        data = svc.users().messages().attachments().get(
            userId="me", messageId=message_id, id=att_id).execute()
        raw = base64.urlsafe_b64decode(data["data"])
        p = pathlib.Path(out_dir) / fn
        p.write_bytes(raw)
        saved.append({"filename": fn, "path": str(p), "bytes": len(raw),
                      "status": "saved", "source_message_id": message_id})
    return saved


def ingest_attachment(att: dict, store) -> dict:
    """把附件解析成 payload 并记录溯源。

    落库时记来源邮件与发件人——Remediation Ledger 里能回答
    「这份数据哪来的、谁发的」（readme 16.4）。
    """
    import ingest_file
    if att.get("status") != "saved":
        return {"ok": False, "why": att.get("status")}
    payload = ingest_file.read_any(att["path"])
    fp = ingest_file.canonical_fingerprint(payload)
    store.remember(f"attachment.{att['filename']}", "provenance",
                   f"message_id={att.get('source_message_id')};fingerprint={fp}",
                   "system:inbound")
    return {"ok": True, "rows": len(payload["rows"]),
            "columns": payload["columns"], "fingerprint": fp,
            "source_message_id": att.get("source_message_id")}
