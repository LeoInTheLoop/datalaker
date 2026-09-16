# Snapshot 输入隔离与一次 Docker 实测

2026-09-15。运行 `demo-535dd16a19a6`，Snapshot 为
`northwind-boss-points-dba`。**本次业务判定 FAIL；输入取证完整。**
实际模型 `deepseek-v4-pro-0813`；Hermes revision
`63279301bcbdc185c1b07b98a9312eb0c862f26d`，与本机 Hermes revision 相同。

## 开始、停止和通过标准

隔离 PostgreSQL/湖/邮箱清空，全新 Hermes 会话；没有历史邮件或已登记联系人。
只向 GreenMail 投递一封来信：

> 发件人：boss@acme.com  
> 主题：Northwind 的连接信息请找数据库管理员  
> 正文：数据库管理员（dba@acme.com）负责 Northwind 的数据库连接。

正常 Hermes 系统提示、Skills、工具定义照常加载。原生首次使用介绍也保留，
不是删掉它后跑一个更容易通过的测试。健康检查的 ping 调用是独立请求，不进入业务会话。

运行前固定时间窗 300 秒，实际部署同时有 `CLAW_MAX_TURN=40`。本轮计数为 5/40，
未触及次数上限；300 秒到期由外部停止网关。`completion.json` 记录
`reason=window_closed`、`stopped_at=1789486928.6326134`、`returncode=1`；
退出码来自外部停止后的进程结果。停止后独立检查，没有存活的 Hermes gateway。
该轮 `CLAW_TURN_SCOPE` 未设置，计数落在默认 scope；本轮清空隔离库，无上轮累计。

预先固定通过标准：登记 Northwind DBA，真实邮件送达 DBA，邮件确实索取**只读**连接信息。
预期结果不送入模型，不用于改变可调用工具或压制正常 cron。

## 实际结果

| 项目 | 结果 |
|---|---|
| 联系人落库 | PASS：`northwind` → `dba@acme.com` |
| DBA 真实收件箱 | PASS：收到模型调用 `send_contact_email` 发出的邮件 |
| 索取只读连接信息 | FAIL：邮件只索取 DSN，没有明确要求只读账号/连接 |
| 完整输入证据 | 6 份原生请求、6 条 request hook、6 条 response hook；无 API 错误 |
| 请求来源 | Email 5 次，正常 cron 1 次 |
| 外部停止 | `window_closed`，网关已停止 |

DBA 收到的业务请求原文：

> 想请您提供 Northwind 数据库的连接串（DSN），以便我把它注册成数据源、开始接入。谢谢。

没有追加对话、替模型调用工具、点击审批或回写答案来补救。内容判定由独立审阅完成，
绑定邮件 id 与 SHA-256，记录在观察器 verification；模型看不到该判定。
本轮说明“收到真实业务转介后能登记并发信”，**不能据此声称无人来信时主动索取账号已验收**。
一次试跑也不代表稳定通过率。

## 修复的测试问题

- 删除 Case 专属工具限制及其 `[SNAPSHOT_SCOPE]` 提示；删除生产 monitor 读取预期结果的分支。
- Agent 容器不挂载 `demo/`、测试目录和观察器 runs；统一加载正常项目配置。
- 状态恢复结束后才启动网关/cron；历史邮件不能作为额外新来信重放。
- 原生请求和只读 hook 双重留痕，缺失/不完整时不判完整证据。
- 修正原生请求目录：Hermes 实际写入 `sessions/`；结束记录独立持久化。
- 邮件送达与内容判定分开；拒绝票、其他关系的票或随便一封邮件不能充当关联确认。

本地相关检查通过：门禁 103、身份 28、任务状态 15、到期唤醒 18、停止点 13；
Snapshot 相关 unittest、现有全局 turn 计数测试、模型过期策略检查均通过。
这不是完整仓库回归，也未把另外两份 Snapshot 算成实测。

上一轮 `demo-7416230b605e` 的原生请求未及时归档，后来从 session 恢复，
缺少持久化停止记录；保留为诊断记录，不作为本次完整通过证据。

## 证据位置

页面：<http://127.0.0.1:8088>，实际输入：<http://127.0.0.1:8088/api/inputs>。

本地原始证据（凭证脱敏，运行文件不入 Git）：
[inputs.json](../../.hermes/test-home/snapshot-evidence/2026-09-15-demo-535dd16a19a6/inputs.json)、
[status.json](../../.hermes/test-home/snapshot-evidence/2026-09-15-demo-535dd16a19a6/status.json)、
[mail.json](../../.hermes/test-home/snapshot-evidence/2026-09-15-demo-535dd16a19a6/mail.json)、
[completion.json](../../.hermes/test-home/snapshot-evidence/2026-09-15-demo-535dd16a19a6/completion.json)。

输入契约见 [snapshot-testing.md](../snapshot-testing.md)。
