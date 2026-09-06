"""审批存储 —— 两表 append-only 模型。

职责隔离（readme 9.4 机制三）：
  approvals  Agent 可 INSERT（发起请求）+ UPDATE used_at（消费票据），但**不可**写决定
  decisions  Agent 只有 SELECT。仅审批 callback 服务可 INSERT

单表放不下这个隔离：Agent 必须能创建请求，若同表就无法阻止它写 decision 字段。
拆成两张表后，「Agent 伪造一条批准」在权限层面直接不可能。

生产（Postgres）的授权：

    GRANT INSERT (id, run_id, action_hash, tool_name, args_json, approver, expires_at)
        ON approvals TO agent_role;
    GRANT UPDATE (used_at) ON approvals TO agent_role;
    GRANT SELECT ON approvals, decisions TO agent_role;
    -- 注意：不授予 decisions 的 INSERT / UPDATE

SQLite 无列级权限，故本地开发用 readonly 连接模拟；集成测试须跑在 Postgres 上。
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid

DDL = """
-- 「待答事项」：审批 = 选项固定为 approve/deny 的提问（readme 5.7）
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    action_hash  TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    args_json    TEXT NOT NULL,
    approver     TEXT NOT NULL,          -- 角色名或邮箱
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    used_at      REAL,
    kind         TEXT NOT NULL DEFAULT 'approval',   -- approval | question
    abandoned_at REAL,                  -- 超时放弃：退出活跃队列但不删除
    escalation_level INTEGER NOT NULL DEFAULT 0,
    options      TEXT,                   -- JSON: [{key,label,desc,recommended}]
    question     TEXT,
    evidence     TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id           TEXT PRIMARY KEY,
    approval_id  TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('approve','deny','answered')),
    chosen       TEXT,                   -- question 类型：选中的选项 key
    approver     TEXT NOT NULL,
    decided_at   REAL NOT NULL,
    token_jti    TEXT UNIQUE NOT NULL,
    message_id   TEXT,
    client_ip    TEXT,
    user_agent   TEXT
);
-- 角色化：绑角色不绑人（readme 10.4）
CREATE TABLE IF NOT EXISTS role_assignment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    person      TEXT NOT NULL,
    valid_from  REAL NOT NULL,
    valid_to    REAL,
    granted_by  TEXT NOT NULL,
    reason      TEXT
);
-- 业务知识沉淀：问过的不再问（readme 5.7）
CREATE TABLE IF NOT EXISTS asset_semantics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at REAL NOT NULL,
    source_item  TEXT,
    UNIQUE(asset, key)
);
-- 运维账本（readme 20.1）：落库而非进程内存，监控独立于被监控对象
CREATE TABLE IF NOT EXISTS query_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    source_id   TEXT NOT NULL,
    purpose     TEXT,
    sql_text    TEXT NOT NULL,
    rows_out    INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0,
    est_rows    REAL,
    status      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    run_id        TEXT,
    model         TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    purpose       TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT
);
-- Remediation Ledger（readme 7）：治理动作的审计轨 + 给源系统的整改清单。
--
-- **只在 lake 里清洗，等于给源头的缺陷付永久利息。**
-- 因此这张表的价值不在建议的数量，而在被采纳的比例，以及
-- 「清洗规则数量在下降」这个反直觉的成功指标。
CREATE TABLE IF NOT EXISTS remediation_ledger (
    rl_id            TEXT PRIMARY KEY,
    source_table     TEXT NOT NULL,
    field            TEXT,
    issue_type       TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT 'data',   -- data / permission
    observed_pattern TEXT,
    action_taken     TEXT,
    confirmed_by     TEXT,
    message_id       TEXT,
    suggestion       TEXT,
    owner_role       TEXT,
    status           TEXT NOT NULL DEFAULT 'open',   -- open/proposed/accepted/fixed/rejected
    reject_reason    TEXT,
    recurrences      INTEGER NOT NULL DEFAULT 1,
    rows_cleaned     BIGINT NOT NULL DEFAULT 0,
    rule_revisions   INTEGER NOT NULL DEFAULT 0,
    first_seen       REAL NOT NULL,
    last_seen        REAL NOT NULL,
    due_at           REAL
);
CREATE INDEX IF NOT EXISTS ix_rl_status ON remediation_ledger(status);

-- 清洗规则。**每条都要标注它在补哪个洞**（readme 7）——
-- 没有 ledger_ref 的规则无法退役，因为没人知道它为什么存在。
CREATE TABLE IF NOT EXISTS cleaning_rules (
    name         TEXT PRIMARY KEY,
    ledger_ref   TEXT NOT NULL,
    fixes        TEXT NOT NULL,
    retire_when  TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    revisions    INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    retired_at   REAL
);
-- 任务注册表（readme 2「Harness 能力」/ 4.1）。
--
-- **Agent 不是一次调用，它会等人。** 一条线可能是：
--   发现数据 → 找 Owner → 发邮件 → 等 8 小时 → 回复 → 请求审批 → 等 1 天 → 恢复 → 执行
-- 而且同时并行着好几条，各自挂在不同的人身上。
--
-- 没有这张表，「挂起 → 恢复」就只能靠同一个进程一直活着 —— 进程一死全丢。
-- 有了它，每条线的状态在库里，谁先被批准谁先被拉起，彼此不互相阻塞。
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    params      TEXT NOT NULL,
    status      TEXT NOT NULL,           -- running / waiting_human / done / abandoned / failed
    waiting_on  TEXT,                    -- 阻塞它的 approval_id
    checkpoint  TEXT,
    note        TEXT,
    owner_role  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    resumed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);
-- 增量同步状态（readme 6.1）。R3 只在 Postgres 里手工建过，
-- 于是 SQLite 后端整条同步路径直接崩在「表不存在」上——DDL 必须跟代码走。
CREATE TABLE IF NOT EXISTS sync_state (
    asset            TEXT PRIMARY KEY,
    strategy         TEXT NOT NULL,
    watermark        TEXT,
    last_synced_at   REAL,
    data_as_of       REAL,
    freshness_sla_h  INTEGER NOT NULL DEFAULT 24,
    schema_hash      TEXT,
    row_count        INTEGER,
    last_error       TEXT
);
-- 授权源清单（readme 8 凭证层）：**谁告诉过我们这个源存在**。
-- 源是人给的，不是 Agent 自己找的 —— 未经授权的扫描本身就是违规。
-- 与 decisions 同一条原则：Agent 只读，写在人那一侧（铁律 2 的形状）。
-- 清单为空 = 未启用（沿用引导源的开发形态）；非空即生效，其余一律拒。
CREATE TABLE IF NOT EXISTS source_grants (
    source_id   TEXT PRIMARY KEY,
    revealed_by TEXT NOT NULL,
    revealed_at REAL NOT NULL,
    note        TEXT
);
-- 源系统凭证（readme 8 凭证层）：**Agent 读不到这张表。**
-- 人在邮件里给的连接串经 `connect_source`（L3，批准后）落在这里，
-- 之后 Agent 只提交 {source_id, sql}，DSN 只有 Connector 自己读 ——
-- 这是「Agent 不持有 DSN」从注释变成机制的那一步。
-- 与 decisions 同一条原则：PG 侧靠不给 agent_role SELECT，
-- SQLite 侧靠代码里没有那条读路径（本地开发无列级权限，如实记在这）。
CREATE TABLE IF NOT EXISTS source_secrets (
    source_id     TEXT PRIMARY KEY,
    dsn           TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'postgres',
    approval_id   TEXT NOT NULL,        -- 没有批准就没有数据源
    registered_by TEXT NOT NULL,
    registered_at REAL NOT NULL
);
-- 资产台账（readme 13 血缘）：**一张表的来历，一处记全。**
--
-- 在这之前「这张表哪来的」要跨 5 张表现拼：source_grants 查谁给的源、
-- source_secrets 查什么账号、approvals+decisions 查谁批的、sync_state 查
-- 什么时候入的湖、asset_semantics 查按谁的口径 —— 而找审批只能靠
-- `args_json LIKE '%表名%'` 模糊匹配，又慢又脆。外部系统（审计、BI）
-- 更是无从下手。
--
-- **append-only**：每个关键动作发生时写一行，不改不删。血缘是历史事实，
-- 改写它等于伪造审计轨 —— 与 decisions 同一条原则。
CREATE TABLE IF NOT EXISTS asset_provenance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset       TEXT NOT NULL,          -- acme.fin_invoice
    event       TEXT NOT NULL,          -- source_registered/ingested/cleaned/...
    actor       TEXT,                   -- 谁做的、谁批的
    approval_id TEXT,                   -- 依据的那份审批
    detail      TEXT,                   -- JSON：账号身份、行数、规则、口径……
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prov_asset ON asset_provenance(asset, ts);

-- 资产档案（R6 闭环 A）：**认知可复用 —— 不回源库也说得清一张表。**
--
-- 在这之前有两个毛病。一是每问一次结构就回源库三趟往返
-- （`describe_asset`：列、外键、lake 清单），新会话等于从零开始。
-- 二是推断与人工确认混在 `asset_semantics` 里，靠 `confirmed_by`
-- 的字符串前缀（`system:fk_inference`）区分，而 `UNIQUE(asset,key)`
-- 让人一确认就把推断**覆盖掉** —— 「系统曾经猜错过什么」查不出来，
-- 而那正是下次少犯错的依据。
--
-- 三类内容按 `status` 分行存，**各自成行，互不覆盖**：
--
--   observed   源库里到底有什么。**只有采集路径写**（`services/catalog.py`），
--              没有任何工具让模型写这一层 —— 模型能改事实的那一刻，
--              整套档案就不可信了
--   inferred   推断。必须带 `evidence`，默认待验证
--   confirmed  人确认。记确认人与依据；确认**不删**推断，只 supersede
--   refuted    被否定的假设。留着，免得下次再猜同一个错
--
-- append-only：改写等于伪造审计轨（与 `decisions`、`asset_provenance` 同一条原则）。
-- 取代关系记在 `superseded_by`，旧行仍然查得到。
--
-- **旧档案是比较基线，不能证明源库没变。** 读档省的只是重复往返；
-- 发现变化仍然需要新的观测（闭环 B）。
CREATE TABLE IF NOT EXISTS asset_catalog (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset         TEXT NOT NULL,        -- acme.fin_invoice
    kind          TEXT NOT NULL,        -- schema / foreign_keys / ownership / link
    key           TEXT NOT NULL,        -- 同一 kind 下的条目名
    value         TEXT NOT NULL,        -- JSON 或一句话
    status        TEXT NOT NULL CHECK (status IN ('observed','inferred','confirmed','refuted')),
    evidence      TEXT,                 -- 凭什么这么说（JSON）
    actor         TEXT NOT NULL,        -- connector:acme / system:fk_inference / 人
    observed_at   REAL NOT NULL,        -- 这条内容对应的观测或确认时刻
    fingerprint   TEXT,                 -- 内容指纹：没变就不再写一行
    superseded_by INTEGER               -- 被哪一行取代；NULL = 当前有效
);
CREATE INDEX IF NOT EXISTS ix_cat_cur ON asset_catalog(asset, kind, superseded_by);
CREATE INDEX IF NOT EXISTS ix_appr_hash ON approvals(action_hash, run_id);
CREATE INDEX IF NOT EXISTS ix_dec_appr  ON decisions(approval_id);
"""

TTL = 72 * 3600

# 谁取代谁（`asset_catalog`）。**确认不删推断，只把它标成被取代的。**
#
# observed 只取代 observed：源库多了一列，不代表人定过的口径就失效了 ——
# 那是两码事，失效判断由巡检显式做（闭环 B），不在这里顺手替人决定。
_SUPERSEDES = {
    "observed":  ("observed",),
    "inferred":  ("inferred",),
    "confirmed": ("inferred", "confirmed", "refuted"),
    "refuted":   ("inferred", "refuted"),
}
CATALOG_STATUS = tuple(_SUPERSEDES)


def _catalog_norm(value, evidence, fingerprint):
    """档案行的三处规范化，两个后端共用（值、依据、指纹）。"""
    val = value if isinstance(value, str) else json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    ev = evidence if isinstance(evidence, (str, type(None))) else json.dumps(
        evidence, sort_keys=True, ensure_ascii=False)
    fp = fingerprint or hashlib.sha256(val.encode()).hexdigest()[:16]
    return val, ev, fp


def _catalog_row(r):
    """档案行 → dict。**`value` 是 JSON 就解开**，调用方不必各自 loads。"""
    v = r[4]
    if isinstance(v, str) and v[:1] in "[{":
        try:
            v = json.loads(v)
        except Exception:                                    # noqa: BLE001
            pass
    ev = r[6]
    if isinstance(ev, str) and ev[:1] in "[{":
        try:
            ev = json.loads(ev)
        except Exception:                                    # noqa: BLE001
            pass
    return {"id": r[0], "asset": r[1], "kind": r[2], "key": r[3], "value": v,
            "status": r[5], "evidence": ev, "actor": r[7],
            "observed_at": float(r[8]), "superseded_by": r[9]}


# 凭证类字段：**不参与动作身份**。
#
# 「接入 acme 这个源」是一个动作；换了个口令重连仍然是同一个动作。
# 把 dsn 算进指纹的后果实测踩过：恢复时模型调 `connect_source(source_id=acme)`
# 不带 dsn（它没必要记住密码，工具会从审批记录里取回），指纹对不上原票据，
# 于是**又发一份新审批、又开一条新线** —— 人批过的那次白批了。
#
# 顺带也更干净：`approvals.action_hash` 里不再藏着口令的哈希。
_CREDENTIAL_KEYS = {"dsn", "password", "passwd", "secret", "token",
                    "api_key", "apikey", "credential", "conn_str"}


def action_hash(tool_name: str, args: dict) -> str:
    """动作指纹（readme 9.4 机制二）。参数规范化后参与哈希。

    **凭证字段被剔除** —— 见 `_CREDENTIAL_KEYS` 上面那段。
    """
    keys = None
    try:
        from .policy import IDENTITY_KEYS
        keys = IDENTITY_KEYS.get(tool_name)
    except Exception:                                        # noqa: BLE001
        keys = None
    picked = {k: v for k, v in (args or {}).items() if k in (keys or ())}
    if keys and picked:
        # 声明了身份字段、而且这次调用确实给了：只认这些。
        # **缺的字段按缺处理**，不补默认值 —— 补了就等于替调用方
        # 决定「它其实是想传 X」。
        clean = picked
    elif keys:
        # **一个身份字段都没给到 → 退回全字段（最严）。**
        # 不退的话 clean 会是空字典，于是「同一个工具的任何调用」都算
        # 同一个动作 —— 三条参数完全不同的线被幂等判重吃掉两条，
        # 实测撞过。指纹宁可吵，不可松：松一次就是两个不同的动作
        # 共用一张票。
        clean = {k: v for k, v in (args or {}).items()
                 if k.lower() not in _CREDENTIAL_KEYS}
    else:
        clean = {k: v for k, v in (args or {}).items()
                 if k.lower() not in _CREDENTIAL_KEYS}
    canon = json.dumps(clean, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}\x00{canon}".encode()).hexdigest()


class PgStore:
    """Postgres 后端。生产用这个 —— 列级 GRANT 只有它做得到。

    与 SQLite 版共享同一套方法签名，调用方无需区分。
    """

    def __init__(self, dsn, readonly=False):
        import psycopg
        self.readonly = readonly
        self.db = psycopg.connect(dsn, autocommit=True)

    action_hash = staticmethod(action_hash)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def find_valid(self, action_hash_, run_id=None):
        """有效票据 = 已批准 + 未消费 + 未过期。

        **按动作指纹匹配，run_id 只记录不参与。**

        原先把 run_id 也放进 WHERE，后果是审批只在同一个进程/会话里有效：
        Agent 一重启，人批过的东西就作废，得重新打扰一遍 —— 这与 readme 4.1
        「状态不能只活在进程里」直接冲突。接上 Hermes 之后立刻暴露：
        每次 `hermes -z` 是新会话，同一个动作的 action_hash 一模一样，
        run_id 却是新 UUID，于是批准永远消费不掉。

        去掉 run_id 不放松安全：批的是「接入 shippers」这件事，
        动作指纹逐字节相同就意味着效果相同（机制二）；一次性（`used_at`）
        与 72 小时过期（`expires_at`）两道限制都还在。
        """
        with self.db.cursor() as c:
            c.execute(
                "SELECT a.id FROM approvals a JOIN decisions d ON d.approval_id = a.id "
                "WHERE a.action_hash=%s AND d.decision='approve' "
                "AND a.used_at IS NULL AND a.expires_at > now() LIMIT 1",
                (action_hash_,))
            return c.fetchone()

    def is_denied(self, action_hash_):
        with self.db.cursor() as c:
            c.execute("SELECT 1 FROM approvals a JOIN decisions d ON d.approval_id = a.id "
                      "WHERE a.action_hash=%s AND d.decision='deny' LIMIT 1", (action_hash_,))
            return c.fetchone() is not None

    def granted_sources(self):
        """人告诉过我们哪些源存在。**空集 = 清单未启用**（见 DDL）。"""
        with self.db.cursor() as c:
            c.execute("SELECT source_id FROM source_grants")
            return {r[0] for r in c.fetchall()}

    def grant_source(self, source_id, revealed_by, note=""):
        """写在**人**那一侧 —— Agent 的 GRANT 里没有这张表的 INSERT。"""
        with self.db.cursor() as c:
            c.execute("INSERT INTO source_grants (source_id, revealed_by,"
                      " revealed_at, note) VALUES (%s,%s,%s,%s)"
                      " ON CONFLICT (source_id) DO NOTHING",
                      (source_id, revealed_by, time.time(), note))

    def pending(self, action_hash_, run_id=None):
        """已在等的同一动作。**同样不看 run_id。**

        看 run_id 的后果是每个新会话都为同一件事再发一份审批 ——
        实测三次 `hermes -z` 之后 approvals 里躺着三行同指纹的待决请求。
        人会被同一件事重复打扰，而 readme 10.7 的 WIP 限制存在的理由
        正是「不允许把任何人淹没」。
        """
        with self.db.cursor() as c:
            c.execute(
                "SELECT a.id FROM approvals a LEFT JOIN decisions d ON d.approval_id = a.id "
                "WHERE a.action_hash=%s AND d.id IS NULL "
                "AND a.abandoned_at IS NULL AND a.expires_at > now() LIMIT 1",
                (action_hash_,))
            r = c.fetchone()
            return str(r[0]) if r else None

    def request(self, run_id, action_hash_, tool_name, args_json, approver):
        existing = self.pending(action_hash_, run_id)
        if existing:
            return existing, False
        aid = str(uuid.uuid4())
        with self.db.cursor() as c:
            c.execute(
                "INSERT INTO approvals (id, run_id, action_hash, tool_name, args_json,"
                " approver, created_at, expires_at) VALUES"
                " (%s,%s,%s,%s,%s,%s, now(), now() + interval '72 hours')",
                (aid, run_id, action_hash_, tool_name, args_json, approver))
        return aid, True

    def consume(self, approval_id):
        with self.db.cursor() as c:
            c.execute("UPDATE approvals SET used_at=now() WHERE id=%s AND used_at IS NULL",
                      (approval_id,))
            return c.rowcount == 1

    def decide(self, approval_id, decision, approver, token_jti=None,
               message_id=None, client_ip=None, user_agent=None, chosen=None):
        """见 SQLite 版的 docstring —— `chosen` 那一列此前从没人写过。"""
        if self.readonly:
            raise PermissionError("Agent 侧连接不允许写入决定")
        import psycopg
        try:
            with self.db.cursor() as c:
                c.execute(
                    "INSERT INTO decisions (id, approval_id, decision, chosen,"
                    " approver, decided_at, token_jti, message_id, client_ip,"
                    " user_agent) VALUES (%s,%s,%s,%s,%s, now(), %s,%s,%s,%s)",
                    (str(uuid.uuid4()), approval_id, decision, chosen, approver,
                     token_jti or str(uuid.uuid4()), message_id, client_ip, user_agent))
            return True
        except psycopg.errors.UniqueViolation:
            return False                      # 令牌重放
        except psycopg.errors.InsufficientPrivilege:
            raise PermissionError("该连接无权写入 decisions —— 列级 GRANT 生效")

    def stage_choice(self, chosen):
        with self.db.cursor() as c:
            c.execute("SELECT MIN(d.decided_at) FROM decisions d JOIN approvals a"
                      " ON a.id = d.approval_id WHERE a.kind='question'"
                      " AND d.decision='answered' AND d.chosen=%s", (chosen,))
            r = c.fetchone()
        if not r or r[0] is None:
            return None
        return r[0].timestamp() if hasattr(r[0], "timestamp") else float(r[0])

    def stage_proposals(self):
        with self.db.cursor() as c:
            c.execute("SELECT count(*) FROM approvals WHERE kind='question'"
                      " AND args_json LIKE '%%__stage__%%'")
            return c.fetchone()[0]

    def put_source_secret(self, source_id, dsn, approval_id, by, kind="postgres"):
        """见 SQLite 版。PG 侧 agent_role 对这张表**连 SELECT 都没有**。"""
        if self.readonly:
            raise PermissionError("Agent 侧连接不允许写入源凭证")
        with self.db.cursor() as c:
            c.execute(
                "INSERT INTO source_secrets (source_id, dsn, kind, approval_id,"
                " registered_by, registered_at) VALUES (%s,%s,%s,%s,%s, now())"
                " ON CONFLICT (source_id) DO UPDATE SET dsn=excluded.dsn,"
                " approval_id=excluded.approval_id,"
                " registered_at=excluded.registered_at",
                (source_id, dsn, kind, approval_id, by))

    def record_provenance(self, asset, event, actor="", approval_id="",
                          detail=None):
        with self.db.cursor() as c:
            c.execute(
                "INSERT INTO asset_provenance (asset, event, actor,"
                " approval_id, detail, ts) VALUES (%s,%s,%s,%s,%s,"
                " extract(epoch from now()))",
                (asset, event, actor or "", approval_id or "",
                 json.dumps(detail or {}, ensure_ascii=False)))

    def provenance(self, asset=None, limit=200):
        sql = ("SELECT asset, event, actor, approval_id, detail, ts"
               " FROM asset_provenance")
        args = []
        if asset:
            sql += " WHERE asset = %s OR asset LIKE %s"
            args += [asset, asset + ".%"]
        sql += " ORDER BY ts, id LIMIT %s"
        args.append(limit)
        with self.db.cursor() as c:
            c.execute(sql, args)
            rows = c.fetchall()
        out = []
        for a, e, who, aid, d, ts in rows:
            try:
                d = json.loads(d or "{}")
            except Exception:                                # noqa: BLE001
                d = {}
            out.append({"asset": a, "event": e, "actor": who,
                        "approval_id": aid, "detail": d, "ts": float(ts)})
        return out

    # ---------- 资产档案（R6 闭环 A）----------
    def catalog_put(self, asset, kind, key, value, status, actor,
                    evidence=None, fingerprint=None):
        """写一行档案，返回行 id；**内容没变就不写**，返回 None。

        幂等靠 fingerprint：巡检每天跑一遍，源库没变就不该多出 365 行。
        """
        if status not in _SUPERSEDES:
            raise ValueError(f"未知的档案状态：{status}（只认 {CATALOG_STATUS}）")
        val, ev, fp = _catalog_norm(value, evidence, fingerprint)
        marks = _SUPERSEDES[status]
        with self.db.cursor() as c:
            c.execute("SELECT id FROM asset_catalog WHERE asset=%s AND kind=%s"
                      " AND key=%s AND status=%s AND fingerprint=%s"
                      " AND superseded_by IS NULL",
                      (asset, kind, key, status, fp))
            if c.fetchone():
                return None
            c.execute(
                "INSERT INTO asset_catalog (asset,kind,key,value,status,evidence,"
                " actor,observed_at,fingerprint) VALUES"
                " (%s,%s,%s,%s,%s,%s,%s,extract(epoch from now()),%s) RETURNING id",
                (asset, kind, key, val, status, ev, actor, fp))
            new_id = c.fetchone()[0]
            c.execute(
                "UPDATE asset_catalog SET superseded_by=%s WHERE asset=%s"
                " AND kind=%s AND key=%s AND status = ANY(%s)"
                " AND superseded_by IS NULL AND id <> %s",
                (new_id, asset, kind, key, list(marks), new_id))
        return new_id

    def catalog(self, asset=None, kind=None, status=None,
                current_only=True, limit=500):
        """读档案。**默认只给当前有效的行**；要看历史传 current_only=False。"""
        sql = ("SELECT id, asset, kind, key, value, status, evidence, actor,"
               " observed_at, superseded_by FROM asset_catalog WHERE 1=1")
        args = []
        if asset:
            sql += " AND (asset = %s OR asset LIKE %s)"
            args += [asset, asset + ".%"]
        if kind:
            sql += " AND kind = %s"
            args.append(kind)
        if status:
            sql += " AND status = %s"
            args.append(status)
        if current_only:
            sql += " AND superseded_by IS NULL"
        sql += " ORDER BY observed_at, id LIMIT %s"
        args.append(limit)
        with self.db.cursor() as c:
            c.execute(sql, args)
            rows = c.fetchall()
        return [_catalog_row(r) for r in rows]

    def round_closed_at(self):
        with self.db.cursor() as c:
            c.execute("SELECT MAX(ts) FROM events WHERE kind='ROUND_CLOSED'")
            r = c.fetchone()
        return float(r[0]) if r and r[0] is not None else None

    def last_stage_decision_at(self):
        with self.db.cursor() as c:
            c.execute("SELECT MAX(d.decided_at) FROM decisions d JOIN approvals a"
                      " ON a.id = d.approval_id WHERE a.kind='question'"
                      " AND d.decision='answered'")
            r = c.fetchone()
        if not r or r[0] is None:
            return None
        return r[0].timestamp() if hasattr(r[0], "timestamp") else float(r[0])

    # ---------- 与 SQLite Store 对齐的方法（方言不同，故各自实现）----------
    def resolve_role(self, role):
        with self.db.cursor() as c:
            c.execute("SELECT person FROM role_assignment WHERE role=%s "
                      "AND valid_from <= now() AND (valid_to IS NULL OR valid_to > now()) "
                      "ORDER BY valid_from DESC LIMIT 1", (role,))
            r = c.fetchone()
            return r[0] if r else None

    def current_holders(self) -> set:
        with self.db.cursor() as c:
            c.execute("SELECT DISTINCT lower(person) FROM role_assignment "
                      "WHERE valid_from <= now() AND (valid_to IS NULL OR valid_to > now())")
            return {r[0] for r in c.fetchall()}

    def assign_role(self, role, person, granted_by, reason=""):
        with self.db.cursor() as c:
            c.execute("UPDATE role_assignment SET valid_to=now() "
                      "WHERE role=%s AND valid_to IS NULL", (role,))
            c.execute("INSERT INTO role_assignment (role, person, granted_by, reason)"
                      " VALUES (%s,%s,%s,%s)", (role, person, granted_by, reason))

    def open_count(self, approver=None):
        sql = ("SELECT count(*) FROM approvals a LEFT JOIN decisions d "
               "ON d.approval_id=a.id WHERE d.id IS NULL AND a.expires_at > now() "
               "AND a.abandoned_at IS NULL")
        args = []
        if approver:
            sql += " AND a.approver = %s"
            args.append(approver)
        with self.db.cursor() as c:
            c.execute(sql, args)
            return c.fetchone()[0]

    def stale_items(self):
        with self.db.cursor() as c:
            c.execute("SELECT a.id::text, a.approver, a.tool_name, a.kind,"
                      " a.escalation_level,"
                      " extract(epoch from (now()-a.created_at))/3600.0"
                      " FROM approvals a LEFT JOIN decisions d ON d.approval_id=a.id"
                      " WHERE d.id IS NULL AND a.abandoned_at IS NULL"
                      " ORDER BY a.created_at")
            return c.fetchall()

    def bump_escalation(self, item_id, level):
        with self.db.cursor() as c:
            c.execute("UPDATE approvals SET escalation_level=%s WHERE id=%s",
                      (level, item_id))

    def abandon(self, item_id):
        with self.db.cursor() as c:
            c.execute("UPDATE approvals SET abandoned_at=now() WHERE id=%s", (item_id,))

    def abandoned(self):
        with self.db.cursor() as c:
            c.execute("SELECT id::text, approver, tool_name FROM approvals "
                      "WHERE abandoned_at IS NOT NULL ORDER BY abandoned_at DESC")
            return c.fetchall()

    def ask(self, run_id, asset, question, options, approver, evidence=""):
        h = action_hash("__question__", {"asset": asset, "q": question})
        existing = self.pending(h, run_id)
        if existing:
            return existing, False
        qid = str(uuid.uuid4())
        with self.db.cursor() as c:
            c.execute(
                "INSERT INTO approvals (id, run_id, action_hash, tool_name, args_json,"
                " approver, created_at, expires_at, kind, options, question, evidence)"
                " VALUES (%s,%s,%s,%s,%s,%s, now(), now()+interval '72 hours',"
                " 'question',%s,%s,%s)",
                (qid, run_id, h, "__question__",
                 json.dumps({"asset": asset}, ensure_ascii=False), approver,
                 json.dumps(options, ensure_ascii=False), question, evidence))
        return qid, True

    def known(self, asset, key):
        with self.db.cursor() as c:
            c.execute("SELECT value, confirmed_by FROM asset_semantics "
                      "WHERE asset=%s AND key=%s", (asset, key))
            r = c.fetchone()
            return {"value": r[0], "confirmed_by": r[1]} if r else None

    def remember(self, asset, key, value, confirmed_by, source_item=None):
        with self.db.cursor() as c:
            # **source_item 也要更新。** 原先 DO UPDATE 只覆盖 value 和
            # confirmed_by，于是口径改了、出处还是上一次那份审批 ——
            # 新口径配旧出处，追溯时指向一份**根本没提过这条口径**的记录。
            # `confirmed_at` 是 DOUBLE PRECISION NOT NULL 且**无默认值**，
            # 原先 INSERT 压根没给它（新行必然 NOT NULL 违例），UPDATE 又
            # 把 timestamptz 的 now() 往 double 里塞 —— 两条都跑不通，
            # 也就是说 PG 后端的口径沉淀一直是坏的。演练跑在 SQLite 上，
            # 所以没暴露。统一用 epoch，与本文件其它 PG 方法一致。
            c.execute("INSERT INTO asset_semantics (asset,key,value,confirmed_by,"
                      " confirmed_at,source_item)"
                      " VALUES (%s,%s,%s,%s,extract(epoch from now()),%s)"
                      " ON CONFLICT (asset,key) DO UPDATE SET"
                      " value=excluded.value, confirmed_by=excluded.confirmed_by,"
                      " confirmed_at=excluded.confirmed_at,"
                      " source_item=excluded.source_item",
                      (asset, key, value, confirmed_by, source_item))

    def approver_stats(self, role):
        with self.db.cursor() as c:
            c.execute("SELECT count(*),"
                      " count(*) FILTER (WHERE d.decision='approve'),"
                      " count(*) FILTER (WHERE d.decision='deny'),"
                      " avg(extract(epoch from (d.decided_at - a.created_at)))"
                      " FROM approvals a JOIN decisions d ON d.approval_id=a.id"
                      " WHERE a.approver=%s", (role,))
            n, ok_, no_, avg_s = c.fetchone()
            c.execute("SELECT count(*) FROM approvals a LEFT JOIN decisions d"
                      " ON d.approval_id=a.id WHERE a.approver=%s AND d.id IS NULL"
                      " AND a.abandoned_at IS NULL", (role,))
            pend = c.fetchone()[0]
            c.execute("SELECT count(*) FROM approvals WHERE approver=%s"
                      " AND abandoned_at IS NOT NULL", (role,))
            aband = c.fetchone()[0]
        n = n or 0
        return {"role": role, "decided": n, "approved": ok_ or 0, "denied": no_ or 0,
                "pending": pend, "abandoned": aband,
                "avg_response_hours": round(float(avg_s) / 3600, 1) if avg_s else None,
                "approve_rate": round((ok_ or 0) / n, 2) if n else None}

    def completed_since(self, ts):
        with self.db.cursor() as c:
            c.execute("SELECT a.tool_name, count(*) FROM approvals a JOIN decisions d "
                      "ON d.approval_id=a.id WHERE d.decision='approve' "
                      "AND a.created_at >= to_timestamp(%s) "
                      "GROUP BY a.tool_name ORDER BY 2 DESC", (ts,))
            return c.fetchall()

    def record_usage(self, model, prompt_tokens=0, output_tokens=0,
                     cost_usd=0.0, run_id="", purpose=""):
        with self.db.cursor() as c:
            c.execute(
                "INSERT INTO usage_ledger (ts, run_id, model, prompt_tokens,"
                " output_tokens, cost_usd, purpose)"
                " VALUES (extract(epoch from now()),%s,%s,%s,%s,%s,%s)",
                (run_id, model, int(prompt_tokens), int(output_tokens),
                 float(cost_usd), purpose))

    def today_usage(self, since):
        with self.db.cursor() as c:
            c.execute("SELECT count(*), coalesce(sum(prompt_tokens+output_tokens),0),"
                      " coalesce(sum(cost_usd),0) FROM usage_ledger WHERE ts>=%s",
                      (since,))
            n, tok, cost = c.fetchone()
        return {"calls": int(n), "tokens": int(tok), "cost_usd": float(cost)}

    def ledger_summary(self, ts):
        with self.db.cursor() as c:
            c.execute("SELECT count(*), coalesce(sum(rows_out),0), "
                      "count(*) FILTER (WHERE status<>'OK') "
                      "FROM query_ledger WHERE ts >= to_timestamp(%s)", (ts,))
            q = c.fetchone()
            c.execute("SELECT count(*), coalesce(sum(prompt_tokens+output_tokens),0), "
                      "coalesce(sum(cost_usd),0) FROM usage_ledger "
                      "WHERE ts >= to_timestamp(%s)", (ts,))
            u = c.fetchone()
            return q, u

    def append_event(self, run_id, kind, payload=""):
        with self.db.cursor() as c:
            c.execute("INSERT INTO events (run_id, ts, kind, payload) "
                      "VALUES (%s, extract(epoch from now()), %s, %s)",
                      (run_id, kind, payload))

    def events(self, run_id):
        with self.db.cursor() as c:
            c.execute("SELECT seq, kind, payload FROM events WHERE run_id=%s ORDER BY seq",
                      (run_id,))
            return c.fetchall()


def open_store(readonly=False, init_schema=True):
    """按配置选择后端。

    DATASTEWARD_DSN 存在   -> Postgres（生产。列级 GRANT 在此生效）
    否则                    -> SQLite（无 docker 时的快速本地测试）
    """
    dsn = os.environ.get("DATASTEWARD_DSN", "")
    if dsn:
        return PgStore(dsn, readonly=readonly)
    path = os.environ.get("DATASTEWARD_DB", os.path.expanduser("~/.datalaker/approvals.db"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return Store(path, readonly=readonly, init_schema=init_schema)


class Store:
    def __init__(self, path, readonly=False, init_schema=True):
        self.readonly = readonly
        # Agent 与审批 callback 是两个进程，会并发访问同一个库。
        # WAL 允许「一写多读」，busy_timeout 让写冲突等待而非立即报错。
        # 生产用 Postgres —— SQLite 的单写者限制在此只是开发期权宜。
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        if init_schema:
            self.db.executescript(DDL)
            self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    action_hash = staticmethod(action_hash)

    # ---------- Agent 侧：读 ----------
    def find_valid(self, action_hash_, run_id=None):
        """有效票据 = 已批准 + 未消费 + 未过期。

        **按动作指纹匹配，run_id 只记录不参与。**

        原先把 run_id 也放进 WHERE，后果是审批只在同一个进程/会话里有效：
        Agent 一重启，人批过的东西就作废，得重新打扰一遍 —— 这与 readme 4.1
        「状态不能只活在进程里」直接冲突。接上 Hermes 之后立刻暴露：
        每次 `hermes -z` 是新会话，同一个动作的 action_hash 一模一样，
        run_id 却是新 UUID，于是批准永远消费不掉。

        去掉 run_id 不放松安全：批的是「接入 shippers」这件事，
        动作指纹逐字节相同就意味着效果相同（机制二）；一次性（`used_at`）
        与 72 小时过期（`expires_at`）两道限制都还在。
        """
        return self.db.execute(
            "SELECT a.id FROM approvals a JOIN decisions d ON d.approval_id = a.id "
            "WHERE a.action_hash=? AND d.decision='approve' "
            "AND a.used_at IS NULL AND a.expires_at > ? LIMIT 1",
            (action_hash_, time.time()),
        ).fetchone()

    def is_denied(self, action_hash_):
        return self.db.execute(
            "SELECT 1 FROM approvals a JOIN decisions d ON d.approval_id = a.id "
            "WHERE a.action_hash=? AND d.decision='deny' LIMIT 1",
            (action_hash_,),
        ).fetchone() is not None

    def granted_sources(self):
        """人告诉过我们哪些源存在。**空集 = 清单未启用**（见 DDL）。"""
        return {r[0] for r in
                self.db.execute("SELECT source_id FROM source_grants")}

    def grant_source(self, source_id, revealed_by, note=""):
        """写在**人**那一侧 —— Agent 的 GRANT 里没有这张表的 INSERT。"""
        self.db.execute(
            "INSERT OR IGNORE INTO source_grants (source_id, revealed_by,"
            " revealed_at, note) VALUES (?,?,?,?)",
            (source_id, revealed_by, time.time(), note))
        self.db.commit()

    def pending(self, action_hash_, run_id=None):
        """已在等的同一动作。**同样不看 run_id。**

        看 run_id 的后果是每个新会话都为同一件事再发一份审批 ——
        实测三次 `hermes -z` 之后 approvals 里躺着三行同指纹的待决请求。
        人会被同一件事重复打扰，而 readme 10.7 的 WIP 限制存在的理由
        正是「不允许把任何人淹没」。
        """
        row = self.db.execute(
            "SELECT a.id FROM approvals a LEFT JOIN decisions d ON d.approval_id = a.id "
            "WHERE a.action_hash=? AND d.id IS NULL AND a.abandoned_at IS NULL "
            "AND a.expires_at > ? LIMIT 1",
            (action_hash_, time.time()),
        ).fetchone()
        return row[0] if row else None

    # ---------- Agent 侧：允许的写 ----------
    def request(self, run_id, action_hash_, tool_name, args_json, approver):
        """创建待决请求。不含决定字段——Agent 写不了决定。

        返回 (approval_id, created)。created=False 表示已有待决请求，
        调用方据此避免重复发信（readme 5.5：一封邮件只问一个决策）。
        """
        existing = self.pending(action_hash_, run_id)
        if existing:
            return existing, False               # 幂等：不重复发信
        aid = str(uuid.uuid4())
        now = time.time()
        self.db.execute(
            "INSERT INTO approvals (id, run_id, action_hash, tool_name, args_json,"
            " approver, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, run_id, action_hash_, tool_name, args_json, approver, now, now + TTL),
        )
        self.db.commit()
        return aid, True

    def consume(self, approval_id):
        """标记票据已使用。只会收紧不会放松，故允许 Agent 写。"""
        cur = self.db.execute(
            "UPDATE approvals SET used_at=? WHERE id=? AND used_at IS NULL",
            (time.time(), approval_id),
        )
        self.db.commit()
        return cur.rowcount == 1

    # ---------- 审批 callback 服务侧：Agent 无此权限 ----------
    def decide(self, approval_id, decision, approver, token_jti=None,
               message_id=None, client_ip=None, user_agent=None, chosen=None):
        """写决定。`chosen` 是 question 型选中的那个选项 key。

        `decisions.chosen` 这一列 R2 就建好了，但**从来没有人往里写** ——
        于是「人选了哪个」这件事只存在于邮件正文里，判分读不到。
        这正是本项目反复摔的那个形状（闸门读的量根本没人写），
        所以补上写侧，而不是在判分那边猜。
        """
        if self.readonly:
            raise PermissionError("Agent 侧连接不允许写入决定")
        assert decision in ("approve", "deny", "answered")
        try:
            self.db.execute(
                "INSERT INTO decisions (id, approval_id, decision, chosen, approver,"
                " decided_at, token_jti, message_id, client_ip, user_agent)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), approval_id, decision, chosen, approver,
                 time.time(), token_jti or str(uuid.uuid4()), message_id,
                 client_ip, user_agent),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False                          # token 重放：jti 唯一约束挡住

    def stage_choice(self, chosen):
        """人有没有拍过这个阶段决定；拍了返回时刻。**没拍返回 None。**

        判分的 `silver_gated` 要的就是这个时刻：批准必须早于第一个
        silver 动作。「有没有」和「什么时候」在这里是同一次查询 ——
        分开查会给出「批了但不知道什么时候」这种半个答案。
        """
        row = self.db.execute(
            "SELECT MIN(d.decided_at) FROM decisions d JOIN approvals a"
            " ON a.id = d.approval_id"
            " WHERE a.kind='question' AND d.decision='answered' AND d.chosen=?",
            (chosen,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def stage_proposals(self):
        """发过几次阶段提案。**0 = 这个部署还没在用轮制**（见门禁注释）。"""
        return self.db.execute(
            "SELECT count(*) FROM approvals WHERE kind='question'"
            " AND args_json LIKE '%__stage__%'").fetchone()[0]

    def put_source_secret(self, source_id, dsn, approval_id, by, kind="postgres"):
        """记下一个源的连接串。**只有 Connector 那一侧会去读它。**

        `approval_id` 是这条注册的依据 —— 没有批准就没有数据源。
        写在人点过链接之后（工具 handler 里），门禁负责保证走到这一步
        时票据是真的。
        """
        if self.readonly:
            raise PermissionError("Agent 侧连接不允许写入源凭证")
        self.db.execute(
            "INSERT INTO source_secrets (source_id, dsn, kind, approval_id,"
            " registered_by, registered_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET dsn=excluded.dsn,"
            " approval_id=excluded.approval_id, registered_at=excluded.registered_at",
            (source_id, dsn, kind, approval_id, by, time.time()))
        self.db.commit()

    def record_provenance(self, asset, event, actor="", approval_id="",
                          detail=None):
        """往资产台账写一行。**append-only，不改不删。**"""
        self.db.execute(
            "INSERT INTO asset_provenance (asset, event, actor, approval_id,"
            " detail, ts) VALUES (?,?,?,?,?,?)",
            (asset, event, actor or "", approval_id or "",
             json.dumps(detail or {}, ensure_ascii=False), time.time()))
        self.db.commit()

    def provenance(self, asset=None, limit=200):
        """读台账。不给 asset 就读全部（按时间序）。"""
        sql = ("SELECT asset, event, actor, approval_id, detail, ts"
               " FROM asset_provenance")
        args = []
        if asset:
            sql += " WHERE asset = ? OR asset LIKE ?"
            args += [asset, asset + ".%"]
        sql += " ORDER BY ts, id LIMIT ?"
        args.append(limit)
        out = []
        for a, e, who, aid, d, ts in self.db.execute(sql, args):
            try:
                d = json.loads(d or "{}")
            except Exception:                                # noqa: BLE001
                d = {}
            out.append({"asset": a, "event": e, "actor": who,
                        "approval_id": aid, "detail": d, "ts": ts})
        return out

    # ---------- 资产档案（R6 闭环 A）----------
    def catalog_put(self, asset, kind, key, value, status, actor,
                    evidence=None, fingerprint=None):
        """写一行档案，返回行 id；**内容没变就不写**，返回 None。"""
        if status not in _SUPERSEDES:
            raise ValueError(f"未知的档案状态：{status}（只认 {CATALOG_STATUS}）")
        val, ev, fp = _catalog_norm(value, evidence, fingerprint)
        marks = _SUPERSEDES[status]
        dup = self.db.execute(
            "SELECT id FROM asset_catalog WHERE asset=? AND kind=? AND key=?"
            " AND status=? AND fingerprint=? AND superseded_by IS NULL",
            (asset, kind, key, status, fp)).fetchone()
        if dup:
            return None
        cur = self.db.execute(
            "INSERT INTO asset_catalog (asset,kind,key,value,status,evidence,"
            " actor,observed_at,fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
            (asset, kind, key, val, status, ev, actor, time.time(), fp))
        new_id = cur.lastrowid
        self.db.execute(
            "UPDATE asset_catalog SET superseded_by=? WHERE asset=? AND kind=?"
            f" AND key=? AND status IN ({','.join('?' * len(marks))})"
            " AND superseded_by IS NULL AND id <> ?",
            (new_id, asset, kind, key, *marks, new_id))
        self.db.commit()
        return new_id

    def catalog(self, asset=None, kind=None, status=None,
                current_only=True, limit=500):
        """读档案。**默认只给当前有效的行**；要看历史传 current_only=False。"""
        sql = ("SELECT id, asset, kind, key, value, status, evidence, actor,"
               " observed_at, superseded_by FROM asset_catalog WHERE 1=1")
        args = []
        if asset:
            sql += " AND (asset = ? OR asset LIKE ?)"
            args += [asset, asset + ".%"]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if status:
            sql += " AND status = ?"
            args.append(status)
        if current_only:
            sql += " AND superseded_by IS NULL"
        sql += " ORDER BY observed_at, id LIMIT ?"
        args.append(limit)
        return [_catalog_row(r) for r in self.db.execute(sql, args)]

    def round_closed_at(self):
        """这一轮宣告结束的时刻。**没结束返回 None。**

        由阶段报告那一步写（`ops/stage-report.py --send`）。门禁读它来判
        「终态之后还发起新动作 = 违规」—— 闸门读的量必须真的有人写，
        所以判据落在一条事件上，不是让门禁自己再算一遍轮的状态。
        """
        row = self.db.execute(
            "SELECT MAX(ts) FROM events WHERE kind='ROUND_CLOSED'").fetchone()
        return row[0] if row and row[0] is not None else None

    def last_stage_decision_at(self):
        """最近一次阶段拍板的时刻（任何一个选项）。**没拍过返回 None。**"""
        row = self.db.execute(
            "SELECT MAX(d.decided_at) FROM decisions d JOIN approvals a"
            " ON a.id = d.approval_id"
            " WHERE a.kind='question' AND d.decision='answered'").fetchone()
        return row[0] if row and row[0] is not None else None

    # ---------- 角色解析（readme 10.4）----------
    def resolve_role(self, role):
        """角色 → 当前持有人。换人只需改 role_assignment，未决事项自动跟随。"""
        row = self.db.execute(
            "SELECT person FROM role_assignment WHERE role=? "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY valid_from DESC LIMIT 1",
            (role, time.time(), time.time()),
        ).fetchone()
        return row[0] if row else None

    def current_holders(self) -> set:
        """当前所有有效的角色持有人。

        角色名是动态的（`owner:FIN` / `steward:CRM`），**不能硬编码枚举**——
        入站白名单要认的是「此刻谁持有任何角色」。
        """
        rows = self.db.execute(
            "SELECT DISTINCT lower(person) FROM role_assignment "
            "WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
            (time.time(), time.time())).fetchall()
        return {r[0] for r in rows}

    def assign_role(self, role, person, granted_by, reason=""):
        """指派角色。旧持有人自动失效——决定是历史事实，不回填改写。"""
        now = time.time()
        self.db.execute(
            "UPDATE role_assignment SET valid_to=? WHERE role=? AND valid_to IS NULL",
            (now, role))
        self.db.execute(
            "INSERT INTO role_assignment (role, person, valid_from, granted_by, reason)"
            " VALUES (?,?,?,?,?)", (role, person, now, granted_by, reason))
        self.db.commit()

    # ---------- WIP 计数（readme 10.7）----------
    def open_count(self, approver=None):
        """在办 = 已发出、等回复。已批准未消费的不算——那是 Agent 自己的活。"""
        sql = ("SELECT count(*) FROM approvals a "
               "LEFT JOIN decisions d ON d.approval_id = a.id "
               "WHERE d.id IS NULL AND a.expires_at > ? AND a.abandoned_at IS NULL")
        args = [time.time()]
        if approver:
            sql += " AND a.approver = ?"
            args.append(approver)
        return self.db.execute(sql, args).fetchone()[0]

    # ---------- 提问（readme 5.7）----------
    def ask(self, run_id, asset, question, options, approver, evidence=""):
        """创建一条提问。与审批共用同一套令牌、超时、WIP 机制。"""
        h = action_hash("__question__", {"asset": asset, "q": question})
        existing = self.pending(h, run_id)
        if existing:
            return existing, False
        qid = str(uuid.uuid4())
        now = time.time()
        self.db.execute(
            "INSERT INTO approvals (id, run_id, action_hash, tool_name, args_json,"
            " approver, created_at, expires_at, kind, options, question, evidence)"
            " VALUES (?,?,?,?,?,?,?,?,'question',?,?,?)",
            (qid, run_id, h, "__question__",
             json.dumps({"asset": asset}, ensure_ascii=False), approver,
             now, now + TTL,
             json.dumps(options, ensure_ascii=False), question, evidence))
        self.db.commit()
        return qid, True

    def known(self, asset, key):
        """问过的不再问（readme 5.7）。"""
        row = self.db.execute(
            "SELECT value, confirmed_by FROM asset_semantics WHERE asset=? AND key=?",
            (asset, key)).fetchone()
        return {"value": row[0], "confirmed_by": row[1]} if row else None

    def remember(self, asset, key, value, confirmed_by, source_item=None):
        """答案沉淀。重复问同一件事是最快失去信任的方式。

        `source_item` 一并更新 —— 见 PgStore 同名方法里那段。
        """
        self.db.execute(
            "INSERT INTO asset_semantics (asset, key, value, confirmed_by,"
            " confirmed_at, source_item) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(asset, key) DO UPDATE SET value=excluded.value,"
            " confirmed_by=excluded.confirmed_by, confirmed_at=excluded.confirmed_at,"
            " source_item=excluded.source_item",
            (asset, key, value, confirmed_by, time.time(), source_item))
        self.db.commit()

    # ---------- 超时升级（readme 5.4）----------
    def stale_items(self):
        """未决且未放弃的事项，附年龄（小时）与当前升级层级。"""
        return self.db.execute(
            "SELECT a.id, a.approver, a.tool_name, a.kind, a.escalation_level,"
            " (? - a.created_at)/3600.0 FROM approvals a "
            "LEFT JOIN decisions d ON d.approval_id = a.id "
            "WHERE d.id IS NULL AND a.abandoned_at IS NULL ORDER BY a.created_at",
            (time.time(),)).fetchall()

    def bump_escalation(self, item_id, level):
        self.db.execute("UPDATE approvals SET escalation_level=? WHERE id=?",
                        (level, item_id))
        self.db.commit()

    def abandon(self, item_id):
        """标记放弃。**不是删除**——退出活跃队列，event log 完整保留，
        人想起来后可随时手动 resume（readme 5.4）。"""
        self.db.execute("UPDATE approvals SET abandoned_at=? WHERE id=?",
                        (time.time(), item_id))
        self.db.commit()

    def abandoned(self):
        return self.db.execute(
            "SELECT id, approver, tool_name FROM approvals "
            "WHERE abandoned_at IS NOT NULL ORDER BY abandoned_at DESC").fetchall()

    def approver_stats(self, role):
        """审批偏好统计（readme 4.6 支柱三）。

        **数据全部来自已有表，只需统计不需新采集**——
        这让「按人调整交互」的成本接近于零。
        """
        row = self.db.execute(
            "SELECT count(*),"
            " sum(CASE WHEN d.decision='approve' THEN 1 ELSE 0 END),"
            " sum(CASE WHEN d.decision='deny' THEN 1 ELSE 0 END),"
            " avg(d.decided_at - a.created_at)"
            " FROM approvals a JOIN decisions d ON d.approval_id=a.id"
            " WHERE a.approver=?", (role,)).fetchone()
        n, ok_, no_, avg_s = row[0] or 0, row[1] or 0, row[2] or 0, row[3]
        pend = self.db.execute(
            "SELECT count(*) FROM approvals a LEFT JOIN decisions d"
            " ON d.approval_id=a.id WHERE a.approver=? AND d.id IS NULL"
            " AND a.abandoned_at IS NULL", (role,)).fetchone()[0]
        aband = self.db.execute(
            "SELECT count(*) FROM approvals WHERE approver=? AND abandoned_at IS NOT NULL",
            (role,)).fetchone()[0]
        return {"role": role, "decided": n, "approved": ok_, "denied": no_,
                "pending": pend, "abandoned": aband,
                "avg_response_hours": round(avg_s / 3600, 1) if avg_s else None,
                "approve_rate": round(ok_ / n, 2) if n else None}

    def completed_since(self, ts):
        return self.db.execute(
            "SELECT a.tool_name, count(*) FROM approvals a JOIN decisions d "
            "ON d.approval_id=a.id WHERE d.decision='approve' AND a.created_at>=? "
            "GROUP BY a.tool_name ORDER BY 2 DESC", (ts,)).fetchall()

    def record_usage(self, model, prompt_tokens=0, output_tokens=0,
                     cost_usd=0.0, run_id="", purpose=""):
        self.db.execute(
            "INSERT INTO usage_ledger (ts, run_id, model, prompt_tokens,"
            " output_tokens, cost_usd, purpose) VALUES (?,?,?,?,?,?,?)",
            (time.time(), run_id, model, int(prompt_tokens), int(output_tokens),
             float(cost_usd), purpose))
        self.db.commit()

    def today_usage(self, since):
        n, tok, cost = self.db.execute(
            "SELECT count(*), coalesce(sum(prompt_tokens+output_tokens),0),"
            " coalesce(sum(cost_usd),0) FROM usage_ledger WHERE ts>=?",
            (since,)).fetchone()
        return {"calls": int(n), "tokens": int(tok), "cost_usd": float(cost)}

    def ledger_summary(self, ts):
        q = self.db.execute(
            "SELECT count(*), coalesce(sum(rows_out),0), "
            "sum(CASE WHEN status<>'OK' THEN 1 ELSE 0 END) "
            "FROM query_ledger WHERE ts>=?", (ts,)).fetchone()
        u = self.db.execute(
            "SELECT count(*), coalesce(sum(prompt_tokens+output_tokens),0), "
            "coalesce(sum(cost_usd),0) FROM usage_ledger WHERE ts>=?", (ts,)).fetchone()
        return q, u

    # ---------- event log ----------
    def append_event(self, run_id, kind, payload=""):
        self.db.execute(
            "INSERT INTO events (run_id, ts, kind, payload) VALUES (?,?,?,?)",
            (run_id, time.time(), kind, payload),
        )
        self.db.commit()

    def events(self, run_id):
        return self.db.execute(
            "SELECT seq, kind, payload FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
