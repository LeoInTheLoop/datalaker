"""英文入站断言。

**不是中文规则的直译**——办公邮件有自己的说法：
批准说 LGTM / go ahead（approve 反而少见），转介说 loop in / check with，
推脱说 not my wheelhouse。按中文模式翻译过去会漏掉大半。
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services"))
import inbound as I

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))

print("\n=== 英文入站：按办公邮件习惯，不是中文直译 ===\n")

INTENT = [
    ("LGTM, go ahead.", "DECISION"),
    ("+1 from me, please proceed.", "DECISION"),
    ("Sounds good, ship it.", "DECISION"),
    ("Approved.", "DECISION"),
    ("Let's hold off on this for now.", "DECISION"),
    ("Not yet — stand down until Q3.", "DECISION"),
    ("Looping in Sarah who owns this.", "DELEGATE"),
    ("Please check with the ops lead.", "DELEGATE"),
    ("Mike owns this, ask him.", "DELEGATE"),
    ("Reach out to jane.doe@acme.com.", "DELEGATE"),
    ("That's not my wheelhouse.", "NOT_MY_SCOPE"),
    ("I do not own that table — wrong person.", "NOT_MY_SCOPE"),
    ("What does this field actually mean?", "QUESTION"),
    ("Can you clarify the retention policy?", "QUESTION"),
    ("I am out of office until Monday.", "NOISE"),
    ("OOO, back next week.", "NOISE"),
]
for body, want in INTENT:
    got = I.classify_intent(body)["intent"]
    chk(f"意图 {want:<13} {body[:34]}", got == want, got if got != want else "")

# 转介抽取：代词不是目标
d = I.parse_delegation("Mike owns this, ask him.")
chk("代词不被当成转介目标", d["raw"] == "Mike", d["raw"])
d = I.parse_delegation("Looping in Sarah (sarah@acme.com) who owns this.")
chk("括号里的邮箱被识别", d["resolved"] and d["email"] == "sarah@acme.com", str(d["email"]))
d = I.parse_delegation("Please check with the ops lead.")
chk("职位描述判为 role（需澄清是谁）", d["is_role"] and not d["resolved"])
d = I.parse_delegation("cc Tom on this thread.")
chk("有人名无邮箱：不是 role，但仍未解析", not d["is_role"] and not d["resolved"], d["raw"])

# 语言跟随
n = I.next_action(I.classify_intent("Please check with the ops lead."),
                  "Please check with the ops lead.")
chk("英文来信用英文追问", n["question"].startswith("Who is"), n["question"][:40])
chk("英文追问问的是「是谁」", "Who is" in n["question"])
n2 = I.next_action(I.classify_intent("cc Tom on this thread."), "cc Tom on this thread.")
chk("知道人名时问的是邮箱", "email" in n2["question"] and "Who is" not in n2["question"],
    n2["question"][:44])
n3 = I.next_action(I.classify_intent("这个你问李哥吧"), "这个你问李哥吧")
chk("中文来信仍用中文追问", "邮箱" in n3["question"] and "Who" not in n3["question"])
n4 = I.next_action(I.classify_intent("That's not my wheelhouse."),
                   "That's not my wheelhouse.")
chk("推脱时英文追问下一个人", n4["action"] == "find_other_owner"
    and n4["question"].startswith("Understood"), n4["question"][:36])

# 自动回复：英文 header 与正文两条路都要认
chk("英文自动回复 header 识别",
    I.classify_intent("I'll be back", {"Auto-Submitted": "auto-replied"})["intent"] == "NOISE")
chk("OOO 正文识别", I.classify_intent("OOO until Friday")["intent"] == "NOISE")

# 英文特有的转介说法：不出现 ask/loop in 也是转介
for body, want_raw in [
    ("The ops lead and IT manager should be able to map our systems.", "IT manager"),
    ("Sarah would know, try her.", "Sarah"),
    ("Tom is the right person for this.", None),
]:
    cls = I.classify_intent(body)
    chk(f"「should/would know」型转介: {body[:30]}", cls["intent"] == "DELEGATE", cls["intent"])
    if want_raw:
        chk(f"  抓到目标 {want_raw}", I.parse_delegation(body)["raw"] == want_raw,
            I.parse_delegation(body)["raw"])

# 泛指不是具体目标
d = I.parse_delegation("you'll need to ask each team directly")
chk("泛指（each team）不当作目标", d["raw"] == "", repr(d["raw"]))
n = I.next_action(I.classify_intent("you'll need to ask each team directly"),
                  "you'll need to ask each team directly")
chk("无明确目标时问「具体是谁」", "specific person" in n["question"], n["question"][:44])
chk("不生成语法不通的问题", "Who is each" not in n["question"])

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
