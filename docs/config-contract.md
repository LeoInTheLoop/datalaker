# 最小配置契约（第一批 A6）

**这份只回答一件事：每个值存在哪儿、谁有权设、缺了怎么办、谁会读它。**
写进这里不等于已经被执行路径消费 —— 每条都标了「现在谁读」，
标着「尚无消费方」的就是还没接通，别当成已生效。

## 三个存放位置，职责不重叠

| 位置 | 放什么 | 谁能改 |
|---|---|---|
| **项目配置**（`.env` / compose 环境） | 环境级默认值与硬上限，对所有资产一视同仁 | 运维；改完要重启进程 |
| **治理库 Asset Policy** | 按资产的窗口、通知对象、敏感度 —— 由有资格的人确认过的 | 有资格的人经审批写入 |
| **审批表单 / 票据** | **这一次**动作的参数，包括这次批准的执行窗口 | 审批人点链接时确定 |

**优先级：审批表单 > Asset Policy > 项目配置默认值。**
三层都没有的值**不允许模型自拟**，要如实报「没有约定，需要谁来定」。

## 零、装机配置（`infra/claw.yaml`）—— 不在上面三层里

上面三层回答的是「这一次动作怎么办」。装机配置回答的是**更早的一个问题**：
这套系统装在谁家、叫什么、谁能批。它在系统进入 ACTIVE 之前就定死，
运行时不再问 —— 所以它没有「模型自拟」这一支，只有「没配就别启动」。

| 字段 | 落到哪儿 | 缺失时 | 现在谁读 |
|---|---|---|---|
| `agent.name` / `agent.tone` / `agent.language` / `organization.name` | system prompt 的称呼段 | 不拼这一段，记 `IDENTITY_MISSING` 事件（身份不是约束，缺了不该拖垮门禁注册） | `.hermes/plugins/data-steward/__init__.py` |
| `email.agent_email` | 派生 `MAIL_FROM`（发信）与 `EMAIL_ADDRESS`（Hermes 收信） | 同下，拒绝启动 | `services/claw_init.derive_env` |
| `roles[]` | **治理库 `role_assignment`**，由 `ops/claw-init.py` 用 admin 账号写 | **网关拒绝启动**（`claw_preflight`） | `resolve_role()` / `current_holders()` |
| `approval.level` | 翻译成已有的 `MANUAL_MODE` / `REQUIRE_DOUBLE_CONFIRM` | 必填，无默认 | 门禁（经环境变量，不新起读取方） |

三条边界：

- **审批资格只能在治理库里**，不是 env。`MAIL_SPONSOR` / `MAIL_OWNER` /
  `MAIL_STEWARD` 仍作回退兜底，但走到回退会记 `APPROVER_FALLBACK_ENV` 事件 ——
  以前它是静默的，于是「没人被指派」和「指派好了」在日志里长得一模一样。
- **`approval.level` 只能往严格走**：`standard` / `strict` / `strict_double`。
  没有放宽档 —— 级别表（`policy.py`）是执行边界，不接受配置覆盖。
- **身份段只有称呼、组织、信箱、语气。** 交付物、停止点、源只读这些留在
  插件的常量里。一个配置文件能改掉「业务分析不是你的交付物」的话，
  那段话就不再是边界，只是建议。

`infra/hermes/config.yaml` 的 `onboarding.profile_build: "off"` 与这份配套：
身份已经定好了，首次对话不再自我介绍、不建个人画像。**引号是必须的** ——
YAML 会把裸 `off` 读成布尔 `False`，而 Hermes 判的是字符串。

## 一、企业工作日历与时区

Cycle 的「时间预算」按工作日历累计，不是按自然时间。

| 字段 | 含义 | 来源 | 缺失时 | 现在谁读 |
|---|---|---|---|---|
| `WORK_CALENDAR_TZ` | IANA 时区名，例 `Asia/Shanghai` | 项目配置 | 按 UTC 计，并在报告首行标明「时区未配置」 | 尚无消费方（B5） |
| `WORK_DAYS` | 计入的工作日，例 `1-5`（周一至周五） | 项目配置 | 默认 `1-5`，报告里标明用的是默认值 | 尚无消费方（B5） |
| `WORK_HOURS` | 每个工作日计入的时段，例 `09:00-18:00` | 项目配置 | 默认全天计入 | 尚无消费方（B5） |

节假日暂不建表：本批不需要，需要时再加一张治理库表，不要往 env 里塞日期列表。

## 二、Cycle 预算与报告预留

**与已有的日预算是两回事，不要互相顶替**：`DAILY_TOKEN_LIMIT` /
`DAILY_COST_LIMIT_USD` 是每天的**硬上限**（超了就挂起，已在门禁里生效）；
Cycle 预算是**这一轮给多少**，到线要停下来报告。

| 字段 | 含义 | 来源 | 缺失时 | 现在谁读 |
|---|---|---|---|---|
| `CYCLE_HOURS_BUDGET` | 本 Cycle 的工作时长预算（小时） | 项目配置，可由有资格的 Admin/Sponsor 逐轮覆盖 | 不开 Cycle：没有预算就没有「到线」 | 尚无消费方（B5） |
| `CYCLE_TOKEN_BUDGET` | 本 Cycle 的 token 预算 | 同上 | 同上 | 尚无消费方（B5） |
| `CYCLE_REPORT_RESERVE_TOKENS` | 给收尾报告留的 token，不得被正常动作吃掉 | 项目配置 | 默认取 `CYCLE_TOKEN_BUDGET` 的 5% | 尚无消费方（B5） |

实际用量取 `usage_ledger`（`post_llm_call` 已经在写），**不估算、不重算**。
到线时正在跑的动作可以收尾，不开新动作。

## 三、复制的执行窗口

**模型不能自拟窗口**，三层来源按优先级取：

| 层 | 字段 / 位置 | 谁定 |
|---|---|---|
| 1 | 审批票据里的 `window`（这次批准的时段） | 审批人点链接时 |
| 2 | 治理库 Asset Policy 的资产级窗口 | 有资格的 System Expert / Owner |
| 3 | `BULK_WINDOW_START` / `BULK_WINDOW_END` | 项目配置，环境级兜底 |

三层都空 = **没有窗口约束**，批量抽取直接放行（`connector._in_bulk_window`
今天就是这个行为）。这不是「随便跑」，而是「这个环境没定过」——
对外说明时要说清楚是哪一种。

**不在窗口内时，今天只是拒绝，没有任何东西被排队**（`connector.query`
的拒绝消息已经改成如实说这句）。要「到点自动跑」必须先有一条落库的
`approved_waiting_window` 任务线由 cron 唤醒 —— 那是 B3 的活，现在还没有。

## 四、执行前通知

| 字段 | 含义 | 来源 | 缺失时 | 现在谁读 |
|---|---|---|---|---|
| 通知对象 | 该资产的 Owner / System Expert | **治理库**（角色表 + 联系人目录），不是 env | 找不到人就不是「先跑再说」，先把人找出来 | `notify` / 联系人目录 |
| `COPY_NOTICE_LEAD_MINUTES` | 执行前多久发通知 | 项目配置 | 默认 0（不提前通知），报告里如实写 | 尚无消费方（B3） |

**通知不是审批。** 通知发出去没人回，复制照常按已有批准执行；
有人回信说「别跑」也不构成撤销 —— 撤销要走正式路径。

## 五、已经在生效的相邻配置（别重复造）

| 字段 | 作用 | 读取方 |
|---|---|---|
| `PER_PERSON_WIP_LIMIT` / `GLOBAL_WIP_LIMIT` | 一个人手上同时有多少待办 | 门禁 |
| `DAILY_TOKEN_LIMIT` / `DAILY_COST_LIMIT_USD` | 每日硬上限，超了挂起 | 门禁、`ops/claw-status.py` |
| `MAX_TOOL_CALLS_PER_RUN` | 单条线的工具调用上限 | **没有读取方**——`.env.example` 里躺着，代码里没人用。要限次数看下一行 |
| `CLAW_MAX_TURN` / `CLAW_TURN_SCOPE` | 硬 turn：一次测试窗口最多让它动手多少次，计数范围是这一轮的 run id。**不设 = 不限**（生产默认） | 门禁 `pre_tool_call` |
| `CONNECTOR_MAX_SCAN_ROWS` / `CONNECTOR_STATEMENT_TIMEOUT_MS` | 源库负载 | Connector |
| `MANUAL_MODE` | 每个动作和每封信都要人点头 | 门禁 |

新增字段前先看这张表：能用现有的就别新起一个名字。
