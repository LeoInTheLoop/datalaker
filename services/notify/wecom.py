"""企业微信通道：模板卡片 + URL 跳转。

与飞书同理，用 URL 跳转而非按钮回调——企微的回调还需要 AES 解密，
引入一套额外的加解密逻辑没有收益（理由见 feishu.py）。

配置：企微群 → 群机器人 → 添加 → 复制 Webhook
    WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
"""
import json
import urllib.request

from . import E, Notifier, approval_links


def _post(url, payload, timeout=15):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    if body.get("errcode"):
        raise RuntimeError(f"企微返回错误 {body['errcode']}: {body.get('errmsg')}")
    return body


class WecomNotifier(Notifier):
    name = "wecom"

    def _hook(self):
        h = E.get("WECOM_WEBHOOK", "")
        if not h:
            raise RuntimeError("WECOM_WEBHOOK 未配置")
        return h

    def send_approval(self, to, approval_id, tool, target, reason, approver):
        ok, no = approval_links(approval_id, approver)
        _post(self._hook(), {"msgtype": "template_card", "template_card": {
            "card_type": "text_notice",
            "main_title": {"title": "数据管家 · 需要你批准一件事",
                           "desc": f"{tool} · {target}"},
            "horizontal_content_list": [
                {"keyname": "动作", "value": tool},
                {"keyname": "对象", "value": target},
                {"keyname": "理由", "value": reason[:80]}],
            "jump_list": [
                {"type": 1, "url": ok, "title": "✅ 批准"},
                {"type": 1, "url": no, "title": "❌ 拒绝"}],
            "card_action": {"type": 1, "url": ok},
            "sub_title_text": "链接 72 小时有效，只能点击一次。"
                              "在你做出决定前，这个动作不会执行。"}})
        return {"channel": "wecom", "to": to, "approval_id": approval_id}

    def send_receipt(self, to, approval_id, decision, tool, target, approver):
        approved = decision == "approve"
        _post(self._hook(), {"msgtype": "template_card", "template_card": {
            "card_type": "text_notice",
            "main_title": {"title": f"已{'批准' if approved else '拒绝'}",
                           "desc": f"{tool} · {target}"},
            "horizontal_content_list": [
                {"keyname": "审计号", "value": str(approval_id)[:8]}],
            "sub_title_text": "该动作将开始执行。" if approved else
                              "该动作不会执行，也不会重复打扰你。"}})
        return {"channel": "wecom", "to": to}

    def send_notice(self, to, subject, body):
        _post(self._hook(), {"msgtype": "markdown",
                             "markdown": {"content": f"**{subject}**\n\n{body}"}})
        return {"channel": "wecom", "to": to}
