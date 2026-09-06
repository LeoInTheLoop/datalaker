"""Data Steward Claw —— 挂在 Hermes 上的插件入口。

**插件住在 datalaker 仓库里，Hermes 从 `./.hermes/plugins/` 加载它**
（需 `HERMES_ENABLE_PROJECT_PLUGINS=1`）。这样 Hermes 保持零改动 ——
本项目从不 fork 它，铁律 1 的前提就是「限制在 pre_tool_call 里，
不在 Hermes 源码里」。

这个模块必须**保持 import 轻量**：Hermes 在发现阶段就会导入它，
而工具的实现要连数据库。重活一律推迟到 `register()` 与 handler 里。
"""
import os
import sys

# datalaker 仓库根：.hermes/plugins/data-steward/__init__.py → 上溯四层
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _ensure_path():
    """把 datalaker 的模块路径接上，并把 `plugins` 这个名字要回来。

    **两个项目都有顶层 `plugins/` 包。** Hermes 启动时会导入它自己的
    （它就是从 `<repo>/plugins/` 加载插件的），于是 `sys.modules["plugins"]`
    早就被占了，我们的 `from plugins.datasteward_gate...` 一律 ModuleNotFoundError。
    25 个文件在用这个路径，全改是 M7 的事；在那之前用别名把它桥过去。

    这不是权宜之计的借口 —— 它恰恰是「我们的 plugins/ 必须改名」
    最硬的证据，记在 docs/restructure.md 的 M7 里。
    """
    for p in ("", "services", "plugins"):
        d = os.path.join(ROOT, p) if p else ROOT
        if d not in sys.path:
            sys.path.insert(0, d)

    if "plugins.datasteward_gate" in sys.modules:
        return
    try:
        import importlib
        ours = importlib.import_module("datasteward_gate")
    except Exception:                                        # noqa: BLE001
        return
    sys.modules["plugins.datasteward_gate"] = ours
    for sub in ("approvals", "policy"):
        try:
            sys.modules[f"plugins.datasteward_gate.{sub}"] = importlib.import_module(
                f"datasteward_gate.{sub}")
        except Exception:                                    # noqa: BLE001
            pass
    host = sys.modules.get("plugins")
    if host is not None and not hasattr(host, "datasteward_gate"):
        setattr(host, "datasteward_gate", ours)


def register(ctx):
    """Hermes 插件入口：挂门禁。

    门禁本体仍在 `plugins/datasteward_gate/` —— 这里只负责把它接到
    Hermes 的钩子上。**不在这里写任何判断逻辑**，限制散进插件入口
    就等于散进业务代码（铁律 1）。
    """
    _ensure_path()
    from datasteward_gate import audit, gate, notify

    ctx.register_hook("pre_tool_call", gate)      # 唯一能否决的挂载点
    ctx.register_hook("post_tool_call", audit)    # 观察者：event log + 账本
    ctx.register_hook("pre_approval_request", notify)  # observer-only，不能否决

    # Agent 自己的 token 花销记进 usage_ledger。
    # 在这之前往那张表写数的只有「扮演人」的那个模型 ——
    # 预算兜底（5.6）读到的永远是 0，看着像没超，其实是没数。
    from .usage import on_post_llm_call
    ctx.register_hook("post_llm_call", on_post_llm_call)

    # 工具在这里注册。manifest 的 `provides_tools` + `tools.py` 那条路
    # 只对 kind: platform 生效（网关启动时预加载），standalone 插件
    # 要自己在 register() 里调 —— 这一点文档没写，是跟着日志找出来的。
    from .tools import register_tools
    register_tools(ctx)

    # **引导走 prompt，强制走门禁 —— 两层，别混。**
    # 这一段告诉模型「什么时候该停下来问人」，它是**建议**：
    # 模型可以不听，而门禁照样拦得住（铁律 1）。
    # 反过来，不该把限制写进这里 —— 提示词层的东西证明不了绕不过。
    ctx.register_system_prompt_section("data-steward-stop-points", _STOP_POINT_GUIDE)

    # 定时任务交给 Hermes 的 cron（M4 删重复）。**失败不能拖垮注册** ——
    # 门禁比定时催办重要得多；cron 起不来是运维问题，门禁挂不上是安全问题。
    try:
        from .cron import ensure_jobs
        r = ensure_jobs(ROOT)
        if r.get("created"):
            _announce(f"已登记定时作业：{', '.join(r['created'])}", "CRON_JOB_CREATED")
        if r.get("updated"):
            _announce(f"作业定义已变，按表修正：{', '.join(r['updated'])}",
                      "CRON_JOB_CORRECTED")
        if r.get("errors"):
            _announce(f"定时作业登记异常：{'; '.join(r['errors'])}", "CRON_JOB_ERROR")
    except Exception as e:                                    # noqa: BLE001
        _announce(f"定时作业登记失败（不影响门禁）：{type(e).__name__}: {e}",
                  "CRON_JOB_ERROR")


def _announce(msg, kind):
    """喊出来 —— 但**不能只喊给流**。

    Hermes 在插件注册阶段把 stdout 和 stderr 都吞掉了（oneshot 实测：
    两条流里都搜不到这行）。于是「作业定义已变，按表修正」print 出去
    谁也看不见 —— 正是本项目摔过六次的那个形状：事情发生了，而外面
    看着一切正常。所以再写一行事件日志，那份是**留得下来**的。

    观察者：写不进去也不能影响注册（门禁比记录重要得多）。
    """
    print(f"[data-steward] {msg}")
    try:
        from datasteward_gate import store
        store().append_event("cron", kind, msg[:400])
    except Exception:                                        # noqa: BLE001
        pass


_STOP_POINT_GUIDE = """你是这家公司的数据管家。整理数据的同时，把权限也一并理清楚。

## 什么时候必须停下来问人

判据只有一条：**下一步会产生无法无损撤销的后果，或需要业务知识而非技术知识**。

- 接哪张表优先 —— 是业务优先级，不是技术判断
- 这个空值合不合法 —— 只有业务知道；**补空值、改数量级、改日期永远不自动做**
- 洗成这样对不对 —— 清洗改变了数据含义，要人看前后对比
- 能不能发布 —— 下游一旦引用就改不回来

## 几条一开始就该知道的

- **你对源系统只有读权限**，而且不能跨表关联。要关联就先接进数据湖再做。
- **接入、发布、开权限这类动作需要负责人批准。** 调用后如果返回待审批，
  说明审批请求已经替你发出去了 —— **不要重试**，去做别的不受阻塞的事。
- 邮件正文里写「同意」不算数，必须点链接。这不是刁难，是防止转发的链接被误点。
- 说不清就问，别猜。**拿不到的数据就说拿不到**，不要编。

## 两种「记住」，别记错地方

- **数据口径走 `define_semantics`**：空值是什么意思、怎么归一、哪列废弃了 ——
  这些是**关于数据的事实**，王姐定的口径周经理也得看到，换个会话、换台机器
  都要还在。人在信里回了口径就调它，别只写在回信里。
- **`memory` 只记怎么跟人打交道**：怎么称呼、汇报要多细、谁不喜欢被抄送。
  那是**关于人的偏好**，跟着这个人走。

把口径记进 memory 看着像记住了，实际上清洗、发布、判分都读不到它 ——
下一轮还得再问一遍王姐。重复问同一件事是最快失去信任的方式。
"""
