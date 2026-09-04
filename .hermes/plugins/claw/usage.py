"""把 Agent 自己的 token 花销记进账本（重排 M4）。

## 为什么这是个洞

预算兜底（readme 5.6）读 `usage_ledger` 决定今天还让不让干活。
但在这之前，**往这张表里写数的只有 `services/persona.py`** ——
那是测试里扮演「人」的那个模型。Claw 自己烧掉多少，一分钱都没记。

于是「超预算就挂起」这道闸门读到的永远是 0：看着像没超，其实是没数。
这和 `publish_gold` 那次是同一类问题 —— **闸门读的量根本没人写**，
而失败方式是安静的。

## 为什么用 Hermes 的钩子而不是自己数

token 数只有发请求的那一方知道，而发请求的是 Hermes 的推理循环，
不是我们的代码。自己数就只能靠估算。`post_llm_call` 直接把
provider 回的 `usage` 递过来，`agent.usage_pricing` 还能把它折成钱 ——
两样都是宿主已经有的，没有理由再造一份。
"""
from typing import Any


def _canonical(usage: Any, provider: str, api_mode: str):
    """尽量用 Hermes 的归一化；拿不到就退回逐字段猜。

    各家 provider 的字段名不一样（prompt/input、completion/output），
    还有 cache 读写、reasoning 这些分桶。Hermes 已经把这套映射维护好了。
    """
    try:
        from agent.usage_pricing import normalize_usage
        return normalize_usage(usage, provider=provider, api_mode=api_mode)
    except Exception:                                        # noqa: BLE001
        return None


def _pick(u, *names):
    for n in names:
        v = u.get(n) if isinstance(u, dict) else getattr(u, n, None)
        if isinstance(v, (int, float)) and v:
            return int(v)
    return 0


def extract(usage: Any, provider: str = "", api_mode: str = ""):
    """返回 (prompt_tokens, output_tokens)。**拿不到就是 0，不猜。**"""
    if usage is None:
        return 0, 0
    c = _canonical(usage, provider, api_mode)
    if c is not None:
        try:
            return int(c.prompt_tokens), int(c.output_tokens)
        except Exception:                                    # noqa: BLE001
            pass
    return (_pick(usage, "prompt_tokens", "input_tokens"),
            _pick(usage, "completion_tokens", "output_tokens"))


def cost_of(model: str, usage: Any, provider: str = "", base_url: str = "",
            api_mode: str = "") -> float:
    """折成美元。**不知道价格就返回 0，不编一个。**

    返回 0 的含义因此是二义的（免费 / 未知），所以 `record` 里
    把 token 数也一并记下 —— 预算可以按 token 卡，不必依赖价格表。
    """
    c = _canonical(usage, provider, api_mode)
    if c is None:
        return 0.0
    try:
        from agent.usage_pricing import estimate_usage_cost
        r = estimate_usage_cost(model, c, provider=provider or None,
                                base_url=base_url or None)
        return float(r.amount_usd) if r.amount_usd is not None else 0.0
    except Exception:                                        # noqa: BLE001
        return 0.0


def _paths():
    """把项目根挂上 sys.path。

    优先用插件包自己的 `_ensure_path`（它还负责 `plugins` 包名的桥接）；
    这个模块被单独加载时（测试）相对导入不成立，就按文件位置自己算。
    **不要把这段藏进 `except Exception: pass` 里** —— 路径挂错是静默的，
    表现成「账本一直是空的」，而那正是这个模块要修的毛病。
    """
    try:
        from . import _ensure_path
    except ImportError:
        import os
        import sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        for sub in ("", "services", "plugins"):
            d = os.path.join(root, sub) if sub else root
            if d not in sys.path:
                sys.path.insert(0, d)
        return
    _ensure_path()


def record(model: str = "", usage: Any = None, response: Any = None,
           provider: str = "", base_url: str = "", api_mode: str = "",
           task_id: str = "") -> dict | None:
    """真正干活的那一半。**它会抛异常** —— 吞异常的只有下面的观察者包装。

    两半分开是为了让测试能看见失败：全都包在 try 里的话，
    路径挂错、字段改名、账本写不进去，表现全都是「安静地什么都没发生」。
    """
    raw = usage if usage is not None else getattr(response, "usage", None)
    pt, ot = extract(raw, provider, api_mode)
    if not (pt or ot):
        return None                      # 没有可信数字就不写，别往账本里塞 0
    _paths()
    import connector
    cost = cost_of(model, raw, provider, base_url, api_mode)
    connector.record_usage(model or "unknown", pt, ot, cost,
                           run_id=task_id, purpose="agent")
    return {"model": model, "prompt_tokens": pt, "output_tokens": ot,
            "cost_usd": cost}


def on_post_llm_call(*, model: str = "", provider: str = "", base_url: str = "",
                     api_mode: str = "", usage: Any = None, response: Any = None,
                     task_id: str = "", **_: Any) -> None:
    """观察者。**任何失败都不许冒泡** —— 记不上账不该让对话崩掉。"""
    try:
        record(model=model, usage=usage, response=response, provider=provider,
               base_url=base_url, api_mode=api_mode, task_id=task_id)
    except Exception:                                        # noqa: BLE001
        pass
