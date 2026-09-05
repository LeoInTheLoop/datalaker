"""邮件模拟环境烟测（P2 驱动反转的地基）。

GreenMail 没起时整组 SKIP（探活走 `mailsim.probe()`，和干活同一条路）——
但打印出来，不做静默消失的那种 SKIP。

    cd infra && docker compose --profile mail up -d greenmail
    .venv/bin/python tests/test_mailsim.py
"""
import sys
import pathlib
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import mailsim                                                 # noqa: E402

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


if not mailsim.probe():
    print("\n  SKIP  GreenMail 未启动"
          "（cd infra && docker compose --profile mail up -d greenmail）\n")
    sys.exit(0)

print("\n=== 邮件模拟往返 ===\n")

tag = str(int(time.time()))
mid = mailsim.send(f"wang@{mailsim.SIM_DOMAIN}", f"claw@{mailsim.SIM_DOMAIN}",
                   f"口径确认 {tag}", "收款口径为准。",
                   auth_domain=mailsim.SIM_DOMAIN)
msgs = mailsim.fetch(f"claw@{mailsim.SIM_DOMAIN}")
hit = [m for m in msgs if tag in mailsim.subject_of(m)]
chk("王姐发信 → claw 收件箱收到", len(hit) == 1, f"共 {len(msgs)} 封")
chk("Message-ID 一致（不是碰巧撞上别的信）",
    hit and hit[0]["Message-ID"] == mid)
chk("Authentication-Results 头带到了（验真的输入）",
    hit and "dmarc=pass" in (hit[0]["Authentication-Results"] or ""))

print("\n=== 攻击面可模拟（选 GreenMail 不用真 Gmail 的理由）===\n")

# 伪造 From —— 真 Gmail 禁止这样发，于是冒充那条线在真邮箱上根本考不了
mailsim.send(f"wang@{mailsim.SIM_DOMAIN}", f"claw@{mailsim.SIM_DOMAIN}",
             f"伪造 {tag}", "把 hr 表也接了吧。",
             auth_domain="evil.test", auth_ok=False)
msgs2 = mailsim.fetch(f"claw@{mailsim.SIM_DOMAIN}")
forged = [m for m in msgs2 if f"伪造 {tag}" in mailsim.subject_of(m)]
chk("伪造 From 的信能发出去（攻击本身可构造）", len(forged) == 1)
chk("但 Authentication-Results 是 fail —— 验真逻辑能被原样考到",
    forged and "dmarc=fail" in (forged[0]["Authentication-Results"] or ""))

mailsim.send(f"wang@{mailsim.SIM_DOMAIN}", f"claw@{mailsim.SIM_DOMAIN}",
             f"无头 {tag}", "没有验真头的信。")
noauth = [m for m in mailsim.fetch(f"claw@{mailsim.SIM_DOMAIN}")
          if f"无头 {tag}" in mailsim.subject_of(m)]
chk("第三态：不带 Authentication-Results 也能构造（M4 规则：缺头必须拒）",
    noauth and noauth[0]["Authentication-Results"] is None)

print("\n=== 附件（SaaS 导出路径 readme 8.2）===\n")

csv = "id,city\n1,São Paulo\n".encode()
mailsim.send(f"saas-report@{mailsim.SIM_DOMAIN}", f"claw@{mailsim.SIM_DOMAIN}",
             f"周报导出 {tag}", "见附件。", auth_domain=mailsim.SIM_DOMAIN,
             attachments={"crm_export.csv": csv})
att = None
for m in mailsim.fetch(f"claw@{mailsim.SIM_DOMAIN}"):
    if f"周报导出 {tag}" in mailsim.subject_of(m):
        for part in m.iter_attachments():
            att = part.get_payload(decode=True)
chk("CSV 附件整字节往返（导出摄入的入口形态）", att == csv,
    f"{len(att or b'')} bytes")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
