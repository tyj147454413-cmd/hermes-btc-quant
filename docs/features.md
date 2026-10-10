# Hermes 特征字典

> 由 `scripts/feature_store.py --emit-dict` 自动生成，请勿手改列名表；
> 改特征定义请改 feature_store.py 的 SPEC 后重新生成。

- 一行 = 一个 slug（一个 1H 合约），取该 slug 的信号窗口快照（pick 模式见脚本头注释）。
- 标签列来自 `data/hermes_results.jsonl`：side=信号方向，actual=实际结算方向，
  win=1/0，buy_price=bot 成交价，pnl=模拟盈亏。
- 缺失值统一为空（NaN）；bool 因子 1/0；方向因子 UP=+1/DOWN=-1。

## 价格与偏差

| 列 | 含义 |
|---|---|
| anchor_price |  |
| current_price |  |
| deviation_pct |  |
| volatility_5m |  |
| dev_expanding |  |
| dev_stable |  |
| dev_shrinking |  |

## 24h 价格位置

| 列 | 含义 |
|---|---|
| pos24h_percentile |  |
| pos24h_upper_half |  |
| chg_pct_24h |  |

## 资金面（Binance fapi）

| 列 | 含义 |
|---|---|
| taker_bs_ratio |  |
| taker_bs_avg_1h |  |
| oi_change_24h_pct |  |
| long_short_ratio |  |
| ob_bid_vol_top10 |  |
| ob_ask_vol_top10 |  |
| ob_wall_ratio |  |
| flow_imbalance_5m |  |
| flow_imbalance_15m |  |

## 情绪与历史季节性

| 列 | 含义 |
|---|---|
| fear_greed |  |
| hist_up_rate |  |
| hist_samples |  |

## Polymarket 盘口

| 列 | 含义 |
|---|---|
| pm_up |  |
| pm_down |  |
| pm_volume |  |
| pm_liquidity |  |

## 关键位距离

| 列 | 含义 |
|---|---|
| dist_weak_support_pct |  |
| dist_weak_resist_pct |  |

## 开盘微观形态

| 列 | 含义 |
|---|---|
| open_dir |  |
| open_range_pct |  |
| open_vol_ratio |  |
| open_fakeout |  |
| open_vreversal |  |
| vreversal_15m |  |

## 上一小时惯性

| 列 | 含义 |
|---|---|
| prev_side |  |
| prev_chg_pct |  |
| prev_consec_up |  |
| prev_consec_down |  |
| prev_flips |  |

## 宏观趋势族（round41+）

| 列 | 含义 |
|---|---|
| macro_up_count |  |
| macro_down_count |  |

## OI-价格关系

| 列 | 含义 |
|---|---|
| oi_rel_signal_neutral |  |

## 时间框架因子（6 个 TF × 9 个指标 = 54 列）

列名格式 `{指标}_{tf}`，tf ∈ 1m/5m/15m/1h/4h/1d（快照内为 Binance K 线）。

| 指标 | 含义 |
|---|---|
| rsi_* | RSI(14) |
| bb_pos_* | 布林带位置% |
| vol_ratio_* | 成交量比率 |
| macd_hist_* | MACD 柱 |
| macd_accel_* | MACD 加速度(hist-前值) |
| recent_dir_* | 最近方向 UP+1/DOWN-1 |
| consec_up_* | 连续上涨根数 |
| consec_down_* | 连续下跌根数 |
| taker_pct_last_* | Taker 主动买占比末值 |

## 其他（未分组）

bb_pos_1m, vol_ratio_1m, macd_hist_1m, macd_accel_1m, recent_dir_1m, consec_up_1m, consec_down_1m, taker_pct_last_1m, bb_pos_5m, vol_ratio_5m, macd_hist_5m, macd_accel_5m, recent_dir_5m, consec_up_5m, consec_down_5m, taker_pct_last_5m, bb_pos_15m, vol_ratio_15m, macd_hist_15m, macd_accel_15m, recent_dir_15m, consec_up_15m, consec_down_15m, taker_pct_last_15m, bb_pos_1h, vol_ratio_1h, macd_hist_1h, macd_accel_1h, recent_dir_1h, consec_up_1h, consec_down_1h, taker_pct_last_1h, bb_pos_4h, vol_ratio_4h, macd_hist_4h, macd_accel_4h, recent_dir_4h, consec_up_4h, consec_down_4h, taker_pct_last_4h, bb_pos_1d, vol_ratio_1d, macd_hist_1d, macd_accel_1d, recent_dir_1d, consec_up_1d, consec_down_1d, taker_pct_last_1d

## 标签列

| 列 | 含义 |
|---|---|
| side | AI 信号方向 UP/DOWN |
| actual | 实际结算方向 UP/DOWN |
| win | 1=方向对，0=错 |
| buy_price | bot 成交价（PAPER 模式为挂单价） |
| filled | 是否成交 1/0 |
| pnl | 模拟盈亏（美元） |
| strategy_version | 规则版本锚点 |
