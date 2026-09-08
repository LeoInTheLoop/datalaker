# 实测轨迹：northwind 接入到跨表回答

> 真模型（qwen3.8-27b / DashScope）+ 真网关（Hermes email 平台）+ 真 Postgres。
> 邮件出入站都走 GreenMail 模拟。起点由 `tests/reset_live.py` 清空并自验。
>
> 选 northwind 而不是 olist：14 张表、**13 条外键**，含自引用
> （`employees.reports_to → employees.employee_id`）、两张联结表、
> 三跳链（`order_details → orders → customers`）。olist 行数大但只有 4 条外键。

## 起点

```
湖空 · 治理库无 · 邮箱空 · 无会话残留 · 源库完好
northwind: 14 表 / 13 外键 / orders 830 行 / customers 91 行
可用只读账号: ops_reader（northwind 上 14 张表 SELECT）
```

---
## 第一轮（发现 3 个 bug，已修，重跑）

按真实顺序推：先说要接入、**不给账号** → DBA 给了个**错的账号**
（`crm_reader`，在 northwind 上零权限）→ 批准 → 连不上。

模型表现对的部分：没有越权去扫；先问账号；走了审批；
自己注意到「crm_reader 名字看着像 CRM 的账号」；还提醒口令在邮件正文里明文出现过。

**Bug 1：连不上的通知发错人。**
`_notify_connect` 里 `to = _resolve_to(owner) or given_by` —— owner 优先。
于是 dba 给了个没权限的账号、失败通知却发给了 wang（owner），
**而 wang 手里根本没有账号**。能修的人一直不知道出了事。
`source_secrets.registered_by` 里明明记着 `dba@acme.com（sun 转交）`，只是没人读。

修：失败时发给 `given_by`，成功时仍发给 owner（先接哪几张表是业务判断）。
顺带补了 `_mail_of()` —— `given_by` 是模型转述的自由文本，
`dba@acme.com（sun 转交）` 直接当收件人用发不出去，而**发不出去是静默的**。

**Bug 2：拿已经证明连不通的账号反复重试。**
模型接着调了 `connect_source(source_id="northwind")`（不带 dsn，想走恢复那条路）。
门禁给它开了张新审批票，票里只有一个 source_id。人批了也没用 —— 里面没有新账号；
更糟的是批完之后 handler 会用 `_dsn_from_approval` 从旧审批里取回
**那份已经连不通的** dsn 再连一次。而模型刚跟人说过「不会反复重试」。

修：连接失败记一条 `SOURCE_CONNECT_FAILED` 事件（只记身份 `user@host/db`，不记口令），
`_dsn_from_approval` 跳过它们；门禁加 `_connect_without_dsn`：
没有可用连接串时**直接拒绝、不发审批** —— 现在发审批没有意义。

**Bug 3：人发来修正后的账号会被静默丢掉。**
动作身份不含凭证（`_CREDENTIAL_KEYS`，这是有案底的正确设计：
否则恢复时模型不带 dsn 就对不上票）。副作用是「接入 northwind」不论换几个账号
都是同一张票 —— 模型带着**新**账号来，会挂到那张参数里还是旧账号的待批票上。
人收到的信里看不到新账号；批完之后 `_replay_approved_args` 又把新账号换回旧的。

修：`amend_pending()` —— **只改还没人做过决定的票**，改完重新发信，
让人看见自己批的是哪个账号。已经批过或拒过的一律不动（改它就是伪造授权）。

---
## 第二轮（重跑时又抓到 1 个，是我修出来的）

**Bug 4：新加的门禁把「恢复」自己挡住了。**
`_usable_dsn_exists` 里写了 `rows = st.db.execute(...) if hasattr(...) else []`，
后面跟一句 `if not rows: <改走 st.db.cursor()>`。
失败事件表**本来就是空的**（还没失败过），于是走进 SQLite 不支持的
`with db.cursor()`，异常被外层吞成 `False` —— 「没有可用凭证」，
人批过的票被挡在门外（日志里只有 `BLOCKED_NEED_DSN`）。

**「查出来是空的」不等于「后端猜错了」。** 项目里第三次撞这个形状
（前两次记在 `catalog._semantics_rows` 的注释里），这次补了共用助手
`approvals.rows_of()`：按后端类名判，不按 `hasattr`，更不按结果空不空。

顺手把失败通知里的报错加宽到 240 字并带上账号身份 ——
原来截在 120 字，`permission denied for database "northw` 正好砍掉库名，
而收信的人手里有好几个账号，「连不上」他没法判断是哪一个。

## 第三轮：三个修复全过

```
dba 给错账号(crm_reader) → 批准 → 连不上
  ✅ 失败通知发给 dba（不是 wang）
  ✅ 记下 crm_reader@127.0.0.1:5432/northwind 连不通
dba 发来正确账号(ops_reader)
  ✅ 开了新票 c5acb2c9，**票里是新账号**（旧票已批过，按规则不动它）
  ✅ 模型还主动请人去拒掉作废的旧票
批准 → 接入成功，14 张表
  ✅ 成功通知发给 wang（owner）—— 先接哪几张是业务判断
```

## 接表：又两个 bug

**Bug 5：`employees` 接不进来，报错看不懂。**
```
SyncError: Insert query has mismatched column types: Table: [bigint, varchar, varchar, ...
```
`employees.photo` 是 `bytea`，Trino 那边读成 `varbinary`；
而 `_pg_type_to_trino` 最后一行是 `return "varchar"` —— **任何没登记的类型
都悄悄变成 varchar**，错不在建表那一刻暴露，而在插数那一刻，
报错既不指名哪一列也不指名哪个类型，接入线只留一句「失败」。

修：`bytea/blob/binary → varbinary`，补上 `time`、`uuid`、`json` 等；
**认不出来的类型直接抛 `UnknownColumnType`，不再蒙。**
（Iceberg 不收带长度的类型，所以不能照抄源库类型名，映射表必须留着。）

**Bug 6：做完的事每一轮都重新申请一次审批。**
一轮演练里 `customers` / `orders` / `order_details` **各被申请了 3 次**，
`connect_source` 多了 3 次 —— 每次 cron 唤醒模型都重新规划一遍。
工具自己是幂等的（「6 分钟前刚接过，没有重接」），但那句话是
**人点完链接之后**才出现的：人已经白点了 9 次。

之前试过在 `list_source_tables` 的输出里标「已接入」提醒它，没用 ——
照铁律 1，这道门得在 hook 里。加 `_already_done()`：
接过的表、已接上的源，**拦在建票之前**；`full_refresh` 不拦
（它的语义就是「明知有也要重来」），换账号也不拦（那是新动作）。

实测生效，模型自己说的：「这次重试被挡回来了，没再发审批」。

---

## 跨表回答（复杂库上的闭环 C）

问题：**哪个销售卖得最好？** 三跳：`order_details → orders → employees`。
单看任何一张表都答不了 —— 金额在 order_details，销售是谁在 employees，
中间要靠 orders 接。

模型自己给的核对材料：全表总额 = 9 人之和、零孤儿行、换一种算法复核一致、
并注明「用的两条连接还没走人确认，要发 gold 之前会补」。

**独立核对**（直接在源库 psql 算，不走 Agent 那条路）：

| 排名 | 销售 | Agent | 源库直算 |
|---|---|---|---|
| 1 | Peacock (4) | 232,890.85 / 156 单 / 420 行 | 232890.85 / 156 / 420 |
| 2 | Leverling (3) | 202,812.84 / 127 / 321 | 202812.84 / 127 / 321 |
| 3 | Davolio (1) | 192,107.60 / 123 / 345 | 192107.60 / 123 / 345 |
| 4 | Fuller (2) | 166,537.76 / 96 / 241 | 166537.76 / 96 / 241 |
| 5 | Callahan (8) | 126,862.28 / 104 / 260 | 126862.28 / 104 / 260 |
| — | 总额 | 1,265,793.04 | 1265793.04 |

6–9 名也逐个对上。**全部一致。**

## 补测试时又抓到的（回归红了才发现）

**Bug 7：整场回归默认往公网发真信。**
`.env` 里是 `MAIL_TRANSPORT=gmail_api` 加真实收件人（那是给真演练用的），
而 63 个测试文件里只有 14 个自己设了 outbox —— 其余靠 `.env` 默认值。
写这次的新测试时看到 owner 解析出一个真 Gmail 地址才注意到。
这次没发出去（新测试自己设了 outbox），但下一条走到 notify 的新测试就会。

修：`run_all.sh` 顶上 `export NOTIFY_CHANNEL=${NOTIFY_CHANNEL:-outbox}`，
要真发的那几组自己覆盖。项目已经为这个形状撞过一次（见 live-rehearsal §二）。

**新门禁遮住了一条安全断言。**
`_already_done` 排在票据检查之前，于是「票据一次性」那两条断言
（`test_pipeline` / `test_hermes_tool_loop`）拿到的是 ALREADY_DONE，
测的就不再是「票用过一次还能不能再用」。

**没有弱化那条断言**：改成先把 `sync_state.last_synced_at` 调老两天，
绕过 ALREADY_DONE 之后走回原来那条路，安全判据一个字没松。

**`test_join` 硬编码了「employees 不在湖里」。**
这次真接了 employees，它就红了 —— 红的不是规则，是测试自己的前提。
改成按湖里实际有什么验规则：**join 路径里出现的对端，正好是湖里有的那些**。
比验一个实例强，而且不会再被一次正常的接入推翻。

**`test_scenario_discovery` 调 `connect_source` 时不带连接串。**
真实链路是人在邮件里给账号、模型带着它来申请；不带 dsn 的调用现在会被挡
（没有凭证的接入，人批了也接不上）。测试改成带上 dsn，更贴近真链路。
同时把门禁里那句「缺 source_id」退回去 —— 按 CLAUDE.md，
输入校验归 handler，门禁只判授权，抢着报会把参数写错说成授权问题。

## 新增测试

`tests/test_connect_retry.py`（33 条）：六个坑逐条盯，包括
「失败事件里没有口令」「批过的票一律不动」「两天前接的不算刚接过」
「不认识的类型抛错不蒙 varchar」。接进回归 4.435 组。

**Bug 8：探活通过 ≠ 我们的服务起来了。**
回归第 9 组（审批闭环）红了 5 条：「点击批准成功 http 0」「链接重放被拒 http 200」。
看起来像审批链路坏了。实际是 **8787 上有一个上次跑剩的 callback 进程**，
它拿着另一个库，`/health` 照样回 `ok`，探活于是通过 ——
然后所有令牌都对不上。查了半天才发现是僵尸进程。

修两处：
- `/health` 改成回 `ok pid=<pid> db=<db>`，**报出自己是谁在应答**；
- `run_all.sh` 的探活比对 pid，不是我们刚起的那个就直接报 FAIL，
  而不是让它继续往下跑、红在一个完全无关的断言上。

顺带发现 `run_all.sh` 里 `approval_callback.py` 走的是 `python3`（本机 3.14），
而测试走 `./.venv/bin/python`（3.13）—— CLAUDE.md 写明不用 3.14。这条没动，记着。

---

## 这一轮的账

| # | 问题 | 修在哪 |
|---|---|---|
| 1 | 连不上的通知发给 owner，而账号是 dba 给的 | `tools._notify_connect` + `_mail_of` |
| 2 | 拿已证明连不通的账号反复重试，还开人处理不了的票 | `SOURCE_CONNECT_FAILED` 事件 + 门禁 `_connect_without_dsn` |
| 3 | 人发来的新账号被静默丢掉 | `approvals.amend_pending` + 门禁 `_args_changed` |
| 4 | 「查出来是空的」被当成「走错后端」 | 共用助手 `approvals.rows_of` |
| 5 | `bytea` 蒙成 varchar，错推迟到插数 | `sync._pg_type_to_trino` + `UnknownColumnType` |
| 6 | 做完的事每轮重新申请审批，人白点九次 | 门禁 `_already_done` |
| 7 | 整场回归默认往公网发真信 | `run_all.sh` 顶上兜底 outbox |
| 8 | 探活通过但应答的是僵尸进程 | `/health` 报 pid+db；探活比对 pid |

1/2/3/6 都是**接入失败之后**才暴露的 —— 接入成功那条路早就测过了。
5 是「不认识就蒙一个」的代价：错不在建表那刻暴露，而在插数那刻，且报错不带列名。
4/8 是同一个形状的两次：**把「没看到」当成「不是这条路」**。

---

## 收尾

**全量回归**：`1178 passed / 0 failed`，退出码 0，
3 项 SKIP（Phoenix 未启动；两项因 `reset_live` 清了湖、相应产物还没重建）。

新增组：
- `4.435` 接入失败之后（33 条）
- `4.44` 闭环 C（57 条，本轮仍全绿）

**没做的**：`run_all.sh` 里 `approval_callback.py` 走 `python3`（本机 3.14），
测试走 `.venv`（3.13）。CLAUDE.md 写明不用 3.14。这次两个解释器都验过能跑通，
没动它 —— 但它是个不该存在的分叉。
