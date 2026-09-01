"""审批令牌：HMAC 签名、一次性、有期限（readme 9.4 机制四）。

ponytail: 不引入 JWT 库。HMAC-SHA256 + base64url 用 stdlib 就够，
需求只是「这个链接确实是我们发出的、没被改过、还没过期」。
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid

SECRET = os.environ.get("DATASTEWARD_TOKEN_SECRET", "dev-only-change-me").encode()
TTL = 72 * 3600


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(approval_id: str, decision: str, approver: str, ttl: int = TTL,
          stage: str = "click") -> str:
    """签发一枚令牌。批准与拒绝是两枚不同的令牌 —— 意图明确。

    `stage` 支持双重确认（readme 10.5）：

        click   邮件/卡片里的第一次点击 —— 只触发确认信，不落库
        confirm 确认信里的第二次点击 —— 才真正写入决定

    **链接被转发多少次都无所谓：确认信只发到 approver 的注册邮箱。**
    这比要求输验证码轻，比 OAuth 简单，且不依赖任何身份判断。
    """
    assert decision in ("approve", "deny")
    assert stage in ("click", "confirm")
    payload = {
        "aid": approval_id,
        "d": decision,
        "who": approver,
        "st": stage,
        "exp": int(time.time()) + ttl,
        "jti": uuid.uuid4().hex,          # 一次性标识，落库时唯一约束防重放
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify(token: str) -> dict:
    """校验签名与有效期。失败抛 ValueError —— 调用方必须 fail closed。"""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise ValueError("令牌格式错误")

    expect = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):      # 定时安全比较
        raise ValueError("签名无效")

    payload = json.loads(_unb64(body))
    if payload.get("exp", 0) < time.time():
        raise ValueError("令牌已过期")
    payload.setdefault("st", "click")     # 兼容双重确认之前签发的令牌
    return payload
