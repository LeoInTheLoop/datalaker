#!/usr/bin/env python3
"""把演练环境还原到「什么都没发生过」，然后**验证真的还原了**。

「从头跑一遍」只有在起点确定时才有意义。而这个项目最会骗人的就是残留：
bronze 里有 600 行、审批里有 11 份、邮箱里躺着上一轮的批准链接 ——
看着像这一轮干的，其实是上一轮的。判分、验收、复盘全都会读错。

清哪些、为什么：

  数据湖      bronze / silver / gold 全删（含历史 eval 表）—— 它们是产物
  治理库      审批、决定、任务线、口径、凭证、事件 —— **审批也是历史**
  邮箱        GreenMail 重启即清 —— 旧的批准链接还能点，会污染下一轮
  Hermes      会话、state、cron 状态、附件缓存 —— 模型会「记得」上一轮
  临时文件    outbox JSONL、点击记录

**源库不清**：它模拟的是公司已有的生产库，本来就该是既存事实。

    python3 tests/reset_live.py            # 清 + 验
    python3 tests/reset_live.py --check    # 只验，不动
"""
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins"),
                str(ROOT / "tests")]

LIVE_HOME = ROOT / ".hermes" / "live-home"
TMP_FILES = ["/tmp/live.db", "/tmp/live.db-wal", "/tmp/live.db-shm",
             "/tmp/live.outbox.jsonl", "/tmp/clicked.txt"]
HERMES_RUNTIME = ["sessions", "state.db", "state.db-wal", "state.db-shm",
                  "cron", "cache", "memories", "logs"]
MAILBOXES = ["boss@acme.com", "wang@acme.com", "zhou@acme.com", "sun@acme.com",
             "dba@acme.com", "it@acme.com", "claw@acme.test", "sponsor"]


def clear_lake() -> str:
    try:
        import sync
    except Exception as e:                                    # noqa: BLE001
        return f"跳过（连不上 lake：{type(e).__name__}）"
    n = 0
    for schema in ("silver", "gold", "bronze"):
        try:
            tabs = sync._trino(
                "SELECT table_name FROM iceberg.information_schema.tables"
                f" WHERE table_schema='{schema}'")
        except Exception:                                     # noqa: BLE001
            continue
        for t in tabs:
            try:
                sync._trino(f'DROP TABLE IF EXISTS iceberg.{schema}."{t}"')
                n += 1
            except Exception:                                 # noqa: BLE001
                pass
    return f"删了 {n} 张表"


def clear_mail() -> str:
    """重启 GreenMail —— 它是内存存储，重启即清空。"""
    r = subprocess.run(["docker", "restart", "datalaker-greenmail-1"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return f"重启失败：{r.stderr[:60]}"
    import mailsim
    for _ in range(30):
        if mailsim.probe():
            return "已重启，邮箱清空"
        time.sleep(2)
    return "重启了但连不上"


def clear_state() -> str:
    n = 0
    for f in TMP_FILES:
        p = pathlib.Path(f)
        if p.exists():
            p.unlink()
            n += 1
    for name in HERMES_RUNTIME:
        p = LIVE_HOME / name
        if p.is_dir():
            shutil.rmtree(p)
            n += 1
        elif p.exists():
            p.unlink()
            n += 1
    return f"清了 {n} 项"


def check() -> list:
    """**验证真的清干净了。** 返回残留项，空 = 干净。"""
    bad = []
    try:
        import sync
        for schema in ("bronze", "silver", "gold"):
            tabs = sync._trino(
                "SELECT table_name FROM iceberg.information_schema.tables"
                f" WHERE table_schema='{schema}'")
            if tabs:
                bad.append(f"lake.{schema} 还有 {len(tabs)} 张表：{tabs[:3]}")
    except Exception as e:                                    # noqa: BLE001
        bad.append(f"lake 查不了：{type(e).__name__}")

    db = os.environ.get("DATASTEWARD_DB", "/tmp/live.db")
    if pathlib.Path(db).exists():
        bad.append(f"治理库还在：{db}（审批与口径都是历史，必须清）")

    for f in TMP_FILES:
        if pathlib.Path(f).exists():
            bad.append(f"临时文件还在：{f}")
    for name in HERMES_RUNTIME:
        if (LIVE_HOME / name).exists():
            bad.append(f"Hermes 运行时还在：{name}（模型会记得上一轮）")

    try:
        import mailsim
        if mailsim.probe():
            for box in MAILBOXES:
                n = len(mailsim.fetch(box))
                if n:
                    bad.append(f"{box} 还有 {n} 封（旧的批准链接还能点）")
    except Exception:                                         # noqa: BLE001
        pass

    # 源库**应该**还在 —— 它不是产物，是既存事实。清掉了才是错。
    try:
        import psycopg
        for u, p, d in (("fin_reader", "F1n2026x", "acme"),
                        ("crm_reader", "Crmro88", "olist_raw"),
                        ("ops_reader", "Opsread7", "northwind")):
            with psycopg.connect(f"postgresql://{u}:{p}@127.0.0.1:5432/{d}",
                                 connect_timeout=5) as c:
                n = c.execute("SELECT count(*) FROM information_schema.tables"
                              " WHERE table_schema='public'").fetchone()[0]
            if not n:
                bad.append(f"源库 {d} 空了 —— 它不该被清")
    except Exception as e:                                    # noqa: BLE001
        bad.append(f"源库连不上：{str(e)[:60]}")
    return bad


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" not in argv:
        print("\n=== 还原演练环境 ===\n")
        print("  数据湖   ", clear_lake())
        print("  邮箱     ", clear_mail())
        print("  库与状态 ", clear_state())
        print("  源库      保持原样（那是公司已有的生产库，不是产物）")

    print("\n=== 验证起点干净 ===\n")
    bad = check()
    for b in bad:
        print("  ❌", b)
    if bad:
        print(f"\n还有 {len(bad)} 处残留 —— 起点不干净，跑出来的数不算数")
        return 1
    print("  ✅ 湖空 · 库无 · 邮箱空 · 无会话残留 · 源库完好")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
