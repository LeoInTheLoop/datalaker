# Handoff: P0 —— 独立真模型演练展示页

## 目标与边界

这是 Data Steward Claw 的展示和真测页面，不是第二个 Agent、前端工作流或
预写结果演示。Hermes Gateway 仍负责模型、会话、工具选择、审批申请与恢复；
`data-steward` 仍通过原有确定性 gate、callback、数据库权限和 Connector 执行。

页面仅能：选择固定 Case/Snapshot、发送模拟人员邮件、读取 GreenMail/治理库/
Trino 证据、把用户点击的真实审批令牌转发给 callback、启动隔离 Snapshot，和
执行独立只读真测。它没有治理工具 API，也没有写审批决定的身份。

## 当前实现

- `demo/cases.json`：固定 Northwind Case 与兼容 Snapshot。Case 只包含人写的
  业务背景与邮件历史，不含工具序列、SQL 或模型答案。
- `services/demo_ui.py`：单页工作台。页面中的 SQL 来自实际 `query_ledger`；
  跨表结果必须由模型邮件/`answer_with_link` 台账产生，旁边的九行销售表由页面
  独立只读源库计算，不能替代模型答案。
- 运行数据采用 `run_id`：每次开始会清 demo lake、治理运行态和 GreenMail，随后
  通过共享 control volume 让 agent entrypoint 重启 Hermes 并清除会话，再投递
  Case 邮件。源库永远不被清或写入；Snapshot 指向预置只读源表。
- `docker/agent-entrypoint.py`：只管理真实 Hermes 进程与 reset 请求；仍先探测
  实际 chat completion + tool calling。端点不可用时状态为 `blocked`，不降级。
- `infra/docker-compose.demo.yml`：demo overlay，复用 core、GreenMail、callback
  和 steward-agent。UI 的管理员连接仅用于确定性 Snapshot 初始化，页面接口
  不暴露 SQL 或任意数据库操作。
- `infra/init-steward.sql`：为 Connector 增加数据库 owner 托管的
  `datasteward_put_source_secret` 窄写函数和按 `source_id` 取回单行凭证的
  `datasteward_get_source_secret` 函数。Agent 对 `source_secrets` 仍无 SELECT；
  只在审批放行后写入凭证，Connector 只按已批准的 source id 读取，不暴露整表。
- `infra/demo-init.sh`：demo 的 Trino `northwind` catalog 使用独立的
  `agent_ro` 只读账号，与邮件中 DBA 提供的 `ops_reader` 分离。
- 门禁把 Case 显示名按已获人类授权的 source id 做大小写归一；PostgreSQL UUID
  审批号统一转字符串，待决票只开放 `args_json` 的列级更新，支持新账号
  `amend_pending()`，仍不能写 `decisions`。

## 真实审批与真测

- 邮箱解析到的审批 token 不返回浏览器。浏览器仅持有 link id；服务端重新从真实
  GreenMail 邮件索引该 token，再转发至 `approval-callback` 内网地址。
- callback 仍是唯一写 `decisions` 的进程。重复点击同一真实链接将得到 callback
  的 `409`，并记录在本轮展示历史，供真测读取。
- callback 对同一 `approval_id` 也只接受第一条决定；approve/deny 两枚令牌不能
  把同一张票写成两个相反结果。页面审批表按 `decisions` 终态渲染，不只看
  `approvals.used_at`。
- `/api/mail`、`/api/status` 和 `runs.json` 都会脱敏 DSN 口令与审批 URL/token；
  轮询收件箱是只读观察，不再把整份邮箱快照重复追加进历史。
- 真测只读当前状态：网关/tool calling、邮件、`agent_role` 决策写权限、源账号
  写权限、错误账号通知、amend/new-ticket 分支、重放、bronze/silver/gold 与
  `answer_with_link`/九行源库直算。证据不足只能显示 `pending`。

## 运行与验证状态

Docker 启动命令（必须使用新项目名，避免复用其他本地数据）：

```bash
docker compose -p datalaker-demo --project-directory infra --env-file .env \
  -f infra/docker-compose.yml -f infra/docker-compose.demo.yml --profile demo up --build
```

浏览器入口为 `http://127.0.0.1:8088`。demo 为避免占用已有本机栈，GreenMail
仅映射 `127.0.0.1:13025/13143`，callback 映射 `127.0.0.1:18787`；容器间仍使用
GreenMail 的 3025/3143 和 callback 的 8787。

2026-09-08 已用 Docker + Playwright 真操作验证：新 run
`demo-db63564b2ab3` 从 `reset_requested` → `reset_acknowledged` →
`case_delivered`，Agent 与页面是同一个 `run_id`，Gateway `model_probe=pass`。
Case 邮件、GreenMail 收发、模型 tool calling、审批转发均走真实容器链路；同一
审批按钮首次返回 200，重放返回 409。错误账号分支在前一轮真实 run 中落下
`SOURCE_CONNECT_FAILED`/失败通知并发给 DBA；当前这轮正确账号经受控函数落库，
模型重新发起三张 `ingest_table` 票并在页面批准后，Trino 真实 Bronze 行数为：
`orders=830`、`order_details=2155`、`employees=9`。

当前模型已完成 Bronze 和湖内质量核验，并真实发起了语义口径审批；silver、gold、
跨表九行回答仍为 `pending`，因为口径审批/清洗/发布尚未完成。页面真测已通过模型
tool calling、GreenMail、Agent 无 decisions 写权限、源库无写权限、已决票新建、
审批重放、Bronze 行数等判据；证据不足仍只显示 `pending`，不补造结果。最近 5 次
`/api/mail` 轮询前后 `runs.json` 字节数保持不变，公开接口中未发现完整审批 token
或 DSN 密码。

随后以 `northwind-sales` + `northwind-full` 重跑真模型链路，Bronze 已实际落下四张
源表：`orders=830`、`order_details=2155`、`employees=9`、
`demo_order_status=24`；silver 已出现脏状态的 `_raw` 与洗后值。模型同时识别出
`order_details` 不能只按 `order_id` 去重（真实键是复合键），因此没有继续发布
gold，也没有补造跨表九行答案。这是模型在真实数据证据下停下，不是页面脚本的
预置失败。随后等待 Trino 完全就绪后重跑 `tests/run_all.sh`，最终退出码为 0；
其中未设置 Hermes、未启动 Phoenix 等项按脚本契约标记为 `SKIP`。新增 PostgreSQL
授权合同实测 3/3，`test_grants.sh` 12/12。

Hermes source-build 镜像没有官方 s6 入口，demo overlay 显式设
`HERMES_ALLOW_ROOT_GATEWAY=1`。它仅可用于隔离的 `hermes_demo_state` named volume，
不得抄到宿主机或生产 Hermes 运行时。

模型免费额度有效期由 `DASHSCOPE_MODEL_EXPIRATIONS` 显式登记，格式为
`model=YYYY-MM-DD`。`docker/agent-entrypoint.py` 在启动和每次网关运行期间做硬检查：
采用保守截止：到期日前一天即停止使用；主模型进入截止日时按 `OPENAI_MODEL_FALLBACKS` 选择仍安全的模型；
缺少日期或全部候选过期则状态为 `blocked`，不会发起 chat completion。页面状态同时
展示实际选择的 `model` 与 `model_expires_on`，便于核对。

静态契约（仅用于接手时快速检查，不代替真模型验收）：

```bash
python3 tests/test_demo_ui.py
python3 -m py_compile services/demo_ui.py docker/agent-entrypoint.py
docker compose -p datalaker-demo --project-directory infra --env-file .env \
  -f infra/docker-compose.yml -f infra/docker-compose.demo.yml --profile demo config --quiet
```

## 接手时先看

1. `services/demo_ui.py`：确认新增页面操作仍没有变成直接治理工具调用。
2. `docker/agent-entrypoint.py`：确认 reset 后等待 `email connected` 才标记 ready。
3. 用全新 demo volume 走一次 Case，手动点击审批，再运行页面真测；审查
   GreenMail、callback、治理库、Trino 与源库独立证据。

## 不要做

- 不为稳定展示而在前端预写模型回答、SQL、审批决定或 lake 产物。
- 不把 token、DSN 密码、模型 API key 放进页面、run history 或 handoff。
- 不把 UI 的 Snapshot 管理权限扩成任意 SQL、任意容器控制或审批写接口。
- 不用桩模式通过替代真模型结果；桩仅能证明管道，不能证明模型判断。
