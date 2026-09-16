# Snapshot 测评模型

这份文件只定义**测评链路的概念和分工**。谁在代码里实现、实现到哪一步，看
[交接记录](handoff/)；产品目标看 [readme.md](../readme.md)。概念变了改这里，
进度变了别改这里。

一句话：

> Case 定义世界，Snapshot 是某时刻**可恢复的完整状态**，Gate 定义边界，
> max_turn 定义测试窗口，New Trajectory 记录这次怎么走，
> Expected Outcome 是隐藏目标状态，Evaluator 最后判分。

```
Case                       固定世界 / fixture
  人、数据库、原始表、权限规则
        │
        │ restore
        ▼
Snapshot                   某时刻真实可恢复状态
  ├─ Agent-visible state   邮件 / memory / todo / 当前 context
  │                        —— 当时 Hermes 真正能看到的东西
  ├─ Environment state     DB 状态 / 已连接 source / 已登记 tables /
  │                        relationships / approvals / contacts
  └─ Old Trajectory        只用于页面展开、debug、audit
                           不重新塞给 Hermes
        │
        ▼
Run                        Hermes 从这个 Snapshot 正常继续，固定 max_turn
        │
        ▼
New Trajectory             这次所有 messages / tool calls / results
        │
        ▼
Evaluator（Hermes 不可见）  对 Expected Outcome 判分
        │
        ▼
PASS / FAIL
```

## 概念

**Case —— 固定的测试世界（fixture）。** 人物、职责、数据源、基础资产、权限边界、
外部系统。只有这些基础环境变了才需要新 Case。Northwind 可以长期只有一个 Case。
**表清单不属于 Case**，Case 只说这个源库作为资产存在、边界在哪。

**Snapshot —— 某时刻真实可恢复的完整状态。** 不是"过去轨迹的摘要"，是把那一刻
**恢复出来**。两部分都要恢复：

- **Agent-visible state** —— 当时 Hermes 真正能看到的东西：邮箱里的邮件、memory、
  todo、当前 system / context。
- **Environment state** —— 当时系统里已经成立的事实：DB 状态、已连接 source、
  已登记 tables、relationships、approvals、contacts。

**Old Trajectory —— 到达这个 Snapshot 之前发生过什么。** 它**只**用于页面展开、
debug 和 audit，**不重新塞给 Hermes**。Snapshot 关联 old trajectory 用于追溯，
但真正恢复的是 old trajectory 执行到那一刻后形成的完整 State。

**Run —— 从某个 Snapshot 开始的一次新执行。** 一次 Run 是一个 trial。
**被测对象是整套 agent**（gateway ＋ 插件 ＋ Skills ＋ 门禁 ＋ Connector ＋ 治理库
＋ 邮件链路），不是其中那个模型 —— 失败落在 Skill、门禁、工具签名还是模型判断上，
都是这套系统的缺陷，不能因为「那是 prompt 的问题」就不算。跑一次 Run 的那套东西叫
**Snapshot Runner**：isolate run、过程记录、max time、max turn、final state dump。
两者都见 [snapshot-testing.md](snapshot-testing.md)。

**New Trajectory —— 这次执行实际走了什么路径。** 模型输出、tool call、tool 结果、
审批、邮件、block、数据库变化。**必须和 Snapshot 分开**：Snapshot 是起点，
New Trajectory 是这次怎么走的。

**Tool Scope / Gate —— 规定 Hermes 允许做什么。** **安全边界，不是测试答案。**
使用正常生产配置和 `pre_tool_call` 治理规则。Case 的 `tool_scope` 不得改变
这些规则，也不得通过错误消息告诉模型「本轮只能调用哪些工具」。

**当前实现约定（2026-09-15）**：演练网关有**两道外部停止**，都不依赖模型自觉：

- **固定时间窗**（`DEMO_WINDOW_SECONDS`，默认 900 秒）：到点停网关及其进程组。
- **硬 turn**（`CLAW_MAX_TURN`）：由 `pre_tool_call` 计数，**一次 turn = 一次进
  gate 的工具调用，被 block 的也算**；到线后一律不放行（连读元数据也不放），
  计数范围是这一轮的 run id（`CLAW_TURN_SCOPE`，由容器 entrypoint 传）。
  **不设 = 不限**，生产默认就是不限。数不出来按停止处理 —— 数不清的窗口等于没有窗口。

上限值来自部署配置，**不来自 case 文件**：Agent 容器不挂载 `demo/`，门禁不该认识
Snapshot。Snapshot 里的旧 `max_turn` / `tool_scope` 字段仍不参与执行，只作旧材料记录。
下文的 Calibration 仍是测评设计，不是已实现的东西。具体输入隔离、触发与证据边界见 [Snapshot 测试契约](snapshot-testing.md)。

**Expected Outcome —— 隐藏的目标状态，也就是 Oracle。** 只给 Evaluator，不给 Hermes。
浏览器可以展示一句人话版「怎样算通过」，**机器判据不进模型上下文**。

**Evaluator —— max_turn 之后检查最终环境状态 + New Trajectory**，判断有没有达到
Expected Outcome。它**只判分，不指导 Hermes 怎么做**。
**优先检查环境结果**，trajectory 用来理解为什么会这样。缺证据只能 pending，
相反的实际结果才 fail。

**这些概念落到 case 文件上就三个字段**（Anthropic commerce-evals 的 case shape，
本项目两层都按它对齐）：

| 字段 | 放什么 | 对应上面哪个概念 |
|---|---|---|
| `state` | 这一刻已经成立的事实 | Environment state ＋ Agent-visible state 里不靠消息承载的部分 |
| `turns` | **默认一条**当前消息 | Agent-visible state 里靠消息承载的那部分 |
| `expected` | 隐藏判据 | Expected Outcome |

本项目行为 eval 那层的 `stimulus` 就是 `turns` 的单条形态；演练台那层的当前来信
（`opening`）同理，而 Snapshot 的 `state` 由 `restore_snapshot_state()` 写回库里。

**max_turn —— 测试窗口。** **Hermes 不知道「做到哪一步就算答案」**，它只是在限定
turn 内自主完成任务。达到 max_turn 后**由外部强制停止**，再统一判分。

**`max_turn` 和 `max_tool_calls` 是两个东西，别混。**

| | 是什么 | 超了会怎样 |
|---|---|---|
| `max_turn` | **执行窗口**，本项目写在 Snapshot 上，由 `pre_tool_call` 数并强制停止 | 跑不动了，然后判分 |
| `expected.max_tool_calls` | **效率判据**，按「一个守规矩的 agent 需要多少次」定 | 这条 case 判 FAIL |

commerce-evals 的 `max_tool_calls` 是后者，而且它连「结束这一轮的那次展示调用不计入」
都写明了。Calibration 调的是前者（窗口），效率判据要不要加是另一件事。

## 起点怎么构造：注入 state，不转述过去

**把过去总结成一段话再喂给模型，测出来的就不是原来的系统。**

这条不是口味问题。主流做法都是"恢复状态"而不是"转述过去"：

- **LangGraph 的 checkpoint** 是某时刻的完整 graph state，`invoke(null, checkpointConfig)`
  以"state exactly as it was"继续执行，checkpoint 之前的节点不重跑。它不是轨迹摘要。
  `get_state_history` 则专门用来回看"agent 在做那个决定时到底知道什么" —— 正是
  old trajectory 的 audit 用途。
- **τ-bench** 直接忽略 transcript，只比最终数据库状态和标注好的目标状态，
  而目标状态不给 Agent。
- **Anthropic 的 agent eval 指南**把 transcript/trace 定义为一次 trial 的完整记录，
  outcome 定义为跑完后的真实环境状态，并明确建议**优先用 outcome / state 检查**，
  transcript 用来复盘原因。它举的反例正好对得上本项目：agent 说"航班已订"
  而数据库里没有订单。

所以判断一条信息能不能进模型上下文，只有一个问法：

> **生产环境里的 Hermes 在那一刻，是不是真的就看到这个东西？**

真实收到过的邮件 —— 是，进。人为写的「之前你已经连接 Northwind、发现 8 张表、
获得审批」 —— 不是，不进；那八张表要让它在环境里**查得到**，不是在正文里**读到**。

### 前置条件注入 state，不靠历史消息表达

Anthropic 的 commerce-evals 规范把这条写成了硬规则：

> `state` is the precondition; `turns` is **one message** unless the behavior under
> test is carrying state across turns.
>
> Preconditions go in injected state …, which the runner loads into the session
> state and the memory store before the turn. **Earlier turns are for state the
> behavior itself carries, and for nothing else.**

所以默认形状是 **state + 一条当前消息**，不是一段对话史。要测「已连接之后会不会
正确发现并登记表关系」，**不要**去构造「老板来信 → 找 DBA → DBA 回信 → connect_source
→ 审批 → tool result」那一串；直接把 `source.northwind.connected = true`、
`approvals.connect_source = approved`、`tables = [customers, orders]`、
`relationships = []` 注入成事实，然后给一条当前消息让它从这里跑。

那串邮件往来**不是**还原，它是另一次测试 —— 而且每多一轮就多一处可能偏离真实起点。

**什么时候才需要历史消息**：当被测行为本身就是「跨 turn 带着状态」的时候。
例如测「前面已经说过不要乙醇汽油，这一轮的推荐会不会忘」，那段对话是被测行为的
载体，必须在 `turns` 里。除此之外没有第二个理由。

**例外（Anthropic 明确强调）**：如果一个缺陷只在**很长、很乱、互相矛盾**的历史之后
才出现，那种历史条件就必须构造进去 —— 不能永远从干净起点测，否则那一类缺陷永远
测不出来。这类 case 的 `turns` 会很长，而且那个长度本身就是被测条件。

真需要历史邮件时，它必须是**真实原文、各自成封投进邮箱**。拼进当前来信的正文就已经
变成摘要了：邮箱里躺着四封信，和一封信里引用了三封信，是两种不同的输入。
但先问一遍上面那个问题 —— 大多数情况下答案是「这段历史该变成 state」。

## 判不判路径

默认**判终态**：最后一次工具调用的参数、以及它产生的状态。路径判据只用在
**路径本身就是被测行为**的地方 —— 「必须先读了再答」、「这个写操作一次都不许发生」。
其余情况用「可接受集合」，不钉死某一条路。

Anthropic 这条要记住，它是 case 烂掉的主要方式：

> When a live run takes a route the case did not expect and the answer was right,
> **widen the case to the acceptable set; do not re-pin it to the route observed.**

实测走了意料外的路、但结果是对的 —— 那是 case 太窄，不是模型错。这时要放宽到可接受
集合，**不是**把 case 改成钉住刚观测到的那条路。后者会让 case 越跑越像「复述上次那一
遍」，而不是「这件事有没有做对」。

反过来，case 在代码改动后变红，只有两种可能：改动弄坏了行为，或者 case 记的是一条
过期的行为。**修掉其中一个，并在提交信息里说清是哪一个。**

## Calibration 与正式 Eval

**Calibration** —— 新 Snapshot 第一次先给较大预算（`max_turn=100`）。观察它成功时
**首次达到 Expected Outcome 的 turn 数 T**，再把正式预算设成 **T + 10**。
更稳妥的做法是跑 3 次，取最慢成功值再加 10。最大迭代次数是 agent loop 常用的
stopping condition，这一条和主流一致。

**正式 Eval** —— Calibration 完成后 `max_turn` 固定，**不因为失败临时加时间**。
超过预算仍未达到 Expected Outcome 就是 FAIL / TIMEOUT。只有预算固定，
不同版本的模型、Prompt、Harness 才能公平比较。

**一个 Snapshot 要跑多次。** Agent 是随机系统，一次通过不代表可靠。
τ-bench 用 **pass^k —— k 次全部成功的概率**，而且是**故意反过来**定的：
客服 agent 每个客户只有一次机会，又没法自检，所以"它多久失败一次"才是要问的。
本项目同理：治理 agent 写错一次治理库就已经错了，要的是可靠性，
不是"试够多次总能成"。

**Minimum Turns to Success** —— 顺便记录的指标。例如 17 / 21 / 19 turns，说明这个
Snapshot 当前最慢成功需要 21 turns。以后比较模型和 Agent 改版时，这个指标比
通过率更有分辨力。

**比失败集合，不比总分。** 两次 live 跑之间总分动一两个点是噪声；有判断力的读法是
把两次的**失败集合**并排看 —— 哪几条新红了、哪几条转绿了。集合级的通过阈值配多次
trial 用，单条 case 的一次绿不是结论。

## 不泄题的三条红线

1. **Expected Outcome 的机器判据不进模型上下文。** 人话版「怎样算通过」只给浏览器；
   判据字段、答案值、判据行留服务端。浏览器和模型是两条路，不要因为同一份数据文件
   就一起放开。
2. **演练元数据一律不进模型上下文。** 终点（`terminal_condition`）、测评说明
   （`note`）、表范围（`table_scope`）、Case / Snapshot 标题、工具白名单
   （`tool_scope`）全部不给模型 —— 其中 `note` 写的就是"本轮判断模型是否……"，
   那是答案；而白名单只剩两个工具时，边界几乎等于答案。
   边界由 Gate 在它真撞上来时告知，这也是生产里它本来会遇到的形状。
3. **页面不预写模型答案、不预排后续步骤。** 判定只认数据库事件、lake 实物、
   provenance 和独立直算。

## 两层共用这套术语

仓库里有两层测评，**术语以本文为准**，各自的 runner 用法和判据清单在自己的 README：

| | 行为 eval（`evals/behavior/`） | 演练台 Snapshot（`demo/cases.json`） |
|---|---|---|
| 测什么 | 机制：这个局面下挡不挡得住、终态对不对 | 完整业务链路：从这一刻起它自己能不能走到交付 |
| 一次执行 | 注入起点 → 推一步 → 取证判分 | 真实 Hermes agent loop 跑到 max_turn |
| 判不判路径 | **不判**（单步，没有路径可判） | 优先判环境结果，路径用来查有没有越权 |
| 入口 | [evals/behavior/README.md](../evals/behavior/README.md) · [案例编写](../evals/behavior/cases/README.md) | [docs/handoff/P0-demo-ui.md](handoff/P0-demo-ui.md) |

"不判路径"和"优先判环境结果、也看路径"不是两套标准 —— 前者是单步注入，路径只有
一步；后者跑几十个 turn，越权发生在路径里，不看就查不出来。两者都**不拿模型自述
当证据**，这一条是共同的底线。

字段对应关系（行为 eval 的 JSON 字段 → 本文概念）：

| 行为 eval JSON | 本文概念 | 说明 |
|---|---|---|
| 一个 `*.json`（一个 `case_id`） | **一个 Snapshot** | runner 管它叫 `case`，那是历史命名；一个业务 Case 可以提取多份 |
| `state.catalog` / `runs` / `tickets` / `roles` / `sources` | **Environment state** | 注入进治理库的已成立事实 |
| `state.mails` | **Agent-visible state** 的邮件部分 | 当时邮箱里真有的信 |
| `stimulus` | 本轮**唯一**新增输入 | 当前来信 / 审批事件 / `resume_tick` |
| `attempt` | 仅 `--driver gate` 用的注入动作 | 不是模型自主决定；`--driver live` 下由模型自己定 |
| `expect` | **Expected Outcome** | 判据，不进模型上下文 |
| `origin` | 来源 | 取值约定由 [cases/README.md](../evals/behavior/cases/README.md) 定：`design` / `regression:<发现>` / `snapshot:<运行>/<截取点>` / `derived:<基线>/<改动>` |
| `polarity` + `pair` | 正反对照 | negative 单绿不算数，要 positive 对照成立 |
| —— | **max_turn** | 行为 eval 没有：它不跑 loop |

**`origin` 是这套体系里最容易烂掉的字段。** `design` 表示这个局面是设计出来的，
不是实测截取的 —— **改了说明文字不会让它变成实测快照**，也不准给旧 `design` 用例
补「已实测」标签或编造 run ID。`snapshot:` / `derived:` 目前只是文本约定，
没有自动采集或来源校验，所以派生改动必须在同名 `.md` 里写清基线和改了什么。

## 页面流程

```
选择 Case
→ 选择 Snapshot
→ 看 Old Trajectory 摘要（可展开完整记录，仅供追溯）
→ 看当前人员 / 数据源 / 表 / 关系 / 权限状态（= Environment state）
→ 看「本轮测什么」
→ 看人话版「怎样算通过」
→ Run
→ 展示 New Trajectory
→ max_turn 到达后停止
→ Expected vs Actual
→ PASS / FAIL
```

页面上的摘要是**给人看的**，和模型上下文是两条路 —— 把页面摘要顺手喂给模型，
就是上面那节说的失真。

这套结构从单 Snapshot 扩到全量回归时形状不变：全测模式是把多个 Snapshot 各跑
k 次再汇总 pass^k，Snapshot 本身的定义、边界和判分都不用改。

## 参照

- [LangGraph Persistence / Checkpointing](https://docs.langchain.com/oss/python/langgraph/persistence)
- [τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045)
- [Tau-Bench Explained: pass^k, Simulated Users, State Grading](https://futureagi.com/blog/tau-bench-explained/)
- [Anthropic — Demystifying evals for AI agents](https://ai-eval.org/deep-dive/anthropic-demystifying-evals-for-ai-agents)
