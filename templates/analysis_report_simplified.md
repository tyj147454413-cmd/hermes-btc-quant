# Hermes BTC 1H 分析报告 — YYYY-MM-DD HH:00 ET (Xam/pm-et)

**信号已落盘并被 bot 消费完毕**

## 0. 本轮结论速览

| 项 | 值 |
|---|---|
| slug | bitcoin-up-or-down-<月>-<日>-<年>-<时>am/pm-et |
| 方向 | UP / DOWN |
| 净分 S₀(+PM行) | ±N (S₀=±N, PM行=±N) |
| 概率 | 引擎机算 0.xx |
| 置信度 | 高/中/低（约束来源：xxx） |
| EV | ±0.xx |
| 动作 | ORDER / SKIP |
| 一句话理由 | xxx |

## 1. 数据情境

锚定 $xxxxx（来源：Binance xxx）/ 当前价 $xxxxx（xx 贴线/非贴线）/ 波动率 5m x.xx%（>0.3% 锁置信度中/低）/ 距结算 ~xxmin（xx 期）/ Regime：摆动市/趋势市（G3 豁免可用/不触发）/ 数据完整性 G1 全 ✓。

## 2. last-check

Binance BTC/USDT 最新价 **$xxxxx** vs 快照 $xxxxx → Δdev=**x.xxxx%** < 0.10%,**v1 未刷新直接写** / 或 ≥0.10% v2 refreshed 方向 X→Y / 或三次振荡 SKIP。

## 3. 因子读数与计分

| 因子 | 读数 | 计分 |
|---|---|---|
| 价格位置 ×1 | 锚定偏差 ±0.xxx%（\|偏差\|≥0.10% 选边计 ±1；<0.10% 或 PM 弱共识计 0，非 24h 分位） | ±1/0 |
| K线序列 15m窗 ×1 | last_4_candles=阳阳阴阴 → 阳多 | +1 |
| K线序列 1H窗 ×1 | last_4_candles=阳阴阴阴 → 阴多 | -1 |
| Taker B/S 5m窗 ×1 | 3.54 → 买占比 78% > 2.33 | +2 |
| Taker B/S 15m窗 ×1 | 0.39 → <0.43 机构级卖压 | -2 |
| 订单流 5m/15m ×1 | -0.26/-0.26 双窗卖压共振 | -1 |
| momentum_burst ×2 | vol_ratio=1.82 + recent_dir=UP（≤1.5 写数值+不触发，禁标数据缺） | +2 |
| PM 动量 ×2（round44.1） | up_price_delta=+0.035（\|Δ\|≥0.02 触发，嵌套字段 polymarket.* 勿漏读） | +2 |
| 15m MACD（仅参考） | +5.36 | 不计分（round41.8.9 废除方向计分） |
| ...（短反弹先兆/季节性/开盘微观/宏观趋势按矩阵生效状态） | ... | ... |

**S₀ = -1+1-1+2-2-1+2+2 = +2 → UP**

## 4. 门槛 G1-G6

- G1 ✅ 数据完整
- G2 ✅ PM<80%
- G3 ✅ PM<60%
- G4 ✅ |dev|=0.022%<0.15%
- G5 中性档略偏诱多
- G6 ✅ 置信度已锁低
- H12 ✅ 1H/4H MACD 不同向

## 5. 概率映射

|净分|=1 → 0-2 档 + 非贴线 + 逆 PM → **30.8% 低**（round41.2 重校表值；一律以引擎回写 probability_raw 为准，禁凭记忆引旧值）

## 6. 上小时复盘

**结算**：<上小时slug> 锚定 $xxxx → 当前 $xxxx = **UP/DOWN** (+x.xx%)
**AI预测** UP/DOWN vs 实际 → **WIN/LOSS** | P&L = **±$x.xx** (entry=0.xx, status=matched, order_id 0x...)

**归因四件套**：

- ✅ 对的因子表：xxx 因子读数 + 贡献分 + 如何被验证
- ❌ 错因子表：xxx 因子读数 + 误导方向 + 为什么错
- 🎯 关键决策点：G5/H9/H12/G1-G6 哪个决定性
- 📚 教训 (3-5 条)：
  1. 一句话一条,具体到博弈期订单流 > Taker 比率这类可操作规则
  2. ...
  3. ...

## 7. 风险点

- ⚠️ xxx
- ⚠️ xxx
- ⚠️ xxx
- ⚠️ xxx
- ⚠️ G1-G6 全部通过,无强制翻向

---

**📁 文件落盘位置**：

- [分析报告](computer:///Users/tangyijun/Desktop/hermes%20btc/data/reports/analysis_report_<HHMM>_<YYYYMMDD>.md)
- [X小时 复盘笔记](computer:///Users/tangyijun/Desktop/hermes%20btc/BTC_1H_Trade_Journal/YYYY-MM-DD_HH00_复盘.md)
- [本轮笔记](computer:///Users/tangyijun/Desktop/hermes%20btc/BTC_1H_Trade_Journal/YYYY-MM-DD_HH00_复盘.md)
- 信号 JSON：`data/claude_signal.jsonl`（已 bot 消费）

**上小时战绩：<X>PM WIN/LOSS ±$x.xx**