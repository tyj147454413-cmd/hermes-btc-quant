"""round43 双闸落地 mock 测试: MAKER_CAP 0.80→0.65 + TAKER_MAX 0.88→0.65."""
import sys, os, re

# 读取常量(避免导入模块副作用)
# round44 路径修复: 原硬编码沙箱路径 /sessions/dazzling-gracious-tesla/mnt/hermes btc 已失效
# （换环境即 FileNotFoundError），改项目根相对定位，与其余 round 测试一致。
_BOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "hermes_1h_bot.py")
with open(_BOT) as f:
    src = f.read()

def get_const(name):
    m = re.search(rf"^{name}\s*=\s*([\d.]+)", src, re.M)
    return float(m.group(1))

MAKER_CAP = get_const("MAKER_CAP")
TAKER_MAX = get_const("TAKER_MAX")
FAST_TAKE_MAX = get_const("FAST_TAKE_MAX")
MAKER_DISCOUNT = get_const("MAKER_DISCOUNT")

print(f"=== 验证常量 ===")
print(f"MAKER_CAP = {MAKER_CAP} (期望 0.65)")
print(f"TAKER_MAX = {TAKER_MAX} (期望 0.65)")
print(f"FAST_TAKE_MAX = {FAST_TAKE_MAX} (期望 0.75 不变)")
print(f"MAKER_DISCOUNT = {MAKER_DISCOUNT} (期望 0.03 不变)")
assert MAKER_CAP == 0.65
assert TAKER_MAX == 0.65
assert FAST_TAKE_MAX == 0.75
assert MAKER_DISCOUNT == 0.03
print("✅ 常量全部正确\n")


def first_segment(best_ask):
    """第一段定价:返回 (action, mode, order_price, effective_cap)"""
    if best_ask is None:
        return ("SKIP", "book缺失", None, None)
    if best_ask < 0.25:
        return ("SKIP", "<0.25硬底线", None, None)
    if best_ask <= FAST_TAKE_MAX:
        return ("ORDER", f"taker快道≤{FAST_TAKE_MAX}", round(best_ask, 2), TAKER_MAX)
    # 贵区 maker 静置
    order_price = round(min(best_ask - MAKER_DISCOUNT, MAKER_CAP), 2)
    return ("ORDER", "maker静置", order_price, TAKER_MAX)


def second_segment(new_ask, recheck_pass):
    """第二段追价:返回 (action, mode, final_price)"""
    if not recheck_pass:
        return ("RECHECK_CANCELLED", "Gamma反超", None)
    if new_ask > TAKER_MAX:
        return ("SKIP", f"ask={new_ask}>TAKER_MAX={TAKER_MAX}", None)
    return ("ORDER", "taker追价", round(new_ask, 2))


# === 第一段测试 ===
print("=== 第一段定价测试 ===")
first_tests = [
    # (name, ask, expected_action, expected_mode_substr, expected_price)
    ("T1: 便宜区 ask=0.55 → taker 直吃", 0.55, "ORDER", "taker快道", 0.55),
    ("T2: 贵区 ask=0.87 → maker 静置 0.65", 0.87, "ORDER", "maker静置", 0.65),
    ("T3: 极贵 ask=0.97 → maker 静置 0.65", 0.97, "ORDER", "maker静置", 0.65),
    ("T4: book 缺失 → SKIP", None, "SKIP", "book缺失", None),
    ("T5: ask=0.20 → <0.25 硬底线", 0.20, "SKIP", "<0.25", None),
    ("T9: ask=0.92 → maker 静置 0.65(赔率修复)", 0.92, "ORDER", "maker静置", 0.65),
    ("T10: ask=0.75 FAST_TAKE 边界 → taker 直吃", 0.75, "ORDER", "taker快道", 0.75),
    ("T11: ask=0.80 → maker 静置 0.65", 0.80, "ORDER", "maker静置", 0.65),
    ("T12: ask=0.65 → 仍可 taker 快道", 0.65, "ORDER", "taker快道", 0.65),
    ("T13: ask=0.66 → taker 快道 0.66(FAST_TAKE_MAX=0.75 仍允许直吃)", 0.66, "ORDER", "taker快道", 0.66),
]
passed1 = 0
for name, ask, exp_a, exp_m, exp_p in first_tests:
    a, m, p, cap = first_segment(ask)
    ok = (a == exp_a) and (exp_m in m) and (p == exp_p)
    print(f"{'✅' if ok else '❌'} {name}: action={a}, mode={m}, price={p}")
    if ok:
        passed1 += 1

# === 第二段测试 ===
print("\n=== 第二段追价测试 ===")
second_tests = [
    # (name, new_ask, recheck_pass, expected_action, expected_mode_substr, expected_price)
    ("T6: 静置后 ask 跌至 0.60 复核通过 → taker 0.60", 0.60, True, "ORDER", "taker追价", 0.60),
    ("T7: 静置后 ask 仍 0.85 复核通过 → SKIP 超 TAKER_MAX=0.65", 0.85, True, "SKIP", "TAKER_MAX", None),
    ("T8: 静置后 ask 0.92 复核失败 → RECHECK_CANCELLED", 0.92, False, "RECHECK_CANCELLED", "Gamma反超", None),
    ("T14: 静置后 ask 0.65 复核通过 → taker 0.65(边界)", 0.65, True, "ORDER", "taker追价", 0.65),
    ("T15: 静置后 ask 0.66 复核通过 → SKIP 超 0.65", 0.66, True, "SKIP", "TAKER_MAX", None),
]
passed2 = 0
for name, ask, rp, exp_a, exp_m, exp_p in second_tests:
    a, m, p = second_segment(ask, rp)
    ok = (a == exp_a) and (exp_m in m) and (p == exp_p)
    print(f"{'✅' if ok else '❌'} {name}: action={a}, mode={m}, price={p}")
    if ok:
        passed2 += 1

total = passed1 + passed2
total_n = len(first_tests) + len(second_tests)
print(f"\n=== 总结: {total}/{total_n} 通过 ===")
assert total == total_n, f"测试未全通过: {total}/{total_n}"
print("✅ 全部通过")
