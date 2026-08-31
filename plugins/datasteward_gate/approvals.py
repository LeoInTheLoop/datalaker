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
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    action_hash  TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    args_json    TEXT NOT NULL,
    approver     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    used_at      REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id           TEXT PRIMARY KEY,
    approval_id  TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('approve','deny')),
    approver     TEXT NOT NULL,
    decided_at   REAL NOT NULL,
    token_jti    TEXT UNIQUE NOT NULL,
    message_id   TEXT,
    client_ip    TEXT,
    user_agent   TEXT
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
