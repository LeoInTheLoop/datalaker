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

# datalaker 仓库根：.hermes/plugins/claw/__init__.py → 上溯四层
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

    # 工具在这里注册。manifest 的 `provides_tools` + `tools.py` 那条路
    # 只对 kind: platform 生效（网关启动时预加载），standalone 插件
    # 要自己在 register() 里调 —— 这一点文档没写，是跟着日志找出来的。
    from .tools import register_tools
    register_tools(ctx)
