# BTC 1小时涨跌预测 — 本地模型训练方案

> 制定日期：2026-09-01
> 硬件：i7-6700K 4C8T / 32GB DDR4-2133 / RTX 3060 / Win10 专业工作站版
> 现有基础：hermes-btc 引擎体系（btc-agent 信号源）、`data/features/hermes_features_*.csv`、`ml_dataset.csv`（333 行）、`ml_shadow_predictions.jsonl`、`wf_reports/`
> 核心原则：**先扩数据、影子先行、融合而非替换、随时可回退**

***

## 一、硬件资源评估与优化建议

| 资源  | 现状                                                          | 评估                               | 用途分配                          |
| --- | ----------------------------------------------------------- | -------------------------------- | ----------------------------- |
| GPU | RTX 3060（WMI 显示 4GB 是 32 位上限截断，实际应为 12GB，用 `nvidia-smi` 确认） | 深度模型训练主力                         | LSTM/TCN 训练、Optuna 搜索         |
| CPU | i7-6700K 4C8T @4.0GHz                                       | 弱项，深度学习数据加载易瓶颈                   | LightGBM/特征计算尚可               |
| 内存  | 32GB DDR4-2133                                              | 充裕（5 年 1h 数据约 4.4 万行，全部内存计算毫无压力） | 全数据集常驻内存                      |
| 存储  | C 盘 SSD 275GB（余 53GB）+ D 盘 HDD                              | 数据集小（<100MB）                     | 模型+数据放 `D:\hermes-btc\model\` |

**优化建议**：

1. BIOS 开启 **XMP 内存超频**（Z170 支持，2133→3000+），特征计算提速 20-30%
2. PyTorch 安装 CUDA 12.1 版，训练用 **AMP fp16**（Ampere 架构有效）
3. 数据加载用 pandas 向量化 + 预生成特征落盘，避免训练时实时计算（弥补 CPU 弱点）
4. 训练时段避开整点引擎信号生成窗口（xx:55\~xx:05），CPU 抢占不影响 bot

**预期成果**：LightGBM 单次训练 <1 分钟；深度模型单次 <15 分钟（3060 fp16）。

## 二、数据获取与预处理流程

**现状问题**：`ml_dataset.csv` 仅 333 行样本，统计上不足以支撑稳健模型（现有 LR 结果置信区间太宽）。**扩数据是本方案第一优先级**。

### 2.1 数据源与采集（扩到 18 个月以上）

| 数据                                        | 来源                                                     | 获取方式                                                         |
| ----------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------ |
| 1h K线（OHLCV+quote vol+taker buy vol+成交笔数） | Binance Spot `GET /api/v3/klines`                      | 历史回补脚本一次拉全 + 复用现有 `HermesBTC1HCollect`                       |
| 15m K线（滚动窗口特征）                            | Binance                                                | 同上                                                           |
| 资金费率、持仓量                                  | Binance Futures API `fundingRate` / `openInterestHist` | 历史回补（合约情绪因子）                                                 |
| PM 市场价格/赔率                                | 复用 `data/pm_prices/pm_*.jsonl`                         | 已有                                                           |
| 标签                                        | PM 结算结果                                                | 已有（`hermes_results.jsonl`），历史回补用 Binance 1h close 判定 up/down |

**历史回补脚本** `model/backfill_data.py`：按小时对齐生成逐样本行（与现有 `hermes_features` 列结构一致，向后兼容），目标 **≥10,000 样本**（18 个月 × 720 小时/月，扣除无效时段）。

### 2.2 特征体系（三个层级）

1. **量价类**（现有 23 个特征全保留）：price\_position、taker\_bs、kline\_15m/1h、imb\_5m/15m、volatility\_5m、open\_micro、dev\_pct 等
2. **新增时序类**：多周期收益率(1h/4h/24h)、滚动波动率(24h/72h)、成交量 z-score、taker buy ratio 24h 均值、连续同向小时数、距离 24h 高/低点位置
3. **新增环境类**：资金费率、OI 变化率、小时/星期周期编码（sin/cos）、PM 价格（若可获取则作为强特征）

### 2.3 标签定义与防泄漏

- 主标签：该小时 **close vs open** 方向（与 PM 结算一致，对齐结算时刻）

- 增强：死区标签——|涨跌| < 0.1% 的样本剔除（约减少 15% 样本，显著降噪）

- **严格防泄漏**：所有特征只用 t 时刻前数据；标准化参数只在训练折内 fit；walk-forward 切分禁用 shuffle

**预期成果**：`model/data/dataset.parquet`，≥10,000 行 × \~40 特征，附数据质量报告。

## 三、模型架构选择与设计

| 模型                 | 角色   | 理由                                                      |
| ------------------ | ---- | ------------------------------------------------------- |
| **LightGBM（主力）**   | 基础模型 | 表格数据 + 中小样本量下最强；CPU 4C8T 足够；可解释（特征重要性直接对接现有因子分析习惯）      |
| **小 LSTM/TCN（辅助）** | 时序模型 | 输入最近 168h 的标准化量价序列，3060 fp16 训练很快；捕捉 LightGBM 难以表达的序列形态 |
| **融合层**            | 最终输出 | 验证集上按逆 Brier 加权，或简单规则：两模型概率均值，分歧大（差>0.2）时降置信度           |

输出统一为 `P(up)` 概率 + 校准（isotonic regression 在验证集拟合），**保证输出可直接映射到引擎的 prob/EV 框架**。bot 侧无需改动信号处理逻辑（source 仍为 `btc-agent` 白名单）。

**不建议**：Transformer 大模型、AutoML 重搜索——样本量（万级）和数据频率决定了简单模型是正确选择。

## 四、训练参数配置

**LightGBM**（CPU，10 分钟内完成含调参）：

```python
params = dict(
    objective='binary', metric='auc',
    num_leaves=31, max_depth=6,          # 小样本防过拟合
    learning_rate=0.03, n_estimators=2000,
    early_stopping_rounds=200,
    min_child_samples=150,               # 约束叶节点最小样本
    feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
    reg_alpha=0.1, reg_lambda=1.0,
)
```

**LSTM**（3060 fp16）：

- 输入：168h × 8 特征（标准化的收益率/量/波动/RSI/MACD/资金费等）

- 结构：2 层 LSTM(hidden=64, dropout=0.3) → FC → sigmoid

- batch=256，AdamW(lr=1e-3, weight\_decay=1e-4)，cosine 调度，最多 50 epoch + 早停(patience=8)

**Walk-forward 切分**（复用现有 `wf_reports/` 机制）：

- 训练窗 12 个月 → 验证窗 2 个月（早停+校准）→ 测试窗 1 个月

- 测试窗逐月滚动前进，最后拼接全部测试窗得真实 OOS 评估

## 五、评估指标设定

| 层级          | 指标                                       | 门槛                                        |
| ----------- | ---------------------------------------- | ----------------------------------------- |
| 判别力         | OOS AUC                                  | **≥0.58 接受，≥0.62 良好**（1h 方向极难，>0.65 需查泄漏） |
| 校准度         | Brier score、可靠性曲线                        | 优于基准频率校准                                  |
| 分方向         | up/down 各自 acc                           | 两方向都不低于 55%                               |
| **决策力（核心）** | 在 prob≥0.60/0.65/0.70 阈值下的 hit rate 与 EV | 与引擎同期信号对比                                 |
| 端到端         | 接入 bot 规则的 paper 回测（含 0.65 首挂/追价/撤单全流程）  | 净 EV 优于现行引擎规则                             |

**关键对比设计**：模型和现行引擎对**同一时段**各自出信号，利用已有的 `hermes_results.jsonl` 结算数据做 A/B，影子预测写 `ml_shadow_predictions.jsonl`（机制已存在）。至少积累 **300 个影子样本**才允许决策切换。

## 六、预测系统实现步骤

```
D:\hermes-btc\model\
├── TRAINING_PLAN.md      # 本方案
├── backfill_data.py      # 历史数据回补
├── features.py           # 特征计算（复用/扩展 hermes_features 逻辑）
├── train_lgbm.py         # LightGBM 训练+walk-forward评估
├── train_lstm.py         # LSTM 训练
├── calibrate.py          # isotonic 校准
├── registry\             # 模型版本（lgbm_v1.txt, lstm_v1.pt, metadata.json）
├── predict_hourly.py     # 每小时推理，输出引擎信号格式
└── data\                 # dataset.parquet, 回补缓存
```

**实施步骤**：

1. **第 1 步（数据）**：跑 `backfill_data.py` 回补 18 个月数据，生成 dataset.parquet + 质量报告
2. **第 2 步（训练）**：`train_lgbm.py` 完成 walk-forward 评估；达标后再训 LSTM
3. **第 3 步（影子接入）**：`predict_hourly.py` 注册为定时任务 `HermesBTC1HModelPred`，**每小时 :15-:16（dm15 模型）与 :35-:36（dm35 模型）各跑一次**（决策点=H+15/:35，特征只用已收盘 K线），预测连同\*\*当时 PM 盘口价（pm\_up/pm\_down）\*\*一起写 `ml_shadow_predictions.jsonl`，**不动现有交易链路**。bot 实际 :40 开始分析下单（analysis\_result.json 时间戳佐证），两个模型预测均在其之前就绪
4. **第 4 步（评估裁决）**：累积 ≥300 影子样本后，跑对比报告（复用 `_g35_backtest.py` 风格），**以「模型 P 与当时 PM 价格的背离」计算真实可交易 EV**（AUC 高≠能赚钱：:40 时 PM 价格已反映同样信息， dm35 OOS AUC 0.926 主要由 dev\_pct@:35 驱动，须扣除价格因素后仍有正 EV 才算有效）；若稳定优于引擎 → 进入第 5 步
5. **第 5 步（融合上线）**：选定模型概率与引擎概率融合后作为最终 prob 写信号文件，bot 侧零改动；保留降级开关（模型异常时回退纯引擎）

**预期成果**：全程约 2-3 周数据积累+开发；第 3 步后即有无风险的影子对比数据可看。

## 七、模型迭代优化策略

1. **再训练节奏**：每月滚动重训（walk-forward 顺延一个月），自动更新 registry，保留最近 3 版可回滚
2. **漂移监控**：滚动 200 样本的 AUC 与 hit rate 低于门槛时告警（复用现有 `drift_alerts.jsonl` 机制）
3. **特征迭代**：每月基于 LightGBM importance + 现有因子归因报告（`hermes_factor_attribution_*`）淘汰末位特征、试验新特征，一次只改一处
4. **超参搜索**：Optuna（3060 上跑 LSTM 搜索，LightGBM 用 CPU 50 trial），每月一次，防止过拟合调参
5. **数据增强方向**（按优先级）：资金费率/OI（已列）→ Binance 订单簿深度快照 → 现有 `BTC_1H_Trade_Journal` 复盘笔记的结构化经验规则 → 高级（tick 级）仅在前面用尽后考虑
6. **失败复盘闭环**：模型与影子记录的错判样本自动生成日报（对接现有 review 体系），人工审查后转新特征假设

***

## 环境准备（附录）

```powershell
# 在现有 .venv 中补充训练依赖
D:\hermes-btc\.venv\Scripts\pip install lightgbm duckdb pyarrow optuna
D:\hermes-btc\.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cu121
nvidia-smi   # 确认 CUDA 可用与实际显存
```

