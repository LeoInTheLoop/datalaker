# Snapshot Runner

**它解决的是一件事：让整套 agent 能被反复地、长期地真实实测。**

> Snapshot = sufficient state required for the agent to continue as if execution
> had never been interrupted.

不是测模型答得好不好。被测对象是 **prompt ＋ Skill ＋ harness ＋ 门禁 ＋ Connector
＋ 治理库 ＋ 真实邮件链路** 这一整套合起来到底有没有效。所以一次 run 里，
下一步做什么始终由正常 Hermes 自己决定；Runner 只负责四件事：

| | 做什么 | 为什么非要它不可 |
|---|---|---|
| **还原环境** | 把某个时点的完整状态原样恢复 | 起点不确定，结论就不可比 |
| **导入前因** | 已连接、已登记、已批准、票还有多久过期 | 缺失状态当成空状态，产出的是错结论不是小样本 |
| **到限停机** | 时间窗、turn 上限，都由外部停 | 它不知道「做到哪算完」，不停就一直烧 |
| **完整记录** | 输入、输出正文、工具参数与结果、停止原因 | 没有记录的实测等于没测 |

一句话：**完整复原某个时点，按下开始，看这套 agent 真实怎么反应，并把过程原样留下。**

## 两层起点：bundle 是基线，state 是覆盖层

```
bundle（冷检查点）   整机状态，从实测抓，不可手写
   │                 postgres / objects / iceberg / runtime / mail / deployment
   │ 之上
state（可选覆盖）    只改要改的那一个条件，JSON，diff 得出来
   ▼
这一轮的起点
```

**为什么不是二选一。** bundle 是唯一能做到完整恢复的——session、memory、cron 排期、
lake 物理产物，声明式写不出来。但 bundle 是二进制归档，review 不了、也没法「只改一个
条件」派生反例，而那恰恰是写反例的标准做法。所以 state 不作为独立起点存在，
只作为 bundle 之上的覆盖层。

**纯 state 起点在 CLI 层面就构造不出来**：`run` 的 bundle 是必填位置参数，
`--state` 是可选覆盖。不需要额外加拒绝检查。

对应 `origin` 约定：bundle 写 `snapshot:<运行>/<截取点>`，
带覆盖的写 `derived:<基线>/<改动>`。`restore()` 的返回里
`from_bundle` 和 `overridden`（含每项的 before / after）分开报，
**覆盖了什么必须说得出来**。

## 命令

```bash
python -m services.snapshot_runner provision                     # 起隔离栈，agent 空转
python -m services.snapshot_runner validate <bundle>             # 只校验归档，不碰环境
python -m services.snapshot_runner capture  <bundle>             # 停机后抓整机状态
python -m services.snapshot_runner restore  <bundle>             # 还原到某个 bundle
python -m services.snapshot_runner run      <bundle> \
        --output <dir> --seconds 900 \
        [--state overlay.json] [--mail current.json]
```

`--mail` 缺省表示**自主续跑**：不投新信，让正常 cron 从恢复出来的待办自己往下走。

## 模块

| 文件 | 职责 |
|---|---|
| [`bundle.py`](bundle.py) | 冷检查点的读写与整包校验。**空产物是显式的，缺产物是错误** |
| [`docker_backend.py`](docker_backend.py) | 停机（quiesce）、抓取、还原、跑一轮。停机由它负责，不在运行中抓 |
| [`mailbox.py`](mailbox.py) | 邮箱状态经 IMAP 无损往返：MIME 原文 ＋ flags ＋ internaldate |
| [`restore.py`](restore.py) | 治理状态覆盖层。**独立进程**，生产指纹、事务提交、自验后才落盘 |
| [`input_audit.py`](input_audit.py) | Hermes hook。记录每次**实际进入模型**的 system / messages / tools，带哈希与脱敏 |
| [`trajectory.py`](trajectory.py) | 导出 New Trajectory：模型输出正文、工具调用与结果，含最后一条 |
| [`evidence.py`](evidence.py) | Evaluator 侧读回证据，判断记录完不完整；**缺就是缺，不拿别的顶替** |

术语和边界见 [`docs/eval-model.md`](../../docs/eval-model.md)、
[`docs/snapshot-testing.md`](../../docs/snapshot-testing.md)。

## 一轮的流程

```
观察器          校验 Snapshot 声明   ← 不支持的依赖在这里就拒绝，环境还没动
  ↓
观察器          请求 reset
  ↓
agent 容器      停网关、清运行时、确认
  ↓
观察器          清 PG / 湖 / 邮箱
  ↓
restore         还原 bundle，再叠加 state 覆盖 → 自验 → 提交
  ↓
观察器          写 prepared/{run_id}.json
  ↓
agent 容器      网关 + cron 启动
  ↓
观察器          等网关 ready 后投递当前来信（`--mail` 缺省则不投，靠 cron 自主续跑）
  ↓
input_audit     每次请求落一条              （运行期间持续）
  ↓
停止：时间窗到期 / turn 用满 / 模型到期 / 被顶替 / 网关自行退出 / 启动失败
  ↓
finish_run()    停执行 → 落盘请求 → 导出轨迹 → 写 completion.json
```

**所有退出路径统一走 `finish_run()`**，六种停止原因分开记录，不混成一个
`window_closed`。顺序是固定的：先停，再等记录落盘，最后导出并写结束记录。

## 四件容易做错的事

**还原器为什么是独立进程。**
`decisions` 的合法写入方只有 approval callback（`approver_role` 是唯一有
`INSERT ON decisions` 的角色）。观察器拿超级用户也写得进去，但那样「谁能写决定」
在代码层面就糊了。`tests/test_demo_ui.py` 有一条断言守着观察器源码里不许出现
`INSERT INTO decisions`——换个文件名同进程调用只能骗过字符串检查。

历史决定分三段看：**恢复前**由还原器写并记来源；**运行中**新决定仍只能由
callback 产生；**判分时**历史决定属于基线，不算本轮新完成的审批。

> **进程已拆，数据库身份尚未拆。** `SNAPSHOT_RESTORE_DSN` 存在，但默认部署里
> 它没有单独配，会回退到 `DEMO_ADMIN_DSN`（postgres 超级用户）。
> 要真正拆开身份，得给还原器一个只在还原阶段有写权限的角色。**这一条还没做。**

**票据要带生产指纹，否则等于没有审批。**
门禁靠 `action_hash` 把「模型现在要做的事」和「人批过的那张票」对上。还原器
自己拼一个 `restored:xxx`，库里票据看着齐全，真到门禁那里一条都匹配不上。
`restore.py` 调的是生产那一份 `action_hash()`。

**自验要验 agent 实际读到的值，不是库里存在哪一行。**
角色表按 `valid_from DESC LIMIT 1` 取人。只验「那一行在不在」会漏掉一整类缺陷：
初始化写的当前角色比 Snapshot 里的历史角色更新，于是**声明 alice、实际生效 wang，
而自验通过**。所以还原器先删掉该角色的历史再写，自验则在**未提交的事务上**
用生产读实现（`identity.resolve_to`，和工具调的是同一个函数）确认最终生效的人。

这条推广到所有「多行按规则取一行」的状态：联系人、口径、档案版本。

**挡住工具不等于运行停止。**
turn 到线后门禁只是不放行，网关仍会继续请求模型、再试下一个工具，一直烧到
时间窗到期——那些调用产生不了任何动作，却照样计费。所以门禁到线时写一条信号，
entrypoint 轮询到就停进程组，停止原因记成 `turn_limit`（和 `window_closed`
分开：一个是动作数够了，一个是时间到了）。

## 轨迹怎么导，以及为什么不能只信一边

Hermes 的 `post_api_request` **会传** `response` 和 `assistant_message`
（`agent/conversation_loop.py`）。但只靠 hook 记录仍然不够：hook 可能被截断、
可能漏，而停止总是正好停在最后一条上——那一条往往就是结论本身。

所以两边都取，交叉核对：

- **会话库**（`state.db` 的 `messages`）给已持久化的消息，只读打开
- **hook 观察记录**给 `response` / `tool_result` / `tool_blocked` 事件

`final_assistant` 优先取 hook 直接拿到的 `assistant_message`，会话库兜底。
发起了但没有结果的调用进 `interrupted_calls`，没等到响应的请求进
`interrupted_requests`，到线后被拦的工具名进 `blocked_after_limit`。
**hook 报 truncated、或请求没结束，整份判 `inconclusive`** ——
读到了消息不等于轨迹完整。

## 覆盖层怎么写

时间一律是**相对量**，落库时才换算。写死时间戳的 case 放一周就全部过期，
而「票还有两小时到期」「这条线挂了三十小时」正是恢复类测例要表达的东西。

```json
{
  "roles":    [{"role": "steward", "person": "alice@acme.com", "age_h": 720}],
  "contacts": [{"source_id": "northwind", "email": "dba@acme.com", "age_h": 30}],
  "catalog":  [{"asset": "northwind.orders", "kind": "link",
                "key": "customer", "value": "...", "status": "confirmed"}],
  "runs":     [{"id": "r1", "kind": "ingest_table", "status": "waiting_human",
                "age_h": 40, "updated_h": 26, "next_action_h": -1,
                "params": {"source": "northwind", "table": "orders"}}],
  "tickets":  [{"id": "t1", "run": "r1", "tool": "ingest_table", "approver": "owner",
                "age_h": 26, "expires_h": 46, "waits": true,
                "decision": "approve", "decided_by": "wang@acme.com", "decided_h": 2,
                "args": {"source": "northwind", "table": "orders"}}],
  "patches":  [{"table": "runs", "key": {"run_id": "r1"},
                "expect": {"next_action_at": null},
                "set": {"next_action_at": {"hours_from_restore": 2}}}]
}
```

顶层键只有 `roles` / `contacts` / `catalog` / `runs` / `tickets` / `patches`。

- `expires_h` 为负 = **已经过期**，这样才写得出过期票的反例
- `waits: true` 把 `runs.waiting_on` 回填到这张票 —— `runs.resumable()` 是
  `runs JOIN decisions ON waiting_on`，不回填就永远查不到
- `patches` 改单个字段，**`expect` 是前置断言**：改之前的值对不上就整份回滚，
  防止在一个已经不是预期的状态上打补丁
- **未知字段一律拒绝**，不因为调用入口不同而放宽。字段清单在 `restore.py` 的
  `FIELDS`，观察器的校验委托给它，不抄第二份

## 现在支持到哪

| 能恢复 | 还不能 |
|---|---|
| Postgres 治理库（整库归档 ＋ 17 张表的字段级覆盖） | 任意源库环境变体 |
| 对象存储与 Iceberg 元数据 | 独立的还原器数据库身份（回退超级用户） |
| Hermes runtime：会话、memory、todo、cron | |
| 邮箱：MIME 原文 ＋ flags ＋ internaldate | |
| 角色、联系人、档案、任务线、票据、历史决定、有效期 | |

不支持的依赖**在启动前拒绝**，不会降级成空状态继续跑。

## 测试

```bash
python -m unittest discover -s tests -p 'test_snapshot*.py'        # 离线，40 项

SNAPSHOT_LIVE_TESTS=1 \
DEMO_ADMIN_DSN=postgresql://postgres:postgres@127.0.0.1:55432/steward \
python -m unittest discover -s tests -p 'test_snapshot_runner_live.py'   # 真 PG + 真 GreenMail，6 项
```

live 那组自己建库、自己起邮件容器，跑完删掉，不碰演示业务记录，也不需要模型。
它覆盖的正是光读代码看不出来的几条：历史角色覆盖初始化后**生产查询取到谁**、
`expect` 不匹配时其余改动**是否真的回滚**、还原出来的票据与决定**能否被生产
resume 读到**、邮箱往返后 **flags 与 internaldate 有没有变**。
