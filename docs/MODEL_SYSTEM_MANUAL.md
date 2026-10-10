# Hermes BTC 本地模型系统说明书

> 生成日期：2026-09-05 | 维护：研究机（本地）+ 生产机（192.168.1.22 / D:\hermes-btc）
> 配套文档：`model/TRAINING_PLAN.md`（原始训练方案）、`ROADMAP_TOP_QUANT.md`（顶级量化路线图）

---

## 一、系统定位：这套模型是干什么的

**业务背景**：Polymarket 每小时开一个「Bitcoin Up or Down」二元合约市场（ET 整点锚定，close vs open 判定输赢）。现有交易引擎（hermes_1h_bot.py）在每小时 :40 左右决策下单。

**本地模型的角色**：用机器学习预测该小时 BTC 涨跌方向，输出 `P(up)` 概率。当前定位：

```
                 ┌──────────────────────────────────────┐
                 │        交易引擎（现有实盘链路）          │
                 │  hermes_collect → hermes_1h_bot → PM  │
                 └──────────────────────────────────────┘
                                  ↑ 不动（硬约束）
┌─────────────────────────────────────────────────────────────┐
│                  本地模型体系（影子模式）                        │
│  每 5 分钟一个决策点 → 预测 + 当时 PM 盘口价 → 写影子文件          │
│  目标：积累数据证明「模型概率 vs 市场价格」存在可持续正 EV          │
└─────────────────────────────────────────────────────────────┘
```

**核心硬约束**（写进项目记忆，不可违反）：
1. 300+ 影子样本前不碰实盘
2. 模型评估必须 walk-forward（随机切分仅允许在 gs 试点阶段，且已标注局限）
3. EV 必须用决策时点的实时 PM 价格计算
4. 旁路研究脚本不得修改现有交易链路

---

## 二、核心概念速查表

| 术语 | 含义 |
|---|---|
| **锚点（anchor）** | ET 整点开盘时刻，锚定价 = 该小时 BTC 开盘价，PM 合约的判定基准 |
| **决策点（dm, decision_min）** | 每小时 12 个时点 :00/:05/:10/.../:55，dm=N 表示 H+N 分钟时做预测 |
| **slug** | PM 小时合约唯一标识，如 `bitcoin-up-or-down-september-5-2026-7am-et`（ET 时区构造） |
| **影子文件** | `data/ml_shadow_predictions.jsonl`，每条 = 一次预测的完整现场（概率+盘口价+特征快照） |
| **死区（dead_zone）** | \|小时涨跌幅\| < 0.1% 的样本，方向噪声大，训练时剔除（约 15%） |
| **EV** | 期望价值。买 UP 的 EV = p·(1−pm_up) − (1−p)·pm_up，pm 价 ≤ 模型胜率才正 EV |
| **校准（calibration）** | 把模型原始分映射为真实概率的函数。v1 用 isotonic（阈值表），gs 用 Platt（sigmoid） |
| **盘感（gs）** | PM 盘口价衍生特征（pm_up_dec/pm_delta），让模型"看到"市场怎么定价 |

---

## 三、数据层：样本是怎么造出来的

### 3.1 数据源

| 数据 | 来源 | 频率/说明 |
|---|---|---|
| 1h/15m/5m/4h K线（含主动买量） | Binance Spot `api.binance.com`（墙内回退 `data-api.binance.vision` 镜像） | OHLCV + taker_buy_base + 成交笔数 |
| 资金费率 | Binance Futures `fapi.binance.com` | 8h 一条，全历史 |
| 恐惧贪婪指数 | alternative.me | 日频（生产直连不可达，实际训练时被剔除） |
| PM 盘口价 | Gamma API `gamma-api.polymarket.com` | 每决策点实时拉 |
| PM 盘口演变 | 影子文件自身（历史决策点的 pm_up） | 递归自举：预测时读之前 dm 写入的价 |
| 订单簿 CLOB 快照 | `binance_ob_snapshot.py` 旁路采集 | 5 分钟一次（计划任务 MicroSnap0-55） |

### 3.2 样本构造（backfill_data.py，防泄漏是第一设计原则）

每个样本 = 「某小时的某个决策点」上，**当时能知道的一切**：

```
时间轴：  H:00 ────── H+dm(决策点) ────────────── H+1:00
           │            │                          │
         锚定价      特征截止于此                    标签判定
      (小时开盘价)   只用已收盘K线               close vs open 方向
```

- **特征截止**：所有特征只用 `t_dec`（=H+dm 分钟）前**已收盘**的 K 线。4h 蜡烛必须 `开盘时刻+4h ≤ t_dec` 才可用，防止用到含 H 未来的信息
- **标签**：`label = 1 if close > open`（与 PM 结算口径一致）；`dead_zone = |ret| < 0.1%`
- **hist_up_rate**（同时段历史涨率）：expanding 均值 + `shift(1)`，严格只用过去
- **不可回溯特征**（订单簿、OI 变化、PM 实时价）：历史行 = NaN，LightGBM 原生处理缺失

### 3.3 数据集与训练-推理一致性（防漂移的关键）

```
model/data/dataset{suffix}.parquet     # suffix: "" = dm15, "_dm35" = dm35, ...共 12 份
model/data/manifest{suffix}.json       # 特征表 + 元信息
model/data/raw/*.parquet              # 原始K线缓存
```

**训练与推理共用同一份特征代码**：`backfill_data._precompute()/_row()` 被 `predict_hourly.py` 直接 import。特征口径只有一份实现，从结构上杜绝"训练用 A 公式、推理用 B 公式"的漂移。

当前规模：每个 dm 约 900 行（近 60 天）× 34 特征。

---

## 四、特征体系（34 个特征全表）

| 类别 | 特征 | 说明 |
|---|---|---|
| **量价偏差** | dev_pct | 决策点价 vs 锚定价偏差%。**动量特征，争议核心：市场已定价** |
| | volatility_5m | 近 12 根 5m 收盘 pct-change 标准差 |
| | pos24h_percentile | 现价在 24h 窗口的位置百分位 |
| | range_24h_pct | 24h 振幅 % |
| **主动买卖流** | taker_1h_buy_pct | 近 60 分钟 taker 买占比 |
| | taker_5m_last / taker_15m | 最新 5m / 决策窗 15m 的 taker 买占比 |
| | imb_5m / imb_15m | 最新 5m / 近 3 根 5m 订单不平衡 |
| **K线动能** | kline_15m / kline_1h | 已收盘 15m / 1h 蜡烛方向（c[-1] vs c[-3]） |
| | momentum_burst | 15m 收益 z-score。**动量特征，争议核心** |
| | macro_up_1h / macro_up_4h | 决策点前 6 根已收盘 1h / 4h 阳线计数 |
| **技术指标**（H-1 收盘） | rsi_1h / rsi_4h | Wilder RSI(14) |
| | bb_pos_1h / bb_pos_4h | 布林带位置 0-100 |
| | macd_1h_hist / macd_4h_hist | MACD 柱 |
| | vol_ratio_1h | 1h 量 / 20 期均量 |
| **惯性** | prev_chg_pct / prev_consec | 上一小时涨跌 / 连续同向小时数 |
| | roll_up_6h / ret_24h | 近 6h 阳线占比 / 24h 涨跌幅 |
| | hist_up_rate | 该 ET 小时历史上涨率（expanding, shift1） |
| **环境** | funding_rate | 决策点前最近资金费率 |
| | fear_greed | 恐惧贪婪指数（生产不可达，训练时剔除） |
| **季节性** | hour_sin / hour_cos | ET 小时周期编码 |
| **开盘形态** | open_dir / open_range_pct / open_vol_ratio | [H:00, H:15] 蜡烛的方向/振幅/量比 |
| **盘感代理**（gs 专属） | pm_up_dec | 决策点最新 PM UP 盘口价（市场对涨跌的定价） |
| | pm_delta | pm_up 演变 = 最新 − 该小时最早（压价/拉升过程） |

---

## 五、模型层：4 类模型各司其职

| 模型 | 文件（registry/） | 训练方式 | 校准 | 角色 |
|---|---|---|---|---|
| **v1** | lgbm{suffix}_v1.txt | walk-forward（训 12 月→验 2 月→测 1 月，逐月滚动），**部署物 = 最后一个窗口的模型** | isotonic 阈值表（np.interp 插值） | 纯 K 线基线，历史口径 |
| **gs** | lgbm{suffix}_gs_v1.txt | 随机 80/20 分层切分（试点阶段，已标注局限；pm 特征只最近 ~2 天有值，时间切分会全 NaN） | Platt = LogisticRegression(raw_score)，sigmoid(coef·x+intercept) | v1 特征 + 盘感 2 特征。**9/4 已转正为主输出 p_model** |
| **gs_nomtm** | lgbm{suffix}_gs_nomtm_v1.txt | 同 gs，另剔除 dev_pct / momentum_burst | 同 gs（Platt） | **A/B 对照组**：检验剔除动量后还剩多少真本事 |
| **LSTM** | lstm{suffix}_v1.pt | 168 根 1h K线 × 8 序列特征 + 静态特征，2 层 LSTM(64)，AMP fp16 | 无（直接 sigmoid） | 辅助参考，长期表现不佳（过拟合） |

**超参（gs 系列，小样本防过拟合）**：num_leaves=16, max_depth=5, lr=0.05, min_child_samples=40, feature/bagging_fraction=0.8, 正则 α=0.3/λ=2.0，早停 100 轮。

**校准演进记录**：v1 时代用 isotonic → 9/5 审计发现小样本下阶梯化失真（15.9% 输出饱和 1.0）→ gs 系列换 Platt（2 参数抗过拟合），metadata 带 `kind` 字段供推理端按类型还原。

### 模型关系图

```
                     影子文件(ml_shadow_predictions.jsonl)
                              │ 每小时 12 个 dm 各写一条
        ┌─────────────────────┼─────────────────────┐
        v1: p_up          gs: p_gs              gs_nomtm: p_gs_nomtm
     (纯K线基线)      (盘感+全特征,主输出)        (盘感-动量,对照组)
        p_model = p_gs(缺失时回退 p_up)              │
        └───────────────── EV 计算 ─────────────────┘
                 与 PM 价对比 → 2 周后裁决「动量特征去留」
```

---

## 六、推理链路：predict_hourly.py 逐步分解

**触发**：Windows 计划任务 HermesBTC1HModelPredXX，每 5 分钟一次，决策点后 1 分钟运行（dm35 → :36 触发）。

```
1. 参数解析：--decision-min N（0~55 十二选一），--hours-ago（复盘用）
   live 运行强制绕过K线缓存（防 dm15 留下的半截缓存污染 dm35）

2. 拉数据：Binance 1h/15m/5m/4h K线 + 资金费率 + FNG
   （失败即退出，不写垃圾记录）

3. 构造特征：_precompute + _row（与训练同一份代码）
   → 特征行 r（34 列）

4. v1 推理：Booster.predict → isotonic 插值 → p
   NaN 兜底 0.5（防污染影子文件）

5. LSTM 推理（可选）：p_lstm，文件缺失/异常不阻断

6. PM 盘口：Gamma API 拉 pm_up/pm_down（记 null 不阻断）

7. 盘感特征：fetch_pm_feats(slug, dm) 读影子文件里
   当小时 ≤dm 的所有 pm_up → pm_up_dec / pm_delta

8. gs 推理：r + 盘感2特征 → Platt sigmoid → p_gs
   gs_nomtm 推理：同上但特征表不含动量 → p_gs_nomtm

9. 主输出：p_model = p_gs ?? p_v1（9/4 拍板转正）

10. EV：fair_up=p_model, ev_up = p·(1−pm_up) − (1−p)·pm_up

11. book_momentum：读订单簿快照 → CVD/诱空大单 flag（旁路信号）

12. 幂等写入影子文件（同 slug+dm 已存在则跳过）
    （文件历史遗留 UTF-8 BOM，读取须 utf-8-sig）
```

**影子文件一行记录包含**：时间戳、slug、锚定价、p_model/方向、p_up（v1 对照）、p_lstm、p_gs、**p_gs_nomtm**、pm 特征、pm 盘口价、fair/EV、dev_pct/momentum_burst（供事后审计）、book_momentum。

---

## 七、评估体系：怎么判断模型有没有真本事

### 7.1 三层评估哲学

```
第1层  AUC/ACC/Brier     —— 模型分得准不准？（判别力）
第2层  校准分桶           —— 模型说的概率靠不靠谱？（说 0.7 真有 70% 吗）
第3层  EV 结算 paper PnL  —— ★唯一真相★ 按模型方向+PM价下单能赚钱吗？
```

**为什么第 3 层是唯一真相**：dm35 的 dev_pct 信息，PM 做市商同样看得到——他们 :35 的报价已经把这个信息定价进去了。AUC 0.93 但 EV 为负 = 模型只是复读市场。这正是 gs_nomtm 对照实验要回答的问题。

### 7.2 cross_validate_shadow.py（双周裁决脚本）

```
输入：影子文件（--dm 35 或其他决策点）
  ↓ settle()：Binance 镜像拉 anchor+1h 收盘价 → label
  ↓ （缓存键 slug|anchor_time_utc，同 slug 各 dm 行 anchor_price 一致，键安全）
  ├─ v1 / gs / gs_nomtm 各自的 n / AUC / ACC / Brier / hit@0.65
  ├─ gs vs v1 同窗对比（按行配对，防位置切片错位）
  ├─ 按天分组稳定性
  ├─ 校准分桶（6 桶，bias>0.1 标警）
  └─ EV 结算：全出手 PnL / 仅正 EV 出手 PnL
输出：model/data/cv_report.json
```

### 7.3 双周裁决机制（2026-09-05 上线）

- 影子字段 `p_gs_nomtm` 自 2026-09-05 11:00 UTC 起在生产积累，每 dm 每天 24 条
- 定时任务每月 1 号、15 号跑裁决（cron 无法表达严格双周，此为最接近方案）
- **裁决标准**：
  - gs 显著优于 gs_nomtm → 动量信号有效，保留
  - gs_nomtm 追平或胜出 → 原高性能只是市场已定价信号，建议剔除动量
  - 任一 dm 样本 < 100 → 只汇报趋势不下结论

---

## 八、文件地图（谁干什么）

| 文件 | 职责 |
|---|---|
| model/backfill_data.py | 数据集构造（K线拉取+特征+标签+防泄漏），`_precompute/_row` 是训练推理共用的特征唯一实现 |
| model/train_lgbm.py | v1 训练（walk-forward），产物 registry/lgbm*_v1.txt |
| model/train_lgbm_gs.py | gs / gs_nomtm 训练（--no-momentum 切换），产物 lgbm*_gs[_nomtm]_v1.txt |
| model/train_lstm.py | LSTM 训练 |
| model/predict_hourly.py | 影子推理主程序（每 5 分钟） |
| model/cross_validate_shadow.py | 影子数据裁决（AUC/校准/EV 结算） |
| model/ev_watch.py | EV 仪表盘（模型 fair 价 vs PM 市价的持续监控） |
| model/pm_traj_feats.py | PM 多时点轨迹特征计算（含 --max-dm 防泄漏） |
| model/ablate_features.py | 消融实验（发现动量依赖的功臣） |
| binance_ob_snapshot.py | 订单簿/CVD 旁路采集（计划任务 5 分钟一次） |
| hermes_common.py | 公共库：slug 构造、fetch_pm_feats、fetch_book_momentum、HTTP（代理自适应） |
| data/ml_shadow_predictions.jsonl | 影子预测主数据（累积中） |
| data/book_snapshots.jsonl | 订单簿快照（累积中） |

**部署流水线**：研究机 commit+push → GitHub → 生产机 `git pull` → 生产机重训（git 身份 tangyijun@local）→ 计划任务自动加载新模型（registry 文件名不变）。

---

## 九、当前状态与已知问题（2026-09-05）

### 已验证的表现

| 指标 | 数值 | 说明 |
|---|---|---|
| v1 walk-forward（dm35，106 窗） | AUC=0.872 / ACC=0.793 / Brier=0.154 | 但主要由动量驱动 |
| 剔除动量后（消融） | **AUC=0.541 ≈ 随机** | 核心问题：未找到独立 alpha |
| gs vs v1 同窗 23 窗 | ACC 0.826 vs 0.739 | gs 略胜 |
| 仅正 EV 出手 paper PnL | +2.56 | 100 样本，样本太小仅供参考 |

### 已知局限（诚实清单）

1. **gs 用随机切分**（试点阶段妥协）：盘感特征只有最近 2 天有值，walk-forward 训练窗全 NaN。样本积累后必须回归时序切分
2. **Platt 校准在验证子集上拟合**：小样本无奈之举，样本大了要换干净的校准窗
3. **0.65-0.70 校准桶 bias=-0.327**（n=6）：小样本偏差，持续监控
4. **LSTM 长期弱于树模型**：过拟合，暂无独立价值
5. **EV 用 Gamma mid 价**：非可成交价，未建模价差/滑点
6. **fear_greed 生产直连不可达**：特征实际未用
7. **60 天样本**：远低于 TRAINING_PLAN 目标的 18 个月
