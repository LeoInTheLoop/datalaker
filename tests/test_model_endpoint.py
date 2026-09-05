"""验证模型端点可用，且 tool calling 可工作。

整个方案依赖 tool calling —— 端点不支持就全盘作废。
ponytail: 用 stdlib urllib，不引入 openai SDK。
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path=os.path.join(ROOT, ".env")):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


E = load_env()
BASE = E["OPENAI_BASE_URL"].rstrip("/")
KEY = E["OPENAI_API_KEY"]
MODEL = E["OPENAI_MODEL"]

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def call(payload, timeout=60):
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


print(f"\n=== 模型端点验证 ===\n  endpoint: {BASE}\n  model:    {MODEL}\n")

# **额度耗尽 / 限流不是代码坏了。** 这一组打的是外部付费端点，
# 余额没了就测不了 —— 那时候整组红会淹掉真正的回归失败。
# 但**必须喊出来**，且必须是探活走干活同一条路（真发一次请求）：
# 静默跳过正是这个项目摔过六次的形状。
def _endpoint_unavailable() -> str:
    try:
        call({"model": MODEL, "messages": [{"role": "user", "content": "hi"}],
              "max_tokens": 4}, timeout=30)
        return ""
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode(errors="replace")
        if e.code in (402, 403, 429) and any(
                k in body.lower() for k in ("quota", "balance", "rate", "billing",
                                            "exhaust", "insufficient")):
            return f"HTTP {e.code}: {body[:160]}"
        return ""
    except Exception:                                         # noqa: BLE001
        return ""


_why = _endpoint_unavailable()
if _why:
    print(f"  SKIP  端点额度/限流不可用，整组跳过 —— **这不是代码问题**\n"
          f"        {_why}\n"
          f"        余额恢复后必须重跑：tool calling 是整个方案的前提。\n")
    sys.exit(0)

# 1. 基本连通
try:
    r = call({"model": MODEL, "messages": [{"role": "user", "content": "回答一个字：好"}],
              "max_tokens": 10})
    txt = r["choices"][0]["message"].get("content", "")
    check("基本 chat completion", bool(txt), repr(txt[:20]))
except urllib.error.HTTPError as e:
    check("基本 chat completion", False, f"HTTP {e.code}: {e.read()[:120].decode(errors='replace')}")
except Exception as e:
    check("基本 chat completion", False, str(e)[:80])

# 2. tool calling —— 方案的命门
TOOLS = [{
    "type": "function",
    "function": {
        "name": "ingest_table",
        "description": "把源系统的一张表接入数据湖的 bronze 层",
        "parameters": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "表名，如 FIN.monthly"}},
            "required": ["table"],
        },
    },
}]
try:
    r = call({"model": MODEL, "tools": TOOLS,
              "messages": [{"role": "user", "content": "请把 FIN.monthly 这张表接入数据湖。"}]})
    msg = r["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    check("tool calling 被支持", bool(calls),
          calls[0]["function"]["name"] if calls else f"无 tool_calls: {str(msg)[:60]}")
    if calls:
        args = json.loads(calls[0]["function"]["arguments"])
        check("工具参数正确解析", args.get("table") == "FIN.monthly", str(args))
except urllib.error.HTTPError as e:
    check("tool calling 被支持", False, f"HTTP {e.code}: {e.read()[:150].decode(errors='replace')}")
except Exception as e:
    check("tool calling 被支持", False, str(e)[:80])

# 3. 工具结果回填后能继续对话（Hermes loop 依赖这一步）
try:
    r = call({"model": MODEL, "tools": TOOLS, "messages": [
        {"role": "user", "content": "请把 FIN.monthly 这张表接入数据湖。"},
        {"role": "assistant", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "ingest_table", "arguments": '{"table":"FIN.monthly"}'}}]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": "[PENDING_APPROVAL] 已向 owner 发起审批，当前不要重试。"},
    ]})
    txt = r["choices"][0]["message"].get("content", "") or ""
    check("工具结果可回填并继续", len(txt) > 0, txt[:50].replace("\n", " "))
    # 模型是否理解「不要重试」—— 影响挂起语义能否奏效
    retried = bool(r["choices"][0]["message"].get("tool_calls"))
    check("收到 PENDING 后未立即重试", not retried,
          "又调了一次工具" if retried else "行为符合预期")
except Exception as e:
    check("工具结果可回填并继续", False, str(e)[:80])

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
