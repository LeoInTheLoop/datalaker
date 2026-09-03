"""outbox 通道：把通知写成 JSONL，不真发信。

**为什么是通道而不是测试里的 mock**：
门禁逻辑与通道无关（readme 第 9 节），所以「不真发信」也该是一个通道，
而不是每个测试各写一份 `class Recorder`。现在有三处重复的 Recorder，
它们各自实现一遍三个方法，漏一个就静默失败。

用途：
  · 测试与 eval —— 断言「审批邮件发给了谁」，不依赖真实邮箱
  · 大规模场景演练 —— 10 个人几十封信，不该真的发出去

    NOTIFY_CHANNEL=outbox  NOTIFY_OUTBOX=/path/to/outbox.jsonl
"""
import json
import os
import pathlib
import time

from . import Notifier


def _path():
    return pathlib.Path(os.environ.get("NOTIFY_OUTBOX", "/tmp/claw_outbox.jsonl"))


class OutboxNotifier(Notifier):
    name = "outbox"

    def _write(self, kind, to, **kw):
        rec = {"ts": time.time(), "kind": kind, "to": to, **kw}
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"channel": self.name, "to": to, "kind": kind}

    def send_approval(self, to, approval_id, tool, target, reason, approver):
        return self._write("approval", to, approval_id=approval_id, tool=tool,
                           target=target, reason=reason, approver=approver)

    def send_receipt(self, to, approval_id, decision, tool, target, approver):
        return self._write("receipt", to, approval_id=approval_id,
                           decision=decision, tool=tool, target=target)

    def send_notice(self, to, subject, body):
        return self._write("notice", to, subject=subject, body=body)


def read(path=None) -> list:
    """读回已发通知。判分器用它断言「谁收到了什么」。"""
    p = pathlib.Path(path) if path else _path()
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
