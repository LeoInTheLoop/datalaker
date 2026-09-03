# 程序重排：Hermes 是主程序

> 定稿于 2026-09-03。这份文件描述**目标形态**与**迁移顺序**，
> 不是现状。现状见 `docs/handoff/R4.md`。

## 0. 一句话

**Hermes 就是 Data Steward Claw 本体。** 邮件、loop、会话持久化、记忆
全都在它上面缝；体外只留一层管起停与限额的监控；再外面是能独立运行的 lakehouse。

## 1. 四层

```
┌─────────────────────────────────────────────────┐
│ ④ supervisor    起停 Hermes · token 限额 · 健康   │  体外，Hermes 挂了它还活着
├─────────────────────────────────────────────────┤
│ ③ Hermes（主程序）= Data Steward Claw            │
│    loop / 工具分发 / 邮件收发 / 会话 / 记忆        │  ← 上游提供
│    ┌───────────────────────────────────────┐    │
│    │ claw/  我们缝上去的插件与工具           │    │  ← 我们写
│    │  门禁(pre_tool_call) · 数据工具 ·        │    │
│    │  Connector 护栏 · 台账 · Policy Sync    │    │
│    └───────────────────────────────────────┘    │
├─────────────────────────────────────────────────┤
│ ② approval      独立进程 + 独立 DB 账号（铁律 2） │  故意不在 Hermes 里
├─────────────────────────────────────────────────┤
│ ① lakehouse     Trino / Iceberg / MinIO / 源库    │  Claw 挂了它仍是完整交付物
└─────────────────────────────────────────────────┘
```

层与层之间只走**接口**，不走 import：supervisor 用进程信号与用量 API，
Hermes 用工具签名，lakehouse 用 SQL。

## 2. 现状与目标最大的一条差

**现在 Hermes 什么都没跑。** 16 条线的大 case、批量 eval，全是
`tests/run_case_full.py` 和 `tests/run_eval_case.py` 这两个脚本**扮演 Agent**
在驱动 —— 它们直接 import `services/*` 并按剧本调用。Hermes 侧唯一实测的
是 11 条「门禁能挂上去且能否决」。

目标形态里，这两个脚本要**换边**：从扮演 Agent 变成扮演人。

```
现在   脚本 ──调用──► services/*                     Hermes 在旁边看着
目标   脚本 ──发消息──► Hermes ──调用工具──► claw/*    脚本扮演王姐、李哥
```

这不是重构，是**把被测对象换了**。在此之前测的是「我写的函数对不对」，
之后测的才是「这个 Agent 干得对不对」。

## 3. 可以删的：Hermes 已经有了

装上真实 Hermes 之后逐项比对的结果。**这一栏是重排的第一价值**：

| 我们写的 | Hermes 已有 | 处置 |
|---|---|---|
| `services/notify/email_channel.py` SMTP/Gmail | `plugins/platforms/email/adapter.py`（1601 行，含 TLS / IMAP 轮询 / 重连） | **删**，配置 email 平台 |
| `services/inbound.py` 的收信与白名单 | 同上，`EMAIL_ALLOWED_USERS` + `_is_automated_sender` | **删传输层**，留语义层（见下） |
| `ops/scheduler.py` 循环 | `cron/` + `hermes_cli/loops.py` | **删**，改注册 cron |
| `services/tracing.py` | `plugins/observability/` | 评估后合并 |
| token 限额 | `hermes_cli/model_cost_guard.py`、`resource_limits.py` | supervisor 对接，不自己数 |
| `services/memory.py` 的通用部分 | `plugins/memory/` | 只留领域记忆（口径、归属） |

`services/notify/{feishu,wecom}.py` 同理 —— Hermes 的 `plugins/platforms/`
下有 22 个适配器，飞书企微都在。

## 4. 必须留的：领域特有，Hermes 不可能有

| 留什么 | 为什么不能交给 Hermes |
|---|---|
| **门禁**（`pre_tool_call`） | Hermes 自带的审批管的是危险终端命令（`rm_rf`/`sudo` 模式）；「谁有权批准接入财务表」是业务判断。而且 `pre_approval_request` **是 observer-only 不能否决**，文档明写「要拦工具用 `pre_tool_call`」—— **铁律 1 因此仍然成立** |
| **Connector 护栏** | AST 准入、行数估算、单表禁 join、账本。这是铁律 3 的落点 |
| **审批两表 + 独立进程** | 铁律 2：与 Agent 同进程时列级 GRANT 形同虚设 |
| **入站语义层** | 四层归属、意图分类、转介解析、双重确认。传输归 Hermes，**「这封信是在回哪件事、算不算批准」归我们** |
| DQ 规则 / 停止点 / 清洗档位 | 纯领域逻辑 |
| 台账 / 权限发现 / Policy Sync | 同上 |
| `runs` 注册表 | 待评估：Hermes 有会话持久化，但「N 条线各挂在不同人身上、谁先批谁先走」是领域语义 |

## 5. 目标目录

```
datalaker/
├── lakehouse/                ① 独立可交付的数据平台
│   ├── infra/                compose · trino 配置 · 初始化 SQL
│   ├── sync.py               源 → bronze（增量 / 全量 / 漂移检测）
│   ├── clean.py              bronze → silver（三档：auto/propose/ask）
│   ├── dq_lake.py            lake 侧全量检查（那个被挪进来的 join）
│   └── policy_sync.py        分类 → Trino 授权规则
│
├── approval/                 ② 独立进程 + 独立 DB 账号
│   ├── callback.py           点击链接 → 落决定
│   ├── tokens.py             HMAC 签名令牌
│   └── store.py              两表 append-only（Agent 侧只读）
│
├── claw/                     ③ 缝在 Hermes 上的插件
│   ├── plugin.yaml           照 plugins/platforms/email 的格式
│   ├── gate/                 pre_tool_call 门禁 + policy 表
│   ├── connector/            源系统唯一出入口 + 账本 + 画像取样入口
│   ├── tools/                注册给 Hermes 的工具，一个文件一组
│   │   ├── discover.py       list_source_tables · get_table_metadata
│   │   ├── profile.py        profile_table · run_dq_check · dq_verdict
│   │   ├── ingest.py         ingest_table · ingest_export
│   │   ├── clean.py          propose_cleaning · apply_cleaning_rule
│   │   ├── govern.py         台账 · 权限发现 · policy_sync
│   │   └── ask.py            停止点 → 走 Hermes 的 clarify 工具集
│   ├── inbound/              入站语义层（归属 / 意图 / 转介 / 双重确认）
│   ├── domain/               纯函数：dq_rules · stop_points
│   └── store/                runs 注册表 · 领域记忆
│
├── supervisor/               ④ 体外监控
│   ├── run.py                起停 Hermes · 健康探活 · 崩溃重启
│   ├── budget.py             token 限额（读 Hermes 用量，超限停）
│   └── status.py             运维面板（现 ops/claw-status.py）
│
├── evals/                    体外判分（形态已经对，不动）
└── tests/
    ├── unit/                 纯函数
    ├── contract/             插件挂载 · 门禁否决（对真实 Hermes）
    └── conversation/         ★ 新形态：跟活的 Hermes 对话
        ├── personas.py       10 个人（现 services/persona.py）
        ├── drive.py          发消息给 Hermes · 收它的回复 · 记轨迹
        └── cases/            剧本（现 evals/cases/acme_full.json）
```

## 6. 迁移顺序：薄切片优先，物理搬迁放最后

**不要先搬目录。** 先证明形态成立，再机械移动。

| 阶段 | 做什么 | 完成的标志 |
|---|---|---|
| **M1 打通一条** ✅ | `list_source_tables` 注册进 Hermes，真模型问一句「northwind 有哪些表」→ 它调工具 → Connector 读真库 → 答出 14 张表与真实行数 | 已实测 |
| **M2 挂上门禁** ✅ | `ingest_table`（L3）挂起 → 批准 → 恢复 → 真写 bronze，全程在 Hermes 里 | 已实测；顺带修掉「审批绑会话」与「plugins 包撞车」 |
| **M3 工具搬家** 🟡 | 已搬 6 个（发现 / 结构 / 画像 / 清洗提案 / 台账 / 接入），多步对话串通 | 645 全绿；剩余工具继续搬 |
| **M4 删重复** | 删掉 email 传输层、scheduler 循环，改用 Hermes 的 | 行数净减少 |
| **M5 驱动反转** | `run_case_full.py` 从扮演 Agent 改成扮演人 | 大 case 里 Hermes 是主语 |
| **M6 supervisor** | 起停 + 限额 | 杀掉 Hermes 能自动拉起；超预算能停 |
| **M7 目录搬迁** | 纯机械移动 + 改 import | 树与第 5 节一致 |

M5 是真正的分水岭 —— 在它之前，所有「Agent 干得对不对」的数字都还是
我的脚本在自问自答。

## 7. 重排中绝不能丢的三件

1. **门禁仍在 `pre_tool_call`**，不因为「工具搬进 Hermes 了」就改成工具内部自检。
   限制散进业务代码分支，就再也证明不了「Agent 绕不过」。
2. **审批服务仍是独立进程 + 独立账号**。它是唯一一个**故意不放进 Hermes**
   的组件，理由写在铁律 2。
3. **判分器仍在体外**。`evals/` 不 import 被测代码这条不因重排松动 ——
   与铁律 2 同一条理由。

### M1 实测记录（2026-09-03）

Hermes 零改动，插件住在 `datalaker/.hermes/plugins/claw/`，
靠 `HERMES_ENABLE_PROJECT_PLUGINS=1` 加载。途中摸清四件文档没写的事：

| 发现 | 说明 |
|---|---|
| **standalone 插件的工具要在 `register()` 里注册** | manifest 的 `provides_tools` + `tools.py` 那条路只对 `kind: platform` 生效（网关启动时预加载）。照文档写不会报错，工具就是不出现 |
| **插件默认不启用** | 扫到 57 个，没写进 `plugins.enabled` 的一律跳过。跟我们的工具白名单是同一条原则 |
| **配置压过环境变量** | `model.base_url` 写死后 `OPENAI_BASE_URL` 不生效（与 `NOTIFY_CHANNEL` 那个坑同款） |
| **非核心工具走延迟下发** | prompt 里只有 `tool_search` / `tool_call` / `tool_describe`，claw 的工具按需取 —— 与我们「不注入省 1.4 万 token」是同一个目的，**这条已经由上游解决了** |

同时补了**模型桩**（`tests/model_stub.py`）：本地 OpenAI 兼容端点，由剧本
驱动，Hermes 照常做工具分发与门禁。它让机制类断言可重复、不联网、零成本；
「真模型会不会选对工具」仍由真端点单独验。

桩本身也踩了一个坑：Hermes 一次运行会发多个请求（主对话 + 标题 + 记忆抽取），
辅助请求不带 `tools`。早先不加区分，**辅助请求把剧本步骤吃掉了**，
表现为工具从来没被调用而日志一切正常。现在按有无 `tools` 路由。

### M3 进行中（2026-09-04）

已注册 6 个工具：`list_source_tables` `get_table_metadata` `profile_table`
`propose_cleaning` `record_finding` `ingest_table`。一次对话里连着调完前五个，
提案正确地对空值率与数量级异常说「**必须你给口径，我不会自己动**」。

加了一条**结构性断言**：manifest 里列出的每个工具都必须在 `policy.py` 里
显式声明级别。以后新增工具忘了声明，测试直接红 —— 铁律 5 从纪律变成机制。

修掉一个只有在 Hermes 里才会暴露的缺陷：**存储句柄跨线程**。
Hermes 在线程池里跑 handler，而 `store()` 缓存的是模块级单例，
SQLite 报「object was created in thread id X and this is thread id Y」，
错误被 handler 吞成一句「失败」，表面上像工具本身有问题。
改成线程本地。`_notify_async` 里其实早写过这条注释
（「后台线程必须建自己的连接」），但只修了那一处 ——
**单线程驱动下看不出来的假设，换个宿主就成了 bug**。

### M2 完成（2026-09-04）

完整闭环第一次全程在 Hermes 里：**L3 被拦 → 外部批准 → 下一次运行消费票据
→ 真写了 bronze（5 行）→ 票据一次性，第三次重新挂起。**

途中修掉两个**一直都在、只是以前不会暴露**的问题：

#### ① 审批绑了会话，进程一重启就作废

`find_valid` 与 `pending` 都把 `run_id` 放进 WHERE。以前驱动方是同一个
长活脚本，run_id 不变，所以看不出来。接上 Hermes 之后每次 `hermes -z`
是新会话：`action_hash` 逐字节相同（`d48622ae…`），`run_id` 却是新 UUID，
于是**批准永远消费不掉**，而且每次会话都为同一件事再发一份审批 ——
三次运行之后 approvals 里躺着三行同指纹的待决请求，人被同一件事打扰三遍。

这与 readme 4.1「状态不能只活在进程里」直接冲突，也与 10.7「不允许把
任何人淹没」冲突。改成**按动作指纹匹配，run_id 只记录不参与**：
批的是「接入 shippers」这件事，指纹相同就意味着效果相同（机制二）；
一次性（`used_at`）与 72 小时过期（`expires_at`）两道限制都还在。

#### ② 两个项目都有顶层 `plugins/` 包

Hermes 启动时导入它自己的 `plugins`（它就是从那儿加载插件的），
`sys.modules["plugins"]` 被占，我们的 `from plugins.datasteward_gate...`
一律 ModuleNotFoundError —— **25 个文件在用这个路径**。

现在在插件的 `_ensure_path()` 里用别名桥过去。**这是 M7 必须改包名最硬的
证据**：两个 `plugins` 撞车不是靠 sys.path 顺序能解决的，Hermes 先导入就先占名。
M7 把 `plugins/datasteward_gate/` 搬成 `claw/gate/` 之后，那段桥接删掉。

### M2 原始记录（2026-09-03）

**已验证成立**（`tests/test_hermes_tool_loop.py`，17/18）：

- L3 动作在 Hermes 里被门禁拦下，**`HERMES_YOLO_MODE` 也拦得住** ——
  oneshot 会开 YOLO 绕过 Hermes 自带的审批，而我们的门禁是 `pre_tool_call`
  钩子，不受它影响。这正是铁律 1 的意思
- 拦截消息把「不要重试、去做别的」传给了模型
- 审批请求落库、**Agent 侧没有产生任何决定**（机制三）
- 没有真的写 bronze
- 票据一次性：再来一次重新挂起

**唯一没过的一条：批准之后没能恢复。**
用独立连接落了 approve 决定，下一次 Hermes 运行仍然重新挂起。

领先假设（**未验证，下个 session 第一件事**）：票据是按
`(action_hash, run_id)` 找的，而每次 `hermes -z` 是一个新会话 ——
`run_id` 变了，`find_valid` 自然找不到上一次的批准。

如果成立，说明**跨会话恢复的票据不该绑会话**：

| 方案 | 代价 |
|---|---|
| 票据只绑动作指纹，不绑 run_id | 别的会话也能消费同一张票 —— 要看这算不算问题 |
| 恢复时把原 run_id 传回 Hermes | 需要 `runs` 注册表与 Hermes 会话 id 打通（第 8 节那个待定项） |
| 票据绑「资产 + 动作」而不是「会话 + 动作」 | 语义上更接近真实审批：批的是「接入 orders」这件事 |

先确认假设再选 —— 别急着改，这条会决定 `runs` 与 Hermes 会话的边界。

## 7.5 关于改不改 Hermes 本体

**可以改，但尽量不改** —— 因为这个 checkout 最终就是产品本身
（Hermes → Data Steward Claw）。优先级：

```
① 项目插件（./.hermes/plugins/）  零改动，Hermes 可随时 rebase 上游
② 配置 / 环境变量                 零改动
③ 改 Hermes 本体                  可以，但每处都要能说清「为什么插件做不到」
```

M1 走的是 ①：`HERMES_ENABLE_PROJECT_PLUGINS=1` 时 Hermes 会扫
`./.hermes/plugins/`，**插件住在 datalaker 仓库里**，Hermes 目录保持干净。
等到需要改品牌、改默认 SOUL、或者某个钩子上游根本没有时，再动 ③ ——
届时把每处改动记在这一节，rebase 上游时才知道要保住什么。

## 8. 已知的待定项

- `runs` 注册表与 Hermes 会话持久化的边界：跨天挂起是 Hermes 的 session
  能力还是我们的领域状态？M2 会给出答案。
- `services/tracing.py` 与 `plugins/observability/` 是否重复，M4 时评估。
- Hermes 的 `plugins/memory/` 能否承载「口径记忆」，还是只放通用记忆。
