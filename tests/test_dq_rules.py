"""DQ 规则库：纯函数，不连库。

对应 R5 首轮实测暴露的能力缺口 —— 十类注入里有六类没有对应规则。
断言重点是**不误报**和**不漏报**同等重要：一个见谁都报的规则等于没有规则。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services"))
import dq_rules as R

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== enum 漂移 ===\n")

v = ["delivered"] * 40 + ["Delivered"] * 5 + ["DELIVERED"] * 3 + ["shipped"] * 20
r = R.enum_drift(v)
chk("大小写变体被抓到", r and r["issue"] == "enum_drift")
chk("给出具体变体", r and "delivered" in str(r["groups"]))
chk("干净的枚举不误报", R.enum_drift(["a"] * 30 + ["b"] * 20) is None)
chk("高基数列不误报", R.enum_drift([f"id_{i}" for i in range(200)]) is None)
chk("空列表安全", R.enum_drift([]) is None)

print("\n=== 拼写漂移 ===\n")

city = ["São Paulo"] * 20 + ["Sao Paulo"] * 15 + ["SAO PAULO"] * 5 + ["Rio"] * 10
r = R.spelling_drift(city)
chk("音标变体被抓到", r and r["issue"] == "spelling_drift", r and r["detail"][:40])
chk("纯大小写不重复报（那是 enum_drift 的活）",
    R.spelling_drift(["abc"] * 10 + ["ABC"] * 5) is None)
chk("统一拼写不误报", R.spelling_drift(["Sao Paulo"] * 30) is None)

print("\n=== 类型污染 ===\n")

nums = [str(i) for i in range(100)]
r = R.type_mismatch(nums + ["unknown"] * 5)
chk("数字列里的文本被抓到", r and r["issue"] == "type_mismatch")
chk("给出坏样例", r and "unknown" in str(r["bad_samples"]))
chk("纯数字列不误报", R.type_mismatch(nums) is None)
chk("纯文本列不误报", R.type_mismatch(["abc"] * 50) is None)
chk("样本太少不下结论", R.type_mismatch(["1", "2", "x"]) is None)

print("\n=== 单位错误 ===\n")

prices = [f"{39 + i % 10}.9" for i in range(100)]
r = R.unit_outlier(prices + ["3990"] * 5)
chk("数量级异常被抓到", r and r["issue"] == "unit_outlier", r and r["detail"][:40])
chk("正常价格不误报", R.unit_outlier(prices) is None)
chk("用中位数而非标准差（脏数据撑不大它）",
    R.unit_outlier(prices + ["3990"] * 30) is not None)
chk("样本太少不下结论", R.unit_outlier(["1", "100"]) is None)

print("\n=== 日期矛盾（同表跨列，不算 join）===\n")

good = [("2026-01-01", "2026-01-05")] * 50
r = R.date_before(good + [("2026-02-01", "2026-01-20")] * 4, "下单", "送达")
chk("送达早于下单被抓到", r and r["issue"] == "date_before_order")
chk("严重级别为 high", r and r["severity"] == "high")
chk("正常日期不误报", R.date_before(good) is None)
chk("非日期值被跳过而非报错", R.date_before([("x", "y")] * 10) is None)

print("\n=== 组合调用与登记表 ===\n")

f = R.check_column("order_status", v)
chk("check_column 返回列名", all(x["column"] == "order_status" for x in f))
chk("一列可命中多条规则", len(f) >= 1, str([x["issue"] for x in f]))
chk("规则登记表可枚举", set(R.RULES) == {"enum_drift", "spelling_drift",
                                        "type_mismatch", "unit_outlier"})
chk("跨表规则被明确排除在源库之外",
    set(R.LAKE_ONLY) == {"broken_foreign_key", "total_mismatch", "pk_unique_full"})


def boom(values):
    raise ValueError("坏规则")


f2 = R.check_column("c", ["a"], rules={"boom": boom})
chk("单条规则崩溃不拖垮整轮", len(f2) == 1 and "失败" in f2[0]["detail"])

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
