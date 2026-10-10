"""8/21 方案A3 追价重挂逻辑 dry-run 测试（PAPER 实例 + mock，不触网不下单）"""
import sys, time, json
sys.path.insert(0, "/sessions/optimistic-relaxed-turing/mnt/hermes btc")
import hermes_1h_bot as m

# 干掉 sleep 加速测试
m.time.sleep = lambda s: None

bot = m.HermesBot(live=False)
bot.client = type("FakeClient", (), {"cancel_order": lambda self, order_id: None})()

# 公共 mock（C2审计修复🟡5: 适配 _record_fill 新签名 size_matched/avg_price/info 关键字参数）
fills_recorded = []
bot._record_fill = lambda oid, slug, side, filled, status, **kw: fills_recorded.append((oid[:12], filled, status))
bot._remove_pending_record = lambda oid: None

def reset(oid, age_min=10, chase=0, price=0.50, orig=0.50):
    bot._pending_orders.clear()
    bot._pending_orders[oid] = {
        "slug": "test-slug", "side": "UP", "ts": time.time() - age_min * 60,
        "amount": 10.0, "token_id": "tok123", "retry": 0, "max_price": 0.60,
        "price": price, "orig_price": orig, "chase_count": chase, "last_chase_check": 0.0}

# 场景1: OPEN + 10min + ask 0.50→0.55 → 应撤旧挂新
oid1 = "0x" + "a" * 40
reset(oid1, age_min=10)
bot._check_order_status = lambda o: "OPEN"
bot._check_liquidity = lambda tid, mp: (True, 0.55, "ok")
placed = []
bot.place_order = lambda tid, price, size, side="BUY": (placed.append((price, size, side)) or {"ok": True, "order_id": "0x" + "b" * 40})
bot._poll_pending_orders()
assert len(placed) == 1 and placed[0][0] == 0.55, f"场景1失败: {placed}"
assert "0x" + "b" * 40 in bot._pending_orders, "场景1: 新单未入 _pending_orders"
new_info = bot._pending_orders["0x" + "b" * 40]
assert new_info["chase_count"] == 1 and new_info["orig_price"] == 0.50, "场景1: chase字段错误"
assert new_info["amount"] == 10.0 and abs(new_info["price"] - 0.55) < 1e-9
print("✅ 场景1: ask上移→撤旧挂新, chase_count=1, 继承ts")

# 场景2: 撤单瞬间竞态 FILLED → 记FILLED不重挂
oid2 = "0x" + "c" * 40
reset(oid2, age_min=10)
state = {"n": 0}
def status_seq(o):
    state["n"] += 1
    return "OPEN" if state["n"] == 1 else "FILLED"  # 第一次轮询OPEN, 撤后复查FILLED
bot._check_order_status = status_seq
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 0, f"场景2失败: 不应重挂 {placed}"
assert fills_recorded and fills_recorded[-1] == (oid2[:12], True, "FILLED"), f"场景2: 终态记录错误 {fills_recorded}"
print("✅ 场景2: 撤单竞态FILLED→记成交不重挂(无双重仓位)")

# 场景3: 累计涨幅超限: 首挂0.45→封顶0.60 涨0.15>0.12 → 放弃
oid3 = "0x" + "d" * 40
reset(oid3, age_min=10, price=0.45, orig=0.45)
bot._check_order_status = lambda o: "OPEN"
bot._check_liquidity = lambda tid, mp: (True, 0.65, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 0, f"场景3失败: 不应追价 {placed}"
print("✅ 场景3: 首挂0.45累计涨幅0.15>0.12→放弃追价")

# 场景4: chase_count=2 已达上限 → 不再追价
oid4 = "0x" + "e" * 40
reset(oid4, age_min=10, chase=2)
bot._check_liquidity = lambda tid, mp: (True, 0.60, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 0, f"场景4失败: {placed}"
print("✅ 场景4: chase_count=2达上限→不再追价")

# 场景5: ask 仅上移 0.01(<0.02) → 不动
oid5 = "0x" + "f" * 40
reset(oid5, age_min=10)
bot._check_liquidity = lambda tid, mp: (True, 0.51, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 0, f"场景5失败: {placed}"
print("✅ 场景5: ask上移0.01<0.02→不追(防频繁操作)")

# 场景6: ask 0.65 被封顶到 0.60, 首挂0.52 涨幅0.08→追价在0.60
oid6 = "0x" + "1" * 40
reset(oid6, age_min=10, price=0.52, orig=0.52)
bot._check_liquidity = lambda tid, mp: (True, 0.65, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 1 and placed[0][0] == 0.60, f"场景6失败: {placed}"
print("✅ 场景6: ask 0.65→封顶挂0.60(永不突破MAX_ORDER_PRICE)")

# 场景7: 未满8分钟 → 不追价
oid7 = "0x" + "2" * 40
reset(oid7, age_min=5)
bot._check_liquidity = lambda tid, mp: (True, 0.60, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 0, f"场景7失败: {placed}"
print("✅ 场景7: 未满8分钟→不评估追价")

# 场景8: 节流 180s 内二次评估 → 不重复追价
oid8 = "0x" + "3" * 40
reset(oid8, age_min=10, chase=1, price=0.55, orig=0.50)
bot._pending_orders[oid8]["last_chase_check"] = time.time() - 30  # 30s 前刚评估过
bot._check_liquidity = lambda tid, mp: (True, 0.60, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 0, f"场景8失败: {placed}"
print("✅ 场景8: 180s节流生效→不重复评估")

# 场景9: 挂价已顶 0.60 + ask 0.65 → 同价重挂无意义, 不动（8/21 17:3x 边界修复）
oid9 = "0x" + "4" * 40
reset(oid9, age_min=10, price=0.60, orig=0.58)
bot._check_liquidity = lambda tid, mp: (True, 0.65, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 0, f"场景9失败: 不应同价重挂 {placed}"
print("✅ 场景9: 挂价已顶0.60封顶+ask 0.65→不重挂(保持等跌模式)")

# ===== round37 条件封顶（高概率通道）=====
# 场景10: _effective_price_cap 纯函数
assert m._effective_price_cap({"probability_raw": 0.75}) == 0.70
assert m._effective_price_cap({"probability_raw": 0.65}) == 0.60
assert m._effective_price_cap({"probability_raw": 0.70}) == 0.70  # 边界值=门槛
assert m._effective_price_cap({"probability": 0.933}) == 0.70     # 旧格式退化 praw 缺失
assert m._effective_price_cap({"probability": 0.62}) == 0.60      # 现行 R2 封顶 0.67 → 不开通道
assert m._effective_price_cap({}) == 0.60                          # 全缺失 → 保守
assert m._effective_price_cap({"probability_raw": "abc"}) == 0.60  # 脏数据 → 保守
print("✅ 场景10: 条件封顶函数 7 分支全对")

# 场景11: 追价继承 price_cap=0.70 → 挂0.60 + ask 0.68 → 重挂 0.68（step 0.08≤0.12）
oid11 = "0x" + "5" * 40
reset(oid11, age_min=10, price=0.60, orig=0.60)
bot._pending_orders[oid11]["price_cap"] = 0.70
bot._check_liquidity = lambda tid, mp: (True, 0.68, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 1 and abs(placed[0][0] - 0.68) < 0.005, f"场景11失败: {placed}"
new_info = [v for k, v in bot._pending_orders.items() if k != oid11][0]
assert new_info.get("price_cap") == 0.70, f"场景11失败: price_cap 未继承 {new_info.get('price_cap')}"
print("✅ 场景11: 高概率通道追价 0.60→0.68 重挂成功 + price_cap 继承")

# 场景12: 默认 cap 0.60（回归）→ 挂0.60 + ask 0.68 → min(0.68,0.60)=0.60=挂价 → 不重挂
oid12 = "0x" + "6" * 40
reset(oid12, age_min=10, price=0.60, orig=0.60)
bot._check_liquidity = lambda tid, mp: (True, 0.68, "ok")
placed.clear()
bot._poll_pending_orders()
assert len(placed) == 0, f"场景12失败: {placed}"
print("✅ 场景12: 无 price_cap(默认0.60) + ask 0.68 → 不重挂(边界修复回归)")

print("\n全部 12 场景通过 ✅")
