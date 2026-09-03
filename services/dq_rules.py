"""DQ 规则库 —— 纯函数，输入一列取样值，输出结论。

R5 首轮实测暴露的最大缺口：`run_dq_check` 只有三条规则
（空值率 / 主键唯一 / 常量列），而注入的十类缺陷里有六类**注入了也测不到**。
那不是漏检，是压根没有对应规则。

拆成纯函数有三个理由：
  · 不连库就能测，规则本身的正确性与取数解耦
  · 与 `stop_points.py` 一样是 4.5 三层执行形态的第一层，Pipeline 和 loop 共用
  · 新增一条规则 = 加一个函数 + 在 `RULES` 里登记一行

**跨表的规则不在这里**（外键完整性、跨表矛盾）：源库禁 join（铁律 3），
它们只能在 bronze 落地后于 lake 上全量算 —— 见 `lake_dq_check`。
"""
import re
import statistics
import unicodedata

BLANKS = {"", "null", "none", "n/a", "na", "-", "unknown", "未知", "无"}


def _clean(values):
    return [str(v).strip() for v in values
            if v is not None and str(v).strip().lower() not in BLANKS]


def _fold_case(s):
    return s.strip().lower()


def _fold_accent(s):
    """去掉音标并折叠大小写：São Paulo / Sao Paulo / SAO PAULO → sao paulo。"""
    n = unicodedata.normalize("NFKD", s)
    return "".join(c for c in n if not unicodedata.combining(c)).strip().lower()


# ---------------------------------------------------------------- 规则
def enum_drift(values, min_variants=2, min_rows=5):
    """同一个值的大小写/空格变体（`delivered` / `Delivered` / `DELIVERED`）。

    **没有 distinct 上限。** 原来卡在 50，而大小写漂移跟基数没关系 ——
    几千个城市名照样会有大小写不一致。实测这个上限吃掉了一多半的召回：
    生成器往「不同值少于 2%」的列注入，两万行样本里那可以是四百个不同值，
    远超 50，于是规则**拒绝去看**，被算成漏检。
    （`spelling_drift` 上的同款上限 500 早先已经拿掉，这次是同一个毛病。）

    改成按**覆盖行数**筛：孤零零两条不同写法不值得打扰人，
    真的影响到若干行的才报。
    """
    vs = _clean(values)
    if not vs:
        return None
    counts = {}
    groups = {}
    for v in vs:
        counts[v] = counts.get(v, 0) + 1
        groups.setdefault(_fold_case(v), set()).add(v)
    drifted = {}
    for k, g in groups.items():
        if len(g) < min_variants:
            continue
        if sum(counts[x] for x in g) < min_rows:
            continue
        drifted[k] = sorted(g)
    if not drifted:
        return None
    biggest = max(drifted.values(), key=lambda g: sum(counts[x] for x in g))
    return {"issue": "enum_drift", "severity": "medium",
            "detail": f"{len(drifted)} 组大小写变体，例如 {biggest[:3]}",
            "groups": {k: v for k, v in list(drifted.items())[:10]}}


def spelling_drift(values, min_variants=2, min_rows=5):
    """拼写与标准化差异（音标、全半角）。比 enum_drift 更宽，因此单列一条。

    **没有 distinct 上限。** 早先设了 500，结果 `customer_city`（2263 个城市）
    直接被跳过 —— 而城市名恰恰是最容易出现拼写漂移的一类列。
    近似唯一的列由上游 `run_dq_check` 的候选过滤排除，这里不必重复防一次。

    改成按**覆盖行数**筛：只有真的影响到若干行的漂移才值得报，
    孤零零两条不同写法不是问题。
    """
    vs = _clean(values)
    if not vs:
        return None
    groups = {}
    for v in vs:
        groups.setdefault(_fold_accent(v), set()).add(v)
    # 只报「折叠后同、折叠前大小写也不同」之外的那些 —— 纯大小写归 enum_drift
    counts = {}
    for v in vs:
        counts[v] = counts.get(v, 0) + 1
    drifted = {}
    for k, g in groups.items():
        if len(g) < min_variants or len({_fold_case(x) for x in g}) <= 1:
            continue
        if sum(counts[x] for x in g) < min_rows:
            continue                      # 影响面太小，不值得打扰人
        drifted[k] = sorted(g)
    if not drifted:
        return None
    biggest = max(drifted.values(), key=lambda g: sum(counts[x] for x in g))
    return {"issue": "spelling_drift", "severity": "low",
            "detail": f"{len(drifted)} 组拼写变体，例如 {biggest[:3]}",
            "groups": {k: v for k, v in list(drifted.items())[:10]}}


NUMERIC = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def type_mismatch(values, expect="numeric", max_bad_rate=0.0):
    """声明是数字的列里混进了文本（`postal_code = "unknown"`）。

    **不看列类型看内容** —— bronze 原样落地，源里是 text 的列照样落成 text，
    真正的信号是「绝大多数是数字，少数不是」。

    这里**不能用 `_clean`**：`unknown` / `未知` 正在 BLANKS 里，
    而它们恰恰就是要找的污染值。滤掉它们等于把答案先删了再考试。
    """
    vs = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if len(vs) < 10:
        return None
    good = [v for v in vs if NUMERIC.match(v)]
    rate = len(good) / len(vs)
    if rate < 0.8 or rate == 1.0:
        return None                       # 本来就是文本列，或者完全干净
    bad = sorted({v for v in vs if not NUMERIC.match(v)})[:5]
    return {"issue": "type_mismatch", "severity": "medium",
            "detail": f"{rate:.1%} 的值是数字，其余是文本，例如 {bad}",
            "bad_samples": bad}


def unit_outlier(values, factor=20.0):
    """数量级异常（价格 39.9 被写成 3990）。

    用**中位数比值**而不是标准差：脏数据本身会把标准差撑大，
    用它做判据等于让异常值给自己开脱。
    """
    nums = []
    for v in _clean(values):
        if NUMERIC.match(v):
            nums.append(float(v))
    if len(nums) < 20:
        return None
    med = statistics.median([abs(x) for x in nums if x])
    if not med:
        return None
    out = [x for x in nums if abs(x) > med * factor]
    if not out:
        return None
    return {"issue": "unit_outlier", "severity": "medium",
            "detail": f"{len(out)} 个值超过中位数 {med:g} 的 {factor:g} 倍，"
                      f"最大 {max(out):g}",
            "count": len(out), "median": med}


DATE_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}")


def date_before(pairs, label_a="", label_b=""):
    """跨列日期矛盾（送达时间早于下单时间）。

    `pairs` 是 [(a, b), ...]，期望 a <= b。**同一张表内的跨列比较不算 join**，
    所以它可以在源库的采样上算。
    """
    bad = 0
    total = 0
    sample = []
    for a, b in pairs:
        a, b = (str(a or "").strip(), str(b or "").strip())
        if not (DATE_PAT.match(a) and DATE_PAT.match(b)):
            continue
        total += 1
        if b < a:
            bad += 1
            if len(sample) < 3:
                sample.append((a, b))
    if not total or not bad:
        return None
    return {"issue": "date_before_order", "severity": "high",
            "detail": f"{bad}/{total} 行 {label_b} 早于 {label_a}，例如 {sample}",
            "count": bad, "checked": total}


# 登记表：新增规则改这里，不改调用方
RULES = {
    "enum_drift": enum_drift,
    "spelling_drift": spelling_drift,
    "type_mismatch": type_mismatch,
    "unit_outlier": unit_outlier,
}

# 需要两列的规则单独走，签名不同
PAIR_RULES = {"date_before_order": date_before}

# 只能在 lake 上算的（源库禁 join，铁律 3）
LAKE_ONLY = ("broken_foreign_key", "total_mismatch", "pk_unique_full")


def check_column(column: str, values, rules=None) -> list:
    """对一列跑所有单列规则。返回 findings 列表。"""
    out = []
    for name, fn in (rules or RULES).items():
        try:
            r = fn(values)
        except Exception as e:                               # noqa: BLE001
            r = {"issue": name, "severity": "low",
                 "detail": f"规则执行失败：{type(e).__name__}"}
        if r:
            out.append({"column": column, **r})
    return out
