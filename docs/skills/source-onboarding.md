# Skill：接入一个新数据源

> **这是提示词层的编排，不是安全边界**（readme 4.6）。
> 本文件只写「按什么顺序判断」和「有哪些坑」。
> **任何「不可以」都不在这里**——限制在 gate 与 Connector Service 里，见文末。

## 判断顺序

**1. 它有 SQL 接口吗（JDBC / ODBC）？**

有 → **直连，不要导出。**
Postgres、MySQL、**Snowflake、Databricks、BigQuery** 都算。
从一个能直接查的仓库导 CSV 是降级：丢类型、丢下推、丢 Delta/Iceberg 时间旅行，
还要跑一次对方的 warehouse（按 DBU/credit 计费）。

没有 → 下一步。

**2. 它能定时导出并自动发到收件箱吗？**

能 → **走邮件附件路径，这是默认答案。**
Salesforce 报表、HubSpot、飞书多维表格都支持定时邮件发送。
接收侧的白名单、嗅探、溯源全是现成的，不需要新代码。

全量快照顺带解决了两个增量同步难题：不存在分页不一致；删除可以靠两次快照做主键集合差找出来。

不能 → 下一步。

**3. 只有导出被堵死时，才考虑 API 数据面。**

判据：源系统没有导出功能 / 导出被管理员禁用 / 需要的新鲜度高于定时报表上限
（多数 SaaS 最快每小时）/ 单次体量超附件上限且无法分批。

走到这一步要先跟 Owner 谈配额，不要自己开工。

## 无论走哪条，都要单独取一次权限现状

**权限导不出来，只能读。** 报表给得了行，给不了「谁能看这些行」。

| 平台 | 读什么 |
|---|---|
| Salesforce | Profile / PermissionSet / SharingRules / FieldPermissions |
| HubSpot | Users & Teams |
| Databricks | Unity Catalog `system.information_schema.table_privileges` |
| Snowflake | `SHOW GRANTS` |
| Postgres | `pg_roles` / `pg_catalog`（已有 `list_*` 工具） |
| 飞书 / 企微 | 通讯录与应用可见范围、多维表格协作者 |

这是 R4 权限治理唯一的素材来源。**接入时顺手拿，比事后补容易得多**——
事后补要重新走一遍审批。

## 已知的坑

**列名是显示标签，不是 API 名。**
导出给的是「客户来源」，API 里叫 `LeadSource`。业务方改个显示名，列名就跟着变，
schema drift 追不动。第一次接入时把映射跟人确认一次，记进记忆层。

**导出的所有列都是文本。**
数值、日期、布尔全变字符串，空值有 `NULL` / `''` / `未知` / `N/A` 多种形态。
这是正常的，不是错误——bronze 原样落，silver 修。用 `blank_rate` 而不只是 `null_rate` 判断。

**Salesforce 的只读证明不了。**
`api` scope 全有全无，只读得靠连接用户的 Profile / Permission Set。
接入审批里要请 Owner 确认「这个用户是只读的」，并把 Permission Set 名称或截图存进 Ledger。
**这是全项目唯一一处「只读」我们这侧无法自证的地方。**

## 边界不在本文件

以下都由代码强制，跟本文件写什么无关：

| 约束 | 落在哪 |
|---|---|
| 模型 SQL 的放行 / 审批 / 拒绝 | Connector `review_sql()` + gate `_sql_guard()`（sqlglot AST，按 `plane=source|lake` 区分外部源和已入湖表） |
| 源账号只读 | 源库 GRANT + 容器隔离 |
| 接入必须先有审批 | `register_source(approval_id=...)` |
| 附件发件人 / 类型 / 大小 | 入站附件路径 |
| 工具分级、未声明即拒绝 | `plugins/datasteward_gate/policy.py` |

**自检：把这个文件整个删掉，系统还安全吗？**
答案必须是「是」。如果某条删掉就不安全了，说明它写错地方了，该搬进 gate。
