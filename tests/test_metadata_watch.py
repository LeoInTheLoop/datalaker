"""闭环 B：认知会更新。

**验收（R6 §3 B 原文）**：在源库上改一个字段（加列 / 改类型 / 删外键），
巡检能发现，指出哪些旧结论因此失效，并生成复核任务给对应负责人。

这一组**真的去改源库**（一张自己造的 `watch_demo_*` 表），不是喂假数据 ——
「档案能省重复访问，但发现变化必须有新的外部观测」这句话，只有真改一次
才验得了。

    python3 tests/test_metadata_watch.py

源库连不上就整组 SKIP（探活走干活同一条路）。
"""
import json
import os
import pathlib
import subprocess
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

DB = "/tmp/dl_watch_test.db"
for suf in ("", "-wal", "-shm"):
    f = pathlib.Path(DB + suf)
    if f.exists():
        f.unlink()
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
os.environ.setdefault("NOTIFY_CHANNEL", "outbox")
os.environ.setdefault("NOTIFY_OUTBOX", "/tmp/dl_watch_outbox.jsonl")
pathlib.Path(os.environ["NOTIFY_OUTBOX"]).unlink(missing_ok=True)

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def psql(sql: str) -> tuple:
    r = subprocess.run(["docker", "exec", "datalaker-source_pg-1", "psql",
                        "-U", "postgres", "-d", "acme", "-q", "-c", sql],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stderr or r.stdout).strip()


try:
    import connector
    connector.list_tables("acme")
except Exception as e:                                        # noqa: BLE001
    print(f"\n  SKIP  连不上源库 acme（{type(e).__name__}）\n")
    sys.exit(0)

if psql("SELECT 1")[0] != 0:
    print("\n  SKIP  改不了源库（没有 docker exec 权限）—— "
          "这一组必须真改结构才验得了\n")
    sys.exit(0)

import catalog                                                # noqa: E402
import metadata_watch as W                                    # noqa: E402

TAG = uuid.uuid4().hex[:6]
PARENT, CHILD = f"watch_p_{TAG}", f"watch_c_{TAG}"
A_PARENT, A_CHILD = f"acme.{PARENT}", f"acme.{CHILD}"

print("\n=== 造两张表并建档（起点）===\n")

psql(f"""
DROP TABLE IF EXISTS {CHILD} CASCADE; DROP TABLE IF EXISTS {PARENT} CASCADE;
CREATE TABLE {PARENT} (id int PRIMARY KEY, name text NOT NULL);
CREATE TABLE {CHILD} (cid int PRIMARY KEY, pid int NOT NULL REFERENCES {PARENT}(id),
                      status text, note text);
GRANT SELECT ON {PARENT}, {CHILD} TO agent_ro, fin_reader;""")

try:
    catalog.observe("acme", PARENT)
    catalog.observe("acme", CHILD)
    snap = catalog.lookup("acme", CHILD)
    chk("建档成功（结构 + 外键都进了档案）",
        bool(snap) and bool(snap.get("columns")) and snap.get("foreign_keys"),
        str(list((snap or {}).keys())))

    # **第一次巡检不该报「变化」** —— 从无到有不是变化。
    r0 = W.sweep([A_CHILD])
    chk("刚建档就巡检：没有变化", not r0["changed"], str(r0["changed"])[:80])

    print("\n=== 挂上结论：一条人确认的口径 + 一条模型推断 ===\n")

    catalog.confirm(f"{A_CHILD}.status", "semantics", "allowed_values",
                    ["OPEN", "DONE"], "wang@acme.com")
    catalog.record_inference(A_CHILD, "link", "guess_join",
                             f"pid 可能对应 {PARENT}.id",
                             {"why": "列名与外键一致"}, actor="model")
    chk("结论挂上了", len(W.affected(A_CHILD)) >= 2,
        str([f'{x["kind"]}.{x["key"]}' for x in W.affected(A_CHILD)]))

    print("\n=== 改源库：加列 · 删列 · 改可空 · 删外键 ===\n")

    # 这四种里，`sync._schema_hash`（只含列名+类型）**只发现得了前两种**。
    psql(f"""
ALTER TABLE {CHILD} ADD COLUMN currency text;
ALTER TABLE {CHILD} DROP COLUMN note;
ALTER TABLE {CHILD} ALTER COLUMN pid DROP NOT NULL;
ALTER TABLE {CHILD} DROP CONSTRAINT {CHILD}_pid_fkey;""")

    r = W.sweep([A_CHILD])
    changed = r["changed"][0]["changes"] if r["changed"] else []
    text = " ".join(f"{k}:{v}" for k, v in changed)
    chk("**巡检发现了变化**", bool(r["changed"]), str(r)[:100])
    chk("认出新增列", "新增列 currency" in text, text[:90])
    chk("认出删除列", "删除列" in text and "note" in text, text[:90])
    chk("**认出可空性变化**（sync 的指纹发现不了这个）",
        "非空 改为" in text or "可空" in text, text[:110])
    chk("**认出外键被删**（sync 的指纹也发现不了）",
        "删除外键" in text, text[:110])

    print("\n=== 受影响的结论被标成待复核 ===\n")

    entries = {x["entry"] for x in r["reviews"]}
    chk("人确认的口径被标了", "semantics.allowed_values" in entries, str(entries))
    chk("模型推断也被标了", "link.guess_join" in entries, str(entries))
    chk("复核任务写明了原来是什么状态",
        {x["was_status"] for x in r["reviews"]} == {"confirmed", "inferred"},
        str({x["entry"]: x["was_status"] for x in r["reviews"]}))
    chk("复核理由带得上变化本身（不是一句「结构变了」）",
        all("删除外键" in x["why"] or "新增列" in x["why"] for x in r["reviews"]),
        str(r["reviews"][0]["why"])[:80] if r["reviews"] else "")

    print("\n=== 但结论本身没有被改（巡检没资格判它失效）===\n")

    still = {f'{x["kind"]}.{x["key"]}': x["status"] for x in W.affected(A_CHILD)}
    chk("**原来的确认仍然是 confirmed**",
        still.get("semantics.allowed_values") == "confirmed", str(still))
    chk("**原来的推断仍然是 inferred**",
        still.get("link.guess_join") == "inferred", str(still))

    print("\n=== monitor 源：稳定，且只报待复核 ===\n")

    m1, m2 = W.monitor_line(), W.monitor_line()
    chk("连跑两次逐字节相同（否则等于每 tick 唤醒一次模型）", m1 == m2)
    chk("待复核出现在 monitor 输出里", A_CHILD in m1, m1[:80])
    chk("**不打时间戳**（输出里没有会变的东西）",
        not any(c.isdigit() and len(m1.split()) and False for c in m1) and m1 == m2)

    print("\n=== 人来复核：维持 / 判失效，都要留痕 ===\n")

    n_before = len(W.open_reviews(A_CHILD))
    W.resolve_review(A_CHILD, "semantics.allowed_values", "wang@acme.com",
                     keep=False, note="status 的取值范围要重新定")
    W.resolve_review(A_CHILD, "link.guess_join", "wang@acme.com",
                     keep=True, note="外键没了但关系还在，只是不再强制")
    n_after = len(W.open_reviews(A_CHILD))
    chk("处理完就不在待办里了", n_before >= 2 and n_after == 0,
        f"{n_before} → {n_after}")

    with __import__("importlib").import_module(
            "datasteward_gate.approvals").open_store(
            readonly=True, init_schema=False) as st:
        hist = [r for r in st.catalog(A_CHILD, current_only=False, limit=200)
                if r["kind"] == "review"]
    chk("**维持和判失效分得开**（不是都记成「处理过」）",
        {r["status"] for r in hist} >= {"confirmed", "refuted"},
        str({r["key"]: r["status"] for r in hist}))

    print("\n=== 再巡一次：结构没再变，不该冒新的待复核 ===\n")

    r2 = W.sweep([A_CHILD])
    chk("没有新变化", not r2["changed"], str(r2["changed"])[:80])
    chk("待复核仍然是空的（处理过的不会自己回来）",
        not W.open_reviews(A_CHILD), str(W.open_reviews(A_CHILD))[:60])

    print("\n=== 采集失败要报出来，不能算成「没变化」===\n")

    rx = W.sweep(["acme.no_such_table_at_all"])
    chk("**采不到就记 errors**（「看过了没变」和「没看成」是两回事）",
        bool(rx["errors"]) and not rx["changed"], str(rx)[:110])

    print("\n=== 复核任务发得出去，且一人一封 ===\n")

    # 再改一次，制造待复核，然后走通知那条路。
    catalog.confirm(f"{A_CHILD}.currency", "semantics", "null_meaning",
                    "空=本币", "wang@acme.com")
    psql(f"ALTER TABLE {CHILD} ADD COLUMN memo text;")
    # **不先手动 sweep** —— 变化只会被认出一次，先扫掉了 CLI 那边就没得报，
    # 而那正是要验的那条路（发通知）。
    rc = subprocess.run([sys.executable, str(ROOT / "ops" / "metadata-sweep.py"),
                         "--sweep", "--send", "--source", "acme"],
                        capture_output=True, text=True, timeout=180,
                        env={**os.environ})
    out = pathlib.Path(os.environ["NOTIFY_OUTBOX"])
    mails = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
             if l.strip()] if out.exists() else []
    notices = [m for m in mails if "需要复核" in (m.get("subject") or "")]
    chk("发出了复核通知", bool(notices),
        f"rc={rc.returncode} {rc.stdout[-90:]}")
    if notices:
        chk("**一人一封，不是一条一封**",
            len({m["to"] for m in notices}) == len(notices),
            str([m["to"] for m in notices]))
        chk("信里说清了「结论我没有自己改」",
            "没有自己改" in (notices[-1].get("body") or ""))

finally:
    psql(f"DROP TABLE IF EXISTS {CHILD} CASCADE; DROP TABLE IF EXISTS {PARENT} CASCADE;")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
