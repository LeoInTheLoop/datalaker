"""系统初始化：把 `infra/claw.yaml` 变成确定性的启动前置。

**这份是「装在谁家」，不是「这一次怎么办」。** 后者仍按
docs/config-contract.md 的三层走：审批表单 > Asset Policy > 项目配置。

三件事各有归宿，不要混：

- **审批资格**（`roles`）落治理库 `role_assignment`。写由 `ops/claw-init.py`
  用 admin 账号执行；Agent 容器只有 SELECT，网关启动前只做 `verify()`。
  角色表为空就拒绝启动 —— 而不是像现在这样悄悄回退到 `.env` 的 MAIL_OWNER，
  让「没配审批人」和「配了审批人」长得一模一样。
- **身份**（`agent` / `organization`）只提供称呼、组织名和语气，
  拼在插件那段硬编码的职责边界**前面**。配置文件改不动「业务分析不是你的
  交付物」「源系统只有读权限」这类话 —— 那是执行边界 1 的引导层。
- **审批档位**（`approval.level`）翻译成**已有的**开关 `MANUAL_MODE` /
  `REQUIRE_DOUBLE_CONFIRM`，不新起读取方（config-contract 五）。
  只能往严格走：没有「把 L3 降成 L1」那一档。

校验失败一律抛 `ClawInitError` 并中止启动。缺配置不是空配置 ——
带着半份初始化跑起来，产出的不是「功能少一点的系统」，是错的系统。
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "infra" / "claw.yaml"

# 角色名白名单。`owner:<数据线>` 是现有的细分形式（readme 10.4 绑角色不绑人），
# 所以前缀匹配而不是全等。**admin 不是审批角色**，它是 role_assignment 的
# granted_by，也是唯一能改角色表的人（modify_role_assignment 是 L4）。
BASE_ROLES = ("admin", "sponsor", "steward", "owner")
TONES = ("concise_professional", "concise_direct", "warm_professional")
LANGUAGES = ("zh", "en")
LEVELS = ("standard", "strict", "strict_double")

SCHEMA = {
    "agent": ("name", "tone", "language"),
    "organization": ("name",),
    "email": ("agent_email",),
    "roles": None,       # 列表，单独校验
    "approval": ("level",),
}


class ClawInitError(ValueError):
    """初始化文件不可用。**这个异常必须中止启动，不能被兜底吞掉。**"""


def _require(condition, message):
    if not condition:
        raise ClawInitError(message)


def _text(value, where):
    _require(isinstance(value, str) and value.strip(), f"{where} 必须是非空字符串")
    return value.strip()


def _email(value, where):
    v = _text(value, where)
    _require("@" in v and " " not in v, f"{where}={v!r} 不是邮箱")
    return v


def valid_role(role: str) -> bool:
    return role in BASE_ROLES or (
        role.startswith("owner:") and len(role) > len("owner:"))


def load(path=None) -> dict:
    """读取并校验初始化文件。**未知字段一律拒绝**，不当成可忽略的注释。

    与 `validate_snapshot()` 同一个形状：错在启动前说出来，
    而不是启动后表现成「某个功能好像没生效」。
    """
    import yaml

    target = pathlib.Path(path or os.environ.get("CLAW_INIT_FILE") or DEFAULT_PATH)
    _require(target.is_file(), f"初始化文件不存在：{target}")
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ClawInitError(f"初始化文件不是合法 YAML：{type(exc).__name__}") from None
    _require(isinstance(raw, dict), "初始化文件的顶层必须是对象")

    for key in raw:
        _require(key in SCHEMA,
                 f"顶层字段 {key} 不认识；可用：{'、'.join(SCHEMA)}")
    for key, subkeys in SCHEMA.items():
        _require(key in raw, f"缺少必填段 {key}")
        if subkeys is None:
            continue
        _require(isinstance(raw[key], dict), f"{key} 必须是对象")
        for sub in raw[key]:
            _require(sub in subkeys,
                     f"{key}.{sub} 不认识；可用：{'、'.join(subkeys)}")
        for sub in subkeys:
            _require(sub in raw[key], f"缺少必填字段 {key}.{sub}")

    agent = raw["agent"]
    name = _text(agent["name"], "agent.name")
    tone = _text(agent["tone"], "agent.tone")
    _require(tone in TONES, f"agent.tone={tone} 不支持；可用：{'、'.join(TONES)}")
    language = _text(agent["language"], "agent.language")
    _require(language in LANGUAGES,
             f"agent.language={language} 不支持；可用：{'、'.join(LANGUAGES)}")

    level = _text(raw["approval"]["level"], "approval.level")
    _require(level in LEVELS,
             f"approval.level={level} 不支持；可用：{'、'.join(LEVELS)}"
             "（没有放宽档：级别表是执行边界，不接受配置覆盖）")

    _require(isinstance(raw["roles"], list) and raw["roles"], "roles 必须是非空数组")
    seen, assignments = set(), []
    for index, entry in enumerate(raw["roles"]):
        where = f"roles[{index}]"
        _require(isinstance(entry, dict), f"{where} 必须是对象")
        for sub in entry:
            _require(sub in ("email", "role"), f"{where}.{sub} 不认识；可用：email、role")
        role = _text(entry.get("role"), f"{where}.role")
        _require(valid_role(role),
                 f"{where}.role={role} 不认识；可用：{'、'.join(BASE_ROLES)}"
                 "，或 owner:<数据线>")
        # 一个角色只能有一个当前持有人 —— 重复声明时谁生效说不清，
        # 而「谁能批」说不清就是最不该含糊的那一类。
        _require(role not in seen, f"角色 {role} 声明了不止一次")
        seen.add(role)
        assignments.append((role, _email(entry.get("email"), f"{where}.email")))

    _require("admin" in seen, "roles 里至少要有一个 admin —— "
                              "它是所有初始指派的 granted_by")

    return {
        "path": str(target),
        "agent": {"name": name, "tone": tone, "language": language},
        "organization": {"name": _text(raw["organization"]["name"], "organization.name")},
        "email": {"agent_email": _email(raw["email"]["agent_email"], "email.agent_email")},
        "roles": assignments,
        "approval": {"level": level},
    }


def admin_email(config: dict) -> str:
    return next(who for role, who in config["roles"] if role == "admin")


# ---------------------------------------------------------------------------
# 派生：一个源，两处消费
# ---------------------------------------------------------------------------
_TONE_TEXT = {
    "concise_professional": "用词专业、简短；结论先行，不铺垫。",
    "concise_direct": "直说，不客套；能一句话说完就不写两句。",
    "warm_professional": "专业但不生硬；对外沟通留出商量的余地。",
}
_LANGUAGE_TEXT = {"zh": "默认用简体中文回复。", "en": "Reply in English by default."}


def identity(config: dict) -> str:
    """拼在职责边界**前面**的那一段。

    **只有称呼、组织、语气和信箱。** 交付物、停止点、源只读这些留在插件里
    硬编码 —— 一个配置文件能改掉「业务分析不是你的交付物」的话，
    那段话就不再是边界，只是建议。
    """
    return (f"你叫 {config['agent']['name']}，是 {config['organization']['name']} "
            f"的数据管家。你自己的信箱是 {config['email']['agent_email']}，"
            f"对外发信都从这个地址发出。\n"
            f"{_TONE_TEXT[config['agent']['tone']]}"
            f"{_LANGUAGE_TEXT[config['agent']['language']]}\n\n"
            "身份、组织和联系人这些你已经知道了，**不要在对话里重新问一遍**，"
            "也不要为对方建个人画像。要知道的环境事实去查，不要向人索取。")


def derive_env(config: dict, environ=None) -> dict:
    """把初始化文件翻译成已有的开关。**显式设过的不覆盖**（运维优先）。

    返回真正被写进去的那些键，调用方要能说出「这一轮到底生效了什么」。
    """
    env = os.environ if environ is None else environ
    wanted = {
        # 同一个信箱现在写两遍：MAIL_FROM 是本项目发信，EMAIL_ADDRESS 是
        # Hermes 收信。派生成一个源，免得改一个忘一个变成单向失联。
        "MAIL_FROM": config["email"]["agent_email"],
        "EMAIL_ADDRESS": config["email"]["agent_email"],
    }
    level = config["approval"]["level"]
    if level in ("strict", "strict_double"):
        wanted["MANUAL_MODE"] = "steward"
    if level == "strict_double":
        wanted["REQUIRE_DOUBLE_CONFIRM"] = "1"

    applied = {}
    for key, value in wanted.items():
        if env.get(key):
            continue
        env[key] = value
        applied[key] = value
    return applied


# ---------------------------------------------------------------------------
# 治理库：写一次，之后只读
# ---------------------------------------------------------------------------
def bootstrap(store, config: dict) -> list[str]:
    """把 `roles` 写进 `role_assignment`。**这是运维动作，不是 Agent 动作。**

    调用方必须持有 admin/approver 级的库连接 —— `ops/claw-init.py` 是唯一入口。
    幂等：当前持有人已经是这个人就跳过，不去 close-and-reinsert 制造无意义的
    任期切换（那会让 `granted_by` 的历史变得没法读）。
    """
    by = admin_email(config)
    changed = []
    for role, person in config["roles"]:
        if (store.resolve_role(role) or "").lower() == person.lower():
            continue
        store.assign_role(role, person, by, f"claw.yaml 初始指派（{config['path']}）")
        changed.append(f"{role}={person}")
    return changed


def verify(store, config: dict) -> list[str]:
    """启动前自检：声明的角色在库里真的解析得到，且解析到的就是这个人。

    只读，Agent 账号就够。**失败即拒绝启动** —— 角色表空着照样能跑的话，
    审批通知会一路回退到 `.env` 的兜底地址，而那条路和「配好了」长得一样。
    """
    missing = []
    for role, person in config["roles"]:
        try:
            actual = store.resolve_role(role)
        except Exception as exc:                             # noqa: BLE001
            # 表不存在、连不上、权限不够 —— 对运维来说和「没人被指派」
            # 是同一件事：现在没人能批。**转成同一个异常**，别让一个
            # 裸 OperationalError 冒到调用方去，那看着像库坏了。
            raise ClawInitError(
                f"读不到角色表（{type(exc).__name__}）：{str(exc)[:120]}") from None
        if not actual:
            missing.append(f"{role} 在治理库里没有持有人")
        elif actual.lower() != person.lower():
            missing.append(f"{role} 当前是 {actual}，claw.yaml 声明的是 {person}")
    if missing:
        raise ClawInitError(
            "治理库的角色表与初始化文件不一致，先跑 `python3 ops/claw-init.py`："
            + "；".join(missing))
    return [f"{role}={person}" for role, person in config["roles"]]
