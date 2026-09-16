"""装机初始化（infra/claw.yaml）—— 确定性检查，不连库、不起 Docker。

**只验两件事：坏配置进不来，好配置到得了该到的地方。**
角色表真的写进 Postgres 那一段由 `ops/claw-init.py --check` 在有库的环境验，
这里用 SQLite 后端验同一套 `bootstrap` / `verify` 逻辑。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "services"), os.path.join(ROOT, "plugins")]

import claw_init                                             # noqa: E402

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def rejects(name, yaml_text):
    """坏配置必须在**加载时**就被拒，不能等到运行时表现成「某功能没生效」。"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as f:
        f.write(yaml_text)
        path = f.name
    try:
        claw_init.load(path)
        check(name, False, "被接受了")
    except claw_init.ClawInitError as exc:
        check(name, True, str(exc)[:70])
    finally:
        os.unlink(path)


GOOD = """
agent: {name: Claw, tone: concise_professional, language: zh}
organization: {name: ACME}
email: {agent_email: claw@acme.test}
roles:
  - {email: boss@acme.com, role: admin}
  - {email: wang@acme.com, role: steward}
  - {email: li@acme.com, role: "owner:crm"}
approval: {level: standard}
"""


print("\n=== 装机初始化（claw.yaml）===\n")

# --- 仓库里那份必须可用 ---
try:
    live = claw_init.load(os.path.join(ROOT, "infra", "claw.yaml"))
    check("仓库里的 infra/claw.yaml 能通过校验", True)
except claw_init.ClawInitError as exc:
    live = None
    check("仓库里的 infra/claw.yaml 能通过校验", False, str(exc)[:90])

# --- 坏配置一律拒绝 ---
rejects("未知顶层字段被拒", GOOD + "\nnickname: claw\n")
rejects("未知子字段被拒", GOOD.replace("name: ACME", "name: ACME, industry: tech"))
rejects("缺整段被拒", GOOD.replace("organization: {name: ACME}", ""))
rejects("没有 admin 被拒", GOOD.replace("role: admin", "role: sponsor"))
rejects("角色名不认识被拒", GOOD.replace("role: steward", "role: operator"))
rejects("裸 owner: 前缀没有数据线被拒", GOOD.replace('"owner:crm"', '"owner:"'))
rejects("同一角色声明两次被拒",
        GOOD.replace('  - {email: li@acme.com, role: "owner:crm"}',
                     "  - {email: other@acme.com, role: steward}"))
rejects("邮箱不成形被拒", GOOD.replace("boss@acme.com", "boss"))
rejects("语气不在枚举里被拒", GOOD.replace("concise_professional", "sassy"))
rejects("approval.level 不认识被拒", GOOD.replace("level: standard", "level: relaxed"))
# **这一条是这份文件的立场**：审批档位只能往严格走。
rejects("没有「放宽」档（low 不存在）", GOOD.replace("level: standard", "level: low"))
try:
    claw_init.load(os.path.join(ROOT, "infra", "nope.yaml"))
    check("文件不存在被拒", False, "被接受了")
except claw_init.ClawInitError:
    check("文件不存在被拒", True)

# --- 身份段：可配置的那一半，和改不动的那一半 ---
if live:
    text = claw_init.identity(live)
    check("身份段带上了名字与组织",
          live["agent"]["name"] in text and live["organization"]["name"] in text)
    check("身份段带上了自己的信箱", live["email"]["agent_email"] in text)
    check("身份段明说不要再问身份、不要建画像",
          "不要在对话里重新问一遍" in text and "画像" in text)
    # 配置文件能改掉执行边界的话，那段话就不再是边界，只是建议。
    for boundary in ("只有读权限", "正式批准", "业务分析"):
        check(f"身份段里没有执行边界的措辞：{boundary}", boundary not in text)

# --- 派生：一个源，两处消费；显式设过的不覆盖 ---
if live:
    env = {}
    claw_init.derive_env(live, env)
    check("信箱派生成 MAIL_FROM 与 EMAIL_ADDRESS（同一个值）",
          env.get("MAIL_FROM") == env.get("EMAIL_ADDRESS")
          == live["email"]["agent_email"])
    env = {"MAIL_FROM": "ops@set.by.hand"}
    claw_init.derive_env(live, env)
    check("显式设过的不被覆盖（运维优先）", env["MAIL_FROM"] == "ops@set.by.hand")

strict = dict(live or {}, approval={"level": "strict"})
if live:
    env = {}
    claw_init.derive_env(strict, env)
    check("strict 翻译成已有的 MANUAL_MODE，不新起开关",
          env.get("MANUAL_MODE") == "steward" and "REQUIRE_DOUBLE_CONFIRM" not in env)
    env = {}
    claw_init.derive_env(dict(live, approval={"level": "strict_double"}), env)
    check("strict_double 再叠 REQUIRE_DOUBLE_CONFIRM",
          env.get("MANUAL_MODE") == "steward"
          and env.get("REQUIRE_DOUBLE_CONFIRM") == "1")
    env = {}
    claw_init.derive_env(dict(live, approval={"level": "standard"}), env)
    check("standard 不动任何审批开关",
          "MANUAL_MODE" not in env and "REQUIRE_DOUBLE_CONFIRM" not in env)

# --- 落库 / 自验（SQLite 后端，同一套代码路径）---
from datasteward_gate.approvals import Store                  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    store = Store(os.path.join(d, "s.db"))
    cfg = claw_init.load(os.path.join(ROOT, "infra", "claw.yaml"))
    try:
        claw_init.verify(store, cfg)
        check("空角色表通不过自验（网关据此拒绝启动）", False, "居然通过了")
    except claw_init.ClawInitError:
        check("空角色表通不过自验（网关据此拒绝启动）", True)

    first = claw_init.bootstrap(store, cfg)
    check("bootstrap 写进了声明的全部角色", len(first) == len(cfg["roles"]),
          "、".join(first))
    check("写完自验通过", bool(claw_init.verify(store, cfg)))
    check("再跑一次没有多余的任期切换（幂等）",
          claw_init.bootstrap(store, cfg) == [])
    check("granted_by 是 admin，不是 Agent",
          store.db.execute("SELECT DISTINCT granted_by FROM role_assignment")
          .fetchall() == [(claw_init.admin_email(cfg),)])

    # 换人：改文件重跑一次就该跟着换，未决事项按 resolve_role 自动跟随。
    moved = dict(cfg, roles=[(r, "new@acme.com" if r == "steward" else p)
                             for r, p in cfg["roles"]])
    claw_init.bootstrap(store, moved)
    check("换人后 resolve_role 指向新人",
          store.resolve_role("steward") == "new@acme.com")
    store.close()

# --- 网关那一侧：没装机就不许起来 ---
import importlib.util                                         # noqa: E402

spec = importlib.util.spec_from_file_location(
    "claw_entrypoint", os.path.join(ROOT, "docker", "agent-entrypoint.py"))
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

check("entrypoint 暴露了装机自检", hasattr(entry, "claw_preflight"))
with tempfile.TemporaryDirectory() as d:
    # 空库 = 谁也没被指派。**这一支必须抛**，不能靠 .env 的兜底地址把
    # 「没人能批」演成「一切正常」。
    saved = {k: os.environ.get(k) for k in
             ("DATASTEWARD_DB", "DATASTEWARD_DSN", "MAIL_FROM", "EMAIL_ADDRESS")}
    empty = os.path.join(d, "empty.db")
    Store(empty).close()           # 建好表结构，但一个角色都不指派
    os.environ["DATASTEWARD_DB"] = empty
    os.environ.pop("DATASTEWARD_DSN", None)
    os.environ.pop("MAIL_FROM", None)
    os.environ.pop("EMAIL_ADDRESS", None)
    try:
        entry.claw_preflight()
        check("角色表为空时 claw_preflight 抛错（网关据此拒绝启动）", False, "居然通过了")
    except claw_init.ClawInitError as exc:
        check("角色表为空时 claw_preflight 抛错（网关据此拒绝启动）", True,
              str(exc)[:60])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

# --- 最容易悄悄失效的一条：YAML 的裸 off 是布尔 ---
import yaml                                                   # noqa: E402

hermes = yaml.safe_load(open(os.path.join(ROOT, "infra", "hermes", "config.yaml"),
                             encoding="utf-8"))
value = (hermes.get("onboarding") or {}).get("profile_build")
check("首次对话不建个人画像：profile_build 关着", value is not None)
# Hermes 判的是 `isinstance(mode, str) and mode.lower() == "off"`，
# 写成裸 off 会被 YAML 读成 False —— 等于没关，且看不出来。
check("profile_build 是字符串 \"off\"，不是布尔 False",
      isinstance(value, str) and value.strip().lower() == "off", repr(value))

print(f"\n{len(ok)} 通过 / {len(bad)} 失败")
if bad:
    print("失败项：" + "、".join(bad))
sys.exit(1 if bad else 0)
