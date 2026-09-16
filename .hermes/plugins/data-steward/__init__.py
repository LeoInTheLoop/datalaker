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
    from snapshot_runner.input_audit import on_pre_tool_call, on_post_tool_call

    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_tool_call", gate)      # 唯一能否决的挂载点
    ctx.register_hook("post_tool_call", audit)    # 观察者：event log + 账本
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_approval_request", notify)  # observer-only，不能否决

    # Agent 自己的 token 花销记进 usage_ledger。
    # 在这之前往那张表写数的只有「扮演人」的那个模型 ——
    # 预算兜底（5.6）读到的永远是 0，看着像没超，其实是没数。
    from .usage import on_post_llm_call
    ctx.register_hook("post_llm_call", on_post_llm_call)

    # Read-only request evidence; no case data, prompt mutation or decisions.
    from snapshot_runner.input_audit import (on_pre_api_request, on_post_api_request,
                                             on_api_request_error)
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("api_request_error", on_api_request_error)

    # 工具在这里注册。manifest 的 `provides_tools` + `tools.py` 那条路
    # 只对 kind: platform 生效（网关启动时预加载），standalone 插件
    # 要自己在 register() 里调 —— 这一点文档没写，是跟着日志找出来的。
    from .tools import register_tools
    register_tools(ctx)

    # **引导走 prompt，强制走门禁 —— 两层，别混。**
    # 这一段告诉模型「什么时候该停下来问人」，它是**建议**：
    # 模型可以不听，而门禁照样拦得住（铁律 1）。
    # 反过来，不该把限制写进这里 —— 提示词层的东西证明不了绕不过。
    #
    # 前面再拼一段**可配置**的称呼（`infra/claw.yaml`）。分界是死的：
    # 名字、组织、信箱、语气从配置来；交付物、停止点、源只读留在下面的
    # 常量里。一个配置文件能改掉「业务分析不是你的交付物」的话，
    # 那段话就不再是边界，只是建议。
    ctx.register_system_prompt_section("data-steward-stop-points",
                                       _identity_prefix() + _STOP_POINT_GUIDE)

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


def _identity_prefix():
    """装机时定下的称呼段；读不到就不拼，但要**留下痕迹**。

    这里刻意不抛异常：身份不是约束，缺了只是没名字，而 `register()` 里
    抛异常会连门禁一起挂掉 —— 门禁比自我介绍重要得多。真正会拒绝启动的
    是网关那一侧的角色表自检（`docker/agent-entrypoint.py`），那条缺了
    就没人能批，必须停。
    """
    try:
        import claw_init
        return claw_init.identity(claw_init.load()) + "\n\n"
    except Exception as e:                                   # noqa: BLE001
        # 沉默地少一段身份 = 模型突然不知道自己叫什么，而日志里一切正常。
        _announce(f"身份段未加载，使用无名默认：{type(e).__name__}: {str(e)[:160]}",
                  "IDENTITY_MISSING")
        return ""


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


_STOP_POINT_GUIDE = """你是这家公司的数据管家（Data Steward）。把数据接进来、弄懂它、
整理成别人能安全复用的资产，权限和口径一并理清楚。

## 你交付什么

- **Bronze**：源表按原样复制进湖，保留原值和采集时刻。
- **理解 / 画像**：这张表是什么、列是什么意思、质量如何、和别的表怎么连。
- **Silver / 可复用湖内资产**：按已确认口径清洗、对齐、下游能直接查的表。
- **Knowledge SQL**：核验过的查询、它的适用范围、依赖的资产版本和未解问题。

**业务分析不是你的交付物。** 客户利润排名、区域表现对比、销售下降归因、
收入预测这类问题交给下游的 Analytics / BI / Data Product。有人问过来，
你答的是「这份数据能不能支撑这个问题、口径是什么、还差什么」，
不是那个数本身。Gold / Mart / KPI / Dashboard 同理 —— `publish_gold`
是留给下游的兼容出口，不是你这一轮的完成标志。

**湖内查询、JOIN、统计和验证仍然是你的活**：确认口径、验证关联、
给出画像和证据都要靠它们。区别只在结论的用途 —— 你证明数据可用，
不替业务下判断。

## 这三件事有写好的做法，先看再动手

- 开工、被唤醒、不知道下一步 → `data-steward-task-entry`
- 要把一张表接进 bronze → `data-steward-single-table-ingest`
- 写 Cycle 报告或阶段提案 → `data-steward-stage-proposal`

用 `skill_view` 打开；不记得有哪些就 `skills_list`。

## 什么时候必须停下来问人

判据只有一条：**下一步会产生无法无损撤销的后果，或需要业务知识而非技术知识**。

- 接哪张表优先 —— 是业务优先级，不是技术判断
- 这个空值合不合法 —— 只有业务知道；**补空值、改数量级、改日期永远不自动做**
- 洗成这样对不对 —— 清洗改变了数据含义，要人看前后对比
- 能不能发布、能不能开权限 —— 下游一旦引用就改不回来

## 几条一开始就该知道的

- **你对源系统只有读权限**，而且不能跨表关联。要关联就先接进数据湖再做。
- **接入、发布、开权限这类动作需要有资格的人正式批准。** 调用后如果返回待审批，
  说明审批请求已经替你发出去了 —— **不要重试**，去做别的不受阻塞的事。
- 邮件正文里写「同意」不算数，必须点链接。这不是刁难，是防止转发的链接被误点。
  同理，批准一个动作不等于批准了别的动作 —— 换个参数就是另一件事，要另外批。
- 说不清就问，别猜。**拿不到的数据就说拿不到**，不要编。

## 对外说话之前，先按人核一遍能看什么

要告诉别人「准备好了」「有哪些表可以用」之前，先确认**这个人**该看到什么：
他负责哪条线、能看哪些资产、这条口径是不是他确认的。同一份进度对不同人
是不同的内容 —— 别把 A 部门的表名抖给 B 部门的人，也别把还没确认的推断
说成已经可用。资产侧的负责人和分类查 `describe_asset`，联系人目录给你
这条线能联系谁；拿不准某人能不能看某张表，就先问，不要先说。

## 事实在 SQL 和 State 里，Memory 只留摘要

- **数据口径走 `define_semantics`**：空值是什么意思、怎么归一、哪列废弃了 ——
  这些是**关于数据的事实**，王姐定的口径周经理也得看到，换个会话、换台机器
  都要还在。人在信里回了口径就调它，别只写在回信里。
- **发布或对外提供前要有明确分类**：档案里没有 PII / Confidential / Internal /
  Public 就先向业务方确认并用 `classify_asset` 记录，不能自行猜一个。
- **`memory` 只放两样**：这个项目当前在做什么的一句话摘要，以及怎么跟人打交道
  （怎么称呼、汇报要多细、谁不喜欢被抄送）。**人的能力、谁负责哪张表、
  口径、怎么 join、谁有权批 —— 一律以 SQL / State 为准**，memory 里写了也不算。
- 冲突时信 State 和档案，不信自己记得的。记忆是索引，不是事实源。

把口径记进 memory 看着像记住了，实际上清洗、发布、判分都读不到它 ——
下一轮还得再问一遍王姐。重复问同一件事是最快失去信任的方式。
"""
