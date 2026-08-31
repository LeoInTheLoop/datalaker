"""探测 DASHSCOPE_FREE_MODELS 中哪些模型仍有额度，并支持 tool calling。

各模型的免费额度独立计算，某一个耗尽不代表全部不可用。
"""
import json, os, sys, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E = {}
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); E[k] = v

BASE = E["OPENAI_BASE_URL"].rstrip("/")
KEY = E["OPENAI_API_KEY"]
MODELS = [m.strip() for m in E["DASHSCOPE_FREE_MODELS"].split(",") if m.strip()]

TOOLS = [{"type": "function", "function": {
    "name": "ingest_table", "description": "接入一张表到数据湖",
    "parameters": {"type": "object",
                   "properties": {"table": {"type": "string"}}, "required": ["table"]}}}]


def call(model, tools=False, timeout=45):
    p = {"model": model, "max_tokens": 60,
         "messages": [{"role": "user",
                       "content": "请把 FIN.monthly 接入数据湖。" if tools else "回一个字：好"}]}
    if tools:
        p["tools"] = TOOLS
    req = urllib.request.Request(f"{BASE}/chat/completions",
                                 data=json.dumps(p).encode(),
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


print(f"\n探测 {len(MODELS)} 个免费模型\n")
usable = []
for m in MODELS:
    try:
        call(m)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        reason = "额度耗尽" if "quota" in body.lower() else \
                 "模型不存在" if e.code == 404 or "not exist" in body.lower() else f"HTTP {e.code}"
        print(f"  ✗  {m:<28} {reason}")
        continue
    except Exception as ex:
        print(f"  ✗  {m:<28} {str(ex)[:40]}")
        continue

    # 有额度，再测 tool calling
    try:
        r = call(m, tools=True)
        tc = r["choices"][0]["message"].get("tool_calls") or []
        if tc:
            args = json.loads(tc[0]["function"]["arguments"])
            print(f"  ✓  {m:<28} 可用 + tool calling  ({args})")
            usable.append((m, True))
        else:
            print(f"  ~  {m:<28} 可用，但未触发 tool call")
            usable.append((m, False))
    except Exception as ex:
        print(f"  ~  {m:<28} 可用，tool calling 失败: {str(ex)[:40]}")
        usable.append((m, False))

print()
tool_ok = [m for m, t in usable if t]
if tool_ok:
    print(f"推荐模型: {tool_ok[0]}")
    print(f"支持 tool calling 的共 {len(tool_ok)} 个: {', '.join(tool_ok)}")
else:
    print("没有模型同时满足「有额度」和「支持 tool calling」")
sys.exit(0 if tool_ok else 1)
