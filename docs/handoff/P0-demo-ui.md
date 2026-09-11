# Handoff: P0 —— 独立真模型演练展示页

## 目标与边界

这是 Data Steward Claw 的 Snapshot Case 展示和真测页面，不是第二个 Agent、前端工作流或
预写结果演示。每个 Snapshot 只提供既有邮件、当前来信与可见源表；Hermes Gateway 仍负责模型、会话、工具选择、审批申请与恢复；
`data-steward` 仍通过原有确定性 gate、callback、数据库权限和 Connector 执行。

页面仅能：选择固定 Case/Snapshot、发送人的入站邮件、读取 GreenMail/治理库/
Trino 证据、把用户点击的真实审批令牌转发给 callback、启动隔离 Snapshot，和
执行独立只读真测。它没有治理工具 API，也没有写审批决定的身份。

## 当前实现

- `demo/cases.json`：当前展示是一个固定的 `Northwind：连接交接` 环境（人、职责、数据源及
  权限边界），下面有两个可独立启动的 Snapshot：老板指向数据库管理员后，模型登记联系人并向
  数据库管理员索取连接信息；以及数据库管理员已发来运行时凭证后，模型走受控接入。Snapshot 是环境在不同时间点的
  冻结状态，不依赖前一个 Snapshot 刚刚执行完成。复杂 Northwind 剧本留在 `staging_cases`，
  不会返回浏览器或进入模型上下文。每个 Snapshot 的 `expected_outcome` 只给独立判分器读取，
  不含工具序列或模型答案。
- `source_contacts` + `register_contact`：联系人目录与 `role_assignment` 分表；登记只保存
  数据源的联络线索，绝不改变角色、审批资格或数据权限。`send_contact_email` 只能投递给
  已登记联系人，拒绝连接串、口令、token 与审批链接，并按联系人+主题去重。
- `services/demo_ui.py` + `services/demo_ui.html`：单页工作台。页面标记拆成独立文件，
  按请求读取（`services/` 是只读 bind mount，改完刷新即可，不必重建镜像），
  同时去掉了原先内联字符串必须的两处 `str.replace` 转义补丁 —— 任一处不再匹配时
  整段脚本会静默失效。浏览器只得到 Snapshot 的历史邮件、当前来信与可选源表；模型在
  Hermes 运行时另行拿到真实工具 schema。页面默认按时间只展示 Snapshot 对话与人工审批；
  模型调用和系统事件由「显示模型后台动作」按需展开，不预排后续步骤，也不预写模型答案。
- 当前状态卡用 Snapshot 当前来信和真实台账事件生成一条确定性经过摘要；审批卡只显示
  服务端从已存参数提取的安全动作说明，不返回原始 `args_json`、DSN 或审批 token。开始前先
  只读探测 Trino 与治理库，未就绪时不创建 run、不清任何数据，避免冷启动留下伪失败记录。
- 人点击真实审批链接后，页面保留点击瞬间的邮件、事件和票据基线；后续轮询到的真实回信、
  人工决定、模型后台动作和新票均标「新」，并在后台动作开关上显示新增数量。它只做前后
  记录差异，不预判模型接下来会走哪一步。
- 每个 Snapshot 的 `expected_outcome` 不进入公开 metadata 或 `case_packet()`；页面只在模型
  实际执行后，以数据库事件、lake 实物、provenance 和独立源库直算判 `pass` / `fail` / `pending`。
  缺证据只能 `pending`，相反的实际结果才 `fail`。
- 页面结构面向「第一次打开的人」：顶栏网关灯 → 可折叠说明条 → 当前 Snapshot 状态 →
  真实审批或真实回信入口 → 邮件与模型动作时间线 → 本轮已记录工具调用 → 调试视图。
  右上角「调试视图」切换（`localStorage` 记住）才展开 events、provenance 和原始 JSON。
- **机器码不再直接示人**：`run.state`、event kind 和工具名都有确定性中文映射表，写在页面里。
  Snapshot 视图不展示泛化「通过率」；只展示当前 Snapshot 的预期结果、每条独立证据和终态判定。
- 顶栏读的是网关此刻的状态，`run.state` 记的是这一轮开始时的状态。两者可以同时为真
  （网关后来自己好了），页面在故障卡里直接说破，不让它看起来像自相矛盾。
- `demo/cases.json` 保留 `you_play` / `steps` / `replies` 作为以后完整模拟视图的后台素材。
  Snapshot 视图既不向浏览器返回这些字段，也不把它们送进 `case_packet()`；模型永远看不到
  剧本步骤（`tests/test_demo_ui.py` 有断言）。回信框不再预填台词，只有人在模型真实来信后
  才能自行写入新的邮件。
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
采用保守截止：到期日前一天即停止使用；仍安全的候选按到期日升序优先使用，主模型进入截止日时再选下一个；
缺少日期或全部候选过期则状态为 `blocked`，不会发起 chat completion。页面状态同时
展示实际选择的 `model` 与 `model_expires_on`，便于核对。

2026-09-11 用 Playwright 真实点击两个最小 Case：`northwind-contact-a` 中模型实际调用
`register_contact`，A 的 GreenMail 收件箱实际收到模型邮件，Case 判定 `pass`；
`northwind-a-credentials` 中 A 的运行时 DSN 邮件只以脱敏形式返回网页，模型实际调用
`connect_source` 并生成真实 `sponsor` 审批票，Case 判定 `pass`。第二条停在审批，未自动批准或执行接入。

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
- 不把 `cases.json` 的 `steps` / `replies` 送进 `case_packet()`。那是给操作页面的人看的剧本，
  进了邮件就成了喂给模型的工具序列。
- 不用桩模式通过替代真模型结果；桩仅能证明管道，不能证明模型判断。
