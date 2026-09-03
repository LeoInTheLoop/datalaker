"""OpenTelemetry 接入 —— 让工具链路、耗时和「等人」那一段在 Phoenix 上看得见。

**为什么要单独一层，而不是各处直接调 SDK**：可观测与通知同级，
适用同一条原则——「通知失败 ≠ 门禁打开」，在这里是「Phoenix 挂了 ≠ 主流程停」。
把「没装 SDK / 没配端点 / 导出失败」三种情况统一收敛成 no-op，
调用方就不必在每个打点处写 try；否则总会有人图省事把打点跳过去。

**为什么挂起要单独一种 span**：`WAITING_FOR_HUMAN` 不是一次慢调用，
它是这类 Agent 的正常状态，而且跨进程、跨天。画成一条横跨真实时长的 span，
是「Agent 会等人」这件事唯一能让外人一眼看懂的证据（readme 14 第 21 幕）。

**为什么挂起 span 不挂在执行 trace 下面**：等待发生在两次进程之间——
发起它的进程通常已经退出了。硬认一个父 span 就是在编造因果；
让它自成一条 trace，反而如实说明了「这段时间没有任何代码在跑」。

开关：设 `OTEL_EXPORTER_OTLP_ENDPOINT`（或 `CLAW_TRACE=1` 走默认端点）即启用。
两个都没设时本模块不 import SDK、不建连接、不留线程。
"""
import atexit
import json
import os
import threading
import time

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
DEFAULT_ENDPOINT = "http://localhost:4317"

_lock = threading.Lock()
_tracer = None        # None=未初始化  False=明确不可用  其余=可用
_provider = None
_root = None          # 本进程当次运行的根 span；事件 span 挂在它下面才连得成链路
_waits: dict = {}     # run_id → 开始等的时刻（进程内兜底，DB 时间戳优先）


def _safe(fn):
    """可观测的任何失败都不许冒泡。**这是本模块存在的主要理由。**"""
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:                                    # noqa: BLE001
            return None
    wrap.__name__ = fn.__name__
    wrap.__doc__ = fn.__doc__
    return wrap


def enabled() -> bool:
    """调用方用它跳过打点前的取数（比如查「等谁」要走一次 DB）。

    自己兜住异常：调用方是 `if tracing.enabled():`，
    这里漏一个异常出去就等于让可观测决定主流程走不走。
    """
    try:
        return _tracer_or_none() is not None
    except Exception:                                        # noqa: BLE001
        return False


def _tracer_or_none():
    global _tracer, _provider
    if _tracer is not None:
        return _tracer or None
    with _lock:
        if _tracer is not None:
            return _tracer or None
        _tracer = False                  # 先落成不可用：下面任何一步失败都停在这
        endpoint = os.environ.get(ENDPOINT_ENV) or ""
        if not endpoint and not os.environ.get("CLAW_TRACE"):
            return None
        endpoint = endpoint or DEFAULT_ENDPOINT
        # grpc 的 C 核心装了 atfork 钩子，本进程每次 fork 子进程（`sync._trino`
        # 就是 docker exec）都会往 stderr 打一行 "FD from fork parent still in
        # poll list"，被调用方连同查询结果一起读走，于是写 bronze 报错。
        # **可观测把主流程搞挂了，等于这一整层白做**——所以在 import grpc 之前关掉。
        # 用 setdefault：运维要调 grpc 日志时仍然说了算。
        os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
        os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter)
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.id_generator import IdGenerator

            class UrandomIds(IdGenerator):
                """span / trace id 不能取自全局 `random`。

                `tests/run_case_full.py` 为了让 case 可复现会
                `random.seed(case["seed"])`，而 OTel 默认的 id 生成器抽的正是
                同一个全局 random——于是每次跑出来的 span id 逐条相同，
                Phoenix 按 id 去重，从第二次起 trace 直接消失。
                **可复现的是业务，不是 trace id。**
                """

                def generate_span_id(self):
                    return int.from_bytes(os.urandom(8), "big") or 1

                def generate_trace_id(self):
                    return int.from_bytes(os.urandom(16), "big") or 1

            res = Resource.create({
                "service.name": os.environ.get("OTEL_SERVICE_NAME",
                                               "data-steward-claw"),
                # Phoenix 按这个资源属性分项目
                "openinference.project.name": os.environ.get(
                    "PHOENIX_PROJECT_NAME", "data-steward-claw"),
            })
            _provider = TracerProvider(resource=res, id_generator=UrandomIds())
            _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=endpoint, insecure=endpoint.startswith("http://"))))
            _tracer = _provider.get_tracer("datasteward")
            atexit.register(_at_exit)
        except Exception:                                    # noqa: BLE001
            _tracer = False
        return _tracer or None


def _ns(ms) -> int:
    return int(ms) * 1_000_000


def _clean(attrs: dict) -> dict:
    """OTel 只收标量。**不在这里放数据行**——readme 13.1 那张表同样适用于 trace。"""
    out = {}
    for k, v in attrs.items():
        if v is None or v == "":
            continue
        out[k] = v if isinstance(v, (str, bool, int, float)) else str(v)[:400]
    return out


# ---------------------------------------------------------------- 根 span
@_safe
def start_root(name: str, start_ms=None, **attrs):
    """一次运行的根。没有它，每个事件各成一条 trace，链路就散了。"""
    global _root
    t = _tracer_or_none()
    if not t:
        return None
    end_root()                           # 上一条没收口的先收口，避免串线
    _root = t.start_span(name, start_time=_ns(start_ms or time.time() * 1000),
                         attributes=_clean({"openinference.span.kind": "CHAIN",
                                            **attrs}))
    return _root


@_safe
def end_root(status: str = ""):
    global _root
    if _root is None:
        return
    if status:
        _root.set_attribute("claw.status", status)
    _root.end()
    _root = None


def _parent_ctx():
    if _root is None:
        return None                      # None = 用当前上下文（即新起一条 trace）
    from opentelemetry import trace
    return trace.set_span_in_context(_root)


# ---------------------------------------------------------------- 事件 span
# 事件类型 → OpenInference 的 span kind，Phoenix 靠它决定怎么渲染
_KIND = {"tool_call": "TOOL", "gate_block": "TOOL", "memory_hit": "RETRIEVER"}
_MD_KEYS = ("approver", "approval_id", "channel", "to", "from_addr", "level",
            "attribution_layer")


@_safe
def event_span(ev: dict, run_id: str = "", case_id: str = ""):
    """把 `trajectory.py` 的一条事件投影成 span。轨迹记什么，trace 就有什么。"""
    t = _tracer_or_none()
    if not t:
        return
    kind = ev.get("event_type") or "event"
    label = ev.get("name") or ev.get("tool_name") or "-"
    md = ev.get("metadata") or {}
    attrs = {
        "openinference.span.kind": _KIND.get(kind, "CHAIN"),
        "claw.event_type": kind,
        "claw.status": ev.get("status") or "ok",
        "claw.step": ev.get("step_index") or 0,
        "claw.run_id": run_id,
        "claw.case_id": case_id,
        "tool.name": ev.get("tool_name") or "",
        "output.value": ev.get("result_summary") or "",
    }
    if ev.get("arguments") is not None:
        attrs["input.value"] = json.dumps(ev["arguments"], ensure_ascii=False)[:400]
    for k in _MD_KEYS:
        if md.get(k) is not None:
            attrs[f"claw.{k}"] = md[k]
    # 成本：事件带了用量就上报，Phoenix 才能按 trace 汇总花钱（readme 20.1）
    for src, dst in (("tokens_in", "llm.token_count.prompt"),
                     ("tokens_out", "llm.token_count.completion"),
                     ("cost_usd", "claw.cost_usd")):
        if isinstance(md.get(src), (int, float)):
            attrs[dst] = md[src]
    span = t.start_span(f"{kind}:{label}", context=_parent_ctx(),
                        start_time=_ns(ev.get("timestamp_ms")
                                       or time.time() * 1000),
                        attributes=_clean(attrs))
    span.end()


# ---------------------------------------------------------------- 挂起 span
@_safe
def suspend_begin(run_id: str, approval_id=None, waiting_for: str = "",
                  note: str = ""):
    """记下开始等的时刻。**这里不建 span**——跨度要等到知道等了多久才成立，
    而那个时刻可能在另一个进程、另一天。"""
    if not _tracer_or_none():
        return
    if len(_waits) > 2000:               # 常驻调度进程里挂起可能只增不减
        _waits.clear()
    _waits[run_id] = {"start_ms": int(time.time() * 1000),
                      "approval_id": approval_id or "",
                      "waiting_for": waiting_for or "", "note": (note or "")[:300]}


@_safe
def suspend_end(run_id: str, since=None, outcome: str = "resumed",
                approval_id=None, waiting_for: str = "", note: str = ""):
    """等到头了，补出那条横跨等待期的 span。

    `since` 是 runs 表里记下的挂起时刻（秒）。**它优先于进程内记录**：
    时间引擎（`evals/clock.py`）把 DB 时间戳往前回拨来模拟等了几天，
    只认进程内的 wall clock 就会把 12 天压成 12 毫秒。取两者中更早的一个，
    等于「以现有证据能证明的最早起点」，不夸大也不抹平。
    """
    t = _tracer_or_none()
    if not t:
        return
    w = _waits.pop(run_id, None) or {}
    cands = [x for x in (int(since * 1000) if since else None,
                         w.get("start_ms")) if x]
    if not cands:
        return                           # 没有起点就没有跨度，宁可不画
    start_ms = min(cands)
    end_ms = int(time.time() * 1000)
    span = t.start_span("WAITING_FOR_HUMAN", start_time=_ns(start_ms),
                        attributes=_clean({
                            "openinference.span.kind": "CHAIN",
                            "claw.event_type": "waiting_for_human",
                            "claw.run_id": run_id,
                            "claw.approval_id": approval_id or w.get("approval_id"),
                            "claw.waiting_for": waiting_for or w.get("waiting_for"),
                            "claw.outcome": outcome,
                            "claw.wait_seconds": round((end_ms - start_ms) / 1000, 3),
                            "claw.wait_days": round((end_ms - start_ms) / 86400000, 3),
                            "output.value": (note or w.get("note") or "")[:300],
                        }))
    span.end(_ns(end_ms))


# ---------------------------------------------------------------- 收尾
@_safe
def flush(timeout_ms: int = 5000) -> bool:
    if _provider is None:
        return False
    return bool(_provider.force_flush(timeout_ms))


def _at_exit():
    end_root()
    flush()
    try:
        _provider.shutdown()
    except Exception:                                        # noqa: BLE001
        pass
