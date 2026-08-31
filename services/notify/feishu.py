"""飞书通道：交互卡片 + URL 按钮。

**为什么用 URL 按钮而不是交互回调**：

飞书的 `action` 回调需要处理它自己的事件协议、验签与去重，
而我们已经有一套经过验证的签名令牌与幂等机制（readme 9.4）。
URL 按钮直接跳到同一个 `/approve` 端点，复用全部安全属性——
少一套协议，少一处可能出错的地方。

身份验证交给入口层（Cloudflare Access，见 13.2），
而不是依赖 IM 平台的回调身份——这样换通道时身份方案不用重做。

配置：飞书群 → 设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook
    FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
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
    if body.get("code") not in (0, None):
        raise RuntimeError(f"飞书返回错误 {body.get('code')}: {body.get('msg')}")
    return body


def _field(label, value, short=True):
    return {"is_short": short, "text": {"tag": "lark_md",
                                        "content": f"**{label}**\n{value}"}}


class FeishuNotifier(Notifier):
    name = "feishu"

    def _hook(self):
        h = E.get("FEISHU_WEBHOOK", "")
        if not h:
            raise RuntimeError("FEISHU_WEBHOOK 未配置")
        return h

    def send_approval(self, to, approval_id, tool, target, reason, approver):
        ok, no = approval_links(approval_id, approver)
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"template": "orange", "title": {
                    "tag": "plain_text", "content": "数据管家 · 需要你批准一件事"}},
                "elements": [
                    {"tag": "div", "fields": [
                        _field("动作", tool), _field("对象", f"`{target}`"),
                        _field("理由", reason, short=False)]},
                    {"tag": "hr"},
                    {"tag": "action", "actions": [
                        {"tag": "button", "type": "primary", "url": ok,
                         "text": {"tag": "plain_text", "content": "批准"}},
                        {"tag": "button", "type": "danger", "url": no,
                         "text": {"tag": "plain_text", "content": "拒绝"}}]},
                    {"tag": "note", "elements": [{"tag": "plain_text", "content":
                        "链接 72 小时有效，只能点击一次。"
                        "在你做出决定前，这个动作不会执行——这是系统层面的拦截。"}]},
                ]}}
        _post(self._hook(), card)
        return {"channel": "feishu", "to": to, "approval_id": approval_id}

    def send_receipt(self, to, approval_id, decision, tool, target, approver):
        approved = decision == "approve"
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"template": "green" if approved else "red",
                           "title": {"tag": "plain_text",
                                     "content": f"已{'批准' if approved else '拒绝'}"}},
                "elements": [
                    {"tag": "div", "fields": [
                        _field("动作", tool), _field("对象", f"`{target}`"),
                        _field("审计号", f"`{approval_id[:8]}`")]},
                    {"tag": "note", "elements": [{"tag": "plain_text", "content":
                        "该动作将开始执行。若非本意请立即联系。" if approved else
                        "该动作不会执行，也不会就同一动作重复打扰你。"}]},
                ]}}
        _post(self._hook(), card)
        return {"channel": "feishu", "to": to}

    def send_notice(self, to, subject, body):
        _post(self._hook(), {"msg_type": "interactive", "card": {
            "header": {"template": "blue",
                       "title": {"tag": "plain_text", "content": subject}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]}})
        return {"channel": "feishu", "to": to}
