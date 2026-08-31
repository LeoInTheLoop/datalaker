"""Gmail OAuth 一次性授权。

跑法：
    .venv/bin/python services/gmail_auth.py

会打开浏览器要求授权，完成后写出 secrets/gmail_token.json，
并把授权账号的邮箱自动填进 .env 的 MAIL_* 字段（demo 中三个角色可以是同一人）。
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",      # 发审批邮件
    "https://www.googleapis.com/auth/gmail.readonly",  # 读回复（找对接人、确认口径）
]
CLIENT = ROOT / "secrets" / "gmail_oauth_client.json"
TOKEN = ROOT / "secrets" / "gmail_token.json"


def authorize():
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        if creds and creds.valid:
            print("已有有效令牌，无需重新授权。")
            return creds

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True,
                                  authorization_prompt_message="请在浏览器中完成授权：{url}")
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)
    print(f"✅ 令牌已写入 {TOKEN}")
    return creds


def fill_env(email):
    env = ROOT / ".env"
    s = env.read_text()
    changed = False
    for k in ("MAIL_FROM", "MAIL_SPONSOR", "MAIL_OWNER", "MAIL_STEWARD"):
        if f"{k}=\n" in s or s.rstrip().endswith(f"{k}="):
            s = s.replace(f"{k}=\n", f"{k}={email}\n")
            if s.rstrip().endswith(f"{k}="):
                s = s.rstrip()[: -len(f"{k}=")] + f"{k}={email}\n"
            changed = True
    if changed:
        env.write_text(s)
        print(f"✅ 已把 {email} 填入 .env 的 MAIL_* 字段")


if __name__ == "__main__":
    creds = authorize()
    profile = build("gmail", "v1", credentials=creds).users().getProfile(userId="me").execute()
    email = profile["emailAddress"]
    print(f"授权账号：{email}")
    fill_env(email)
