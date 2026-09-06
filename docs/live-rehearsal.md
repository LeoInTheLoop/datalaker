# 真模型演练：怎么跑，以及别再踩的坑

> 写于 2026-09-06，来自第一次「真模型 + 真网关 + 真邮箱」的完整演练。
> 那次跑通花了大半天，**其中大部分时间不是在修产品缺陷，是在踩流程坑**——
> 同一个坑第二次踩就是纯浪费，所以写在这。
>
> 产品缺陷记在 [handoff/R5.md](handoff/R5.md) 第 5 节的坑表（34 条）。
> 这份只记**流程**：怎么起、怎么推、怎么验、哪一步不能省。

## 三个脚本

```bash
tests/reset_live.py     # 还原到「什么都没发生过」，并自验起点干净
tests/live_drive.py     # 扮人：起停、发信、点链接、等 cron、看收件箱
tests/verify_live.py    # 跑完独立查库，交叉核对「工具说的 vs 实际」
```

一次完整演练：

```bash
python3 tests/live_drive.py env > /tmp/live_env.sh && . /tmp/live_env.sh
python3 tests/reset_live.py            # 清 + 自验
python3 tests/live_drive.py up         # 起 callback + 网关
python3 tests/live_drive.py say dba@acme.com "账号" "postgresql://fin_reader:...@..."
python3 tests/live_drive.py tick 3     # 点链接 + 等唤醒，跑 3 轮
python3 tests/live_drive.py choose start_silver
python3 tests/live_drive.py tick 2
python3 tests/verify_live.py           # 验收
python3 tests/live_drive.py down
```

---

## 一、起环境时

### 网关必须 `--replace`

旧实例会占着不放，而它的报错混在日志里，**看起来像起来了**：

```
✗ Another gateway instance is already running (PID 60422).
```

我因此干等过 240 秒。`live_drive.py up` 已经带了 `--replace`。

### 信必须在网关起来**之后**发

adapter 启动时会把**存量信全部标记已见**（日志里那句
`IMAP connection test passed. 3 existing messages skipped.`）。
之前到的信它一封都不会处理。顺序反了就是干等。

### 首次接触会吃掉一到两封信

第一次给网关发信，回的不是 Agent 的话，是系统提示：

- `📬 No home channel is set for Email.` —— 配 `EMAIL_HOME_ADDRESS` 消掉
- `↪ Redirected current run.` —— 上一轮还没跑完，新信被当成打断

**都是一次性的**。演练前先发一封废信把它们消耗掉，或者容忍前两封。

### 连发太快会打断上一轮

Agent 处理一封信要几十秒到两分钟。这期间再发一封，Hermes 会把它当成
「纠正当前运行」，上一轮的活就废了。`live_drive.py say` 会等回信再返回。

---

## 二、邮件全走模拟，一个变量都不能漏

出站入站都指到 GreenMail。**漏一个 `MAIL_TRANSPORT=smtp`，信就真发到公网
Gmail 去了** —— 我撞过，而且撞的时候 `SMTP_SECURITY=plain` 明明已经设了。

现在有两道硬保险（[email_channel.py](../services/notify/email_channel.py)）：

- 明文 SMTP **只允许打** `127.0.0.1` / `localhost` / `greenmail`，别的 host 直接拒
- `SMTP_SECURITY=plain` 时**绝不走 Gmail API**，哪怕 `.env` 里写着 `gmail_api`

还有一条更早的教训：**通道配置只读 `.env` 不读进程环境变量**这个坑踩了四处
（`NOTIFY_CHANNEL` / `APPROVAL_BASE_URL` / `SMTP_*` / 收件人 `MAIL_*`）。
现在统一走 `notify.cfg()`，环境变量优先。加新的通道配置项时照着来。

### Gmail 有配额

`test_double_confirm` 曾经挂在真 Gmail 上，跑多了撞 `HttpError 429
user-rate limit`，整组红——而机制本身好好的。已经改走 outbox。
**演练不该依赖任何外部服务的配额。**

---

## 三、推进时

### 等 cron 要认计数，不要傻等秒数

```python
n0 = log.count("Running job 'data-steward-resume'")
# 等到 n > n0，再多给 50 秒让模型跑完
```

傻等固定秒数会卡在两次 tick 之间——我因此误判过「monitor 坏了」，
其实只是等得不够。

### 一条命令别等超过 10 分钟

`until` 循环等 cron 很容易撞上 10 分钟的命令超时（我撞了三次，每次白等）。
拆成 `tick 1` 一轮一轮跑，或者用 `tick N`。

### 调试时把重试粒度调成分钟

`CLAW_RESUME_UNIT_SECONDS=60`（生产是 3600）。这个格子决定**唤醒失败之后
多久自己重试**——按小时的话调试要干等一个钟头。

### 换了模型要注意 cron 被静默跳过

Hermes 对没 pin 模型的 cron 作业有花费保护：全局模型一换就
`[drift_skip:silent]` 跳过，**告警只发一次，之后一直跳过**。
日志里只有一行 ERROR，看起来像 monitor 坏了。

`ensure_jobs` 现在会 pin 并把 model 纳入 drift 比对，重启网关即可修正。

### WIP 满了的表现是「什么都不动」

`PER_PERSON_WIP_LIMIT=3` 是真实约束（不淹没人），但模型爱重复申请，
队列一满新动作全被拦。`live_drive.py click` 会把队列清干净。
看到 `BLOCKED_WIP_LIMIT` 堆积就先点链接。

---

## 四、验收时

### 别信 bronze 里有数据

我差点信了一次：看到 `acme__fin_invoice` 有 600 行就以为这轮成了，
查事件才发现那是**上一轮回归留下的**，这次的 ingest 当时还没执行。

`verify_live.py` 做两件事挡住这个：

- **排除历史 eval 表**（名字里带 `eval_` 的都不是这轮的产物）
- **交叉核对**：`sync_state` 里工具说的行数 vs 独立连 Trino 查的行数，逐张对

### 别信「线是 done」

工具用**返回值**报错时（不抛异常），Hermes 那边 status 一律是成功，
线会被收成 done 而实际失败了。`_looks_failed()` 现在按错误前缀识别，
但**加新工具时错误消息要以「错误：」「XX 失败：」开头**，否则又会静默。

### 审计问题要用全新会话问

`rm -rf .hermes/live-home/sessions` 之后再问「这张表哪来的」。
带着上下文问，它会用记忆答，测不出溯源能力——第一次问就是这么发现
「查不到它就会猜」的（答成了 postgres 超级用户）。

### `describe_asset` 现在默认读档案，不回源库

R6 闭环 A 之后，它答的是 `asset_catalog` 里的快照，输出会写明「采于 X」。
两个后果，演练时都会撞上：

- **注入 schema 漂移（G1）之后，它不会自己发现。** 那正是闭环 B 要补的洞，
  不是这次演练的 bug。要看新列，得让它带 `refresh=true` 再问一次。
- **`reset_live.py` 把治理库整个删了，档案跟着没。** 所以重置后第一次
  `describe_asset` 一定回源采集一次（输出会写「无档，刚刚采集了一次」）——
  这是对的。第二次起才读档。

---

## 五、清环境

**审批也是历史。** `reset_live.py` 清六样：

| 清什么 | 为什么 |
|---|---|
| bronze / silver / gold | 产物 |
| 治理库（审批、决定、线、口径、凭证、事件、台账） | 上一轮的审批链接还能点 |
| GreenMail（重启即清） | 旧的批准链接还能点，会污染下一轮 |
| Hermes 会话 / state / cron 状态 / 缓存 | 模型会「记得」上一轮 |
| `/tmp/live.*`、点击记录 | 同上 |
| **源库不清** | 它模拟公司已有的生产库，是既存事实，不是产物 |

清完**自己验一遍**（`--check`）：湖空、库无、邮箱空、无会话残留、
**源库完好**。起点不干净，跑出来的数不算数。

### 清湖会让两个测试组变红

`test_join` / `test_clean` 依赖 bronze 里跑过 eval 才有的表。它们现在
探活后 SKIP 并说明原因，不再莫名其妙地红。**但这类依赖别再新增**——
测试要么自己准备数据，要么探活跳过。

---

## 六、指纹规则变更 = 存量审批全废

改 `IDENTITY_KEYS` 或 `action_hash` 的算法之后，**在途审批的哈希是用旧规则
算的，新代码查不到** —— 表现是「人批过了但恢复时又发一份新审批」。

开发期直接重算：

```python
for aid, tool, aj in q("SELECT id, tool_name, args_json FROM approvals"
                       " WHERE used_at IS NULL"):
    q("UPDATE approvals SET action_hash=? WHERE id=?",
      (action_hash(tool, json.loads(aj)), aid))
```

生产要一次迁移。**这笔账还没还** —— 记在 handoff 的下一步里。

---

## 七、真模型和桩，分开报数

- **桩**（`tests/model_stub.py`）：工具序列由剧本给，证明**管道通**。
- **真模型**：只喂对话，工具调什么它自己定，证明**判断对**。

拿桩模式的数字说「Agent 判断对」，就是拿自己写的剧本给自己打分。

真模型模式**只能走网关**：`hermes -z` 每次是新会话，`--resume latest` 对
oneshot 无效（实测第二拍答「我没有上下文」）。而 Agent 会问澄清问题，
没有会话就永远停在问问题。
