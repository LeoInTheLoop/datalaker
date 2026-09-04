"""预算兜底的数据来源（readme 5.6）。

**闸门读的量必须真的有人写。** 在这之前往 `usage_ledger` 写数的只有
`services/persona.py` —— 那是测试里扮演「人」的模型；Claw 自己烧掉多少
一分钱都没记，于是「超预算就挂起」读到的永远是 0：
看着像没超，其实是没数。
"""
import importlib.util as ilu
import os
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_usage_{uuid.uuid4().hex[:8]}.db"

import connector                                              # noqa: E402

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


_spec = ilu.spec_from_file_location(
    "claw_usage", ROOT / ".hermes" / "plugins" / "claw" / "usage.py")
U = ilu.module_from_spec(_spec)
sys.modules["claw_usage"] = U
_spec.loader.exec_module(U)

print("\n=== 账本在 SQLite 上也要能写 ===\n")

# 以前 record_usage 自己连 DSN，没有 DSN 就直接 return —— 本地跑
# 账本永远是空的，预算兜底等于没有。
before = connector.today_usage()
chk("空账本从 0 开始", before["tokens"] == 0, str(before))
connector.record_usage("test-model", 1000, 200, 0.05, run_id="r1", purpose="agent")
after = connector.today_usage()
chk("**没有 Postgres 也记得上账**（以前这里静默失效）",
    after["tokens"] == 1200 and after["calls"] == 1, str(after))
chk("花费也累加", abs(after["cost_usd"] - 0.05) < 1e-9, str(after["cost_usd"]))

print("\n=== 从 provider 的 usage 里取数 ===\n")

chk("OpenAI 风格字段", U.extract({"prompt_tokens": 7, "completion_tokens": 3})
    == (7, 3))
chk("Anthropic 风格字段", U.extract({"input_tokens": 11, "output_tokens": 5})
    == (11, 5))


class _U:
    prompt_tokens, completion_tokens = 9, 4


chk("对象形态也认（不只是 dict）", U.extract(_U()) == (9, 4))
chk("拿不到就是 0，不猜", U.extract(None) == (0, 0))
chk("价格表查不到时返回 0 而不是编一个",
    U.cost_of("no-such-model-xyz", {"prompt_tokens": 5}) == 0.0)

print("\n=== 钩子本身：观察者，不许冒泡 ===\n")

# 干活的那一半会抛异常，测试才看得见失败。
# 早先整个钩子包在 try 里，路径挂错时表现成「账本没变」——
# 而这个模块要修的毛病本来就叫「账本一直是空的」。
n0 = connector.today_usage()["calls"]
r = U.record(model="m", usage={"prompt_tokens": 50, "completion_tokens": 10},
             task_id="run-x")
n1 = connector.today_usage()["calls"]
chk("写进了账本", n1 == n0 + 1, f"{n0} → {n1}")
chk("返回记了什么，不是 None", r and r["prompt_tokens"] == 50, str(r))

# 没有可信数字时不该往账本里塞一行 0 —— 那会把「调用次数」变成噪音
chk("没有用量时不写空行",
    U.record(model="m", usage=None) is None
    and connector.today_usage()["calls"] == n1)

U.on_post_llm_call(model="m", usage={"prompt_tokens": 1, "completion_tokens": 1})
chk("钩子这一层也真的写得进去（不是被 except 吞了）",
    connector.today_usage()["calls"] == n1 + 1)

# 观察者钩子抛异常会打断对话。给它一个必然出错的输入。
class _Boom:
    @property
    def usage(self):
        raise RuntimeError("boom")


try:
    U.on_post_llm_call(model="m", response=_Boom())
    chk("**钩子里的异常不冒泡**（记不上账不该让对话崩掉）", True)
except Exception as e:                                        # noqa: BLE001
    chk("**钩子里的异常不冒泡**（记不上账不该让对话崩掉）", False, str(e))

print("\n=== 闸门读到的就是这份账 ===\n")

os.environ["DAILY_TOKEN_LIMIT"] = "100"
import datasteward_gate as G                                  # noqa: E402
G._budget_cache.update(ts=0.0, over=None)
r = G.gate("profile_table", {"table": "x"}, "run-b")
chk("Agent 自己烧掉的量能把闸门触发（以前触发不了）",
    isinstance(r, dict) and "BUDGET" in r.get("message", ""),
    (r or {}).get("message", "")[:40] if isinstance(r, dict) else str(r))
os.environ.pop("DAILY_TOKEN_LIMIT", None)
G._budget_cache.update(ts=0.0, over=None)

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
