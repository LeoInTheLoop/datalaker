# 项目约定

本项目的 agent 约定与开发规范统一在本文件读取和维护。`AGENTS.md` 仅作入口。

## 沟通偏好

- 默认简体中文；用户用纯英文时用英文。源数据或讨论中的其他语言不改变回复语言。
- 默认只给结论或成品；用户追问时再展开理由、过程和备选方案。不用夸赞式开场，不加结尾总结。
- 用户说“我来讲一遍”后进入费曼模式：只指出错误、关键遗漏，并追问一个绕过的问题；不代为补全、不夸。

## 开工与文档分工

先读本文件和 [readme.md](readme.md)，再按任务读取相关代码、测试及最新
[交接记录](docs/handoff/R6.md)。先检查工作区差异，保留用户已有修改。

| 文件 | 唯一职责 |
|---|---|
| `CLAUDE.md` | 开发约定、执行边界、环境与验证要求 |
| `readme.md` | 产品目标、核心架构、能力现状、下一步验收 |
| `docs/handoff/` | 每阶段做了什么、实测环境与结果、剩余问题 |
| `docs/archive/` | 历史设计，只在追查背景时读，不作为当前要求 |

文档与代码冲突时，说明差异：代码说明“现在怎么运行”，README 说明“要做到什么”，
不能把其中一方自动当成另一方。新增要求先区分目标与已实现能力。
不要重复维护工具数量、测试总数和固定章节编号；工具看注册表，策略看 `policy.py`，
实测数字留在带日期和环境的交接或报告中。旧文档与代码注释中的原 README 章节见历史归档。

## 执行边界

1. **约束由确定性代码执行。** Agent 工具授权统一走 `pre_tool_call`；
   SQL 与源库负载由 Connector / lake 查询入口执行校验，数据库账号提供权限隔离。
   prompt 和 Skill 可以解释规则、引导行为，但不能成为唯一限制。
   工具白名单减少暴露与上下文开销，不能替代执行授权。
2. **Agent 不能批准自己。** 审批 callback 使用独立进程和独立数据库账号；
   Agent 对 `decisions` 无写权限。票据绑定具体动作及参数，邮件正文不能充当授权。
   联系信息与审批资格分开，模型不能通过改联系人给自己或他人授予审批权。
3. **源系统只读且控制负载。** 不修改源数据或源权限；业务查询经 Connector。
   模型 SQL 必须声明 `plane=source|lake` 并过 AST 校验。
   当前 source 业务 SQL 的关联限制以 `review_sql()` 为准；参数化元数据查询有独立路径。
   lake 分析仅查询 `iceberg.*`，不能借外部 catalog 绕回生产源库。
   LIMIT、估算、超时与排队降低风险；不把它们描述成生产负载绝对保证。
4. **修复与交付发生在 lake。** bronze 保留接入数据，silver 承载清洗，gold 承载发布。
   业务口径、清洗和发布等审批级别以策略表为准；源侧整改只记录建议。
   已交付的数据与治理记录应能独立查询，不能只留在 Agent 对话中。
5. **新工具显式声明。** 在工具注册表与 `plugins/datasteward_gate/policy.py` 同步登记。
   未声明工具默认 L4 拒绝，不发审批。handler 不重复实现审批判断；输入校验和业务正确性检查保留在对应代码中。

## 实现约定

- 优先通过 `.hermes/plugins/claw/` 的工具、hook、cron 和 Skill 扩展 Hermes；当前不修改上游代码。
- `services/` 是业务模块目录，不代表每个文件都是独立服务；callback 的进程隔离须单独保证。
- 业务知识放治理库，区分观测、推断与人工确认；模型写的摘要不能替代审批证据。
- OpenMetadata 的历史调研按需查阅。目前不引入其 agent、MCP 或 ingestion 作为执行入口；
  连接 schema 可作为字段参考，源访问仍须经过本项目的授权和负载控制。
- 新依赖同步项目环境、`docker/Dockerfile` 和 Hermes 环境；SQL gate 依赖 `sqlglot`，缺失时不得放行。
- 临时取舍用 `ponytail:`，阶段欠账用 `TODO(R<N>):`。凭证不得写进文档、日志或版本库。

## 本地运行与验证

在仓库根目录执行：

```bash
docker compose --project-directory infra --env-file .env --profile core --profile agent up -d
docker compose --project-directory infra --env-file .env --profile mail up -d greenmail
HERMES=<hermes-agent路径> ./tests/run_all.sh
python3 ops/claw-status.py
```

- 本机 Hermes：`/Users/lingyukong/Documents/GitHub/hermes-agent`；现有环境使用 Python 3.13，
  不使用本机 3.14。依赖安装方法按项目和 Hermes 各自环境处理。
- 本机 Docker 内存有限，保持 Trino `-Xmx1G`，不要同时启动 core 与 OpenMetadata governance 全家桶。
- Docker 超时或 I/O 错误先用 `diskutil list external physical`、`ls /Volumes` 查外置盘。
  盘已挂载时先试 `open -a Docker`，等 `docker ps` 恢复后再启动所需 compose profiles。
  这是待验证恢复经验；失败时记录现象，不做无关清理。
- 修改后跑相应检查；完整回归须核对退出码、失败项和跳过项，不能只加总日志中的通过数。
  完整运行需要 core、mail 及 `HERMES`；既有交接允许 Phoenix 跳过，其余缺依赖须明确报告。
  Docker 不可用时运行可执行的本地检查，不宣称完整通过。
- 区分静态检查、桩测试、真实模型、真实服务和业务验收。安全拒绝测试须有可达的正对照；
  连接失败不能算成功拦截。测试数量和工具调用数量不能代替业务价值。
- 真模型 + 真网关的演练走 [docs/live-rehearsal.md](docs/live-rehearsal.md)：
  `tests/reset_live.py` 还原并自验起点、`tests/live_drive.py` 扮人推进、
  `tests/verify_live.py` 独立查库交叉核对。开跑前先扫一遍那份坑清单。
- 纯文档修改检查差异、链接和前后口径，不启动运行环境或正式 eval。
