"""模型桩：一个本地的 OpenAI 兼容端点，由剧本驱动。

**Hermes 还是身体，我们当大脑。** 它照常做工具分发、门禁、会话持久化，
只是「下一步调哪个工具」由剧本指定而不是模型决定。

为什么要有这个：

- **确定性**。真模型每次选的工具不同，回归就没法比。eval 的
  「非确定性只报区间」是给真模型准备的，机制类断言需要的是可重复。
- **零成本、不联网**。跑一次大 case 十几条线，用真模型既慢又烧钱。
- **测的是 Hermes 的真实管道**，不是绕过它 —— 工具注册、schema 下发、
  tool_calls 解析、pre_tool_call 门禁全都真的走一遍。

**它测不了什么**：真模型面对 schema 会不会选对工具。那件事要用真端点测，
所以两条路都留着，别拿桩的绿去代表模型的能力。

    from tests.model_stub import StubServer
    with StubServer([{"tool": "list_source_tables", "args": {"source": "northwind"}},
                     {"text": "好的，已经列出来了。"}]) as s:
        ...  # 把 Hermes 的 base_url 指到 s.base_url
"""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    server_version = "ModelStub/1.0"

    def log_message(self, *a):            # 别把测试输出淹了
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        # /v1/models —— 有些客户端启动时会探一下
        if self.path.rstrip("/").endswith("/models"):
            m = self.server.stub.model
            return self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "stub"}]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                    # noqa: BLE001
            req = {}
        stub = self.server.stub
        stub.requests.append(req)
        step = stub.next_step(req)

        msg = {"role": "assistant", "content": step.get("text") or ""}
        finish = "stop"
        if step.get("tool"):
            msg["content"] = step.get("text") or None
            msg["tool_calls"] = [{
                "id": "call_" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {"name": step["tool"],
                             "arguments": json.dumps(step.get("args") or {},
                                                     ensure_ascii=False)},
            }]
            finish = "tool_calls"

        self._json(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model") or stub.model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        })


class StubServer:
    """按剧本回答的模型端点。

    剧本是一串 step：
        {"tool": "名字", "args": {...}}   → 让 Hermes 调这个工具
        {"text": "一句话"}                → 收尾
    剧本用完之后一律收尾，**不会无限循环**（真模型也不该）。
    """

    def __init__(self, script=None, model="stub-model", port=0):
        self.script = list(script or [])
        self.model = model
        self.requests = []
        self.aux_requests = []
        self._i = 0
        self._srv = HTTPServer(("127.0.0.1", port), _Handler)
        self._srv.stub = self
        self.port = self._srv.server_address[1]
        self._t = None

    # ---------------------------------------------------------------- 剧本
    def next_step(self, req):
        """只有**带工具的主对话**才消耗剧本。

        Hermes 一次运行会发好几个请求：主对话之外还有标题生成、记忆抽取
        这类辅助调用。它们不带 `tools`。早先不加区分，辅助请求把剧本步骤
        吃掉了，主对话拿到的是「剧本已走完」—— 表现为工具从来没被调用过，
        而日志里一切正常。
        """
        if not (req.get("tools") or []):
            self.aux_requests.append(req)
            return {"text": "ok"}
        if self._i < len(self.script):
            step = self.script[self._i]
            self._i += 1
            return step
        return {"text": "（剧本已走完）"}

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def tool_calls_made(self):
        """Hermes 实际把哪些工具结果回传了 —— 从请求里的 tool 消息看。"""
        out = []
        for r in self.requests:
            for m in r.get("messages") or []:
                if m.get("role") == "tool":
                    out.append({"name": m.get("name"),
                                "content": str(m.get("content"))[:400]})
        return out

    def tools_offered(self):
        """Hermes 给模型下发了哪些工具 —— 白名单是否生效看这个。"""
        for r in self.requests:
            names = [t.get("function", {}).get("name")
                     for t in (r.get("tools") or [])]
            if names:
                return sorted(x for x in names if x)
        return []

    # ---------------------------------------------------------------- 生命周期
    def __enter__(self):
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()
        if self._t:
            self._t.join(timeout=5)
