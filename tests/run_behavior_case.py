#!/usr/bin/env python3
"""行为 eval 驱动 —— **注入状态 → 推一步 → 独立取证 → 体外判分**。

    # 机制档：不点模型，只问「如果它真的这么干，会怎样」
    python3 tests/run_behavior_case.py --driver gate

    # 行为档：真模型自己决定下一步（要 .env 与 HERMES）
    HERMES=<path> python3 tests/run_behavior_case.py --driver live

两档测的**不是同一件事**，报告里的 `driver` 字段就是干这个用的：

    gate   下一步做什么由 case 的 `attempt` 指定 —— 测的是门禁与终态，
           确定性、离线、进得了回归。它回答：「它要是这么干，挡不挡得住」
    live   下一步做什么由模型自己决定 —— 这才是行为 eval。
           它回答：「面对这个局面，它会不会这么干」

**negative 单独绿不算数**：gate 档里 `attempt` 是脚本给的，所以「被挡住」
是真证据；live 档里「没发生坏事」可能只是模型这次没试，判分器因此把
「试没试」单独报出来，并要求 positive 对照成立（见 `score_behavior.pair_up`）。

体外隔离靠 **subprocess**：判分器是另一个进程，只吃文件。
"""
import argparse
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins"),
                str(ROOT / "evals")]

CASE_DIR = ROOT / "evals" / "behavior" / "cases"
# 每次跑单独一个目录：`raw.json` 是**一等产物**，判分可以对着它重跑
# （`score_behavior.py <run>/raw.json --cases ...`）。
# gate 档覆盖 `gate-latest`（回归天天跑，不该堆目录）；
# **live 档带时间戳** —— 要花钱、要二十分钟、非确定性，被覆盖就再也拿不回来。
RUNS = pathlib.Path(os.environ.get("BEHAVIOR_RUNS",
                                   str(ROOT / "evals" / "behavior" / "runs")))
WORK = RUNS                                   # 具体目录在 main() 里定
DB = OUTBOX = ""


def _plugin(mod):
    """import 插件里的模块。**必须当成包来 import**，不能按文件路径单独加载：
    `tools.py` 里有 `from . import _ensure_path`，脱离包就是 ImportError。
    目录名带连字符，所以只能走 `import_module`，写不出 import 语句。
    （同 `tests/test_toolset_whitelist.py` 的做法。）"""
    d = str(ROOT / ".hermes" / "plugins")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module(f"data-steward.{mod}")


# --------------------------------------------------------------------------
# 前置条件：缺什么就 SKIP 掉哪些 case，**不假装跑过**
# --------------------------------------------------------------------------
def _have_lake():
    try:
        import sync
        sync._trino("SELECT 1")
        return True
    except Exception:                                        # noqa: BLE001
        return False


def _have_source_pg():
    """探活走**和干活同一条路**（`connector.query`），不另开一条连接。

    第一版这里多传了个 `plane=` —— `query()` 根本没有这个参数，TypeError
    被 `except Exception` 吞成「服务不可用」，于是 docker 起着的时候
    照样报「缺 source_pg」，两条 SQL case 静默跳过。**假 SKIP 比 FAIL 更坏**：
    它长得像「环境没准备好」，没人会去查。
    """
    try:
        import connector
        connector.query("acme", "SELECT 1")
        return True
    except Exception:                                        # noqa: BLE001
        return False


def _have_hermes():
    h = os.environ.get("HERMES", "")
    return bool(h) and pathlib.Path(h, ".venv-h/bin/hermes").exists()


_PROBES = {"lake": _have_lake, "source_pg": _have_source_pg, "hermes": _have_hermes}
_probe_cache: dict = {}


def not_for_driver(case, driver) -> str:
    """这条 case 在这一档下有没有意义。

    **只有确定性属性才配用它**：像「有界子查询不该审批」这种，
    准入结果由 AST 决定，模型选不选那个写法不是这条 case 要测的东西 ——
    live 里它只会红在「模型这次用了别的工具」上，是噪音不是信号。
    那类属性的证据在 gate 档与 `tests/test_sql_admission.py` 里。

    **不是用来藏 live 失败的。** 用了就得在 `why` 里写明白为什么这条
    属性与模型的选择无关。
    """
    ds = case.get("drivers")
    return "" if not ds or driver in ds else f"这条 case 只在 {'/'.join(ds)} 档下有意义"


def missing(case, driver) -> list:
    need = list(case.get("requires") or [])
    if driver == "live":
        need.append("hermes")
    out = []
    for n in need:
        if n not in _probe_cache:
            _probe_cache[n] = _PROBES[n]()
        if not _probe_cache[n]:
            out.append(n)
    return out


# --------------------------------------------------------------------------
# driver = gate：把 case 里的 attempt 送进真的 pre_tool_call
# --------------------------------------------------------------------------
def _mutate_gate(G, how):
    """把护栏**拆掉**，用来验判据有没有牙齿。

    这是 v1「gate-off 对照组」在行为层的同一件事：negative 全绿说明不了
    什么 —— 判据可能压根没在判。拆掉护栏再跑一遍，**negative 必须全红**；
    有哪条还绿着，那条判据就是摆设，得当成 eval 自己的 bug 修。

    **两档，因为护栏不止一层。** `sql_query` 的负载准入在 Connector 里
    （`connector._admit` 抛 `QueryApprovalRequired`），门禁那一份是复用它 ——
    只拆门禁，SQL 照样打不出去，于是「SQL 没进账本」看着像没牙齿，
    其实是**纵深防御**：第二层还在。实测就撞在这上面。

    所以回归用 `guards-off`（两层一起拆），`gate-off` 留着单独看门禁那一层。
    只在 `--mutate` 下生效，正常跑一行都不碰。
    """
    if how not in ("gate-off", "guards-off"):
        raise SystemExit(f"未知 mutate：{how}")
    G.gate = lambda tool_name, args, task_id="", **kw: None
    if how == "guards-off":
        import connector
        connector._admit = lambda sql, **kw: sql


def drive_gate(case, ids, mutate="") -> list:
    """按 `attempt` 调门禁；放行就真的执行 handler。

    **两个 hook 都要走**：`pre_tool_call` 决定放不放，`post_tool_call`
    才写台账和事件。只调前者的话，positive 那边「台账里应该有这一笔」
    永远红，而红的原因跟被测行为无关。
    """
    import datasteward_gate as G
    importlib.reload(G)                         # 上一轮可能被 mutate 拆过门禁
    G._local.store = None                       # 库换过了，线程本地句柄要重开
    if mutate:
        _mutate_gate(G, mutate)
    tools = _plugin("tools")
    log = []
    for att in case.get("attempt") or []:
        tool, args = att["tool"], dict(att.get("args") or {})
        task_id = ids.get(att.get("run"), att.get("run") or "behavior")
        r = G.gate(tool, args, task_id)
        if isinstance(r, dict) and r.get("action") == "block":
            log.append({"tool": tool, "outcome": "blocked",
                        "message": str(r.get("message"))[:300]})
            continue
        if isinstance(r, dict) and r.get("action") == "modify":
            args = dict(r.get("args") or args)
        handler = tools._HANDLERS.get(tool)
        if handler is None:
            log.append({"tool": tool, "outcome": "no_handler"})
            continue
        try:
            out = handler(args)
            status = "ok"
        except Exception as e:                               # noqa: BLE001
            out, status = f"{type(e).__name__}: {e}", "error"
        G.audit(tool, args, result=out, status=status, task_id=task_id)
        log.append({"tool": tool, "outcome": status, "result": str(out)[:300]})
    # **发信是后台线程。** 不等它收尾就去取证，「发给谁、几封」全是空的 ——
    # 而那正好是这一层最关键的几条判据（门禁本身早就返回了）。
    G._drain_mails()
    return log


# --------------------------------------------------------------------------
# driver = live：只给刺激，下一步它自己决定
# --------------------------------------------------------------------------
def _resume_prompt():
    """恢复那条线的 prompt **用 cron 里那一份**，不另写一句。

    自己编一句就等于测了一个生产里不存在的提示词 —— 那种绿最没用。
    """
    return _plugin("cron").JOBS["data-steward-resume"]["prompt"]


def _monitor_output():
    r = subprocess.run([sys.executable, str(ROOT / "ops" / "resumable.py")],
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "DATASTEWARD_DB": DB})
    return r.stdout.strip()


def build_prompt(case) -> str:
    s = case.get("stimulus") or {}
    kind = s.get("kind", "prompt")
    if kind == "prompt":
        return s["text"]
    if kind == "inbound_mail":
        return (f"刚收到一封信，请按它往下推进。\n\n"
                f"发件人：{s['from']}\n主题：{s.get('subject', '(无主题)')}\n"
                f"正文：\n{s['body']}\n")
    if kind == "resume_tick":
        mon = _monitor_output()
        return (f"MONITOR CHANGE DETECTED\n{mon}\n\n{_resume_prompt()}")
    raise SystemExit(f"未知 stimulus.kind：{kind}")


def drive_live(case, ids, mutate="", timeout=300) -> list:
    """真 Hermes + 真模型跑一轮。

    **与网关入站有差别**：这里走 `hermes -z` 一次性会话，adapter 的
    归属判定与会话历史都不参与。整条入站链另有演练
    （`docs/live-rehearsal.md`），这里要的是「同一个局面、同一批工具、
    同一个门禁，模型下一步干什么」。
    """
    home = ROOT / ".hermes" / "behavior-home"
    home.mkdir(parents=True, exist_ok=True)
    src = ROOT / ".hermes" / "home" / "config.yaml"
    if not src.exists():
        return [{"outcome": "skipped", "message": f"缺 {src}"}]
    (home / "config.yaml").write_text(src.read_text(encoding="utf-8"),
                                      encoding="utf-8")
    # 会话与记忆每个 case 清一次 —— 上一 case 的对话留着，模型会「记得」，
    # 而那份记忆不属于这个 case 注入的状态（残留是这个项目的头号骗局）。
    import shutil
    for d in ("sessions", "memories", "cache"):
        shutil.rmtree(home / d, ignore_errors=True)
    env = {**os.environ, "HERMES_HOME": str(home),
           "HERMES_ENABLE_PROJECT_PLUGINS": "1", "CLAW_ROOT": str(ROOT),
           "DATASTEWARD_DB": DB, "NOTIFY_CHANNEL": "outbox",
           "NOTIFY_OUTBOX": OUTBOX}
    env.pop("DATASTEWARD_DSN", None)
    t0 = time.time()
    try:
        r = subprocess.run([f"{os.environ['HERMES']}/.venv-h/bin/hermes", "-z",
                            build_prompt(case)], capture_output=True, text=True,
                           timeout=timeout, cwd=str(ROOT), env=env)
        return [{"outcome": "ran", "seconds": round(time.time() - t0, 1),
                 "stdout": r.stdout[-2000:], "stderr": r.stderr[-800:]}]
    except subprocess.TimeoutExpired:
        return [{"outcome": "timeout", "seconds": timeout}]


DRIVERS = {"gate": drive_gate, "live": drive_live}


# --------------------------------------------------------------------------
def run_one(case, driver, mutate="") -> dict:
    import behavior_probe                       # 体外取证
    import behavior_fixture

    pathlib.Path(DB).parent.mkdir(parents=True, exist_ok=True)
    ids = behavior_fixture.build(case, DB, OUTBOX)
    # **基准取在注入之后、动手之前**，前后两次取证共用它 —— 账本是跨 case
    # 共享的一张大表（9 万行），不划边界既慢又会把上一个 case 的查询算进来。
    mk = behavior_probe.mark()
    before = behavior_probe.take(DB, OUTBOX, mk)
    log = DRIVERS[driver](case, ids, mutate)
    after = behavior_probe.take(DB, OUTBOX, mk)
    return {"case": case, "before": before, "after": after,
            "delta": behavior_probe.delta(before, after), "driver_log": log}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--driver", choices=sorted(DRIVERS), default="gate")
    ap.add_argument("--case", default="", help="只跑某一个 case_id")
    ap.add_argument("--mutate", default="", choices=["", "gate-off", "guards-off"],
                    help="拆掉护栏再跑一遍：negative 必须全红，否则那条判据没有牙齿。"
                         "guards-off 连 Connector 的 SQL 准入一起拆（回归用这档）")
    # 报告路径带上 driver；**live 档还带时间戳** —— 那一趟要花钱、要 20 分钟，
    # 而且非确定性，被下一次跑覆盖掉就再也拿不回来了（已经丢过一次）。
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    global DB, OUTBOX
    tag = a.driver + (a.mutate and f"-{a.mutate}")
    stamp = time.strftime("-%Y%m%d-%H%M%S") if a.driver == "live" else "-latest"
    # **不要写成 `Path(os.environ.get(...)) or 默认`** —— `Path("")` 是
    # `PosixPath('.')`，真值为真，于是没设环境变量时产物直接落进仓库根目录，
    # 而 `os.makedirs(os.path.dirname("gov.db"))` 拿到空串当场炸成
    # 「治理组件异常，已按 fail-closed 拒绝」—— 十个 case 全红在一个
    # 跟被测行为毫无关系的地方。踩过一次。
    _w = os.environ.get("BEHAVIOR_WORK") or ""
    run_dir = pathlib.Path(_w) if _w.strip() else (RUNS / f"{tag}{stamp}")
    run_dir.mkdir(parents=True, exist_ok=True)
    DB, OUTBOX = str(run_dir / "gov.db"), str(run_dir / "outbox.jsonl")
    if not a.out:
        a.out = str(run_dir / "report")

    os.environ.setdefault("NOTIFY_CHANNEL", "outbox")
    os.environ["NOTIFY_OUTBOX"] = OUTBOX
    os.environ["DATASTEWARD_DB"] = DB
    os.environ.pop("DATASTEWARD_DSN", None)

    files = sorted(CASE_DIR.glob("*.json"))
    if not files:
        print(f"  没有 case：{CASE_DIR}")
        return 1
    cases = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    if a.case:
        cases = [c for c in cases if c["case_id"] == a.case]

    print(f"\n=== 行为 eval（driver={a.driver}，{len(cases)} 个 case）===\n")
    # **live 档在源库/湖不通时跑出来的绿不能要。** 实测过一次：docker 没起，
    # 模型试了三次全是 connection timeout，于是「没写口径」这个绿真正的
    # 原因是连不上，不是它守住了规矩。与拆门禁对照同一条道理 ——
    # 「没发生」只有在「本来发生得了」的地方才算证据。
    if a.driver == "live":
        down = [n for n in ("lake", "source_pg") if not _PROBES[n]()]
        if down:
            print(f"  ⚠️ {'/'.join(down)} 不通：这一批 live 结果**不能当行为证据**"
                  f"（模型可能只是碰壁，不是守规矩）。先把 docker 起起来。\n")
    raw, skipped = [], []
    for c in cases:
        why = not_for_driver(c, a.driver)
        if why:
            skipped.append((c["case_id"], [why]))
            print(f"  SKIP  {c['case_id']}  {why}")
            continue
        miss = missing(c, a.driver)
        if miss:
            skipped.append((c["case_id"], miss))
            print(f"  SKIP  {c['case_id']}  缺 {miss}")
            continue
        print(f"  RUN   {c['case_id']}")
        raw.append(run_one(c, a.driver, a.mutate))

    raw_path = run_dir / "raw.json"
    raw_path.write_text(json.dumps({"driver": a.driver, "mutate": a.mutate,
                                    "cases": raw},
                                   ensure_ascii=False, indent=1), encoding="utf-8")

    # **判分在另一个进程里。** 与 evals/run.py 同一条理由：
    # 判分器一旦跟被测代码同进程，它给的数字就是被测系统给自己打分。
    r = subprocess.run([sys.executable, str(ROOT / "evals" / "score_behavior.py"),
                        str(raw_path), "--cases", str(CASE_DIR),
                        "--out", a.out], text=True)
    if a.mutate:
        return _mutation_verdict(a.out, skipped)
    if skipped:
        print(f"\n  跳过 {len(skipped)} 个（缺前置）："
              + "，".join(f"{c}({'+'.join(m)})" for c, m in skipped))
    print(f"  产物：{run_dir}/  （raw.json 可单独重判，不必重跑模型：\n"
          f"        python3 evals/score_behavior.py {raw_path} "
          f"--cases evals/behavior/cases）")
    return r.returncode


def _mutation_verdict(out, skipped) -> int:
    """拆掉门禁之后，negative 应该**全红**。还绿着的那条 = 判据没有牙齿。"""
    rep = json.loads(pathlib.Path(out + ".json").read_text(encoding="utf-8"))
    negs = [r for r in rep["results"] if r.get("polarity") == "negative"]
    toothless = [r["case_id"] for r in negs if r["verdict"] not in ("FAIL", "XFAIL")]
    print(f"\n=== 拆护栏对照（negative {len(negs)} 条）===")
    for r in negs:
        mark = "红（判据有牙齿）" if r["verdict"] in ("FAIL", "XFAIL") else "❌ 仍绿"
        print(f"  {mark}  {r['case_id']}")
    if skipped:
        print(f"  跳过 {len(skipped)} 个（缺前置）：" +
              "，".join(f"{c}({'+'.join(m)})" for c, m in skipped))
    if toothless:
        print(f"\n❌ 护栏拆了这些还绿着，判据是摆设：{toothless}")
        return 1
    print("\n✅ 护栏一拆 negative 全红 —— 正常跑出来的绿是判出来的，不是没判")
    return 0


if __name__ == "__main__":
    sys.exit(main())
