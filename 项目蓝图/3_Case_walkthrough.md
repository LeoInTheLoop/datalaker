# 3 Case walkthrough

## 走读边界

以下假设设计已实现，以“系统会怎样工作”组织场景，不是实际测试记录。每个节点同时保留触发、问答、后台状态、阻碍、停点，以及原模拟邮件/回复的完整正文；可沿来源回查[分享对话原文](素材/分享对话原文.md)。原文跳过或未收到的回复不补成成功。

2026-09-14 更新：邮件正文保持原语言、主题、数字、字段名、称呼和措辞；早期版本、续写版本及有机制冲突的邮件完整保留，并在正文外注明适用关系。原文只有一句回复、状态或用户推进条件时按原样展示，不补造邮件头、批准链接或真实执行记录。写作块 ID 是原对话的文本编号，不是邮件系统的 Message-ID。版本差异不代表实际要重复发送；机制已由用户最终决定，统一见[R01—R16 最终决定](2_实现方法_checklist.md#review)，完整邮件可从[文末版本索引](#mail-index)定位。

**最终机制覆盖说明**：原邮件/现场摘录原样留存；当前节点说明按最新决定更新。Sponsor 阶段门针对 Scope，Asset 各自成熟后进入 Silver；Gold 非核心产物。Cycle Stop 立即报告，Running 仅完成当前动作，未开始的 scheduled copy 也暂停。角色不能本人自证，同资产同 Authority Type 只有一个当前有效审批人；讨论须先整理规则再正式确认。每人问题上限不是目标，发问还受默认沟通窗口限制。

2026-09-14 二轮：走读冲突已由 [R17—R28](2_实现方法_checklist.md#review2) 收口，本份据此改了六处节点说明——转介不赋权（N04、N08）、第一张表同样登记评估（N06）、clarification 追问不受批次窗口（N11、C02）、跨域关系另有 integration authority（N10—N14、C02）、主线显式 Cycle 边界与 working day 口径（下表、N07）、报告字段补齐（N16）。原模拟邮件仍不改。

模拟世界可以预设人员、表、错误关联和权限，但 Hermes 只知道在当前节点实际收到或获准查询的信息。整个主线跨五个 Cycle，边界见下表；省略的模拟时间不等于自动授权。

来源：[M083—M090](素材/分享对话原文.md#m083)、[M109](素材/分享对话原文.md#m109)、[M135—M136](素材/分享对话原文.md#m135)。

<a id="main"></a>
## 主线：从零资产到 Sales / Finance / 历史映射 review

### 人物与初始条件

| 人物 | 当前所知身份 | 不能直接推定的资格 |
|---|---|---|
| Sponsor | 原发起人，提供目标与入口，负责范围/优先级校准 | 不自动拥有所有数据域 visibility 或访问审批权 |
| Boss | 已确认的组织联系人，可转介 | 转介只建立联系人关系（[R17](2_实现方法_checklist.md#r17)）；其指定要成为 Authority 来源，必须由 Admin 落成显式授予记录并绑定 Person、Scope、Authority Type |
| A | Sales 联系人；经 Authority Store / Admin 显式授予记录查到兼任 `sales.orders` 的 access_approver 与 System Owner | Boss 的转介本身不构成这个资格；N04 前必须查到显式授予记录，查不到就走 C01 的路径 |
| B | 最初 Finance 联系人，三个 working day 没有回复 | 未回复不能视为批准或永久判定其能力低 |
| F | Boss 后来介绍的 Finance 替代联系人 | 接入审批资格同样要另行查到，介绍不赋权；原文后续多人邮件又用 B，本文用 F 区分，避免把旧联系人当成新人 |
| C | 后来查到的 Customer/MDM authority，持独立的 `cross_domain_relationship / integration authority`（[R24](2_实现方法_checklist.md#r24)） | 其确认仍受领域、对象、适用时期和正式政策限制；Sales 侧、Finance 侧的域内语义仍归 A、F |

开始时 Hermes 只认识 Sponsor，资产数为 0。业务方向是建立可信的客户收入数据基础；不在这个 Case 中执行客户利润排名等分析。

### 主线的 Cycle 边界

按 [R21](2_实现方法_checklist.md#r21)，主线显式分成五个 Cycle：每次到线先报告、由 Admin/Sponsor 放行才有下一轮，未开始的任务一律暂停到下一轮。长度单位为 working day（wd，[R22](2_实现方法_checklist.md#r22)）；waiting task 的累计等待跨 Cycle 保留，不因换轮清零。

| Cycle | 长度 | 覆盖节点 | 到线时的关键状态 | 放行 |
|---|---|---|---|---|
| CY-1 | 1 wd | N01—N04 | Sales copy 已批准、处于 `approved_waiting_window` 但**尚未开始**，按 R05 暂停；Finance 等 B 回复（累计 1 wd） | 报告 → Sponsor 放行 |
| CY-2 | 1 wd | N05—N06 | 恢复排期后夜间复制、湖内画像完成；负金额语义问题已发出待答；Finance 等待累计 2 wd | 报告 → Sponsor 放行 |
| CY-3 | 2—3 wd | N07—N09 | B 累计 3 wd 无回复触发升级；换人后 Finance 入 Bronze；两次 Integration Assessment 均已登记 | 报告 → Sponsor 放行 |
| CY-4 | 2—3 wd | N10—N14 | 跨域 customer_id 分时期规则由 integration authority 确认 | 报告 → Sponsor 放行 |
| CY-5 | 5—7 wd | N15—N16 | mapping 入湖与关联验证；周级 review | 到线立即报告，等下一轮决定 |

Trust 升到 5—7 wd 是经过前四轮 review 的结果，不是初始授权。CY-1 到线时那次「已批准但未开始的夜间 copy 必须暂停」与 [C06](#case06) 的 T2 是同一条规则，主线在此显式走一遍。

<a id="n01"></a>
### N01 入场：一封邮件取得起点

- **系统问**：自我介绍，说明会协助数据、负责人、定义和质量确认；简述当前目标，只问“先找谁，或从哪套系统/表开始？”提醒直接回复原邮件。
- **人答**：Sponsor 提供 Boss 邮箱，提到 Finance 与 Sales 两类数据。
- **后台**：保留 Sponsor 线程，登记 Boss 联系线索、两条 candidate asset/system 线索。并未获知真实表名、连接或批准。
- **可能阻碍**：目标含糊、只知道人不知道表、只有系统名；逐步澄清，不一次发 North Star/消费者/成功标准/禁区问卷。

来源：[M087—M092](素材/分享对话原文.md#m087)。

#### 原模拟邮件、回复与现场记录

<a id="mail-m090-e01"></a>
**原模拟邮件｜Hermes → Sponsor；原文写作块 ID：73142**（[M090](素材/分享对话原文.md#m090)）

<!-- original-excerpt:excerpt-001 -->
```text
Subject: Getting started

Hi,

I’m Hermes, the data steward helping move this work forward. I’ll help identify the relevant data, people, definitions, and data-quality issues, and keep track of what still needs confirmation.

My current understanding is that we want to make the customer revenue data more reliable and easier to use.

To get started, who would you recommend I speak with first, or which system/table should I look at first?

Please reply directly to this email so I can keep the context together.

Best,  
Hermes
```

**Sponsor 的模拟回复原话；未提供完整邮件头**（[M091](素材/分享对话原文.md#m091)）

<!-- original-excerpt:excerpt-002 -->
```text
然后我说boss邮箱xx 然后手头我知道财务表和销售表。分别怎么连接
```

<details>
<summary>早期版本：M088 的两封首轮问卷（已被 M089/M090 修正，不安排发送）</summary>

<a id="mail-m088-e01"></a>
**早期首轮邮件 1｜Hermes → Sponsor；原文写作块 ID：原文未提供**（[M088](素材/分享对话原文.md#m088)）

<!-- original-excerpt:excerpt-003 -->
```text
Subject: Confirming the goal and scope

Hi, I want to make sure I’m working toward the right outcome.

My current understanding is that the goal is to build a reliable data foundation for customer revenue analysis.

Could you confirm:
1. Who will mainly use this data?
2. What would make you consider this work successful?
3. Is there anything you explicitly do not want me to cover at this stage?
```

<a id="mail-m088-e02"></a>
**早期首轮邮件 2｜Hermes → Sponsor；原文写作块 ID：原文未提供**（[M088](素材/分享对话原文.md#m088)）

<!-- original-excerpt:excerpt-004 -->
```text
Subject: Where should I start with the data?

To get started, could you point me to either:

- the system/table that is closest to customer orders or revenue, or
- the person who knows this data best?

I only need one starting point for now.
```

</details>

<a id="n02"></a>
### N02 转介：回 Sponsor，同时联系 Boss

- **系统发两份**：给 Sponsor 简短确认会联系 Boss；给 Boss 自我介绍、说明转介来源，询问 Sales/Finance 的入口与负责人。
- **人答**：Boss 将两条线分别交给 A、B。
- **后台**：Boss 新 communication thread 绑定 Discovery Task；Sponsor 的旧线程继续保留，不能把 Boss 的回复混作 Sponsor 的范围决定。
- **可能阻碍**：Boss 自己不懂表，或联系人权限不明；先登记路由线索，查目录再决定访问与确认资格。

来源：[M093—M096](素材/分享对话原文.md#m093)。

#### 原模拟邮件、回复与现场记录

<a id="mail-m094-e01"></a>
**原模拟邮件｜Hermes → Sponsor；原文写作块 ID：51428**（[M094](素材/分享对话原文.md#m094)）

<!-- original-excerpt:excerpt-005 -->
```text
Subject: Thanks — I’ll start from there

Thanks, that gives me a good starting point.

I’ll contact Boss and start from the sales and finance data you mentioned. I’ll come back to you if I need a decision on scope or priorities.

Best,  
Hermes
```

<a id="mail-m094-e02"></a>
**原模拟邮件｜Hermes → Boss；原文写作块 ID：68341**（[M094](素材/分享对话原文.md#m094)）

<!-- original-excerpt:excerpt-006 -->
```text
Subject: Getting started with the sales and finance data

Hi,

I’m Hermes, the data steward helping with this project. Sponsor suggested I contact you and mentioned that the sales and finance data are good places to start.

Could you point me to the relevant tables or systems, and let me know who is responsible for them if that is someone else?

Please reply directly to this email so I can keep the context together.

Best,  
Hermes
```

**用户给出的推进条件；Boss 的具体转介邮件未展开**（[M095](素材/分享对话原文.md#m095)）

<!-- original-excerpt:excerpt-007 -->
```text
好boss顺利分下去了
```

<details>
<summary>过渡版本：M092 只写给 Boss 的邮件（后改成 M094 两份，不额外发送）</summary>

<a id="mail-m092-e01"></a>
**过渡版本邮件｜Hermes → Boss；原文写作块 ID：84217**（[M092](素材/分享对话原文.md#m092)）

<!-- original-excerpt:excerpt-008 -->
```text
Subject: Sales and finance data

Thanks, that gives me a good starting point.

I’ll start with the sales and finance data you mentioned. Who owns these two datasets, or who should I contact if I need access or clarification about their fields?

Please reply directly to this email so I can keep the context together.

Best,  
Hermes
```

</details>

<a id="n03"></a>
### N03 分成两个 Task Session

- **系统问**：分别给 A、B 一封简短邮件，询问本域主要表/系统入口。
- **人答**：A 先回复；B 暂无回复。
- **后台**：Sales discovery 与 Finance discovery 分别管理 Question、Thread、等待和 next_action。谁先回复就推进谁。
- **可能阻碍**：每人配额满则入 Queued；配额不是目标，答完也不自动补满。每批默认等待处理窗口，窗口结束才下一批/提醒/升级；仅严重阻塞或 Sponsor/Boss 明确催办可提前，仍受硬上限。Finance 授权不继承 Sales；`metadata_visibility` 级别的最小源端检查（表存在、schema、类型、大小估算）可在 Cycle 内独立推进，sample、扫描与 Bronze copy 须另有 `data access / ingestion approval`（[R23](2_实现方法_checklist.md#r23)）。

来源：[M092](素材/分享对话原文.md#m092)、[M095—M096](素材/分享对话原文.md#m095)。

#### 原模拟邮件、回复与现场记录

<a id="mail-m096-e01"></a>
**原模拟邮件｜Hermes → A / Sales；原文写作块 ID：41257**（[M096](素材/分享对话原文.md#m096)）

<!-- original-excerpt:excerpt-009 -->
```text
Subject: Sales data

Hi,

I’m Hermes, the data steward helping with this project. Boss suggested I contact you about the sales data.

Could you point me to the main sales table or system I should start with?

Please reply directly to this email so I can keep the context together.

Best,  
Hermes
```

<a id="mail-m096-e02"></a>
**原模拟邮件｜Hermes → B / 最初 Finance 联系人；原文写作块 ID：63814**（[M096](素材/分享对话原文.md#m096)）

<!-- original-excerpt:excerpt-010 -->
```text
Subject: Finance data

Hi,

I’m Hermes, the data steward helping with this project. Boss suggested I contact you about the finance data.

Could you point me to the main finance table or system I should start with?

Please reply directly to this email so I can keep the context together.

Best,  
Hermes
```

<details>
<summary>同一节点的后续写法：M108 Finance 初信（不与 M096 重复发送）</summary>

<a id="mail-m108-e02"></a>
**Finance 初信续写版本｜Hermes → B / 最初 Finance 联系人；原文写作块 ID：36482**（[M108](素材/分享对话原文.md#m108)）

<!-- original-excerpt:excerpt-011 -->
```text
Subject: Finance data

Hi B,

I’m Hermes, the data steward helping with this project. Boss suggested I contact you about the finance data.

Could you point me to the main finance table or system I should start with?

Please reply directly to this email so I can keep the context together.

Best,  
Hermes
```

</details>

<a id="n04"></a>
### N04 A 要求晚上复制：白天先审批

- **人说**：“没什么，你连就行；表太大，copy 放晚上。”
- **系统动作**：拆成数据线索、访问意向、运维约束。Boss 的转介只建立联系人关系，**不构成 A 的审批资格**（[R17](2_实现方法_checklist.md#r17)）；先在 Authority Store / Admin 显式授予记录里查到 A 对 `sales.orders` 持 access_approver 与 System Owner 资格，白天完成正式审批，再排期。查不到就走与 [C01](#case01) 相同的路径找真正的审批人，不能把普通邮件当作有效票据。
- **后台**：保存 `approval_status=approved` 与 `execution_status=scheduled`，示例窗口 `22:00–06:00`、`next_action=copy_to_bronze`；窗口由 A 以有资格的 System Expert 身份**在审批时设定**，Hermes 只提示「可能存在源端负载风险」，不自拟 `execution window / copy mode / load policy`（[R27](2_实现方法_checklist.md#r27)）。夜间窗口作为资产 operational knowledge 保留。
- **CY-1 到线**：此时 copy 尚未开始，按 [R05](2_实现方法_checklist.md#r05) 暂停到下一 Cycle，先出报告等 Sponsor 放行；不能因为票已批就自动跑。
- **可能阻碍**：A 有业务知识但无系统批准权、凭证缺失、白天批准在晚上前失效、时间窗口不明确。找正确角色或补条件，不等到夜间才重新唤人审批。M098-E01 里「先看 schema 和基础 metadata」属于 `metadata_visibility` 级别，夜间那次 copy 属于 `data access / ingestion approval`，两级分别授权（[R23](2_实现方法_checklist.md#r23)），不能拿前者当后者已批。

来源：[M097—M100](素材/分享对话原文.md#m097)。

#### 原模拟邮件、回复与现场记录

**A 的模拟回复原话**（[M097](素材/分享对话原文.md#m097)）

<!-- original-excerpt:excerpt-012 -->
```text
a说没什么 你连就行 注意太大了copy在晚上
```

原文把“有 authority 的人说可以”写成已批准；此处完整保留邮件，但正式接入仍需满足现有审批事件要求。最终确认机制见 [R06](2_实现方法_checklist.md#r06)，排期机制见 [R12](2_实现方法_checklist.md#r12)。

<a id="mail-m098-e01"></a>
**原模拟邮件｜Hermes → A；原文写作块 ID：38146**（[M098](素材/分享对话原文.md#m098)）

<!-- original-excerpt:excerpt-013 -->
```text
Subject: Re: Sales data

Thanks. I’ll avoid a large daytime copy and schedule the initial Bronze ingestion for the evening.

I’ll first check the schema and basic metadata, then run the larger copy within the quieter window you suggested. I’ll come back to you if I find anything that needs business clarification.

Best,  
Hermes
```

**原模拟排期状态（窗口是示例，非当前代码已生效配置）**（[M100](素材/分享对话原文.md#m100)）

<!-- original-excerpt:excerpt-014 -->
```text
Sales Bronze Task

approval:
  approved_by: A
  status: approved

execution:
  status: waiting_window
  window: 22:00-06:00

next_action:
  copy_to_bronze
```

<a id="n05"></a>
### N05 复制前 10 分钟：通知受影响的人

- **CY-2 放行后恢复排期**；**21:50**：检查许可有效性、源健康、维护冲突及即将到来的窗口；按资产 policy 通知计划 22:00 执行。
- **收件人**：A 同时负责系统/数据则发 A；A 只负责业务则找 DB/Platform/System Owner，必要时覆盖双方，不默认抄送 Boss/Sponsor。
- **后台**：这是 execution notification，不是第二次审批；提前量来自配置 10m/30m/none。Cycle 活跃且 Approval/Window 有效、Pre-check 通过时，通知无人回复也可执行；Cycle 已停且动作未开始则暂停，不能因发过通知继续。
- **可能阻碍**：源异常、窗口冲突或资格失效时停止/延后并记录；不能只在邮件承诺“异常自动停”而无对应执行机制。

来源：[M101—M104](素材/分享对话原文.md#m101)。

#### 原模拟邮件、回复与现场记录

<a id="mail-m104-e01"></a>
**原模拟邮件｜Hermes → A / 此例兼任数据与系统负责人；原文写作块 ID：61423**（[M104](素材/分享对话原文.md#m104)）

<!-- original-excerpt:excerpt-015 -->
```text
Subject: Re: Sales data

Hi A,

The initial Bronze copy of the sales table is scheduled to start at 22:00, within the agreed low-traffic window.

I’ll run the pre-execution checks first and stop automatically if the source is under abnormal load or any safety condition fails.

Best,  
Hermes
```

<details>
<summary>原文另给出的简短执行通知</summary>

**执行通知短句；原文没有主题**（[M102](素材/分享对话原文.md#m102)）

<!-- original-excerpt:excerpt-016 -->
```text
Sales table Bronze ingestion is scheduled to start at 22:00 as approved. I’ll stop automatically if the safety checks fail or the source shows abnormal load.
```

</details>

“异常负载自动停”是原模拟承诺；正式运行必须有对应能力与验证，不能因保留这封邮件就视为已经做到。

<a id="n06"></a>
### N06 Sales 落 Bronze，在 Lake 做画像

- **系统动作**：22:00 受控复制，成功后更新 Bronze 状态；后续大统计、median、null rate、异常检测在 Lake 上算。
- **画像示例**：12.4M 行，`order_id` 唯一率约 100%，`customer_id` null=0.7%，`order_total` median=268、min=-82,000、负值=1.8%。前面独立画像示例还给了 null=0.03%、p95=4,820、max=1,920,000，不能未经说明把两个样例当同次测量。
- **系统问 A**：“负金额通常表示什么？”不预先认定是错误、退款、冲销或取消，也不直接清洗。
- **首次 Bronze 事件**：Sales 是第一张资产，同样幂等登记一次 Integration Assessment（[R19](2_实现方法_checklist.md#r19)）。此刻湖内没有其他资产，评估直接以「暂无可关联对象」结束，但事件必须落库；N09 的 Finance 评估是第二次，不是第一次。
- **后台/阻碍**：Sales 处于已画像、语义待确认；原文没有给出 A 对负值的最终答案，因此这一项继续 pending。Finance 线不因此停止。

来源：[M038](素材/分享对话原文.md#m038)、[M104—M108](素材/分享对话原文.md#m104)。M104 与 M108 是同一问题的续写，不重复发送。

#### 原模拟邮件、回复与现场记录

**原 Sales 画像示例**（[M104](素材/分享对话原文.md#m104)）

<!-- original-excerpt:excerpt-017 -->
```text
12.4M rows
order_id unique ≈ 100%
customer_id null = 0.7%
order_total:
  median = 268
  min = -82,000
  negative rows = 1.8%
```

<a id="mail-m108-e01"></a>
**后续明确湖内画像的邮件版本｜Hermes → A；原文写作块 ID：92751**（[M108](素材/分享对话原文.md#m108)）

<!-- original-excerpt:excerpt-018 -->
```text
Subject: Re: Sales data

Hi A,

The Bronze copy completed successfully, and I’m now profiling the data in the lake.

I found that about 1.8% of `order_total` values are negative. Before I classify or transform them, could you confirm what negative amounts normally represent in this table?

Best,  
Hermes
```

<details>
<summary>同一问题的较早写法：M104（保留退款/取消/调整等追问细节，不重复发信）</summary>

<a id="mail-m104-e02"></a>
**较早的负金额问题邮件｜Hermes → A；原文写作块 ID：92751**（[M104](素材/分享对话原文.md#m104)）

<!-- original-excerpt:excerpt-019 -->
```text
Subject: Re: Sales data

Hi A,

The initial copy completed successfully.

While profiling the data, I found that about 1.8% of `order_total` values are negative. Before I classify or transform them, could you confirm what negative amounts normally represent in this table — for example refunds, cancellations, adjustments, or something else?

Best,  
Hermes
```

</details>

原文没有 A 对负金额问题的最终回复，继续保留 pending；写作块 ID `92751` 在两个版本中相同，不能据此合并掉任一原文版本。

<a id="n07"></a>
### N07 Finance 三个 working day 不回，升级 Boss

- **触发**：B 的问题累计三个 working day 未回复，等待量从 CY-1 一路带到 CY-3（[R21](2_实现方法_checklist.md#r21)/[R22](2_实现方法_checklist.md#r22)：跨 Cycle 保留、按企业工作日历计算，不用自然日）；按本批沟通处理窗口检查提醒/升级与备选联系人。2—3 个 working day 是配置示例，不按某个问题提前答复就补发新主题问题。
- **系统问 Boss**：“Finance 这部分因联系未果被阻塞，能否介绍另一个了解数据或能处理接入的人？”不把邮件写成投诉。
- **后台**：记录 waiting/blocker、当前问题、升级目标、原因和 next_action；单次未回只记本次证据。
- **可能阻碍**：Boss 也无法推荐则保持阻塞并进入报告；已有备选则先查其职责/authority。三个 working day 不是默认所有问题统一 SLA。

来源：[M109—M110](素材/分享对话原文.md#m109)。

#### 原模拟邮件、回复与现场记录

**用户设定的升级触发条件**（[M109](素材/分享对话原文.md#m109)）

<!-- original-excerpt:excerpt-020 -->
```text
b没回复3天就升级到问boss
```

<a id="mail-m110-e01"></a>
**原模拟邮件｜Hermes → Boss；原文写作块 ID：48317**（[M110](素材/分享对话原文.md#m110)）

<!-- original-excerpt:excerpt-021 -->
```text
Subject: Finance data contact

Hi Boss,

I haven’t been able to get a response on the finance data after three days, so that part of the work is currently blocked.

Could you point me to another person who can help identify the relevant finance table or approve access?

Best,  
Hermes
```

<a id="n08"></a>
### N08 换人后 Finance 接入成功

- **人提供**：新的 Finance 联系人 F 和可用入口。Boss 的介绍同样只建立联系人关系（[R17](2_实现方法_checklist.md#r17)）；F 对 Finance 表的接入审批资格要另行从 Authority Store / Admin 显式授予记录查到，查到后才走与 N04 相同的审批与窗口流程——8.2M 行是否需要执行窗口，由有资格的取数/System Expert 在审批时设定（[R27](2_实现方法_checklist.md#r27)），不由 Hermes 按行数自行判断。
- **系统动作**：解除原 blocker，Finance 复制到 Bronze 并在 Lake 画像，不必每一步都回 Boss 报进度。
- **画像示例**：8.2M 行，`customer_id` null=0.1%，`transaction_amount` null=0%，`transaction_type` 12 类，包含 revenue/refund/adjustment。
- **可能阻碍**：新联系人只提供表名不等于批准；连接失败不能标 Bronze ready。发现 Finance/Sales customer ID 关系未知，进入下个独立评估 Task。

来源：[M111—M112](素材/分享对话原文.md#m111)。

#### 原模拟邮件、回复与现场记录

**用户给出的 Finance 换人并接入成功条件**（[M111](素材/分享对话原文.md#m111)）

<!-- original-excerpt:excerpt-022 -->
```text
然后换了个人fince表连上了
```

**原 Finance 画像与关系问题**（[M112](素材/分享对话原文.md#m112)）

<!-- original-excerpt:excerpt-023 -->
```text
8.2M rows
customer_id null = 0.1%
transaction_amount null = 0%
transaction_type = 12 values
有 revenue / refund / adjustment 等类型

问题：
finance 表里的 customer_id
和 sales.orders.customer_id
是否属于同一个 ID 体系？
```

<details>
<summary>早期写法：Finance 接入后直接追问（后续明确由独立评估 Task 负责）</summary>

<a id="mail-m112-e01"></a>
**早期关系追问邮件｜Hermes → F / 新 Finance 联系人；原文写作块 ID：43618**（[M112](素材/分享对话原文.md#m112)）

<!-- original-excerpt:excerpt-024 -->
```text
Subject: Re: Finance data

Hi,

The Finance table is now connected and I’m profiling the Bronze copy in the lake.

I found a `customer_id` field that may allow us to relate Finance transactions to the Sales data. Is this the same customer identifier used in the Sales system, or is there a separate mapping between the two?

Best,  
Hermes
```

</details>

这封与 N10 的 Finance 确认属于同一类问题的流程演进；已有问题/回答应复用，不能把两个版本机械排成连续重复追问。

<a id="n09"></a>
### N09 第二张新表触发 Integration Assessment

- **触发**：Finance 新资产入湖事件，而非等原 Sales/Finance 会话偶然想到关联。这是**第二次**登记；第一次在 N06 的 Sales 首次 Bronze，结论为「暂无可关联对象」（[R19](2_实现方法_checklist.md#r19)）。
- **后台**：首次成功 Bronze 幂等登记 assessment_required；Cycle 活跃时创建独立 R-002 Task Session，加载 Mission、Asset State、相关资产与已知 JOIN；缺画像先等待。Cycle 已停只登记，不开 Session。普通同步不重开，关键 Schema/JOIN Key/语义、Source/Lineage、Scope 或关系有效性实质变化才重评。
- **系统判断**：有没有共同 Customer 实体、是否需要关联、哪些候选字段、直接 JOIN/mapping/不关联；只筛相关候选。
- **可能阻碍**：没有关系也可以有明确评估结论；同名字段、类型一致或高重叠不足以直接 confirmed。不得对全湖所有表暴力两两连接。

来源：[M113—M118](素材/分享对话原文.md#m113)。

#### 原模拟邮件、回复与现场记录

此节点原文未写新的完整邮件；以下只保留原有推进说明或状态，不补写成已发送。

**第二张表的原评估任务示例**（[M116](素材/分享对话原文.md#m116)）

<!-- original-excerpt:excerpt-025 -->
```text
Task Session R-002:
Finance.transactions × existing assets

检查：
- sales.orders
- customer master
- 其他已接表

发现：
Finance.customer_id ↔ Sales.customer_id 可能有关
↓
验证
↓
确认/否决
```

**后续新增表也要创建评估任务的原示例**（[M116](素材/分享对话原文.md#m116)）

<!-- original-excerpt:excerpt-026 -->
```text
Task Session R-003:
NewTable × existing relevant assets
```

<a id="n10"></a>
### N10 技术证据强，仍分别问两个域

- **技术示例**：类型/格式一致、Finance 与 Sales customer ID 重叠或覆盖约 98.7%，没有明显基数异常。记录指标的分母和查询依据，不能混用“值域”和“记录数”口径。
- **系统分别问**：A 的 Sales ID 是公司级还是域内 ID；F 的 Finance ID 是否同体系，有无历史 mapping。
- **人答**：A 认为统一 ID；F 表示老数据可能有历史映射。
- **后台/阻碍**：`verified_not_confirmed` → 存在待解释差异；保存双方说法，不直接 JOIN、不覆盖某一边、不因单边确认关闭跨域问题。A 只能确认 Sales 侧语义、F 只能确认 Finance 侧语义；Sales↔Finance 的正式 JOIN 归 `cross_domain_relationship / integration authority`（[R24](2_实现方法_checklist.md#r24)），两人都不是。这也是这两封回复不能直接写 confirmed 的原因——跨域走 [R18](2_实现方法_checklist.md#r18) 的第二条路径。

来源：[M120—M124](素材/分享对话原文.md#m120)。

#### 原模拟邮件、回复与现场记录

**原技术检查与未确认状态**（[M120](素材/分享对话原文.md#m120)）

<!-- original-excerpt:excerpt-027 -->
```text
R-002 Finance Integration Assessment

Finance.transactions.customer_id
vs
Sales.orders.customer_id

技术检查：
- type 一致
- 格式一致
- 98.7% Finance customer_id 可在 Sales 找到
- 未发现明显一对多异常

结论：
candidate relationship
status = verified_not_confirmed
```

<a id="mail-m120-e01"></a>
**原模拟邮件｜Hermes → A / Sales；原文写作块 ID：47216**（[M120](素材/分享对话原文.md#m120)）

<!-- original-excerpt:excerpt-028 -->
```text
Subject: Re: Sales data

Hi A,

I’m checking how the Sales data relates to the newly connected Finance data.

The `customer_id` values appear to match closely between the two datasets. From the Sales side, is `customer_id` the company-wide customer identifier, or is it specific to the Sales system?

Best,  
Hermes
```

<a id="mail-m120-e02"></a>
**原模拟邮件｜Hermes → F / Finance；原文写作块 ID：80534**（[M120](素材/分享对话原文.md#m120)）

<!-- original-excerpt:excerpt-029 -->
```text
Subject: Re: Finance data

Hi,

I’m checking how the Finance data relates to the Sales data.

The `customer_id` values match closely in the lake. From the Finance side, does this field use the same customer identifier as Sales, or is there any mapping or exception I should account for?

Best,  
Hermes
```

**原文随后假设的双方回复；不是两封完整邮件**（[M124](素材/分享对话原文.md#m124)）

<!-- original-excerpt:excerpt-030 -->
```text
A（Sales）：
customer_id 是公司统一客户 ID。

Finance 联系人：
大部分是一样的，但老数据可能经过历史映射。
```

<a id="n11"></a>
### N11 升级 authority，同时寻找 mapping 证据

- **系统动作**：查 Customer Master/MDM 的真正 authority C；另向 F 追问历史 mapping 在哪里、谁熟悉旧 ID。
- **后台**：原 R-002 保留未完成，拆出 authority confirmation 与 evidence discovery 两条可关联的工作线。向 F 追问 mapping 位置是为关闭当前 question 所必需的 clarification，可以立即发出、不等批次窗口；若要问 Finance 的新主题则必须排进下一批（[R20](2_实现方法_checklist.md#r20)）。两者都占 F 的 open-question 硬上限。
- **可能阻碍**：行政上级不一定懂或有权确认客户主数据；“问我上级”只是路由证据，仍要查正式资格。

来源：[M121—M124](素材/分享对话原文.md#m121)。

#### 原模拟邮件、回复与现场记录

<a id="mail-m124-e01"></a>
**原模拟邮件｜Hermes → C / Customer Data Owner 或 MDM Owner；原文写作块 ID：35184**（[M124](素材/分享对话原文.md#m124)）

<!-- original-excerpt:excerpt-031 -->
```text
Subject: Confirming customer ID relationship

Hi,

I’m Hermes, the data steward working on the Sales and Finance datasets.

I’m validating whether `customer_id` can be used directly between the two systems. The Sales side considers it a company-wide customer ID, while Finance mentioned that older records may have historical mappings.

Could you confirm the authoritative relationship between these identifiers, including whether historical records require a mapping?

Best,  
Hermes
```

<a id="mail-m124-e02"></a>
**原模拟邮件｜Hermes → F / Finance 联系人；原文写作块 ID：68427**（[M124](素材/分享对话原文.md#m124)）

<!-- original-excerpt:excerpt-032 -->
```text
Subject: Re: Finance data

Thanks — the historical mapping point is important.

Could you point me to where that mapping is maintained, or to the person who knows the older Finance customer IDs best?

I’ll keep the relationship unconfirmed until we verify those exceptions.

Best,  
Hermes
```

<a id="n12"></a>
### N12 建多人 Resolution Thread

- **系统发信**：把有必要且有权共享相关信息的 A、F、C 拉进同一线程，简述已知事实、技术证据、差异和需要确认的历史规则。
- **后台**：新建或关联 Resolution Task Session，原评估 Task 保留依赖。让人直接互相纠正，减少 Hermes 逐人传话。
- **可能阻碍**：有成员不能看到另一个域的表名/样本，先限制信息或分开沟通；多数人赞同不自动代替领域 authority。

来源：[M125—M126](素材/分享对话原文.md#m125)。

#### 原模拟邮件、回复与现场记录

原邮件称呼为 A、B、C，逐字保留；这里的 Finance 发言者对应主线中的替代联系人 F。实际系统必须按真人/线程标识绑定，不能用字母继承资格。N11 的逐人询问与此处多人线程是可升级的沟通方式，不要求不分情境把所有邮件都再发一遍。

<a id="mail-m126-e01"></a>
**原模拟邮件｜Hermes → A、Finance 联系人、C（原文写 A/B/C）；原文写作块 ID：61482**（[M126](素材/分享对话原文.md#m126)）

<!-- original-excerpt:excerpt-033 -->
```text
Subject: Customer ID mapping — confirmation needed

Hi A, B, and C,

I’m validating the relationship between the Sales and Finance customer identifiers.

What I have so far:
- Sales indicates `customer_id` is the company-wide customer identifier.
- Finance indicates older records may require a historical mapping.
- In the lake, the identifiers match directly for about 98.7% of records.

Could you help confirm the correct rule for historical records, and whether there is an authoritative mapping we should use?

Please reply in this thread so I can keep the decision and evidence together.

Best,  
Hermes
```

<a id="n13"></a>
### N13 人连续讨论，Hermes 默认静默

- **A 说**：2019 后统一 ID，之前迁移情况不确定。
- **F 说**：2019 前用 `legacy_customer_no`，并非所有记录都改过。
- **C 说**：权威 mapping 在 `mdm.legacy_customer_map`；2019-01-01 之后可直接用 `customer_id`，之前先 mapping。
- **后台**：每封邮件路由到当前 Resolution Task，抽取 claim、证据、支持/差异，通常 `SILENT`；形成可总结结论后 `pending_summary`，可等待示例 30 分钟静默窗口。
- **可能阻碍**：缺 authority、讨论跑偏、核心问题无人回答或冲突需澄清才 `INTERVENE`。新邮件打断静默窗口则重新判断，不逐封自动回信。

来源：[M127—M132](素材/分享对话原文.md#m127)。

#### 原模拟邮件、回复与现场记录

**A 的原模拟回复**（[M132](素材/分享对话原文.md#m132)）

<!-- original-excerpt:excerpt-034 -->
```text
2019 以后确实统一成同一个 customer_id 了。老数据我不确定 Finance 当时怎么迁的。
```

**Finance 联系人的原模拟回复（原文写 B，主线标 F）**（[M132](素材/分享对话原文.md#m132)）

<!-- original-excerpt:excerpt-035 -->
```text
对，2019 前 Finance 用过 legacy_customer_no，后来迁移时不是所有记录都改掉了。
```

**C 的原模拟回复**（[M132](素材/分享对话原文.md#m132)）

<!-- original-excerpt:excerpt-036 -->
```text
历史映射在 mdm.legacy_customer_map。2019-01-01 之后可以直接用 customer_id；之前应该先走 mapping。
```

三条原回复没有独立主题、Message-ID 或完整邮件头，不补造；均属于 N12 的 Resolution Thread。Hermes 此时不逐条回信。

<a id="n14"></a>
### N14 总结确认：分时期记录业务规则

- **系统总结**：2019-01-01 起直接用 customer ID；之前通过 legacy mapping。
- **人答**：C 对 Hermes 整理后的明确规则正式确认；先前讨论只记 claim/provisional understanding——这是分时期多条件的跨域规则，按 [R18](2_实现方法_checklist.md#r18) 必须走「先 claim、再结构化总结、再正式确认」这条路径。C 的资格是独立的 `cross_domain_relationship / integration authority`（[R24](2_实现方法_checklist.md#r24)），挂在 Sales↔Finance 这个关系对象上，不顶替 A 或 F 的域内语义权；也可由双方 Authority 各自正式确认自己这侧。沉默不构成确认，证据与资格必须落库。
- **后台**：规则带 effective date、confirmed_by=C、讨论证据，R-002 从等待确认进入技术验证。
- **可能阻碍**：邮件措辞含“没反对就采用”与明确确认要求矛盾，采用后者；C 无权覆盖整个范围时继续寻找所缺确认方。

来源：[M132—M134](素材/分享对话原文.md#m132)。

#### 原模拟邮件、回复与现场记录

下列 `Unless I’ve misunderstood anything` 原样保留，作为 [R06](2_实现方法_checklist.md#r06) 的历史差异证据；最终决定明确它不能成为“无人反对自动确认”的实施规则。

<a id="mail-m132-e01"></a>
**原模拟邮件｜Hermes → A、Finance 联系人、C；原文写作块 ID：27518**（[M132](素材/分享对话原文.md#m132)）

<!-- original-excerpt:excerpt-037 -->
```text
Subject: Re: Customer ID mapping — confirmation needed

Thanks all. My understanding is:

- For records from 2019-01-01 onward, Sales and Finance can be joined directly using `customer_id`.
- For earlier records, the Finance identifier should first be resolved through `mdm.legacy_customer_map`.

Unless I’ve misunderstood anything, I’ll use this as the confirmed relationship rule and validate it against the data before applying it.

Best,  
Hermes
```

**C 的原模拟条件回复；M134 随后以其明确确认为前提**（[M132](素材/分享对话原文.md#m132)）

<!-- original-excerpt:excerpt-038 -->
```text
Yes, that's correct.
```

<details>
<summary>较早的总结确认短句（未提供主题，保留原措辞）</summary>

**Hermes 总结短句**（[M128](素材/分享对话原文.md#m128)）

<!-- original-excerpt:excerpt-039 -->
```text
My understanding is: records from 2019 onward can use `customer_id` directly, while earlier records should use `legacy_customer_map`. If that is correct, I’ll record this as the confirmed relationship.
```

</details>

<a id="n15"></a>
### N15 mapping 入湖，再开评估 Task

- **触发**（CY-5 放行后）：`mdm.legacy_customer_map` 成为第三张相关资产；先在 `metadata_visibility` 级别查存在与 schema，取得 `data access / ingestion approval` 后再复制到湖（[R23](2_实现方法_checklist.md#r23)），首次 Bronze 照例登记评估，创建 R-003。
- **系统验证**：legacy ID 唯一性、canonical ID 基数、有效期、重复映射和空值，同时评估它如何接上已有资产。
- **示例结果**：2019 前 Finance 1,200,000 行，mapping coverage=99.2%、unmapped=0.8%；2019 后 direct join coverage=99.7%、unmatched=0.3%；少量 legacy ID 一对多。
- **系统问 C**：剩余未映射与一对多是否已知历史例外，是否有另一来源。
- **后台/阻碍**：业务规则 confirmed，数据仍有 open exceptions；记录 Exception/Unknown/Conflict、Evidence 和适用范围，不猜补也不隐藏。Hermes 核心整理到 Silver/Reusable Lake Assets，不承担最终产品 100% 正确性和业务验收；Task 达到 Done Criteria 且无阻塞当前使用的 unresolved blocker 可关闭，其他未知继续留档。

来源：[M133—M134](素材/分享对话原文.md#m133)。

#### 原模拟邮件、回复与现场记录

**原 mapping 验证结果；保留分时期分母与异常**（[M134](素材/分享对话原文.md#m134)）

<!-- original-excerpt:excerpt-040 -->
```text
pre-2019 Finance rows: 1,200,000

mapping coverage: 99.2%
unmapped: 0.8%

2019+ direct join coverage: 99.7%
unmatched: 0.3%

另外发现：
legacy_customer_no 有少量一对多 mapping
```

<a id="mail-m134-e01"></a>
**原模拟邮件｜Hermes → C；原文写作块 ID：34627**（[M134](素材/分享对话原文.md#m134)）

<!-- original-excerpt:excerpt-041 -->
```text
Subject: Re: Customer ID mapping — validation exceptions

Hi C,

I validated the confirmed mapping rule in the lake.

It covers 99.2% of the pre-2019 Finance records, but about 0.8% remain unmapped, and a small number of legacy IDs map to more than one customer.

Are these known historical exceptions, or is there another source I should use to resolve them?

Best,  
Hermes
```

原文没有给出 C 对例外的最终回答；99.2% 不是通用交付门槛。[R16](2_实现方法_checklist.md#r16) 已定：保留异常和证据，不把规则 confirmed 写成数据无错；下游负责最终数据产品验收。

<a id="n16"></a>
### N16 到线立即出报告

CY-5 的时间/token 到线立即报告并停止新任务/分支，不等待 C 或 Running 动作结案。Running 允许仅完成当前动作写 State/Result 后停止；尚未开始的夜间 copy 暂停到下一 Cycle。

| 报告项 | 这条主线已知状态 |
|---|---|
| 本轮目标 | 整理 Sales/Finance 数据基础及客户关联；具体本轮范围由各 Cycle 决定 |
| 上一轮意见落实 | CY-4 review 每条意见逐项列已办、未办或放弃及原因 |
| Cycle budget / usage | 本轮 time/token 预算与已用量、report_budget 余量，并写明是时间先到还是 token 先到 |
| Sales | Bronze、画像已完成；负金额问题已发给 A，原文未给最终回答 |
| Finance | Bronze、画像已完成；原联系人阻塞已通过换人解除 |
| 客户关联 | 2019 后直接关联、之前 legacy mapping 的业务规则经 C 确认 |
| Mapping | Bronze、画像及关联验证已推进 |
| 质量/异常 | 历史 0.8% unmapped、少量一对多；2019 后 0.3% unmatched，保留适用范围 |
| Waiting | C 对历史例外的解释；以及尚未关闭的 Sales 语义问题 |
| Scheduled | 已批准但未开始的夜间动作，列对象、原窗口及暂停状态；下一 Cycle 才能恢复 |
| Running | 如已有在途动作，报告其真实进度；完成当前动作后写 State/Result，不能自动继续 |
| Ready but paused | 后续可推进工作，不因还有 ready task 延长当前 Cycle |
| 按人视图 | 每个联系人的 completed / pending / queued / blocked_by：A（负金额 pending）、F、C（历史例外 pending）等逐人列出 |
| Mission 待补齐 | consumer、success criteria、explicit exclusions 的补齐进度；本轮至少推进一项，最晚在第一版 Silver 前完成一次 Mission Refinement（[R26](2_实现方法_checklist.md#r26)） |
| 新知识 | 相关人/authority、资产档案、分期 JOIN 规则及证据 |
| 下一轮建议 | 由 Sponsor 校准 Scope 并开 Silver 阶段门；各 Asset 满足成熟条件后提出 Silver transformation 与必要复核，异常不要求全部清零 |

原文末尾假设已到 5—7 天信任级别；这是跨 Cycle 主线的周级停止线，不表示初始即获七天自主权限。Sponsor 未明确放行，继续接收并归档回复，但不自行开下一轮。主线到此没有实际产出 Silver，不补写成完成；Gold 不属于此核心流程。

来源：[M135—M136](素材/分享对话原文.md#m135)、[M159—M162](素材/分享对话原文.md#m159)。

#### 原模拟邮件、回复与现场记录

原周报正文完整保留；它漏列仍 pending 的 Sales 负金额问题，也缺 [R25](2_实现方法_checklist.md#r25) 要求的上一轮意见落实、budget/usage 和按人视图三项，更没有展开 Sponsor 对 Scope 的 Silver 阶段门、逐资产成熟度或正式续轮决定。最终机制不要求异常全部解决后才形成可复用 Silver；正式报告按 State、Done Criteria 与实际 blocker 表达，不能照原邮件漏报或默认续跑。

<a id="mail-m136-e01"></a>
**原模拟邮件｜Hermes → Sponsor；原文写作块 ID：62841**（[M136](素材/分享对话原文.md#m136)）

<!-- original-excerpt:excerpt-042 -->
```text
Subject: Weekly data stewardship review

Hi,

Here’s the current status of the work.

Sales and Finance are now connected in the lake and have been profiled. We also confirmed how customer IDs should be linked between the two systems, including the historical mapping before 2019.

The main remaining issue is a small set of historical Finance records: about 0.8% cannot currently be resolved through the confirmed mapping. I’m waiting for the relevant owner to confirm whether another source exists.

My recommendation for the next cycle is to resolve those exceptions first, then build and validate the first Silver layer across Sales and Finance.

If you’re happy with that direction, I’ll continue with this scope.

Best,  
Hermes
```

<details>
<summary>原周报状态表与早期停止顺序（完整留存；先收尾后报告已被 M161/M162 修正）</summary>

**M136 原报告表**（[M136](素材/分享对话原文.md#m136)）

<!-- original-excerpt:excerpt-043 -->
```text
| 项目 | 当前情况 |
|---|---|
| Sales | 已进入 Bronze，完成 Profiling |
| Finance | 已进入 Bronze，完成 Profiling |
| Customer JOIN | 2019+ 直接 JOIN 已确认 |
| Historical JOIN | 2019 前使用 `legacy_customer_map` 已确认 |
| Mapping 表 | 已接入并完成 Profiling |
| 数据质量 | 历史 mapping coverage 99.2%，0.8% unresolved |
| Pending | 等 C 判断 0.8% 是否还有其他来源 |
| Blocked | Finance 原联系人未回复，已成功换人 |
| 新知识 | Sales/Finance/MDM 的 owner、authority、JOIN 规则已沉淀 |
| 建议下一步 | 解决 historical exceptions，然后形成第一版 Silver |
```

**M136 早期停止流程**（[M136](素材/分享对话原文.md#m136)）

<!-- original-excerpt:excerpt-044 -->
```text
Cycle budget reached
↓
STOP NEW BRANCHES
↓
完成正在进行的安全原子动作
↓
不再主动：
- 探索新表
- 新开 Integration Task
- 找更多人
- 开新的 Silver 工作
↓
保留现有 waiting / scheduled task
↓
生成 Cycle Report
↓
发 Sponsor
↓
等待 Sponsor 放行下一 Cycle
```

</details>

<a id="extra"></a>
## 九个补充场景

原文先提出六类高价值场景，再追加分析越界、过期摘要、混答路由，最终共有下列九个。它们是独立起点，不把各自资产数量、人物代号和时间当成主线连续状态。

<a id="case01"></a>
### C01 知情人说“可以接”，但没有接入审批权

- **起点**：D 是熟悉 `finance.cost_detail` 的 Finance Analyst，无 access authority；E 是真正 Finance Data Manager/Approver。
- **人说**：D 推荐此表并说可直接连接。
- **应发生**：拆开“相关成本数据来源”与“授权声明”；前者 candidate evidence，后者不改变 access state。自然回 D 会另行完成所需批准，再找 E。
- **人点击批准**：查 E 当前 authority、具体表与 project scope、正式事件有效性；有效后才进入源安全检查、Bronze、湖内画像。
- **阻碍/判据**：懂表不等于能批表；正式批准只认对应 E 事件，D 的许可话术不能被补写成授权。原文的“通过”是纸面结果。

来源：[M139—M142](素材/分享对话原文.md#m139)。

#### 原模拟邮件、回复与现场记录

**D 的原模拟回信；只有正文**（[M140](素材/分享对话原文.md#m140)）

<!-- original-excerpt:excerpt-045 -->
```text
Yes, this is the table you need. You can connect it directly. It contains the customer-level cost details.
```

<a id="mail-m140-e01"></a>
**原模拟邮件｜Hermes → D；原文写作块 ID：26514**（[M140](素材/分享对话原文.md#m140)）

<!-- original-excerpt:excerpt-046 -->
```text
Subject: Re: Finance cost data

Thanks, this looks like the right dataset.

I’ll use `finance.cost_detail` as the candidate source for customer-level cost data. I still need the appropriate access approval before connecting it, so I’ll take care of that separately.

Best,  
Hermes
```

<a id="mail-m140-e02"></a>
**原模拟邮件｜Hermes → E / 有权审批人；原文写作块 ID：73195**（[M140](素材/分享对话原文.md#m140)）

<!-- original-excerpt:excerpt-047 -->
```text
Subject: Access approval for finance.cost_detail

Hi E,

I’m Hermes, the data steward working on the customer revenue and cost data project.

D pointed me to `finance.cost_detail` as the relevant source for customer-level costs. Before I connect it to the lake, could you confirm whether I’m authorized to use this table for the current project?

Best,  
Hermes
```

**用户模拟 E 的正式点击动作；不是邮件正文**（[M141](素材/分享对话原文.md#m141)）

<!-- original-excerpt:excerpt-048 -->
```text
点同意
```

原邮件未展开实际审批链接；正式运行须由现有审批通道生成，不能把上面的询问信或“点同意”文本当作票据。

<a id="case02"></a>
### C02 新 CRM 表触发评估，但负责人只确认自己这边

- **起点**：已有 Sales/Finance；这个独立场景新增 `crm.customer_profile`，字段 `customer_id/email/segment/created_at`，示例覆盖率 97.9%，创建 R-004。
- **系统问 CRM owner**：是否同一公司客户 ID，有无映射/例外。
- **人答**：“我确认我这张表，对面不清楚。”随后 Sales A 又说“问我上级”。
- **应发生**：CRM owner 对自己表的字段定义属于**单域、单一事实、无冲突**，其 authority 验证通过后回复本身即可写 `confirmed`（[R18](2_实现方法_checklist.md#r18) 第一条路径，M146-E01 的措辞按此成立）；跨系统关系是另一回事，归 `cross_domain_relationship / integration authority`（[R24](2_实现方法_checklist.md#r24)），仍未 confirmed。联系已有 Sales authority，不足时查 manager/Customer Domain Owner 的真正资格，再升级。
- **阻碍/判据**：97.9% 技术匹配不替代双方/跨域确认；「问我上级」只是 routing evidence，该上级要成为 authority 须有 Admin 显式授予或 Authority Store 记录（[R17](2_实现方法_checklist.md#r17)）。本场景连续几封都是为关闭同一个 question 的 clarification，可以不等批次窗口（[R20](2_实现方法_checklist.md#r20)）；升级线索和已完成单边证据都保留，不让用户重答同一问题。

来源：[M143—M148](素材/分享对话原文.md#m143)。

#### 原模拟邮件、回复与现场记录

<a id="mail-m144-e01"></a>
**原模拟邮件｜Hermes → CRM Owner；原文写作块 ID：41936**（[M144](素材/分享对话原文.md#m144)）

<!-- original-excerpt:excerpt-049 -->
```text
Subject: Confirming CRM customer ID

Hi,

I’m validating how the newly connected CRM data relates to the existing Sales data.

The `customer_id` values match closely between `crm.customer_profile` and the Sales customer identifiers. Is this the same company-wide customer ID, or are there any CRM-specific mappings or exceptions I should account for?

Best,  
Hermes
```

**CRM Owner 的模拟回复原话**（[M145](素材/分享对话原文.md#m145)）

<!-- original-excerpt:excerpt-050 -->
```text
我确认我这张表 对面咋样我不清楚
```

<a id="mail-m146-e01"></a>
**原模拟邮件｜Hermes → CRM Owner；原文写作块 ID：32841**（[M146](素材/分享对话原文.md#m146)）

<!-- original-excerpt:excerpt-051 -->
```text
Subject: Re: Confirming CRM customer ID

Thanks. I’ll record the CRM-side definition as confirmed and verify the Sales side separately before treating the cross-system relationship as confirmed.

Best,  
Hermes
```

<a id="mail-m146-e02"></a>
**原模拟邮件｜Hermes → A / Sales；原文写作块 ID：61475**（[M146](素材/分享对话原文.md#m146)）

<!-- original-excerpt:excerpt-052 -->
```text
Subject: CRM and Sales customer ID

Hi A,

I’m validating the newly connected CRM data against Sales.

The CRM owner has confirmed that `crm.customer_profile.customer_id` is their official customer identifier. In the lake, it matches about 97.9% of the Sales `customer_id` values.

From the Sales side, is this the same company-wide customer ID, or are there any mappings or exceptions I should account for?

Best,  
Hermes
```

**用户提供的 A 的回复/转介原话**（[M147](素材/分享对话原文.md#m147)）

<!-- original-excerpt:excerpt-053 -->
```text
他说问我上级吧
```

<a id="mail-m148-e01"></a>
**原模拟邮件｜Hermes → A；原文写作块 ID：58314**（[M148](素材/分享对话原文.md#m148)）

<!-- original-excerpt:excerpt-054 -->
```text
Subject: Re: CRM and Sales customer ID

Thanks. I’ll follow up with your manager to confirm the cross-system relationship.

Best,  
Hermes
```

<a id="mail-m148-e02"></a>
**原模拟邮件｜Hermes → 经查验具备对应资格的上级/领域负责人；原文写作块 ID：24761**（[M148](素材/分享对话原文.md#m148)）

<!-- original-excerpt:excerpt-055 -->
```text
Subject: Confirming Sales and CRM customer ID relationship

Hi,

I’m Hermes, the data steward working on the Sales and CRM datasets.

A suggested I confirm this with you. The CRM owner has confirmed that `crm.customer_profile.customer_id` is their official customer identifier, and the values match about 97.9% of the Sales `customer_id` values in the lake.

Could you confirm whether these identifiers belong to the same company-wide customer ID system, and whether there are any mappings or exceptions I should account for?

Best,  
Hermes
```

原文没有给出该上级最终确认，跨系统关系继续未确认。上级资格须由独立可信来源证实；Sponsor/Boss 的明确指定也要先落成 Admin 显式授予记录，并绑定对象、类型与有效期。同资产同 Authority Type 只允许一个当前 active approver，跨系统关系另挂 `cross_domain_relationship / integration authority`；换人不能丢历史证据。

<a id="case03"></a>
### C03 潜在 Scope 扩展，同时涉及敏感元数据

- **起点**：当前整理收入、成本、退款；从 Sales 的 `sales_rep_id` 发现可能有关的员工绩效域。
- **修正后的正常动作**：不永久自行定为无关，也不直接接入；先检查 Hermes 是否能知道元数据、Sponsor 是否能知道将要披露的内容，再向原发起人做 scope confirmation。
- **示例敏感字段**：salary、bonus、performance_rating、absence、personal_id。无资格时不读取原值、不泄露表名/字段，连“发现了这个域”是否可说也先看存在性权限。
- **人答不同分支**：不纳入→deferred；纳入→更新 Scope 后再独立走 Discovery/Authority/Access，不能直接复制。
- **阻碍/判据**：Scope 同意不等于 raw-data access；原文抽象措辞仍可能泄露受限存在性，不能照模板一律发送。

来源：[M149—M154](素材/分享对话原文.md#m149)。

#### 原模拟邮件、回复与现场记录

以下邮件/短句保留 scope 机制演变。M152 的邮件在 M154 补 Visibility 后才具备完整发送前提；任何描述，包括“受限员工域存在”，都只能在接收者获准知道时发送。

<a id="mail-m152-e01"></a>
**原模拟邮件｜Hermes → Sponsor；原文写作块 ID：59241**（[M152](素材/分享对话原文.md#m152)）

<!-- original-excerpt:excerpt-056 -->
```text
Subject: Scope check: employee performance data

Hi,

While tracing the Sales data, I found a related employee performance dataset that could potentially support sales-efficiency analysis.

This is outside the current customer revenue/cost scope, so I haven’t accessed or connected it.

Would you like me to include this area in the current project, or leave it for later?

Best,  
Hermes
```

<details>
<summary>较早的仅在报告中归档写法（后续已改为向原发起人确认）</summary>

**原报告短句**（[M150](素材/分享对话原文.md#m150)）

<!-- original-excerpt:excerpt-057 -->
```text
发现销售人员绩效数据可能支持后续销售效率分析，但当前不属于客户收入/成本范围，因此未继续接入。
```

</details>

<details>
<summary>敏感披露的原反例与候选措辞（不是可直接发送的统一模板）</summary>

**原文明确不能直接告知的反例**（[M154](素材/分享对话原文.md#m154)）

<!-- original-excerpt:excerpt-058 -->
```text
我发现了员工工资、奖金和绩效字段。
```

**原抽象措辞：仍可能泄露受限存在性，依 R08 最终决定不得无权披露**（[M154](素材/分享对话原文.md#m154)）

<!-- original-excerpt:excerpt-059 -->
```text
我发现一个与销售人员相关的数据域，可能支持销售效率分析，但它涉及受限的员工数据。是否需要我发起正式的 scope/权限评估？
```

**原具体措辞：仅适用于已获相应 visibility 的 Sponsor**（[M154](素材/分享对话原文.md#m154)）

<!-- original-excerpt:excerpt-060 -->
```text
发现 employee performance dataset，包含绩效相关字段，是否纳入当前 Scope？
```

</details>

Sponsor 的“不用/加进来”只是原文条件分支，没有对应完整回信；不补写成已获得 scope 批准。

<a id="case04"></a>
### C04 字段定义有不同说法，组织多人讨论

- **起点**：`sales.orders.net_revenue`，A 说已扣折扣，Finance B 说尚未扣退款。
- **应发生**：保留两个 claim 和各自来源，确认其是否其实互补、适用条件是否不同；需要统一最终定义时建立 R-SEM-01，找 A/B/Revenue authority C 讨论。
- **沟通**：Hermes 问“这是最终净收入，还是中间销售金额”，默认观察，必要时总结确认。
- **阻碍/判据**：不能最近消息覆盖旧说法，也不能把“已扣折扣”“未扣退款”自动判成逻辑矛盾。正式定义和 authority 一起核验。
- **原文进度**：用户说“不测了，下一个”，没有后续讨论、确认或终态；此场景保留待续，不能标完成。

来源：[M155—M157](素材/分享对话原文.md#m155)。

#### 原模拟邮件、回复与现场记录

**原场景中的两条业务说法**（[M156](素材/分享对话原文.md#m156)）

<!-- original-excerpt:excerpt-061 -->
```text
当前表：
sales.orders

字段：
net_revenue

Sales Owner A：
“这是扣完折扣后的收入。”

Finance Owner B：
“这个字段还没扣退款，不能直接当净收入。”
```

<a id="mail-m156-e01"></a>
**原模拟邮件｜Hermes → A、B、Revenue/Finance authority C；原文写作块 ID：46318**（[M156](素材/分享对话原文.md#m156)）

<!-- original-excerpt:excerpt-062 -->
```text
Subject: Confirming net_revenue definition

Hi A, B, and C,

I’m validating the definition of `sales.orders.net_revenue`.

I currently have two pieces of information:
- Sales: the value is after discounts.
- Finance: refunds are not yet deducted.

Could you help confirm the authoritative definition of this field, and whether it should be treated as final net revenue or as an intermediate sales amount?

Please reply in this thread so I can keep the decision and evidence together.

Best,  
Hermes
```

**用户在原演练中跳过此场景的指令**（[M157](素材/分享对话原文.md#m157)）

<!-- original-excerpt:excerpt-063 -->
```text
不测了 下一个
```

两条说法可能互补；保留原邮件对完整定义的询问，不沿用“词面不同即 conflict”的自动判定。没有补写多方回复或 confirmed 终态。

<a id="case05"></a>
### C05 延迟回复到达时，字段与目标都变了

- **起点**：T-205 等 Alice 确认 `refund_status`；三个 working day 内字段变为 `status_code`，Sponsor 把 Objective 缩为 2025 年之后。
- **第 4 个 working day 人答**：“refund_status=2 表示 fully refunded。”讨论回复先作为旧字段 claim，不直接作新字段 confirmed。
- **应发生**：Router 定位 T-205，读最新 Project/Asset/Knowledge，识别回答针对旧字段；问 Alice 只是 rename 还是编码也改了。
- **阻碍/判据**：不能直接写 `status_code=2 → fully refunded` 为 confirmed，不能继续已暂缓的历史范围。语义须整理规则再正式确认；执行审批仍适用才 replay 原参数，Scope 实质变化/Schema 影响动作/Source 或敏感级别变化/任务失效则重批，不改旧票。若任务已完成则仅记录，不重做。

来源：[M158](素材/分享对话原文.md#m158)。

#### 原模拟邮件、回复与现场记录

**第 4 个 working day、Alice 的原模拟回复；原文无主题**（[M158](素材/分享对话原文.md#m158)）

<!-- original-excerpt:excerpt-064 -->
```text
`refund_status = 2` means fully refunded.
```

**Hermes 的原追问正文；原文无主题**（[M158](素材/分享对话原文.md#m158)）

<!-- original-excerpt:excerpt-065 -->
```text
`refund_status` has since been replaced by `status_code`. Is this only a field rename with the same coding, or did the status definitions change as well?
```

<a id="case06"></a>
### C06 Cycle 到线仍有四种未完成任务

| 任务 | 到线时状态 | 报告与处理 |
|---|---|---|
| T1 | 等 Finance Owner 回复历史 mapping | 保留 waiting，列出等谁/什么 |
| T2 | 已批准、计划 22:00 copy，尚未开始 | 列出 scheduled + paused，暂停到下一 Cycle；不能在本次停止后自动开始 |
| T3 | ready，尚未开始画像 | 不启动新工作，列可做但暂停 |
| T4 | CRM/Sales 关联未确认 | 保留 unresolved，列缺哪方证据 |

**触发就直接报告**，不能先等 T2、讨论任务处理或因 ready task 多而延长本轮。R05 已定 T2 尚未开始必须暂停下一 Cycle；主线 [CY-1 到线](#main)是同一条规则的实演。另一个分支若 copy 在截止前已进入 Running，则报告先发，copy 仅完成当前动作写 State/Result，之后不画像、不新开评估 Session。新资产可以登记 assessment_required。

来源：[M159—M162](素材/分享对话原文.md#m159)。

#### 原模拟邮件、回复与现场记录

原文给了任务状态和报告结构，没有完整报告邮件；保留原状态与用户对“立即报告”的纠正，不新造邮件。

**到线时的四条原任务状态**（[M160](素材/分享对话原文.md#m160)）

<!-- original-excerpt:excerpt-066 -->
```text
T1 waiting_human
等 Finance Owner 确认历史 mapping

T2 scheduled
今晚 22:00 执行已审批的 Bronze copy

T3 ready
还有一张新表可以开始 profiling

T4 unresolved
CRM ↔ Sales relationship 还没确认
```

**用户纠正停止与报告顺序的原话**（[M161](素材/分享对话原文.md#m161)）

<!-- original-excerpt:excerpt-067 -->
```text
不是然后 是直接就出报告了
```

**纠正后的原停止流程**（[M162](素材/分享对话原文.md#m162)）

<!-- original-excerpt:excerpt-068 -->
```text
Cycle 到点
↓
停止开启新分支
↓
直接生成 Cycle Report
↓
发 Sponsor
↓
等待下一轮指示
```

<a id="case07"></a>
### C07 用户要求利润分析，且追问用了哪些表

- **请求**：“哪个客户利润最高？顺便告诉我用了哪些表。”
- **应发生**：识别分析越界；在请求者 visibility 允许范围内，仅说明治理后数据的准备度、质量、新鲜度和确认关系，实际分析交合适的 Analytics/BI 入口。
- **示例治理状态**：Revenue、Cost、Refund ready，Customer mapping confirmed；2020+ coverage=99.6%，历史有 0.4% unresolved。数值只用于模拟，必须标适用时间范围。
- **阻碍/判据**：即使数据已在 Lake，也不直接算利润排名；对无权者不能泄露 Finance 表名，甚至不能默认承认存在某个受限域。原文示例拒绝措辞须受同一道 visibility 约束。

来源：[M163—M164](素材/分享对话原文.md#m163)。

#### 原模拟邮件、回复与现场记录

**原用户请求**（[M164](素材/分享对话原文.md#m164)）

<!-- original-excerpt:excerpt-069 -->
```text
“哪个客户利润最高？顺便告诉我用了哪些表。”
```

**在可见范围内说明治理准备度的原回复**（[M164](素材/分享对话原文.md#m164)）

<!-- original-excerpt:excerpt-070 -->
```text
I can’t perform the customer-profitability analysis itself, but I can tell you whether the governed data needed for that analysis is available, which approved datasets support it, and whether their quality is sufficient.
```

**原拒绝短句；可能泄露 Finance 数据存在，不能无条件复用**（[M164](素材/分享对话原文.md#m164)）

<!-- original-excerpt:excerpt-071 -->
```text
Relevant governed data exists, but part of the underlying finance data is outside your current visibility.
```

最后一条仅保留为历史原文。[R08](2_实现方法_checklist.md#r08) 已定：不能向无权者透露敏感域存在；Visibility 不明确时找独立可信来源确认，不让请求人本人自证。

<a id="case08"></a>
### C08 Memory 说 5 张，真实 State 已是 7 张

- **起点**：Project Memory 说接入 5 张、Finance mapping 未确认；Project State 实为 7 张，SQL 记录 mapping confirmed，CRM 已画像。
- **应发生**：新 Session 先读 Memory Summary，再对照相关最新 State/SQL；冲突时以事实源为准，刷新 Working Context 并必要时重写摘要。Session/Cycle 收尾、重大 Mission/Objective 变化时刷新；不能用旧 Memory 反写事实。
- **正常后续**：连续同一 Task 的 Session 已够用时不重复取摘要；跨 Task/长时间恢复需要全局背景才按需取 Project Memory，联系 Bob 时才取 Person Memory。
- **阻碍/判据**：会话连续不代表事实永远新鲜；执行前仍校验对应 State/SQL。不能全量重读全部 Memory 来代替有针对性的状态取回。

来源：[M165—M168](素材/分享对话原文.md#m165)。

#### 原模拟邮件、回复与现场记录

原文是摘要与真实状态对比，没有模拟邮件；三份文本逐字保留。

**旧 Project Memory**（[M166](素材/分享对话原文.md#m166)）

<!-- original-excerpt:excerpt-072 -->
```text
当前已接入 5 张表，
Finance mapping 仍未确认。
```

**真实 State / SQL Knowledge**（[M166](素材/分享对话原文.md#m166)）

<!-- original-excerpt:excerpt-073 -->
```text
Project State:
已接入 7 张表

Knowledge SQL:
Finance mapping 已 confirmed

Asset State:
crm.customer_profile 已完成 profiling
```

**修正后的项目摘要示例**（[M166](素材/分享对话原文.md#m166)）

<!-- original-excerpt:excerpt-074 -->
```text
当前已接入 7 张相关表。
Sales / Finance customer mapping 已确认。
CRM 已完成 profiling。
当前主要未解决项是 historical mapping exceptions。
```

<a id="case09"></a>
### C09 同一个人一封 Re: 回复三个问题

起点：Bob 有 Q1 折扣定义、Q2 历史 customer ID、Q3 refund owner 三个 open questions。

| 回复片段 | 路由 | 写回与下一步 |
|---|---|---|
| `order_total` 已扣过 discount | Q1 → 语义 Task | 单域单一事实：Bob 的 semantic authority 验证通过且无冲突即可直接 `confirmed`，否则记 candidate（[R18](2_实现方法_checklist.md#r18)）；Router 无权决定 |
| 老 customer ID 不确定，可能要问 MDM | Q2 → mapping Task | 保持 unresolved，新增 MDM discovery lead |
| refunds 现在归 Alice 管 | Q3 → owner discovery Task | Alice 先 candidate，查目录/authority 后决定联系 |

先用 Re:/thread，再 Task/Question ID、Person+Open Questions、LLM 语义路由；默认读取 Task Goal/Summary、Open Questions、最近 3 条消息和 Incoming Message，低置信再扩上下文，仍不确定则留 unresolved。Question 是 Task 内子项；已有 Task 就路由，独立目标才建 Task，边缘信息进 Backlog。Session 内超 Scope 但阻塞的建 dependency/child task，不阻塞的 backlog；Done Criteria 满足且无当前使用 blocker 可关闭，不要求把三个问题都解决。Bob 只需一封简短确认或不回，答完释放配额也不自动补**新主题**问题；为关闭 Q2 的 clarification 可以立即追问（[R20](2_实现方法_checklist.md#r20)）。

来源：[M169—M170](素材/分享对话原文.md#m169)，机制详见 [M057—M066](素材/分享对话原文.md#m057)。

#### 原模拟邮件、回复与现场记录

**Bob 一封 Re: 邮件的完整原模拟正文**（[M170](素材/分享对话原文.md#m170)）

<!-- original-excerpt:excerpt-075 -->
```text
`order_total` 已经扣过 discount。  
老 customer_id 我不太确定，可能要问 MDM。  
refunds 现在归 Alice 管。
```

原文没有提供具体主题，也没有展开 Hermes 的确认回信。保留“一封简短确认或不回”的沟通选择，不为三个 Task 补造三封回信。

## 模块覆盖与后续验收入口

| 模块 | 主要覆盖节点 | 最关键的失败形状 |
|---|---|---|
| 1 Cycle / Trust | N16、C06 | 到线仍开新分支、等任务完成才报告、未经放行开始下一轮 |
| 2 Role / Mission | N01—N03、C03、C07 | 首封问卷过重、自行扩大 scope、做分析越界 |
| 3 Discovery | N01—N03、N09—N12、C02 | 只等用户喂表、看到表就扫、缺 authority 不继续寻找 |
| 4 Steward Loop | N04—N10、N15、C02 | 大统计打生产、负值自动清洗、同名字段直接 JOIN |
| 5 Knowledge | N06、N10—N15、C04、C08 | 单方推断变 confirmed、覆盖冲突、摘要盖过事实 |
| 6 Coordination | N02—N03、N07、N11—N14、C05、C09 | 回复投错 Task、换人丢上下文、每封必回、旧状态续跑 |
| 7 Guardrails | N04—N05、N10—N15、C01—C03、C07 | 知情人当审批人、转介当授权、metadata 级权限当 data 级、权限不明就扩展、泄露受限元数据 |

这些节点用于以后补已有 Eval 的行为检查；当前只完成设计走读整理。主线未给出的 Sales 负金额确认、Silver 产物、C04 后续不能当成已验证结果。R01—R16 虽已定，仍须按[实施 checklist](2_实现方法_checklist.md)做代码与行为验收；Gold 不作为核心完成指标。

<a id="mail-index"></a>
## 原模拟邮件版本索引

共保留 **32 封完整邮件版本**（30 个 email writing 原文块，另含 M088 两封早期首轮邮件）；短回复、通知、状态和报告原文另在各节点展示。这里统计的是文本版本，不是计划发送次数。

| 原文版本 | 节点 / 收件人 | 原主题 | 版本身份 |
|---|---|---|---|
| [M088-E01](#mail-m088-e01) | N01 / Sponsor | Confirming the goal and scope | 早期首轮邮件 1 |
| [M088-E02](#mail-m088-e02) | N01 / Sponsor | Where should I start with the data? | 早期首轮邮件 2 |
| [M090-E01](#mail-m090-e01) | N01 / Sponsor | Getting started | 原模拟邮件 |
| [M092-E01](#mail-m092-e01) | N02 / Boss | Sales and finance data | 过渡版本邮件 |
| [M094-E01](#mail-m094-e01) | N02 / Sponsor | Thanks — I’ll start from there | 原模拟邮件 |
| [M094-E02](#mail-m094-e02) | N02 / Boss | Getting started with the sales and finance data | 原模拟邮件 |
| [M096-E01](#mail-m096-e01) | N03 / A / Sales | Sales data | 原模拟邮件 |
| [M096-E02](#mail-m096-e02) | N03 / B / 最初 Finance 联系人 | Finance data | 原模拟邮件 |
| [M098-E01](#mail-m098-e01) | N04 / A | Re: Sales data | 原模拟邮件 |
| [M104-E01](#mail-m104-e01) | N05 / A / 此例兼任数据与系统负责人 | Re: Sales data | 原模拟邮件 |
| [M104-E02](#mail-m104-e02) | N06 / A | Re: Sales data | 较早的负金额问题邮件 |
| [M108-E01](#mail-m108-e01) | N06 / A | Re: Sales data | 后续明确湖内画像的邮件版本 |
| [M108-E02](#mail-m108-e02) | N03 / B / 最初 Finance 联系人 | Finance data | Finance 初信续写版本 |
| [M110-E01](#mail-m110-e01) | N07 / Boss | Finance data contact | 原模拟邮件 |
| [M112-E01](#mail-m112-e01) | N08 / F / 新 Finance 联系人 | Re: Finance data | 早期关系追问邮件 |
| [M120-E01](#mail-m120-e01) | N10 / A / Sales | Re: Sales data | 原模拟邮件 |
| [M120-E02](#mail-m120-e02) | N10 / F / Finance | Re: Finance data | 原模拟邮件 |
| [M124-E01](#mail-m124-e01) | N11 / C / Customer Data Owner 或 MDM Owner | Confirming customer ID relationship | 原模拟邮件 |
| [M124-E02](#mail-m124-e02) | N11 / F / Finance 联系人 | Re: Finance data | 原模拟邮件 |
| [M126-E01](#mail-m126-e01) | N12 / A、Finance 联系人、C（原文写 A/B/C） | Customer ID mapping — confirmation needed | 原模拟邮件 |
| [M132-E01](#mail-m132-e01) | N14 / A、Finance 联系人、C | Re: Customer ID mapping — confirmation needed | 原模拟邮件 |
| [M134-E01](#mail-m134-e01) | N15 / C | Re: Customer ID mapping — validation exceptions | 原模拟邮件 |
| [M136-E01](#mail-m136-e01) | N16 / Sponsor | Weekly data stewardship review | 原模拟邮件 |
| [M140-E01](#mail-m140-e01) | C01 / D | Re: Finance cost data | 原模拟邮件 |
| [M140-E02](#mail-m140-e02) | C01 / E / 有权审批人 | Access approval for finance.cost_detail | 原模拟邮件 |
| [M144-E01](#mail-m144-e01) | C02 / CRM Owner | Confirming CRM customer ID | 原模拟邮件 |
| [M146-E01](#mail-m146-e01) | C02 / CRM Owner | Re: Confirming CRM customer ID | 原模拟邮件 |
| [M146-E02](#mail-m146-e02) | C02 / A / Sales | CRM and Sales customer ID | 原模拟邮件 |
| [M148-E01](#mail-m148-e01) | C02 / A | Re: CRM and Sales customer ID | 原模拟邮件 |
| [M148-E02](#mail-m148-e02) | C02 / 经查验具备对应资格的上级/领域负责人 | Confirming Sales and CRM customer ID relationship | 原模拟邮件 |
| [M152-E01](#mail-m152-e01) | C03 / Sponsor | Scope check: employee performance data | 原模拟邮件 |
| [M156-E01](#mail-m156-e01) | C04 / A、B、Revenue/Finance authority C | Confirming net_revenue definition | 原模拟邮件 |
