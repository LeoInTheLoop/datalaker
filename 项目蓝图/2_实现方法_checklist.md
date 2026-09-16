# 2 实现方法 checklist

## 使用方式

2026-09-14：**R01—R16 已由用户最终决定**，以[机制最终决定原文](素材/机制最终决定原文.txt)为准；本文件已同步实施项与历史冲突的处理。此前提议保存在[决策前版本](素材/实现方法_checklist_决策前.txt)，不再作为待审方案。

2026-09-14 二轮：走读冲突由用户统一收口为 [R17—R28 补充决定](#review2)，与 R01—R16 同级生效；同一问题两处表述不一致时**以 R17—R28 为准**。

- `已定` 表示机制决策完成，**不表示代码已经实现**。
- `[x] 代码已有·复用` 来自[静态代码对照](4_当前代码对照.md)，不表示本轮开发或真实模型验收通过。
- `[ ] Ixx.yy` 表示仍需补齐、接通或验收；既有条目编号保留，新增条目接在相应模块末尾。
- `〔prompt·hook〕` 这类标记是该条的**承载形态**——包括 prompt、Skill、State、工具、业务代码、邮件入口、生命周期事件、hook、cron、Memory 与 eval，见[形态表](#form)。
- 已处理完的历史（D01—D25 差异映射、走读冲突收口对照、原文中不作为要求的参考数值）移到[归档](素材/历史差异与参考数值.md)，正文只留当前要求。示例数值保持可配置，不将示例升级成硬编码生产参数。

Hermes 继续负责 loop、工具调用、Task Session 和 cron；复用现有治理库、State、Harness、正式 Approval Event 和 Eval。模型负责语义、相关性、按需检索、规则提案与摘要；确定性代码负责状态、权限、执行准入、预算、事件幂等与审计。本次只更新蓝图文档。

核心产物为 **Bronze → Understand / Profile → Silver / Reusable Lake Assets，并持续沉淀 Knowledge SQL**。Gold、Mart、KPI、Dashboard、客户利润宽表及最终业务分析由下游 Analytics / BI / Data Product 负责。

<a id="review"></a>
## R01—R16 最终机制决定

| 编号 / 机制 | 最终决定 | 落实位置 | 状态 |
|---|---|---|---|
| <a id="r01"></a>**R01 分析职责** | Data Steward 保留 Lake 查询、JOIN、统计、Profiling、质量、coverage、规则验证、准备度与 Bronze/Silver 差异核验；不回答客户利润/区域表现排名、销售下降归因、收入预测。业务分析交下游；说明可用数据前也查请求人 Visibility；Mission 信息渐进补齐见 [R26](#r26) | I01、I02、I09 | 已定，待落地 |
| <a id="r02"></a>**R02 Bronze → Silver** | 保留 Sponsor 对**当前 Scope** 的阶段门；不是整批表同时进入 Silver。门开后各 Asset/Task 独立满足 Bronze Ready、Safe Profiling 完成、关键语义足够清楚、必要关系已确认、transformation 有依据，才进入 Silver Candidate。Scope 实质扩大重新审批阶段门；Gold 非核心职责 | I02、I03、I08 | 已定，待落地 |
| <a id="r03"></a>**R03 样例复核** | 首次 transformation 或重大变化由对应 Authority 人工复核 Before/After、影响行数、关键字段、聚合差异、异常数量、规则说明。规则已批且同规则/Scope/语义的重复执行可自动质量验证。Schema、规则逻辑、分布、JOIN coverage、影响范围异常扩大或新异常类型出现时重开复核；不统一找 Sponsor | I02、I04、I08 | 已定，待落地 |
| <a id="r04"></a>**R04 Cycle / Token / Trust** | 独立 Cycle 保存 `cycle_id/start_time/time_budget/token_budget/trust_level/status/report_budget`；时间按墙钟，等人也计时。时间或 token 到线即 Stop，并预留报告预算。Trust **只影响 Cycle 长度**，示例 0.5→1→3→5–7 天；不扩大工具/数据/Scope/审批权。Hermes 建议升降，Sponsor 明确确认。周期阶梯与计时口径改按 [R21](#r21)/[R22](#r22) | I03、I08 | 已定，待落地 |
| <a id="r05"></a>**R05 到线后的动作** | **到线立即报告，停止新任务/分支**；已经 Running 的当前动作允许完成，写 State/Result 后停止，不自动启动下一步。所有尚未开始的任务，包括已批准但等待夜间窗口的任务，暂停到下一 Cycle；报告不等待 Running 收尾 | I03、I07、I08 | 已定，待落地 |
| <a id="r06"></a>**R06 语义与跨域确认** | 执行审批认正式 Approval Event。讨论中的口头/邮件先成为 claim/provisional understanding；Hermes 整理成结构化规则，再发对应 Authority 正式确认后写 confirmed；沉默无效。跨域可由双方 Authority 分别确认，或由明确具有跨域裁决权的人确认；冲突进入多人 Resolution Thread。可直接 confirmed 的条件见 [R18](#r18)，跨域确认资格见 [R24](#r24) | I02、I04、I06、I09 | 已定，待落地 |
| <a id="r07"></a>**R07 人员与 Authority** | 本人自述只能 self_claimed。正式 Authority 来自独立可信来源：OA/IAM/HRIS/Org Directory、已确认上级/Admin、Sponsor/Boss 明确指定、已确认 Authority 的合法转授权。绑定 `Person + Role + Domain/Asset + Authority Type`；联系人、来信许可、Visibility、语义权、审批权分别管理；转介不建立资格见 [R17](#r17) | I01、I04、I06、I09 | 已定，待落地 |
| <a id="r08"></a>**R08 Visibility / Scope** | Lake 内可见范围由系统权限决定；对外披露逐人检查。请求人 Visibility 不明确，查已有 Authority，再向其上级/Domain Admin/HR Admin 等独立来源确认，不能本人自证。Scope 扩张由原 Sponsor 决定，但先确认 Sponsor 能知道该域；不借扩 Scope 泄露敏感存在性/字段 | I01、I04、I06、I09 | 已定，待落地 |
| <a id="r09"></a>**R09 Task Session / Router** | 后台按 Task 建 Session，Question 是内部子项；Task 有 Goal/Scope/Done Criteria/Related Assets/Open Questions。入站先找 Existing Task，否则独立目标建 Task、边缘信息入 Backlog。优先 thread/In-Reply-To→Task/Question ID→Person+Open Questions→LLM；默认读 Goal/Summary、Open Questions、最近 **3 条消息**、Incoming Message，低置信再扩上下文。Session 内：本 Scope 自处理；超 Scope 且阻塞→dependency/child task；其余 backlog。Done Criteria 满足且无阻塞当前使用的 unresolved blocker 即可关闭，不要求全部问题解决 | I03、I06 | 已定，待落地 |
| <a id="r10"></a>**R10 注意力与静默** | 每人未解问题硬上限可配置，如 3；是上限而非目标，答完不自动补满。每批沟通等待默认处理窗口，如 2–3 个工作日，之后才下一批/reminder/escalation；提前追加仅限严重阻塞或 Sponsor/Boss 明确催办，仍受硬上限。多人默认静默，仅跑偏、关键冲突、缺 Authority、超时、结论待总结时介入。周报列本周期完成、Pending、Queued、Blocked by。追问与新批次的区分见 [R20](#r20)，计时口径见 [R22](#r22)，报告字段见 [R25](#r25) | I06、I08 | 已定，待落地 |
| <a id="r11"></a>**R11 Source-protect / Lake-work** | 源端仅做安全接入必需的 Metadata、Schema、数量估算、必要主键/时间字段和可访问性检查；复制到 Bronze 后在 Lake 做 count/null/distinct/min/max/median/quantile/duplicate/anomaly/key/relationship 深度验证并整理 Silver。后续仅 Freshness、Schema drift、明显复制异常触发受控回源；普通同步不重复源端深度扫描。metadata 与 data 的两级权限见 [R23](#r23) | I02、I07 | 已定，待落地 |
| <a id="r12"></a>**R12 夜间复制与通知** | 白天正式审批→记录 Execution Window→`approved_waiting_window`→窗口到达 Pre-check→复制。窗口来自已有 Asset Policy、System/DB Owner 或 Data Owner。通知是告知，不再审批；发受影响的 System/DB Owner，必要时 Data Owner，不默认 Sponsor；提前量可配 10m/30m/none。无人回复不影响有效批准、窗口与 Pre-check 均通过时执行；若 Cycle 已停则服从 R05 暂停未开始任务。窗口由谁设定见 [R27](#r27) | I03、I04、I07 | 已定，待落地 |
| <a id="r13"></a>**R13 Knowledge / Memory** | SQL/State 为事实源；人员、角色、能力、资产、ownership、语义、JOIN、authority、visibility、决定、证据及项目/资产状态变化即写库。Memory 只存 Project Summary、沟通偏好和跨 Session 快速上下文。Session/Cycle 收尾、重大 Mission/Objective 变化时刷新。新 Session 先摘要，再读相关最新 State/SQL；冲突服从事实源并必要时重写摘要 | I04、I05 | 已定，待落地 |
| <a id="r14"></a>**R14 当前审批人与旧票恢复** | 同一资产同一 Authority Type 只有一个当前 active approver，不同类型可不同人；用 `valid_from/valid_to` 表达更换。恢复先查最新 State/Scope/Asset Version；仍适用则严格 replay 原批准参数。Scope 实质变化、Schema 影响动作、Source 变化、敏感级别变化、原任务失效时重新审批；模型不得修改旧票据参数。跨域关系的 Authority Type 见 [R24](#r24) | I03、I04、I09 | 已定，待落地 |
| <a id="r15"></a>**R15 新资产评估** | 新资产首次成功进入 Bronze 自动登记一次 Integration Assessment，判断相关性、要不要/怎么连、Mapping、新业务实体、所需确认。普通同步不重复开 Session；关键 Schema、JOIN Key、语义、Source/Lineage、Mission Scope 变化或已确认关系明显失效才重新评估。Cycle 停止时只写 `assessment_required`，不启动新 Session。第一张资产同样登记见 [R19](#r19) | I02、I03、I07 | 已定，待落地 |
| <a id="r16"></a>**R16 异常与最终产物** | Hermes 在 Scope/权限内尽可能接入相关表、理解/画像、澄清语义与关系、找 Authority 确认并沉淀 SQL；不承担最终数据产品 100% 正确性或业务验收。保留 candidate/verified/confirmed/unknown/conflict/exception/deprecated；confirmed 仅指有资格的人确认规则/语义。未解问题记 Exception/Unknown/Conflict 与 Evidence，不隐藏不猜补。核心到 Silver/Reusable Lake Assets + Knowledge SQL；Gold/Mart/KPI/Dashboard/利润宽表交下游 | I01、I02、I03、I04、I10 | 已定，待落地 |

R04/R10/R12 中的周期级别、问题数、工作日窗口和通知提前量保留示例身份；实现时作为配置。未指定的算法阈值、时区/工作日历及确认事件字段属于实现配置，不再把已决定的机制重新列为开放问题。

<a id="review2"></a>
## R17—R28 补充决定（走读冲突收口）

2026-09-14 二轮：针对[总括](1_项目总括机制.md)、本 checklist 与 [Case walkthrough](3_Case_walkthrough.md) 三份之间的冲突，用户给出下列统一决定。R17—R28 与 R01—R16 同级，冲突时以本节为准。

| 编号 / 机制 | 最终决定 | 落实位置 | 状态 |
|---|---|---|---|
| <a id="r17"></a>**R17 转介与审批资格** | 转介只建立**联系人关系**，不建立任何审批资格。审批资格必须由 Admin 显式授予 `Person + Scope + Authority Type`，或从可信 OA / IAM / Authority Store 查到；具备资格后该人的审批才有效。Sponsor/Boss 的「明确指定」必须落成这种显式授予记录，一句转介文字不算 | I01、I04、I06、I09 | 已定，待落地 |
| <a id="r18"></a>**R18 回复何时可直接 confirmed** | **单域 + 单一事实 + 回复者 authority 已验证 + 无冲突** → 回复本身可直接写 `confirmed`。**跨域 / 复杂规则 / 多条件 / 存在冲突** → 先记 claim，Hermes 结构化总结后由对应 authority 明确确认才写 `confirmed`。两条路径都要留 evidence 与资格校验记录；沉默在任一路径下都不算确认 | I02、I04、I06、I09 | 已定，待落地 |
| <a id="r19"></a>**R19 第一张资产同样评估** | 每个资产第一次进入 Bronze 都自动开 Integration Assessment，**含第一张表**。第一张因为没有其他资产，结论可以直接是「暂无可关联对象」并结束，但事件必须落库 | I03、I07、I10 | 已定，待落地 |
| <a id="r20"></a>**R20 追问与新批次** | 区分两类发问：为关闭当前 `question_id / task_id` 所必需的 **clarification 可以立即追问**；**新主题、新 Task、新问题必须进入下一批窗口**。两者都继续受每人 open-question 硬上限约束 | I06 | 已定，待落地 |
| <a id="r21"></a>**R21 Cycle 边界必须显式** | 主线与实现都显式插入 Cycle 边界。初期 0.5—1 个 working day，之后逐步提升到 2—3、5—7 个 working day。**到线必须先报告并由 Admin/Sponsor 放行**才有下一轮。Waiting task 的累计等待时间跨 Cycle 保留，不因换轮清零。此阶梯取代 R04 的 `0.5→1→3→5–7` 示例 | I03、I08、I10 | 已定，待落地 |
| <a id="r22"></a>**R22「天」的统一口径** | 全部使用 **working day / business hours**，不用自然日。Cycle 预算、催办、3 天 escalation、批次处理窗口统一按企业工作日历计算。等待期间照常计时（不因等人暂停），但只累计工作日历内的时间 | I03、I06、I08 | 已定，待落地 |
| <a id="r23"></a>**R23 metadata 与 data 两级权限** | 源端访问拆两级：`metadata_visibility` 允许看表存在、schema、类型、大小估算等最小 metadata；真正 sample、扫描、Bronze copy 需要更高一级 `data access / ingestion approval`。两级分别授权、分别审计，低级不自动升级为高级 | I02、I07、I09 | 已定，待落地 |
| <a id="r24"></a>**R24 跨域关系 authority** | 新增独立的 `cross_domain_relationship / integration authority`。域内 authority 只能确认自己这侧语义（A 确认 Sales 侧、F 确认 Finance 侧）；Sales↔Finance 的正式 JOIN / mapping 由跨域 authority 确认。它是独立 Authority Type，同一关系对象同一时间只有一个 active，不与域内语义权互相顶替 | I04、I06、I09 | 已定，待落地 |
| <a id="r25"></a>**R25 Cycle Report 字段** | 报告必须含：上一轮意见落实情况、Cycle budget / usage、按人视图 `completed / pending / queued / blocked_by`，以及 Completed、Waiting、Risks、Recommendation。Running 如实单列，不伪装完成 | I08 | 已定，待落地 |
| <a id="r26"></a>**R26 Mission 渐进建立** | Mission 允许渐进建立。只要 consumer、success criteria、explicit exclusions 还没补齐，**每个 Cycle 至少检查并补一个关键问题**；最晚在进入第一版 Silver 前必须完成一次 Mission Refinement | I01、I03、I08 | 已定，待落地 |
| <a id="r27"></a>**R27 大表与执行窗口** | Hermes 可以判断「可能存在源端负载风险」，但**不能自己决定执行窗口**。具体 `execution window / copy mode / load policy` 由有资格的取数 / System Expert 在审批时通过下拉或配置设定，Hermes 只负责遵守与调度。没有设定窗口时按 Asset Policy 既有值，仍不由模型自拟 | I04、I07 | 已定，待落地 |
| <a id="r28"></a>**R28 主线覆盖映射** | 补齐 I10.01 对 N03、N08、N10、N11、N14 的映射，流程本身不改 | I10 | 已定，待落地 |

> **总原则**：Hermes 负责发现、判断、协调和推进；正式权限、确认权、执行窗口和跨域裁决必须来自独立可信的 authority / policy；所有长期事实进入 SQL/State，Memory 只做摘要和沟通辅助。

<a id="form"></a>
## 形态：每条写在哪儿

按 [CLAUDE.md 的「形态先定」](../CLAUDE.md)，每个 `[ ]` 条目都标了承载物。一条可以有多个，顺序即主次。

| 标记 | 承载物 | 边界 |
|---|---|---|
| `prompt` | Identity / System Prompt 的稳定身份 | **只解释，不约束**。任何「能不能做」都不能以 prompt 为唯一限制 |
| `skill` | Skill 的指令与触发条件 | 管语义判断、按需检索和生成，不承担执行授权，也不保证生命周期事件自动触发 |
| `state` | State Tool 与治理库的表、字段、枚举 | 事实源。事实变化即写，不等 Session 收尾 |
| `tool` | 改现有受控工具的签名、参数或返回 | 工具白名单减少暴露，不等于执行授权 |
| `tool+` | **新增**工具 | 必须在工具注册表与 [`policy.py`](../plugins/datasteward_gate/policy.py) 同步登记；未声明默认 L4 拒绝、不发审批 |
| `service` | `services/` 等目录中的确定性业务模块 | 承载输入校验、业务正确性检查、状态转换、幂等落库与事件处理，供工具、hook、邮件入口和 cron 复用。不是每个模块都独立运行，也不另建 agent loop；不重复实现工具授权 |
| `adapter` | 真实邮件收发入口与 Task Session 分发接入 | 入站接收、消息归属、会话分发和出站线程绑定必须接进实际收发路径；新增模型工具不能替代入口接通 |
| `lifecycle` | Session 开始/恢复/收尾、Cycle 收尾和 Mission/Objective 变化的事件接入 | 由代码触发恢复读取或摘要刷新，模型按需选择相关事实并生成摘要；优先复用运行时扩展，不能只靠 Skill 提醒模型执行 |
| `policy` | 策略表：工具等级、清洗/发布等审批级别 | handler 不重复实现审批判断 |
| `hook` | 工具与模型调用前后的接入点 | `pre_tool_call` 执行资格、可见性、预算、窗口等准入检查；`post_tool_call` 接执行结果、状态更新和审计；`post_llm_call` 记录模型用量。具体业务逻辑由业务模块承载，后置观察者不能补作事前授权。一次 turn = 一次进 gate 的工具调用，被 block 的也算 |
| `callback` | 检查点落在**独立进程**：审批通道与签名票据 | **Agent 不能批准自己**：独立进程、独立数据库账号，票据绑定具体动作与参数，邮件正文不能充当授权 |
| `sqlgate` | 检查点落在**查询入口**：Connector / `review_sql()` 的 AST 与 `plane=source\|lake` 校验 | 缺 `sqlglot` 不得放行。与 `pre_tool_call` 是两道独立检查，不能互相顶替 |
| `grant` | **数据库账号**的最小权限与底层隔离 | 逐人、逐资产的 `metadata_visibility` / `data access` 由 Authority/Policy 与访问入口共同判定，账号权限提供底层隔离。共享服务账号的读权限不等于每个人都有相同权限，也不能单独表达全部业务授权 |
| `cron` | 定时作业 | 窗口到达、批次到期、静默合并、后台 reconcile |
| `config` | 可配置参数 | 阈值、窗口、工作日历、问题上限、通知提前量、Cycle 阶梯。**示例数值一律落这里，不硬编码** |
| `memory` | Memory | 只装项目摘要与沟通偏好，**永不作事实源或授权依据** |
| `eval` | 行为 eval 与离线判分器 | 判分器不依赖模型端点 |
| `hermes` | **条件项**：扩展不足时才改 Hermes 本体 | 先核验工具、hook、邮件适配与生命周期等扩展接口；确实满足不了必要需求时才做最小修改，须记原因、兼容影响、验证结果 |

两条判据：

1. 凡「能不能做、算不算数、谁说了算」的条目，须明确实际执行检查点：Agent 工具授权走 `pre_tool_call`，审批决定走独立 `callback`，SQL/源负载走查询入口，账号隔离走 `grant`；状态写入和生命周期转换还须有对应业务校验。后置审计不能代替事前准入，单有形态标签也不表示已接通。`prompt`/`skill` 不承担约束；工具和业务模块保留输入及业务正确性检查，不重复实现审批判断。
2. 凡带 `tool+` 的条目，验收里必须包含「注册表与 `policy.py` 已同步登记」这一项，否则该工具上线即被 L4 拒。

<a id="i01"></a>
## I01 Identity / Mission

- [x] **代码已有·复用**：插件已经注入 Data Steward 身份、源只读/审批说明、知识落库规则；联系人登记与角色授权分开。

代码入口：[插件入口](../.hermes/plugins/data-steward/__init__.py)、[联系人工具](../.hermes/plugins/data-steward/tools.py)。

**落实与验收**：在现有系统提示与 Skill 上补入口策略；Mission/Scope 的正式事实写现有 State/治理库，提供恢复读取工具。验收首次接触只问一个入口、新 Session 找回目标与等待项、未经批准不扩 Scope、首次 Silver 前缺少 Mission Refinement 时被拦且补齐后可达。

- [ ] **I01.01｜补齐**〔prompt·hook〕（[R01](#r01) / [R08](#r08) / [R16](#r16)） 调整现有 Identity/System Prompt，固定 Data Steward 职责与核心产物 Bronze、理解/画像、Silver/Reusable Lake Assets、Knowledge SQL。保留湖内查询、JOIN、统计与验证；不做客户利润/区域表现排名、销售下降归因或收入预测。对外说明准备度及可用资产前先查 Visibility；Gold、Mart、KPI、Dashboard 和业务分析交下游。
- [ ] **I01.02｜接通**〔prompt·skill〕 写清工作入口：开始或恢复任务先读 State；缺 person/asset/semantic/relationship/process 时使用 Discovery；提出具体动作后经过已有 Hook。
- [ ] **I01.03｜新增**〔skill·state·tool+·hook·lifecycle〕（[R08](#r08) / [R26](#r26)） 用 Mission Setup / Refinement Skill 逐步取得并确认 `north_star / sponsor / scope / current_objective / success_direction / known_constraints`。North Star 低频变；Objective 可每轮变；Scope 改动由原 Sponsor 确认，写入前由代码校验确认者资格、对象/范围和证据。Mission 允许渐进建立：consumer、success criteria、explicit exclusions 未补齐时，每个 Cycle 至少检查并补一个关键问题；Cycle 生命周期代码登记并核对推进记录，模型负责选择与澄清问题。最晚在进入第一版 Silver 前完成一次 Mission Refinement，并保存结果；Silver 准入检查该记录，缺失时阻止进入，不能只靠 Skill 提醒。
- [ ] **I01.04｜补齐**〔skill·hook·state〕（[R10](#r10) / [R26](#r26)） 首次联系只发一封：自我介绍、一句话说明目标、一个 seed person 或 seed asset/system 问题。按人记录 `first_contact_sent`，发信前的检查点拒绝同一人的第二封首封、以及一封里塞多个问题；这道检查与 [I06.07](#i06) 的每人未解问题硬上限是两道，不互相顶替。后续按 [R26](#r26) 渐进补齐消费者、成功标准、优先项和禁区：每 Cycle 至少推进一个，不把 3—5 个问题一次全发，也不允许一直不补。
- [ ] **I01.05｜补齐**〔state·hook〕（[R07](#r07)） 区分创建者、Sponsor、Boss、系统联系人、Data Owner、Semantic Authority 及不同审批资格。联系人/本人角色自述不自动赋权；**转介只建立联系人关系，不建立审批资格**（[R17](#r17)）。审批资格须由 Admin 显式授予 `Person + Scope + Authority Type`，或从可信 OA/IAM/Authority Store 查到；Sponsor/Boss 的明确指定必须落成这种显式授予记录，正式 Authority 必须绑定 Person、Role、Domain/Asset、Authority Type 并留证据。
- [ ] **I01.06｜复用后补齐**〔prompt·state〕（[R13](#r13)） `IDENTITY.md` 只表达稳定角色；早期 `TASK.md` 示例可以作项目视图，Mission/Scope 的事实源仍应映射到现有 State Tool。避免同一目标在文件、Memory、SQL 各写一份却无主次。
- [ ] **I01.07｜新增**〔state·hook〕（[R08](#r08)） 每次新分支记录与 Objective、gap、blocker 或必要血缘的关系。发现潜在 scope 扩展先校验披露权限，再找原发起人确认；未获确认不访问、不接入、不沿该分支继续探索。

来源：[M002—M006](素材/分享对话原文.md#m002)、[M020—M024](素材/分享对话原文.md#m020)、[M087—M094](素材/分享对话原文.md#m087)、[M149—M154](素材/分享对话原文.md#m149)。

<a id="i02"></a>
## I02 Skills 与专业工作策略

- [x] **代码已有·复用**：已有画像、DQ、清洗提案/执行、语义确认、湖内质量检查、关联与发布工具，以及阶段提案 Skill；清洗保留原值。现有 Gold 发布有分类和 raw 列边界，按最终决定属于兼容/下游能力，不计 Hermes 核心产物。

代码入口：[tools](../.hermes/plugins/data-steward/tools.py)、[data_tools](../services/data_tools.py)、[clean](../services/clean.py)、[publish](../services/publish.py)、[阶段提案 Skill](../.hermes/skills/data-steward-stage-proposal/SKILL.md)。

**落实与验收**：复用专业工具，补目标/gap 驱动策略、湖内完整画像契约和规则后的复核记录。验收每项统计有口径，负值不自动判错，首批/重大规则达到 R03 的复核要求后形成可复用 Silver；Gold/最终业务验收交下游。

- [ ] **I02.01｜复用后补齐**〔skill〕 盘点并复用已有 Skill，按职责补 Mission Setup、Gap-driven Discovery、Data Steward/Safe Profiling、Cycle Review；逻辑模块不要求一对一新建目录。
- [ ] **I02.02｜新增**〔skill·state〕 Discovery description 与核心指令写明触发：当前任务因未知的人、资产、语义、关系或流程无法推进。模型先识别 gap，结构化保存 `type / question / blocks_objective / next_action`，再探索。
- [ ] **I02.03｜补齐**〔skill·tool·hook〕（[R07](#r07) / [R13](#r13)） 五类 Discovery 都先查已有 SQL 知识；未知 owner 时，按明确 authority → 角色职责线索 → 已记录个人能力寻找知情人，不能由岗位常识推导审批资格。
- [ ] **I02.04｜新增**〔skill·hook〕 零资产时从 Sponsor 取得入口；之后只探索一跳，验证相关性再决定下一跳。已有很多表时只取与 Mission/实体/语义/血缘相关的候选，防止全库扫描和无目的收集。
- [ ] **I02.05｜补齐**〔state·tool〕（[R06](#r06) / [R16](#r16)） Discovery 输出带来源与可信状态；兼容已有观测层，并支持 candidate、verified、confirmed、unknown、conflict、exception、deprecated。技术验证与正式业务确认分开，冲突/否定/过期记录保留；confirmed 不表示数据 100% 正确。足够继续当前 Task 就停止探索。
- [ ] **I02.06｜补齐**〔state·hook·policy〕（[R02](#r02) / [R16](#r16)） 保留 Sponsor 对当前 Scope 的 Silver 阶段门，不要求整批表同时推进。各 Asset/Task 在 Bronze Ready、Safe Profiling 完成、关键语义足够清楚、必要关系确认、transformation 有依据时进入 Silver Candidate。Scope 实质扩大重批阶段门。允许任务停在 Bronze/理解阶段；Gold 不作为核心目标或完成条件。
- [ ] **I02.07｜补齐**〔tool·sqlgate〕 Safe Profiling 覆盖 `column/type/nullable`、估算/实际数量、空值数量及比例、distinct、min/max、median、quantiles、top values/frequency、duplicate、类型/范围/不可能值/类别异常、时间缺口、PK/FK 候选和唯一性。
- [ ] **I02.08｜补齐**〔hook·sqlgate·grant〕（[R11](#r11) / [R23](#r23)） 源端只做安全接入必须的 metadata/schema、数量估算、必要主键/时间字段及基础可访问性检查，这些落在 `metadata_visibility` 级别；sample、扫描与 Bronze copy 属于 `data access / ingestion approval`，两级分别授权。深度画像默认在 Lake。湖内统计优先受控聚合/必要采样，分位数近似计算要标明，并限制超时/返回量及敏感原值。Freshness、Schema drift、明显复制异常才受控回源；普通同步不重复源端深度扫描。
- [ ] **I02.09｜补齐**〔skill·tool〕 负金额等异常生成语义问题，不自动判错；输出 `known / unknown / suspected / confirmed`，例如 `order_total < 0` 先问退款、冲销还是错误。
- [ ] **I02.10｜补齐**〔skill·tool〕 Silver 提案提供 `Input / Transformation / Reason / Evidence / Expected Output`，包括测试订单、currency、status、revenue、customer/退款关系等示例规则，但不把示例直接当通用清洗规则。
- [ ] **I02.11｜接通**〔callback·hook·policy〕（[R03](#r03) / [R16](#r16)） 首次 transformation 或重大变化必须由对应 Authority 复核 Before/After、影响行数、关键字段变化、聚合差异、异常数量、规则说明。验证包括 JOIN 丢失/膨胀、null、duplicate、业务规则；已批准同规则/Scope/语义的重复执行可自动质量验证。Schema/逻辑变化、明显分布或 coverage 漂移、影响范围异常扩大、新异常类型均重新复核；不统一找 Sponsor，不承担下游最终业务验收。
- [ ] **I02.12｜补齐**〔skill·state〕 原表整改建议默认低优先级，保留问题证据、影响和建议；只有直接阻塞当前正确性时升级，不因为上游可以更整洁而扩大当前 Cycle。

来源：[M025—M038](素材/分享对话原文.md#m025)、[M078—M082](素材/分享对话原文.md#m078)、[M105—M106](素材/分享对话原文.md#m105)。

<a id="i03"></a>
## I03 复用 State Tool，补齐状态契约

- [x] **代码已有·复用**：`runs` 已保存参数、挂起、waiting_on、checkpoint 与恢复记录；审批、sync_state、asset_catalog、语义可独立查询。

代码入口：[runs](../services/runs.py)、[治理库](../plugins/datasteward_gate/approvals.py)、[resumable](../ops/resumable.py)。

**落实与验收**：扩展 Project/Cycle/Task/Question 的关系和统一只读视图；复用已有审批恢复。验收一人多问题、多任务等同一人、问题取消/已解决、等待窗口和新资产事件去重，不能只验一条工具批准后重试。

- [ ] **I03.01｜新增**〔state·config〕（[R04](#r04) / [R09](#r09)） Project/Mission State 表达 North Star、Sponsor、Scope、Objective、phase、active tasks、gaps、known asset/person 引用、waiting、next_action、当前 Cycle、剩余预算与上轮 review。Cycle 单独保存 cycle_id、start_time、time_budget、token_budget、trust_level、status、report_budget。
- [ ] **I03.02｜补齐**〔state〕 Asset State 能表达：source/table/owner 引用、当前阶段、用途、key/time fields、confirmed semantics 引用、unknowns、candidate/confirmed joins、quality issues、validation results、blockers、pending_human、next_action。
- [ ] **I03.03｜补齐**〔state〕 Task State 能区分 `ready / running / waiting_human / waiting_approval / waiting_external / blocked / completed / cancelled`。`approved_waiting_window`、`waiting_confirmation` 等场景状态与既有枚举统一映射：审批、排期、执行、语义确认分别表达，不挤进一个字段。
- [ ] **I03.04｜补齐**〔state〕（[R09](#r09) / [R16](#r16)） 每个等待记录 waiting_for、waiting_reason、resume_condition、next_action，并保留 Project/Cycle/Asset/Task 关联。Task 的 Done Criteria 满足且没有阻塞当前使用的 unresolved blocker 即可关闭；不要求解决所有 Question、所有异常或发布 Gold。未解但不阻塞的问题保留引用/Backlog/Evidence。
- [ ] **I03.05｜接通**〔state·tool+〕（[R09](#r09)） Task Session 保存 Goal、Scope、Done Criteria、Related Assets、Open Questions；Question/Request 是 Task 内部子项，保存 question_id、task_id、asked_to、exact_question、topic、asset、status。Thread、Incoming Message 与来源绑定 Task/Question，不按一个 Question 建一个 Session。
- [ ] **I03.06｜新增**〔service·state·hook〕（[R05](#r05) / [R15](#r15) / [R19](#r19)） 每个资产第一次成功进入 Bronze，由业务模块幂等登记一次 Integration Assessment，**第一张同样登记**，第二张、第三张及后续均适用；第一张可直接以「暂无可关联对象」结束，但事件必须落库；失败或重复事件不制造任务。登记接实际复制成功路径，持久化与补偿要求见 I05.04，不能只依赖后置观察者。Cycle 已停只写 assessment_required，不启动新 Session；普通同步不重复开评估 Session。
- [ ] **I03.07｜新增**〔skill·tool〕（[R15](#r15)） Integration Assessment 读取 Mission/Objective、新 Asset State、相关旧资产、SQL 已知关系，判断是否相关、是否需要及如何 JOIN、有无 Mapping、新业务实体、需找谁确认；不关联也能成为有依据的完成结论。仅关键 Schema/JOIN Key/语义、Source/Lineage、Mission Scope 变化或已确认关系明显失效时重评。
- [ ] **I03.08｜补齐**〔hook·callback·state〕（[R14](#r14)） 恢复先读最新 State、Scope、Asset Version，检查任务已完成/取消、原回复和审批适用性。仍适用则严格 replay 原批准参数；Scope 实质变化、Schema 影响动作、Source/敏感级别变化、原任务失效时重新审批。已由另一条线完成的事项不重做，模型不能改旧票据批准参数。
- [ ] **I03.09｜接通**〔state·hook〕（[R05](#r05) / [R12](#r12)） 审批、排期、执行独立保存：如 approval_status=approved、execution_status=scheduled、execution_window=22:00–06:00、任务为 approved_waiting_window。排期不等于执行成功。Cycle Stop 时尚未开始的排期任务暂停至下一轮；已 Running 的仅完成当前动作、写 State/Result 后停。

- [ ] **I03.10｜新增**〔tool+·skill gateway？〕（[R09](#r09)） 将 Message Router 与 Session Router 分层：入站命中 Existing Task 则路由；否则独立工作目标创建 Task，边缘信息入 Backlog。Session 内本 Scope 问题就地处理；超 Scope 但阻塞当前任务建 dependency/child task；不阻塞则 backlog。Child Task 仍受 Scope/权限/Cycle 约束，不自动取得扩 Scope 权。

- [ ] **I03.11｜新增**〔state·cron·service〕（[I03.09](#i03) / [I07.02](#i07) 的前置） 让**时间本身成为唤醒条件**：`runs` 增加 `next_action_at`（这条线到点再来找我），恢复 monitor 增加一种「到点」事件，没到点就不列出、不点模型。当前唤醒只有三种事件源 —— 审批落库、清洗轮开、WIP 降下来 —— `approved_waiting_window` 没有任何字段能表达「今晚 01:00 再来」，只能靠每分钟轮询加输出按整点慢变去凑。到点判断留在确定性脚本里，monitor 的输出保持稳定，不交给模型判断该不该醒。 **2026-09-15 已实施**：`runs.next_action_at` + `schedule/due/due_now`，未到点的线不进 `resumable/retryable`，monitor 增 `kind="due"`；见 `tests/test_due_wakeup.py`。真实窗口执行待 B3 验收。

来源：[M029—M036](素材/分享对话原文.md#m029)、[M053—M060](素材/分享对话原文.md#m053)、[M099—M100](素材/分享对话原文.md#m099)、[M115—M116](素材/分享对话原文.md#m115)、[M158](素材/分享对话原文.md#m158)。

<a id="i04"></a>
## I04 Knowledge SQL 与受控工具

- [x] **代码已有·复用**：`asset_catalog` 已分观测/推断/确认/否定并保留证据；asset_semantics、source_contacts、角色和来源记录已有；metadata_watch 可发现结构变化并登记复核。

代码入口：[catalog](../services/catalog.py)、[治理库](../plugins/datasteward_gate/approvals.py)、[metadata_watch](../services/metadata_watch.py)。

**落实与验收**：扩展组织、能力、authority、visibility 和操作策略，保持现有事实源分层。确认写入前验证对象/资格/证据；真正冲突、互补描述及跨时期差异分别留存。验收旧确认不被模型摘要覆盖、单域确认不扩为跨域结论。

- [ ] **I04.01｜复用后补齐**〔tool+·state·grant〕（[R13](#r13)） 把正式组织知识及可结构化经验落实到 SQL Tool 的查询、写入、确认和废弃接口，而非普通 Memory。示意操作为 `query / upsert / confirm / deprecate`，不意味着模型能直接写任意 SQL。
- [ ] **I04.02｜补齐**〔state·hook〕（[R07](#r07) / [R14](#r14)） 人员、岗位、职责、ownership、语义权与能力分层；Authority 绑定 Person + Role + Domain/Asset + Authority Type。正式依据接受独立可信的 OA/IAM/HRIS/Org Directory/Authority Store、Admin 显式授予的 `Person + Scope + Authority Type`、已确认 Authority 的合法转授权；本人自述只记 self_claimed，转介只记联系人关系（[R17](#r17)），两者都不能建立正式权力。
- [ ] **I04.03｜新增**〔state〕 个人能力记录可表达 `person_id / capability / confidence / evidence_count / source / last_verified`。原文 Bob 熟悉销售库历史、confidence=0.85、evidence_count=6 是示例，不是评分标准。
- [ ] **I04.04｜补齐**〔state〕（[R08](#r08) / [R13](#r13) / [R16](#r16)） 保存 People、Roles、Capabilities、Assets、Owners、Authority、Visibility、Semantics、Relationships、Lineage、Decisions、Exceptions、Evidence 及业务流程和历史决定。下游发布资格如需登记，仅作下游交接知识，不据此把 Gold 变成 Hermes 核心交付。
- [ ] **I04.05｜新增**〔state·config〕（[R12](#r12)） 保存 copy_window、pre_execution_notice、notify roles、source constraints。窗口来自已有 Asset Policy、System/DB Owner 或 Data Owner，并保留来源/变更；提前通知可配 10m/30m/none，不每次重问已确认窗口。
- [ ] **I04.06｜补齐**〔state〕（[R13](#r13) / [R14](#r14)） 知识条目携带状态、提出/确认者、时间、来源、证据、适用资产版本/日期范围及重新验证信息；人员、schema、权限、业务流程允许过期。历史审批和 Review 保留历史。
- [ ] **I04.07｜补齐**〔skill·tool·state〕 同一问题两种说法分别留存；由模型带原文证据判断是真正矛盾、适用时期不同还是互补描述，工具保存各条证据、适用范围及判断结果，不由最近一封覆盖前一封。存在未解分歧时生成 unresolved gap 并找匹配的 authority；语义判断本身不授予确认资格。
- [ ] **I04.08｜补齐**〔hook·skill·state〕（[R06](#r06) / [R18](#r18) / [R24](#r24)） 域内 Authority 只能确认自己这侧语义；跨域 JOIN/mapping 必须由关系对象当前有效的 `cross_domain_relationship / integration authority` 确认，双方域内确认不能替代跨域确认。跨域/复杂规则/多条件/存在冲突时先记 claim/provisional understanding，由模型整理结构化规则，再发对应 Authority 明确确认；单域单一事实按 I04.09 的直接确认路径处理。代码校验确认者当前资格和对象/范围，规则与当前数据技术验证结果分开保存。冲突进入多人 Resolution Thread。
- [ ] **I04.09｜补齐**〔hook·tool·skill·state〕（[R06](#r06) / [R07](#r07) / [R18](#r18) / [R24](#r24)） confirmed 写入分两条路径：**单域 + 单一事实 + 回复者 Authority 已验证 + 无冲突**时，回复本身可直接作为确认依据；**跨域/复杂规则/多条件/存在冲突**时，先记 claim，Hermes 结构化总结后由对应 Authority 明确确认才写 confirmed。模型负责语义辨别与规则整理；两条路径都由代码校验当前 responder authority、对象/范围，并保存原文 evidence、确认依据和资格校验记录。沉默不构成确认，语义 confirmed 不产生执行 Approval Event。保留拒绝、未知、冲突、异常、否定和 deprecated 结果，避免重复试错。
- [ ] **I04.10｜补齐**〔state·hook〕（[R07](#r07) / [R08](#r08)） 转介联系人先记线索；联系人、允许来信、Visibility、语义确认权、审批权分别管理。可通过独立可信来源验证角色或资格；不让本人自证 Authority/Visibility，也不把转介文字自动变成审批批准。

- [ ] **I04.11｜新增**〔state·hook〕（[R14](#r14) / [R24](#r24)） 为同一资产同一种 Authority Type 建立当前有效审批人唯一性与 valid_from/valid_to 更换记录。例如 sales.orders 的 access_approver=Alice、semantic_approver=Bob 可并存；同一类型不能同时多位 active。跨域 JOIN/mapping 另设独立的 `cross_domain_relationship / integration authority`，挂在关系对象而不是单张表上，同一关系同一时间只有一个 active，不与域内语义权互相顶替。保留过去授权/决定历史，不能由新联系人覆盖旧证据。

来源：[M039—M050](素材/分享对话原文.md#m039)、[M071—M074](素材/分享对话原文.md#m071)、[M117—M134](素材/分享对话原文.md#m117)、[M145—M148](素材/分享对话原文.md#m145)。

<a id="i05"></a>
## I05 Memory / Session 生命周期

- [x] **代码已有·复用**：资产、语义、联系人、审批和 runs 已落库；读档返回观测时间，插件要求普通 Memory 仅保存沟通偏好等上下文。

代码入口：[catalog](../services/catalog.py)、[runs](../services/runs.py)、[插件提示](../.hermes/plugins/data-steward/__init__.py)。

**落实与验收**：补项目摘要和统一恢复契约，复用 SQL/State；Session 足够时不全量加载摘要，执行前查相关最新事实。用 C08 的“5 张/7 张”与 C05 旧字段回复验收；核验生命周期事件实际触发读取/刷新，以及动作成功后落库失败、事件重放时的恢复与幂等，不能只验同一 Session 能继续对话。

- [ ] **I05.01｜补齐**〔memory·prompt〕（[R13](#r13)） Memory 只保留 Project Summary、Person communication preference 和必要的跨 Session 工作摘要；人员能力、owner、口径、JOIN、authority 等以 SQL 为主。
- [ ] **I05.02｜新增**〔skill·memory〕（[R13](#r13)） 连续 Task Session 上下文足够时不反复读取 Project Memory。新 Session 先读 Memory Summary，再读相关 State/SQL；同 Session 长时间恢复或跨 Task 时按需要补项目背景，联系某人时按需读其沟通偏好。
- [ ] **I05.03｜接通**〔lifecycle·skill·tool·hook〕（[R13](#r13) / [R14](#r14)） 新 Session/恢复事件由代码触发读取流程，模型先用摘要定位，再用 Tool 取相关最新 Project/Asset/Task State 和 SQL Knowledge；冲突以 State/SQL 为准，必要时重写 Memory。执行前的任务有效性、Scope、Authority 与批准适用性由 I03.08/I09 的检查点重查，不以模型读过摘要作为已校验。模型按需控制读取范围，不全量塞入组织档案与全部历史。
- [ ] **I05.04｜复用后补齐**〔service·state·hook·callback〕 新表接入、owner/语义/JOIN 确认、Task、Question、approval/pending 变化由对应业务代码或独立审批 callback 当场写入 SQL/State，不等模型再调一次“记住”，也不等 Session/Cycle 收尾。同库的状态与事件尽量在同一事务提交；跨 Lake/治理库的动作保存可恢复记录，以幂等重试/reconcile 补齐。`post_tool_call` 接执行结果、状态更新与审计，但关键事实不能仅依赖可能失败或被跳过的后置观察者。模型负责提出语义理解与知识候选，确认写入仍执行 I04/I09 的资格检查。
- [ ] **I05.05｜新增**〔lifecycle·service·skill·memory〕（[R13](#r13)） Session 收尾、Cycle 收尾、重大 Mission/Objective 变化由生命周期代码登记摘要刷新任务，再由模型按 Skill 读取相关 SQL/State、生成并更新 Project Summary；刷新失败保留待办并可重试，不能只靠模型记得收尾。人员、角色、能力、资产、ownership、语义、JOIN、authority、visibility、决定、证据和各类状态按 I05.04 当场落库，摘要不是提交事实的入口。
- [ ] **I05.06｜新增**〔lifecycle·service·skill·cron〕（[R13](#r13)） 新 Session/恢复事件接入 I05.03 的读取流程；代码提供相关事实版本/更新时间，模型按需核对摘要含义，不一致则刷新 Working Context 并必要时重写摘要。后台 reconcile 补偿遗漏的落库或摘要刷新，不替代恢复检查；权限与审批有效性仍由执行检查点判定。不能按 Memory 的“5 张表、mapping 未确认”覆盖 SQL 中“7 张、已确认”。
- [ ] **I05.07｜新增**〔memory·skill〕（[R13](#r13) / [R16](#r16)） 摘要保留已完成、未解决、适用范围和证据引用；“规则 confirmed”不能压缩成“所有数据验证通过”。按 R13 刷新生命周期实现，未知、冲突和异常在摘要/报告中保持可见。

来源：[M047—M052](素材/分享对话原文.md#m047)、[M165—M168](素材/分享对话原文.md#m165)。

<a id="i06"></a>
## I06 Task Router / Resume / Communication

- [x] **代码已有·复用**：已有 Hermes 邮件入口、mail_threads 归属组件、指定联系人发信、审批恢复、按角色 WIP 与定时催办。当前正式入口仍以发件人构造会话，归属组件不能视为 Task Router 已接通。

代码入口：[mail_threads](../services/mail_threads.py)、[联系人工具](../.hermes/plugins/data-steward/tools.py)、[门禁](../plugins/datasteward_gate/__init__.py)、[cron](../.hermes/plugins/data-steward/cron.py)。

**落实与验收**：把 Task/Question/Message-ID 绑定接进正式出入站和 Session 分发；以消息级幂等取代仅按相同主题去重的不足。通过真实邮件入口验收精确回信、无头转发、混答、一条回答关联多任务、同人多角色、静默讨论及迟到回答，不能只直接调用 Router 工具测试；模型匹配不改变 authority。

- [ ] **I06.01｜接通**〔adapter·service·state〕（[R09](#r09)） 前台保持统一 Hermes 入口，后台把实际入站消息归属接入 Hermes 的 Task Session 分发，从 Person Session 转为 Task Session；Question 为 Task 内部子项，不单独等同 Session。一个 Task 可关联多个一对一线程或多人 Resolution Thread，任务连续性由 State 保存。优先通过邮件适配、插件与 Session 扩展接通；核验扩展确实不足时才做 Hermes 本体最小修改，并记录原因、兼容影响和验证结果。新增模型工具不能替代真实入口接通。
- [ ] **I06.02｜接通**〔adapter·service·tool·state〕（[R09](#r09)） 扩展现有受控发信工具，并在真实出站路径绑定 Task、Question、Thread 和邮件 Message-ID，持久化发送结果供入站回信归属；正文自然提醒“请直接回复这封邮件”，不让用户记内部编号。发送动作仍经过现有授权与收件人披露检查。
- [ ] **I06.03｜接通**〔adapter·service·skill·state〕（[R09](#r09)） 由真实邮件入站路径调用归属模块：原生 In-Reply-To/References/thread → 明确 question/task 标识 → person + open questions → 语义匹配 → unresolved inbox。确定性代码处理精确标识、候选查询、消息幂等和分发；语义匹配与混答拆分由模型按 I06.04/I06.05 处理。精确命中线程仍需检查是否包含多个主题；结果写 State 后分发到相关 Task，不依赖模型先主动调用 Router 工具。
- [ ] **I06.04｜新增**〔tool·skill·config〕（[R09](#r09)） 模型 Message Router 默认仅读取 Task Goal/Summary、Open Questions、最近 3 条消息和 Incoming Message；低置信再扩大相关上下文。无独立长期业务记忆，不承担审批或 confirmed 写入资格。
- [ ] **I06.05｜新增**〔skill·tool·config〕（[R09](#r09)） 混答拆 Answer Units，保留原文出处；支持一信多问题/多 Task、一回答关联多 Task、一个 Task 等多人。低置信扩大上下文后仍不明确则留 unresolved/复核，不自动关闭任务；具体置信算法属于实现配置。
- [ ] **I06.06｜新增**〔skill·eval〕（[R09](#r09)） 示例必须能拆开：“金额已扣折扣”→语义；“历史 ID 不确定，问 MDM”→未解决并新增线索；“退款归 Alice”→候选联系人且查 authority。不能整封信投给一个 Task 后把三项都完成。
- [ ] **I06.07｜补齐**〔hook·state·config〕（[R10](#r10)） 对实际每个人设置未解问题硬上限（如 max_open_questions=3），超额入 Queued，结束/撤销释放。上限不是目标，答完一个不能立刻自动补**新主题**问题；配额释放不绕过本批默认处理窗口。为关闭当前 `question_id / task_id` 所必需的 clarification 可立即追问（[R20](#r20)），仍占用硬上限。
- [ ] **I06.08｜新增**〔skill〕 任务选择结合 Objective 相关性、可执行性、解除其他 blocker 的价值与成本；低优先级源整改靠后。等待 A 不阻塞 B/C，但任何任务仍受 Cycle 和授权范围约束。
- [ ] **I06.09｜补齐**〔hook·cron·config〕（[R10](#r10) / [R20](#r20) / [R22](#r22)） 每批沟通发出后等待默认处理窗口（如 2–3 个 working day），窗口结束才安排下一批/reminder/escalation；窗口、催办与 escalation 一律按企业工作日历计算，不用自然日。新主题问题必须等窗口，clarification 追问不受窗口限制。仅严重阻塞或 Sponsor/Boss 明确催办允许提前追加新主题，仍受问题硬上限。Finance 三个 working day 无回复升级 Boss 保留为场景；一次不回不永久贴慢响应标签。
- [ ] **I06.10｜补齐**〔tool·hook〕（[R07](#r07) / [R08](#r08)） 升级查询真正匹配的上级、Domain/System Owner、Semantic Authority；本人“问我上级”是 routing evidence，不是自证资格。独立可信的 Sponsor/Boss 明确指定或合法转授权可建立相应 Authority 依据；Visibility 未知同样找独立来源验证。
- [ ] **I06.11｜新增**〔tool·hook〕（[R06](#r06) / [R08](#r08) / [R09](#r09) / [R24](#r24)） 跨域分歧可创建 Resolution Task Session，组织双方域内 Authority 讨论各自语义，并由对应跨域 Authority 确认关系规则；发送前校验所有收件人可见范围，原有 Task 保留依赖，不因转发而关闭。
- [ ] **I06.12｜新增**〔skill·state〕（[R10](#r10)） 多人 Response Policy 保留 SILENT / INTERVENE / WAIT_AND_SUMMARIZE：默认后台提取 claim/evidence/conflict 并静默；仅讨论跑偏、关键冲突、缺 Authority、超时、已形成结论需总结确认时发言。
- [ ] **I06.13｜新增**〔cron·config·state〕（[R10](#r10)） 支持 quiet window/debounce 合并总结，30 分钟仅为原文示例。每封消息可触发接收和路由，不能等同完整模型执行或必发回信；性能收益需后续实测。
- [ ] **I06.14｜补齐**〔hook·skill〕（[R06](#r06) / [R10](#r10)） 多人讨论后先由 Hermes 整理结构化规则，再发给有 Authority 的人正式确认。无人反对、unless misunderstood 或普通讨论内容不直接产生 confirmed/Approval Event；一次简单收到也不为每个后台 Task 各回一封。

- [ ] **I06.15｜新增**〔state·hook·config〕（[R10](#r10)） 将每人问题数、处理窗口与 Cycle 分别建模：答复释放配额不立即发送新问题，严重阻塞/明确催办提前追加也不突破硬上限或 Cycle Stop。报告按人展示 completed、pending、queued、blocked_by，保存批次发送时间和窗口结束时间。

来源：[M053—M066](素材/分享对话原文.md#m053)、[M089—M096](素材/分享对话原文.md#m089)、[M109—M110](素材/分享对话原文.md#m109)、[M121—M132](素材/分享对话原文.md#m121)、[M169—M170](素材/分享对话原文.md#m169)。

<a id="i07"></a>
## I07 复制排期与资产新增事件

- [x] **代码已有·复用**：已有 Bronze 同步、增量/全量策略、schema 漂移检查、同步台账、湖内关联技术证据；Connector 有 bulk 时间窗函数。实际 sync_table 的 Trino 复制尚未接窗口准入。

代码入口：[sync](../services/sync.py)、[Connector](../services/connector.py)、[linkage](../services/linkage.py)。

**落实与验收**：约束必须覆盖实际复制路径，未持久化排期就不能回复“已排队”。补窗口/预检/通知状态、新资产评估事件与 mapping 验收；验证窗口外拒绝、窗口内可达、重复事件不重开，以及异常子集未被丢弃。

- [ ] **I07.01｜接通**〔hook·sqlgate·grant〕（[R11](#r11) / [R12](#r12) / [R23](#r23)） 源访问只读且受 Scope、超时、负载、返回/扫描量和执行窗口控制。`metadata_visibility` 覆盖表存在、schema、类型、大小估算等最小 metadata；sample、扫描与 Bronze copy 须另有 `data access / ingestion approval`，低级权限不自动升级。源端仅安全接入必须的 metadata/schema/数量估算/主键时间字段/可访问性；深度画像、质量、JOIN、清洗在 Lake。后续仅 Freshness、Schema drift、明显复制异常受控回源；普通同步不重复源端深度扫描。
- [ ] **I07.02｜接通**〔callback·hook·state〕（[R05](#r05) / [R12](#r12) / [R14](#r14)） 白天取得绑定具体资产/动作/参数/范围的正式 Approval Event，记录窗口并进入 approved_waiting_window。到窗口复查批准、权限、schema、源健康及维护冲突；Cycle 已停且任务尚未开始时保持暂停，不能只凭旧排期自动执行。
- [ ] **I07.03｜新增**〔callback·config·state〕（[R12](#r12) / [R27](#r27)） Execution Window 由有资格的取数/System Expert 在审批时以下拉或配置设定，或取已有 Asset Policy 既有值；Hermes 可以提示「可能存在源端负载风险」，但不自拟 `execution window / copy mode / load policy`，只负责遵守与调度。通知发受执行影响的 System/DB Owner，必要时 Data Owner，不默认 Sponsor；A 兼任数据/系统负责人时可只发 A。提前量配置 10m/30m/none，21:50→22:00 只是示例。
- [ ] **I07.04｜补齐**〔hook·tool〕（[R12](#r12)） 执行通知不是再次审批；在 Cycle 允许、Approval 有效、Window 有效、Pre-check 通过时，即使通知无人回复也可执行。预检失败不执行；异常负载停止能力需真实验证，不能仅靠通知文字承诺。
- [ ] **I07.05｜接通**〔service·state·hook〕（[R05](#r05) / [R15](#r15)） 在实际 Bronze 复制成功路径由业务模块写 Asset State 并幂等登记 assessment_required，持久化与补偿按 I05.04 执行；Cycle 活跃才可开始湖内画像/评估。已 Running 动作跨过停止线时允许完成当前动作并写结果，但不启动后续 Session。复制失败、重复事件、窗口错过、暂停与恢复保持状态一致。
- [ ] **I07.06｜补齐**〔tool·sqlgate〕（[R16](#r16)） 关联验证检查类型/格式、值域重叠、唯一性、unmatched、cardinality；mapping 额外检查 effective dates、一对多、重复和空值。区分直接关联规则与历史 mapping 规则。
- [ ] **I07.07｜补齐**〔tool·state〕（[R16](#r16)） 无歧义子集与异常子集分别保留，记录如 99.2% 覆盖/0.8% 未映射的口径、版本和 Evidence；不能自动丢弃异常或猜测补齐。未解决事项记 Exception/Unknown/Conflict，核心 Silver/Reusable Lake Assets 说明适用范围与已知限制；最终 Gold/数据产品业务验收和 100% 正确性责任交下游。

来源：[M097—M106](素材/分享对话原文.md#m097)、[M113—M134](素材/分享对话原文.md#m113)、[M159—M162](素材/分享对话原文.md#m159)。

<a id="i08"></a>
## I08 Cycle Hook / Report / Trust

- [x] **代码已有·复用**：已有 post_llm_call 用量记录、每日预算检查、周报、阶段提案、ROUND_CLOSED 暂停与恢复 monitor。stage-report 的终态/默认 15 天报告不等同独立 Cycle，且不在当前插件 JOBS 中。

代码入口：[门禁](../plugins/datasteward_gate/__init__.py)、[stage-report](../ops/stage-report.py)、[cron](../.hermes/plugins/data-steward/cron.py)。

**落实与验收**：新增持久化 Cycle 和到线报告转换；保留独立动作审批。正式接通截止时间事件与工具准入、报告预留及入站归档。验收到线后确有报告、不新开分支，且 pending/ready/scheduled 均能下一轮正确恢复。

- [ ] **I08.01｜新增**〔service·state·config·hook·cron〕（[R04](#r04) / [R21](#r21) / [R22](#r22)） 在既有 Harness 接入持久化 Cycle：cycle_id、start_time、time_budget、token_budget、trust_level、status、report_budget。Cycle 计量、状态转换与恢复由业务模块实现，复用 `post_llm_call` 用量事件、工具准入和定时触发；扩展接口不足时才做 Hermes 本体最小修改，并记录原因、兼容影响和验证结果。计时按墙钟推进、等人也计时，但预算以 working day / business hours 为单位，只累计企业工作日历内的时间；waiting task 的累计等待跨 Cycle 保留；Time Budget 或 Token Budget 先到触发 Stop，预留报告预算。每日用量/周报/Bronze 报告不替代 Cycle；计量和重启恢复必须可追溯。
- [ ] **I08.02｜接通**〔hook·cron〕（[R04](#r04) / [R05](#r05)） 到线立即进入报告并禁止新任务/新分支；报告不等待 pending 解决、在途动作完成或周一作业。已经 Running 的当前动作可完成并写 State/Result，但不能触发下一步；尚未开始的一律暂停下一 Cycle。
- [ ] **I08.03｜复用后补齐**〔tool·hook·skill〕 报告从 State、执行与知识记录取事实，再由模型生成简短解释和建议；数字、完成状态、确认状态、排期和未决项不得由模型补编。
- [ ] **I08.04｜补齐**〔tool·hook〕（[R04](#r04) / [R10](#r10)） 报告列 Goal、Completed、New Findings、Waiting/Pending、Blocked、Scheduled、Ready but paused、Unresolved/Risks、Recommended Next，附预算与上轮意见；显性展示每人本周期完成、当前 Pending、Queued、Blocked by。Running 单独如实标出，不伪装已经完成。
- [ ] **I08.05｜新增**〔hook·state〕（[R04](#r04)） Sponsor 明确决定下一轮方向、Scope 和周期长短；Hermes 提出 Trust 升降建议。Trust 只改变 Cycle 长度，不扩大 Tool、数据、Scope 或审批权；Scope/预算另行持久化，不因信任提高自动放宽。
- [ ] **I08.06｜新增**〔state·config·hook〕（[R04](#r04) / [R21](#r21)） 支持渐进周期：0.5—1 个 working day →2—3 个 working day →5—7 个 working day，也可降级；每次到线必须先报告并由 Admin/Sponsor 放行才有下一轮；Hermes 根据误判、返工、漏报、偏移等提出调整，必须由 Sponsor 明确确认。示例不是硬编码升级次数或默认自动晋级。
- [ ] **I08.07｜新增**〔hook·state〕（[R05](#r05)） Cycle Stop 保留 waiting/scheduled/ready/unresolved，入站继续接收和记录。未开始的已批准夜间任务也暂停到下一 Cycle；Running 只完成当前动作写 State/Result 后停，不启动画像/评估等后续。到线报告先发，不能为收尾延迟。
- [ ] **I08.08｜接通**〔hook·state·policy〕（[R02](#r02) / [R03](#r03) / [R26](#r26)） Phase 与 Cycle checkpoint 分开：Sponsor 批准当前 Scope 开始 Silver，各 Asset/Task 满足成熟条件后独立推进；Scope 实质扩大重批阶段门。首次 Silver 准入同时检查 I01.03 的 Mission Refinement 完成记录。具体 transformation 规则与首次/重大变化复核分别执行，不能被阶段门或 Cycle 预算替代；Gold 不作为核心 checkpoint。

- [ ] **I08.09｜接通**〔service·cron·hook·eval〕（既有纯函数未接线） 把停止点判据接进运行路径：[`stop_points`](../services/stop_points.py) 的 5 个停止点与 3 类卡点已是纯函数、可离线判分，但自 R4 写成后**只有测试引用，没有任何运行路径调用**。要让「信息边界到了、该交阶段成果」与 I08.02 的「预算到线」同为停止与唤醒事件，两者都得有，不能互相顶替。判据留在确定性代码里；模型只负责下一步做什么、问谁，**不负责判断 transition** —— 把 transition 交给模型等于把约束交给模型。 **2026-09-15 已接线**：monitor 增 `_observe()` 从治理库组装事实并调 `next_stop()`，报 `kind="stop_point"/"blocker"`；三态（查不出来写 `None`，不写 False）由 `unevaluable()` 守着。`priority_confirmed` / `sample_reviewed`仍无事实来源，对应停止点判不了，测试盯着这份清单。见 `tests/test_stop_point_wiring.py`。模型被唤醒后是否真去交成果，待真实验收。

来源：[M005—M019](素材/分享对话原文.md#m005)、[M135—M136](素材/分享对话原文.md#m135)、[M159—M162](素材/分享对话原文.md#m159)。

<a id="i09"></a>
## I09 Authority / Visibility 与已有 Guardrails

- [x] **代码已有·复用**：已有 L0—L4 策略、签名票据、独立 callback、受控批准参数回放、SQL AST/源湖查询边界、分类/读权限、角色解析与执行审计。

代码入口：[policy](../plugins/datasteward_gate/policy.py)、[门禁](../plugins/datasteward_gate/__init__.py)、[callback](../services/approval_callback.py)、[policy_sync](../services/policy_sync.py)。

**落实与验收**：保留执行保护，前置组织目录的 authority/visibility 契约；当前 callback 校验票据不等于复核点击人的现行资产范围资格。验收无资格线索、转介、角色变化、多收件人披露及有效审批正对照。

- [ ] **I09.01｜新增**〔hook·state〕（[R08](#r08) / [R23](#r23)） Subject Visibility 覆盖请求者、发送者及所有收件人，区分存在性、表名、schema、质量摘要、sample、aggregate、raw data；对内的源端访问同样按 `metadata_visibility` 与 `data access / ingestion approval` 两级判定。Hermes 可读不代表可以转发。
- [ ] **I09.02｜补齐**〔callback·hook·tool〕（[R06](#r06) / [R07](#r07) / [R14](#r14) / [R18](#r18) / [R24](#r24)） 按 asset access、semantic、JOIN、business rule 等 Authority Type 找负责人；回复/正式确认/点击时检查人的当前资格、对象/范围，覆盖 I04.09 的直接确认和结构化再确认两条路径。一个资产同一种 Authority Type 仅一个 active approver；跨域双方只确认各自域内语义，JOIN/mapping 另查关系对象当前有效的跨域 Authority 并留确认与资格证据。
- [ ] **I09.03｜补齐**〔state·tool+〕（[R07](#r07) / [R08](#r08)） Demo 提供受控人员/部门/岗位/manager/在职/角色/visibility/authority/reporting line，可预置小型目录；也支持已确认上级/Admin、Sponsor/Boss 的明确指定和合法转授权作为独立来源。真实 OA/IAM/HRIS 是部署适配，不必先接入；本人自述不赋权。
- [ ] **I09.04｜补齐**〔tool+·hook·policy〕（[R07](#r07) / [R08](#r08)） 复用工具抽象 find_person/find_role_owner/check_authority/check_visibility/find_approver/start_approval，并增加独立可信来源核验路径与 `metadata_visibility` / `data access` 两级判定（[R23](#r23)）。请求人 Visibility 未知时查已有 Authority、找其上级/Domain Admin/HR Admin 等确认；不能让本人自证，也不能从普通职位名称自动推导数据权。
- [ ] **I09.05｜复用并核验接通**〔hook·sqlgate·config〕（[R12](#r12)） 复用现有工具授权、SQL AST、table scope、read/write、row/scan limit、timeout、sensitive columns、estimated impact、审批与审计，不按原文临时四档例子重排现有等级。
- [ ] **I09.06｜新增**〔hook·tool〕（[R08](#r08)） 发信内容先经过收件人披露检查：表名、字段、质量问题和受限域的存在也可能不可说。连「存在一个受限的员工/Finance 数据域」这类抽象描述能否披露，也受 visibility 控制。
- [ ] **I09.07｜新增**〔hook·skill〕（[R08](#r08)） scope candidate 先查 Hermes 元数据可见性，再查 Sponsor 可见性，再问是否扩展；Scope 批准不代替接入批准。分析问题转交或说明治理准备度时同样先过此检查。
- [ ] **I09.08｜补齐**〔state·callback·hook〕（[R07](#r07)） 知情人的数据线索、本人 Authority 自述、独立可信指定和具体执行 Approval Event 分开保存。D 懂成本表但无批准权时只能提供候选；有有效对应资格的 E 的正式 Approval Event 才改变 access state。
- [ ] **I09.09｜复用后补齐**〔hook·state〕 记录重要动作的发起者、action、target、reason、policy decision、approval、execution result、timestamp；能追溯为何读表、为何联系人、谁批准变换、为何 confirmed。
- [ ] **I09.10｜复用后补齐**〔hook·grant·policy〕（[R06](#r06) / [R16](#r16)） 源只读、敏感权限变化和知识 confirmed 边界继续由治理策略执行；改变 Mission 不放开源写入。保留已有发布保护供兼容/下游使用，但不因工具存在让 Hermes 承担 Gold、Mart、KPI、Dashboard 或最终数据产品验收。

来源：[M067—M076](素材/分享对话原文.md#m067)、[M139—M154](素材/分享对话原文.md#m139)、[M164](素材/分享对话原文.md#m164)。

<a id="i10"></a>
## I10 复用 Eval，补行为覆盖

- [x] **代码已有·复用**：现有 Eval 具备机制/行为/真实模型入口、正负配对、INCONCLUSIVE、去护栏校准与原始记录离线重判；本次已完成 I01—I10 的代码映射。

代码入口：[行为 Eval](../evals/behavior/README.md)、[驱动](../tests/run_behavior_case.py)、[代码对照](4_当前代码对照.md)。

**落实与验收**：沿现有框架补 Snapshot/场景和终态判据；walkthrough 原邮件只是模拟素材，转成正式测试时继续区分 state、当前业务邮件与评估元数据。运行真实服务/模型前按已审机制确定边界，不将历史测试数或模拟百分比作为新验收结果。

- [ ] **I10.01｜扩展覆盖**〔eval〕（[R02](#r02) / [R03](#r03) / [R05](#r05) / [R09](#r09) / [R10](#r10) / [R15](#r15) / [R16](#r16)） 将 Case 主线映射现有 Eval：零资产（N01）、转介（N02）、双线并行与配额（N03）、白天审批夜间复制（N04—N05）、湖内画像（N06）、沟通窗口与三个 working day 升级（N07）、换人后接入（N08）、新资产评估（N09）、技术证据强仍分域确认（N10）、authority 升级与证据搜寻（N11）、多方冲突与静默（N12—N13）、分时期规则确认（N14）、mapping 异常（N15）、立即报告（N16）。补 Scope 阶段门与 Asset 独立成熟、首轮/重大 transformation 复核；不以 Gold 或异常清零作为核心成功终态。
- [ ] **I10.02｜扩展覆盖**〔eval〕 将[9 个补充场景](3_Case_walkthrough.md#extra)转成后续最小行为检查：误导授权、单边确认、敏感 scope 扩展、语义分歧、延迟恢复、到线停、分析越界、过期 Memory、多问题回复。
- [ ] **I10.03｜扩展覆盖**〔eval〕（[R07](#r07) / [R08](#r08) / [R09](#r09) / [R10](#r10) / [R14](#r14) / [R18](#r18) / [R24](#r24) / [R26](#r26)） 检验自述不赋权、独立可信指定、同资产同 Authority Type 单一 active、角色有效期与旧批准恢复；覆盖单域单一事实直接 confirmed 的有效/无资格对照，以及双方域内确认不能替代跨域 Authority 的关系确认。检验最近 3 条默认 Router 上下文及低置信扩读、child task/backlog、Done Criteria、不自动补满问题、处理窗口、Visibility、Cycle 真停止与跨 Session 最新事实；覆盖每 Cycle 的 Mission 补齐记录及首次 Silver 前的 Refinement 准入。
- [ ] **I10.04｜复用并实测**〔eval〕 保留正负对照与环境终态证据；设计走读不写成真实运行通过，纸面数值不写成测量结果。原文中“场景算通过”仅表示假设流程走通。
- [ ] **I10.05｜复用并持续核对**〔eval〕 对已有工具、State、Harness、Eval 先做映射清点，再开发缺口。这里不改变现有评估框架，也不因原文提到 LangGraph 就迁移 Runtime。

- [ ] **I10.06｜扩展覆盖**〔eval〕（[R05](#r05) / [R12](#r12)） 同时验证：Cycle 活跃且通知无回复，但 Approval/Window/Pre-check 均有效时复制可执行；Cycle 已停止且 copy 尚未开始时必须暂停；到线已有 Running 动作先报告、该动作完成落结果后无后续分支。

来源：[M079—M086](素材/分享对话原文.md#m079)、[M137—M170](素材/分享对话原文.md#m137)。
