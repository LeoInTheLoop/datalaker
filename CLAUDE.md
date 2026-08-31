# Data Steward Claw —— 项目约定

数字员工形态的数据管家：连接散落的源系统，通过邮件与人确认口径和审批，
在独立 lakehouse 中整理出 one source of truth，同时把权限一并理清。

完整设计见 [readme.md](readme.md)。本文件只放**不变量**——变化的进度写在 `docs/handoff/`。

## 五条铁律

违反其中任何一条，项目的核心卖点即不成立。

### 1. 限制一律走 hook，不写进 prompt
所有约束（分级、票据、指纹、deny list、SQL 护栏、预算）实现在 Hermes `pre_tool_call` 里。
不在 system prompt 里写「你不可以……」，不把限制散进业务代码分支。

> 理由：限制一旦散进 loop 或依赖模型自觉，就再也无法证明「Agent 绕不过」。

### 2. Agent 对 `decisions` 表无写权限
审批 callback 服务**必须是独立进程 + 独立数据库账号**。
与 Agent 同进程时列级 GRANT 形同虚设，机制三直接失效。

### 3. 源系统只读，且永不 join
- 给 Agent 的源系统账号只有 `SELECT`
- 源库上只允许**单表** `SELECT / SHOW / DESCRIBE`
- 所有关联分析在 Iceberg / Trino 上做

> 理由：join 代价不可预估，单表扫描可用行数估算。不可预估的操作只能发生在自己的地盘。

### 4. 不修改源系统
数据修复发生在 `bronze → silver` 的变换中，不对源表执行任何写操作。
对源系统权限同样只观测、只建议，写入 Remediation Ledger。

### 5. 新工具必须在 `policy.py` 显式声明级别
未声明的工具默认 **L2（需人确认）**，不是默认放行。新增工具时同步加一行。

## 目录

```
readme.md                     完整方案（16 节）
CLAUDE.md                     本文件：不变量
docs/handoff/                 每阶段交接记录
infra/docker-compose.yml      数据平台编排（按 profile 分组）
plugins/datasteward_gate/     Hermes 治理 Plugin —— 项目核心
  ├─ __init__.py              hook 注册与实现
  ├─ policy.py                工具策略表（限制是配置）
  └─ approvals.py             两表 append-only 审批存储
services/                     独立进程，不与 Agent 同进程
  ├─ tokens.py                HMAC 签名令牌
  └─ approval_callback.py     审批 callback 服务
tests/                        断言（安全类要求 100% 通过）
spike/                        R0 概念验证，可随时删除
```

## 环境约束（R0 实测）

- 开发机 8GB 内存，**Docker VM 仅 3.83 GiB**
- `core`（MinIO + Iceberg + Trino）与 `governance`（OpenMetadata 全家桶）**不能同时启动**
- OpenMetadata 全家桶约 4.5GB，本机跑不起来 → R1–R4 用轻量 `assets` 表替代
- Trino 必须限制 `-Xmx1G`，默认配置会吃掉大部分内存

## 开发约定

- 跑测试：`HERMES=<hermes-agent 路径> ./tests/run_all.sh`（**81 条断言必须全绿**）
  - 不设 `HERMES` 时会跳过真实集成那一段
  - Hermes 需要 python 3.11–3.13，本机 3.14 不兼容：用 `uv venv --python 3.13 .venv-h`
- 起数据平面：`cd infra && docker compose --profile core up -d`
- 临时的取舍用 `ponytail:` 注释标记，欠账用 `TODO(R<N>):` 标记
- 提交前确认没有把凭证写进 `infra/trino/catalog/*.properties`

## 参考

- Hermes Agent（MIT，Python）：`NousResearch/hermes-agent`
  - `pre_tool_call` 契约：`website/docs/user-guide/features/hooks.md`
  - Email 适配器：`plugins/platforms/email/adapter.py`
- 推理端点：`https://inference-api.nousresearch.com/v1`（OpenAI 兼容）
