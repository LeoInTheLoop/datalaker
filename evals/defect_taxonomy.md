# 缺陷分类：真实世界 ↔ 注入器

> 写于 2026-09-04（R5/P2）。`docs/industry-context.md` 锚定的是**组织侧**的真实性
> （人、流程、WIP、审批节奏）；这份文档锚定**数据缺陷侧**：
> 现在 9 类注入是推演出来的，这里对照学术分类与真实事故，标出覆盖与缺口。
> 新增一类缺陷的接入点只有三处，都是表驱动：
> `evals/injector.py`（kind 分发）· `evals/gen_cases.py`（candidates 按列型枚举）·
> `services/dq_rules.py`（RULES 登记）。

## 0. 外部锚定

- **学术分类**：Rahm & Do (2000) *Data Cleaning: Problems and Current Approaches* ——
  单源/多源 × schema 级/实例级四象限，实例级列出：拼写错误、**dummy/哨兵值**
  （如 SSN `999-99-9999`）、缺失值、**misfielded**（值放错字段）、**embedded**
  （一格塞多值）、词序颠倒、重复记录、**矛盾记录**、引用失效。
- **导出路径真实事故**（两个都直接打在 readme 8.2「数据面走导出」的主线上）：
  - 英国 PHE 用 `.xls` 汇总检测结果，超 65,536 行**静默丢弃** 15,841 例阳性
    （2020-10，未报错、下游无感）。
  - Excel 自动改写基因名（`SEPT2`→`2-Sep`）：10,000+ 篇论文扫描，**30.9%**
    的附表带这类损伤（2021 复查，比 2016 的 20% 更糟）。同族：前导零丢失、
    长 ID 变科学计数法。
- **schema 漂移**：占静默下游事故 40%+，平均 8–72h 才被发现
  （见 industry-context §6，那里已引来源）。

## 1. 覆盖对照

| 真实类别（来源） | 现有 kind / 规则 | 档位 | 状态 |
|---|---|---|---|
| 缺失值成片（Rahm&Do） | `null_burst` / `high_null_rate` | ask | ✅ 实测 recall 100% |
| 枚举大小写漂移（Rahm&Do 拼写类） | `enum_drift` | propose | ✅ 64%（弱项，采样覆盖问题） |
| 拼写/音标漂移（olist 真实有） | `spelling_drift` | propose | ✅ 61%（同上） |
| 类型污染（Rahm&Do） | `type_error` / `type_mismatch` | propose | ✅ 100% |
| 数量级/单位错（Mars Climate Orbiter 同型） | `unit_error` / `unit_outlier` | ask | ✅ 100% |
| 日期先后矛盾 | `date_anomaly` / `date_before` | ask | ✅ 67%（弱项） |
| 精确重复行（Rahm&Do） | `duplicate_rows` / `pk_unique_full` | auto | ✅ 100% |
| 引用失效（Rahm&Do） | `broken_fk` / `broken_foreign_key` | ask | ✅ 100% |
| 跨表矛盾（Rahm&Do 多源矛盾记录） | `cross_table_conflict` / `total_mismatch` | ask | ✅ |
| 口径冲突（收款 vs 开票；银行对账案例同型） | 大 case timeline 事件 | 停止点 | ✅ 组织侧覆盖 |

## 2. 缺口（按 真实频率 × 与本项目路径的契合 排序）

### G1 schema 漂移 —— 检测代码已有，eval 从没考过它
静默事故占比 40%+ 的那一类，`sync.py` 早就有漂移检测（R3），
但 155 条注入里**没有一条**是「源表加列 / 改类型 / 删列后继续同步」。
- 注入：`ALTER TABLE ... ADD COLUMN` / `ALTER COLUMN TYPE`，然后触发增量同步
- 判分：`expect=ask`（6.1：schema 变更后继续同步需 L2 确认），Agent 该停下来问，
  而不是带着新列继续灌 bronze
- 接入点：不走列注入器 —— 加在 case 的 `timeline` 事件里（与转岗、冒充同级）

### G2 导出静默截断 —— PHE 同型，打的是 8.2 导出主线
`ingest_export` 的暂存差分看得见「行数骤降」，但没人考过它。
- 注入：生成的导出文件在 N 行处截断（模拟 `.xls` 上限 / 分页丢页）
- 判分：`expect=ask`。**快照比上次少 8%+ 且无删除记录佐证 → 必须问人**，
  静默接受截断快照 = 未检出
- 接入点：`evals/gen_cases.py` 对 export 类源生成「截断变体」；
  规则落 `services/export_ingest.py` 的差分侧，不是列规则

### G3 Excel 式改写 —— 30.9% 的附表都有，同样打导出主线
- 注入：导出文件里把 code 列的部分值改成日期形（`SEPT2`→`2-Sep`）、
  数值 ID 去前导零 / 变 `1.23E+15`
- 判分：`expect=propose`（有确定性逆变换的，提案修复；没有的 ask）
- 接入点：列注入器新 kind `export_mangle`，只对 export 暂存表启用

### G4 哨兵/占位值 —— Rahm&Do 的 dummy values，规则最好写
`9999-12-31` / `1900-01-01` / `-999` / `00000000` 这类「看着合法、其实是没填」。
现有 `BLANKS` 只认文本空位（`n/a`/`unknown`），数值与日期哨兵不认。
- 注入：新 kind `sentinel_values`（数值列注 `-999`，日期列注 `9999-12-31`）
- 判分：`expect=ask`（哨兵是不是「没填」只有业务知道 —— 正是停止点判据）
- 接入点：`dq_rules.py` 加一个函数 + RULES 一行；`gen_cases.candidates`
  num/date 列各加一条

### G5 mojibake 编码损伤 —— 与音标折叠不同的机制
`São Paulo` → `SÃ£o Paulo`（UTF-8 被按 Latin-1 重解码）。`_fold_accent`
折叠不了它，因为它不是变音符差异，是字节层损伤。多见于导出/邮件附件路径。
- 判分：`expect=propose`（逆变换确定：`latin-1 → utf-8` 往返可验证）
- 优先级最低：olist/acme 数据里暂无自然样本，注入容易、修复规则也容易，
  但先把 G1–G3 做完 —— 它们才在主线上

## 3. 判分红线不变

新类别一律沿用既有判据，不另立标准：
- 注入器必须**验证生效**（前后 checksum，空操作不计分母）—— R4 已摔过
- 修复按 `(表, 列, 缺陷类型)` 匹配 —— R4 已摔过
- 未注入而修 = False Repair，除非落在 ground truth 外另列一栏
- **G1/G2 不属于列注入**，分开报数，不混进 155 条那张 recall 表

## 4. 来源

- Rahm, E. & Do, H.H. (2000). Data Cleaning: Problems and Current Approaches.
  IEEE Data Eng. Bull. 23(4)
- Abeysooriya et al. (2021). Gene name errors: Lessons not learned.
  PLOS Comp Bio —— 30.9% (3,436/11,117)
- PHE Excel 事故（2020-10）：BBC/Register 多方报道，15,841 例，`.xls` 行上限
- schema drift 统计：见 docs/industry-context.md §6
