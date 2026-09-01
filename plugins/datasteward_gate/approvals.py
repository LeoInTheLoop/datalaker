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
CREATE INDEX IF NOT EXISTS ix_appr_hash ON approvals(action_hash, run_id);
CREATE INDEX IF NOT EXISTS ix_dec_appr  ON decisions(approval_id);
"""

TTL = 72 * 3600


def action_hash(tool_name: str, args: dict) -> str:
    """动作指纹（readme 9.4 机制二）。参数规范化后参与哈希。"""
    canon = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
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

    def find_valid(self, action_hash_, run_id):
        with self.db.cursor() as c:
            c.execute(
                "SELECT a.id FROM approvals a JOIN decisions d ON d.approval_id = a.id "
                "WHERE a.action_hash=%s AND a.run_id=%s AND d.decision='approve' "
                "AND a.used_at IS NULL AND a.expires_at > now() LIMIT 1",
                (action_hash_, run_id))
            return c.fetchone()

    def is_denied(self, action_hash_):
        with self.db.cursor() as c:
            c.execute("SELECT 1 FROM approvals a JOIN decisions d ON d.approval_id = a.id "
                      "WHERE a.action_hash=%s AND d.decision='deny' LIMIT 1", (action_hash_,))
            return c.fetchone() is not None

    def pending(self, action_hash_, run_id):
        with self.db.cursor() as c:
            c.execute(
                "SELECT a.id FROM approvals a LEFT JOIN decisions d ON d.approval_id = a.id "
                "WHERE a.action_hash=%s AND a.run_id=%s AND d.id IS NULL "
                "AND a.expires_at > now() LIMIT 1", (action_hash_, run_id))
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
               message_id=None, client_ip=None, user_agent=None):
        if self.readonly:
            raise PermissionError("Agent 侧连接不允许写入决定")
        import psycopg
        try:
            with self.db.cursor() as c:
                c.execute(
                    "INSERT INTO decisions (id, approval_id, decision, approver,"
                    " decided_at, token_jti, message_id, client_ip, user_agent)"
                    " VALUES (%s,%s,%s,%s, now(), %s,%s,%s,%s)",
                    (str(uuid.uuid4()), approval_id, decision, approver,
                     token_jti or str(uuid.uuid4()), message_id, client_ip, user_agent))
            return True
        except psycopg.errors.UniqueViolation:
            return False                      # 令牌重放
        except psycopg.errors.InsufficientPrivilege:
            raise PermissionError("该连接无权写入 decisions —— 列级 GRANT 生效")

    # ---------- 与 SQLite Store 对齐的方法（方言不同，故各自实现）----------
    def resolve_role(self, role):
        with self.db.cursor() as c:
            c.execute("SELECT person FROM role_assignment WHERE role=%s "
                      "AND valid_from <= now() AND (valid_to IS NULL OR valid_to > now()) "
                      "ORDER BY valid_from DESC LIMIT 1", (role,))
            r = c.fetchone()
            return r[0] if r else None

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
            c.execute("INSERT INTO asset_semantics (asset,key,value,confirmed_by,source_item)"
                      " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (asset,key) DO UPDATE SET"
                      " value=excluded.value, confirmed_by=excluded.confirmed_by,"
                      " confirmed_at=now()",
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
    def find_valid(self, action_hash_, run_id):
        """有效票据 = 已批准 + 未消费 + 未过期。"""
        return self.db.execute(
            "SELECT a.id FROM approvals a JOIN decisions d ON d.approval_id = a.id "
            "WHERE a.action_hash=? AND a.run_id=? AND d.decision='approve' "
            "AND a.used_at IS NULL AND a.expires_at > ? LIMIT 1",
            (action_hash_, run_id, time.time()),
        ).fetchone()

    def is_denied(self, action_hash_):
        return self.db.execute(
            "SELECT 1 FROM approvals a JOIN decisions d ON d.approval_id = a.id "
            "WHERE a.action_hash=? AND d.decision='deny' LIMIT 1",
            (action_hash_,),
        ).fetchone() is not None

    def pending(self, action_hash_, run_id):
        row = self.db.execute(
            "SELECT a.id FROM approvals a LEFT JOIN decisions d ON d.approval_id = a.id "
            "WHERE a.action_hash=? AND a.run_id=? AND d.id IS NULL AND a.expires_at > ? LIMIT 1",
            (action_hash_, run_id, time.time()),
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
               message_id=None, client_ip=None, user_agent=None):
        if self.readonly:
            raise PermissionError("Agent 侧连接不允许写入决定")
        assert decision in ("approve", "deny")
        try:
            self.db.execute(
                "INSERT INTO decisions (id, approval_id, decision, approver, decided_at,"
                " token_jti, message_id, client_ip, user_agent) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), approval_id, decision, approver, time.time(),
                 token_jti or str(uuid.uuid4()), message_id, client_ip, user_agent),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False                          # token 重放：jti 唯一约束挡住

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
        """答案沉淀。重复问同一件事是最快失去信任的方式。"""
        self.db.execute(
            "INSERT INTO asset_semantics (asset, key, value, confirmed_by,"
            " confirmed_at, source_item) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(asset, key) DO UPDATE SET value=excluded.value,"
            " confirmed_by=excluded.confirmed_by, confirmed_at=excluded.confirmed_at",
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
