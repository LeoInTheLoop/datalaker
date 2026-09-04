# Data Steward Claw —— 项目约定

数字员工形态的数据管家：连接散落的源系统，通过邮件与人确认口径和审批，
在独立 lakehouse 中整理出 one source of truth，同时把权限一并理清。

完整设计见 [readme.md](readme.md)。本文件只放**不变量**——变化的进度写在 `docs/handoff/`。

> **程序形态正在重排**：Hermes 是主程序、它就是 Claw 本体；邮件 / loop / 会话
> 都缝在它上面；体外只留起停与限额的监控层；再外面是可独立运行的 lakehouse。
> 目标结构与迁移顺序见 [docs/restructure.md](docs/restructure.md)。
> **下面五条铁律在重排后一条不变**——尤其第 1、2 条，它们规定的正是
> 哪些东西**不能**被搬进 Hermes。

## 五条铁律

违反其中任何一条，项目的核心卖点即不成立。

### 1. 限制一律走 hook，不写进 prompt
所有约束（分级、票据、指纹、deny list、SQL 护栏、预算）实现在 Hermes `pre_tool_call` 里。
不在 system prompt 里写「你不可以……」，不把限制散进业务代码分支。

> 理由：限制一旦散进 loop 或依赖模型自觉，就再也无法证明「Agent 绕不过」。

### 2. Agent 对 `decisions` 表无写权限
审批 callback 服务**必须是独立进程 + 独立数据库账号**。
与 Agent 同进程时列级 GRANT 形同虚设，机制三直接失效。

### 3. 源系统只读，且负载可控
- 给 Agent 的源系统账号只有 `SELECT`
- 模型生成的 SQL 必须声明 `plane=source|lake`，并先过 `sqlglot` AST preflight
- `plane=source` 是外部源系统：破坏性语句默认拒绝，高负载读必须先问人
- `plane=lake` 是已复制到 datalake 的表：允许 JOIN / 聚合，但只能查 `iceberg.*`
- 禁止用 `plane=lake` 查询 `postgres` / `northwind` / `stage` 等外部 catalog
- 所有源查询都受 `LIMIT` 注入、`EXPLAIN` 扫描估算、超时、串行队列和负载账本约束

> 理由：只读不等于无害。一条模型生成的 SQL 即使不改数据，也可能把生产源系统拖死。

**SaaS 源按「数据面 / 控制面」切开**（readme 8.2），不要为每个 SaaS 写一套管道：

- **数据面走导出**：定时报表自动发邮件 → 复用已有的邮件附件路径，零新代码。
  全量快照顺带解决了增量同步的两个难点（分页不一致、删除看不见）
- **控制面走只读元数据 API**：权限现状（Profile / PermissionSet / 可见范围）导不出来，
  只能读。它量小、低频，**且此处允许关系展开**——与现有 `_meta_exec`
  在 `pg_catalog` 上允许 join 是同一条分界，不是新例外
- **Snowflake / Databricks / BigQuery 不属于这里**：它们有 JDBC，是 readme 18 的
  「已有 lake」分支，**直连，不导出**（导出会丢类型、增量、Delta 时间旅行，还烧对方的计算）。
  `_admit()` 按 `source_id` 切 sqlglot 方言即可
- OAuth 的只读**证明不了**（Salesforce `api` scope 全有全无），
  必须让 Owner 确认连接用户是只读的，并把证据存进 Ledger
- 真要写 API 数据面时（导出被堵死），才补：禁关系展开、配额按官方 20% 预留、
  按不可变主键分页。**备选设计，不是主线**

### 4. 不修改源系统
数据修复发生在 `bronze → silver` 的变换中，不对源表执行任何写操作。
对源系统权限同样只观测、只建议，写入 Remediation Ledger。

### 5. 新工具必须在 `policy.py` 显式声明级别
未声明的工具默认 **L2（需人确认）**，不是默认放行。新增工具时同步加一行。

## 目录

```
readme.md                     完整方案（18 节）
docs/industry-context.md      行业真实数据与来源（设计的外部锚定）
CLAUDE.md                     本文件：不变量
docs/handoff/                 每阶段交接记录
infra/docker-compose.yml      数据平台编排（按 profile 分组）
plugins/datasteward_gate/     Hermes 治理 Plugin —— 项目核心
  ├─ __init__.py              hook 注册与实现
  ├─ policy.py                工具策略表（限制是配置）
  └─ approvals.py             两表 append-only 审批存储
services/                     独立进程，不与 Agent 同进程
  ├─ tokens.py                HMAC 签名令牌
  ├─ approval_callback.py     审批 callback 服务
  ├─ connector.py             源系统唯一出入口 + 账本
  └─ notify/                  通知通道（门禁逻辑与通道无关）
      ├─ email_channel.py     SMTP / Gmail API
      ├─ feishu.py            飞书交互卡片
      └─ wecom.py             企微模板卡片
ops/claw-status.py            运维面板（独立于被监控对象，零依赖）
tests/                        断言（安全类要求 100% 通过）
spike/                        R0 概念验证，可随时删除
```

## 环境约束（R0 实测）

- 开发机 8GB 内存，**Docker VM 仅 3.83 GiB**
- `core`（MinIO + Iceberg + Trino）与 `governance`（OpenMetadata 全家桶）**不能同时启动**
- OpenMetadata 全家桶约 4.5GB，本机跑不起来 → R1–R4 用轻量 `assets` 表替代
- Trino 必须限制 `-Xmx1G`，默认配置会吃掉大部分内存
- Docker 数据盘在外置盘上；Mac 睡眠后若 `docker ps` 超时、daemon 连不上或 I/O error，
  先确认外置盘挂载。若盘已挂载，下一次优先试验：
  `open -a Docker` → `cd infra && docker compose --env-file ../.env --profile core --profile agent up -d`。
  这条是待验证恢复流程；不管用就删。

## 开发约定

- **gate 依赖 `sqlglot`**（AST 准入），新增依赖须同步三处：
  项目 `.venv`、容器镜像（`docker/Dockerfile`）、Hermes 的 `.venv-h`
  （uv 建的无 pip，用 `VIRTUAL_ENV=<path> uv pip install`）
- 跑测试：`HERMES=<hermes-agent 路径> ./tests/run_all.sh`
  - 不接外置盘（Docker 起不来）时，**不依赖 docker 的 395 条必须全绿**
  - 接上 docker 后总数更多，**数字待下次实测校准** —— 不要写一个没跑过的数，
    那正是「整组静默 SKIP 而回归看着还是绿的」的来源
- 看状态：`python3 ops/claw-status.py`（退出码 2 = 有告警）
  - 不设 `HERMES` 时会跳过真实集成那一段
  - Hermes 需要 python 3.11–3.13，本机 3.14 不兼容：用 `uv venv --python 3.13 .venv-h`
  - 本机 Hermes 在 `/Users/lingyukong/Documents/GitHub/hermes-agent`（`--depth 1` clone，
    未做任何改动 —— 本项目以 plugin 形态挂上去，从不 fork 它）。
    不设 `HERMES` 时第 5 组 11 条会静默跳过，**回归看着还是绿的**
- 起数据平面：`cd infra && docker compose --profile core up -d`
- 临时的取舍用 `ponytail:` 注释标记，欠账用 `TODO(R<N>):` 标记
- 提交前确认没有把凭证写进 `infra/trino/catalog/*.properties`

## 参考

- Hermes Agent（MIT，Python）：`NousResearch/hermes-agent`
  - `pre_tool_call` 契约：`website/docs/user-guide/features/hooks.md`
  - Email 适配器：`plugins/platforms/email/adapter.py`
- 推理端点：`https://inference-api.nousresearch.com/v1`（OpenAI 兼容）

### OpenMetadata —— 只抄 schema，不用其 agent / ingestion 层

> 结论先行：**只当「连接参数工具箱」+ 读侧 catalog。它的 agent / MCP 层和
> ingestion 取数进程一概不用。** 评估于 2026-09-02，OM 2.0.1。

OM 2.0 把自己重新定位成 "Open Context Layer for Data and AI"，宣传里有
context / ontology / memory / MCP server / AI SDK。名字听起来跟本项目重叠，
实际分工完全不同，容易误判，所以在这里写死。

#### 能用的：连接参数 schema（当文件读，不装包不起服务）

OM 为 130+ 种源维护了连接参数的 JSON Schema，**是纯文件，可以单独抄**：

```
open-metadata/OpenMetadata
  openmetadata-spec/src/main/resources/json/schema/entity/services/connections/
    database/    postgresConnection.json  snowflakeConnection.json
                 bigQueryConnection.json  databricksConnection.json
                 athenaConnection.json    deltaLakeConnection.json  ...
    dashboard/ · pipeline/ · messaging/ · storage/ · api/
```

拉一个文件即可（不必 clone 整个仓库）：

```bash
P=openmetadata-spec/src/main/resources/json/schema/entity/services/connections
gh api "repos/open-metadata/OpenMetadata/contents/$P/database" --jq '.[].name'
gh api "repos/open-metadata/OpenMetadata/contents/$P/database/snowflakeConnection.json" \
  --jq '.content' | base64 -d
```
网页版同路径可直接看：`https://github.com/open-metadata/OpenMetadata/tree/main/$P`

**什么时候用**：给 `services/connector.py` 新增一个源时，照着抄该源的连接字段名、
必填项、认证方式分支（password / IAM / OAuth / keypair）、SSL 与代理选项。
省掉翻各家驱动文档的时间。**只抄字段定义，不引入 `openmetadata-ingestion` 包**
——那个包会拖进 Airflow 一系列依赖，本机装不下也用不上。

#### 能用的：读侧 catalog（位置不变，时机推后）

readme 第 3、13 节给 OM 的位置（Catalog / Lineage / Owner / DQ / Classification，
走 REST API 自封 tool）**不变**。但受环境约束（全家桶 ~4.5GB，Docker VM 只有 3.83GiB），
R1–R4 用轻量 `assets` 表顶替，接口保持一致，R5 需要策略引擎时再决定是否真上。

#### 不用的：MCP / agent 层

**它当前不是权限边界，拿它当门禁会让铁律 1 失效。** 证据（都在 OM 自己的 issue 里）：

- [#30023](https://github.com/open-metadata/OpenMetadata/issues/30023) `search_metadata` / `semantic_search` 两个 MCP 工具不做 domain RBAC——
  `SearchUtils.shouldApplyRbacConditions()` 要求 `!subjectContext.isBot()`，
  而 **bot token 正是接 MCP 的标准方式**，于是直接跳过。官方 2026-08 回复推到 2.1+
- [#32355](https://github.com/open-metadata/OpenMetadata/issues/32355) MCP 的 `patch_entity` 直接调 repository，绕过 PATCH 授权与实体生命周期
- [#32457](https://github.com/open-metadata/OpenMetadata/issues/32457) 官方在评论里自己写死：persona 的 context scope
  「**是相关性过滤，不是权限边界**」，agent 每次调用可带 `ignore_persona_scope` 退出

本项目的门禁必须留在 `plugins/datasteward_gate/`（铁律 1）与
`services/connector.py` 的 `_admit()`（铁律 3）。

#### 不用的：ingestion 取数

OM 的 connector 是**定时抓元数据的 pipeline**，不是取数网关——它给 schema /
owner / lineage / profile，不提供按行取数的接口（sample data 只够喂 profiler）。
bronze 层拉数据它做不了。而且：

- ingestion 跑在**独立进程**（Airflow / ingestion agent），`pre_tool_call` 拦不到，
  源库访问一旦走那条路，「Agent 绕不过」就无法证明——破铁律 1
- 它的 profiler 默认对源表做统计与采样，属于「代价不可预估的操作」——破铁律 3

#### 什么时候该重新评估

OM 2.1+ 修掉 #30023（bot token 的 RBAC 绕过）之后，可以重新看它的 MCP 层
能否承担**读侧**的元数据检索。**写侧与源库取数不在重估范围内**——
那两条由铁律 1 / 3 决定，与 OM 的成熟度无关。
