# AI 人脑定价层 · 实施方案（2026-09-04 保存，待新对话拍板）

> 状态：**方案设计完成，代码未动**。用户计划换新对话继续讨论，本文件为唯一方案载体。
> 决策点见文末"三个必须拍板的点"，拍板后按此实施。

***

## 一、动机：8pm 小时的教训（为什么要有这层）

2026-09-04 20:40 ET（8pm 小时），模型 dm40 给出强信号 P(涨)=0.8077，但：

- 20:42 BTC 现货从 81,428 **急跌**至 81,126（模型特征滞后，抓不到）

- 20:45 实时盘口 Up=0.695，20:48 崩到 0.355

- 最终结算：开盘 81,270 vs 收盘 80,972 = **DOWN**，模型看涨错了

**核心问题：模型只在"决策时点"打一次静态概率，看不到盘口演变与实时资金动向。** bot 又按 round52.4 直接吃实时 ask，可能吃到远高于真实胜率的价格 → 负 EV。

**目标：每窗在 bot 下单前，由 AI（人脑）读取「模型演变 + 引擎信号 + 实时盘口」三层信息，输出一个结构化挂单建议（合理价 fair\_price、方向、动作），bot 按开关消费。**

***

## 二、现有链路（不动，作为背景）

### 1. 引擎信号（信号来源）

- 每小时 :40 前，`btc-agent` 引擎分析（final\_analysis.py 系列）写信号到 `data/claude_signal.jsonl`

- 信号字段：`slug / source / action(ORDER|SKIP) / side / probability_raw / max_price / price_cap / pm_up_price / confidence / ev / reason / strategy_version`

- 示例见 `data/hermes_last_signal.json`（9/3 8pm：side=UP, probability=0.67, max\_price=0.76, price\_cap=0.8, pm\_up\_price=0.565）

### 2. bot 下单（hermes\_1h\_bot.py）

- `process_signal()`（L1016 起）消费信号 → 计算 `order_price`（L1246-1297）

- **round52.4 关键规则（9/2 用户拍板）：首挂直接吃实时 ask，成交率优先**

  - `best_ask ≤ 0.99` → `order_price = round(best_ask, 2)`（直接吃价成交）

  - book 缺失才回退 `min(max_price, gamma_price)` 封顶兜底

  - 已知代价：成交价 = 实时 ask，可能高于引擎 praw（负 EV），成交率优先于单笔 EV

- 其他守卫：目标方向实时价 < 0.25 → 极端反转 SKIP；最终成交价 < 0.25 → 硬底线 SKIP

- 恢复单需含 `price / token_id / chase_count` 键（追价链用），本方案不涉及追价链

***

## 三、模型影子系统现状（AI 的输入源，已就绪）

- 预测文件：`data/ml_shadow_predictions.jsonl`

- **12 个决策时点全覆盖**：:00 / :05 / :10 / :15 / :20 / :25 / :30 / :35 / :40 / :45 / :50 / :55

- 计划任务：`HermesBTC1HModelPred0~55`（每小时 :01/:06/:11/:16/:21/:26/:31/:36/:41/:46/:51/:56 触发）

- 每条记录字段（新版本）：`ts / slug / decision_min / p_up(LGBM) / p_lstm / pred_dir / pm_up / pm_down / fair_price_up / fair_price_down / ev_up / ev_down / model_version`

- 幂等去重：同 slug + decision\_min 不重复写入

- 现有验证数据：

  - dm15：66 样本，命中 62.1%，强信号 76%

  - dm35：64 样本，命中 79.7%，强信号 78%，买涨 EV +0.073（影子口径）

  - 引擎 vs dm35 对比：48 小时命中均 79.2%，方向一致率 95.8%

  - 模型 EV 信号视图：dm35 买涨可买时命中 82%、EV +0.167（data 口径）

***

## 四、方案架构（AI 人脑定价层）

```
每小时一窗：
:36 前 ── 模型影子预测已自动跑完 12 时点演变的可用时点
:39   ── ① AI 决策端触发
          ▼ 读三样输入
          A. 该小时模型演变表（已有时点的 p_up/p_lstm/盘口/ev）
          B. 引擎信号（方向/概率/max_price/price_cap）
          C. 实时 CLOB 盘口（best_ask/best_bid，注意 gamma 标记价有延迟，须用实时）
          ▼ 输出结构化建议（JSON）
          direction · fair_price · action(BUY|WAIT|SKIP) · confidence · reason
          ▼ 写入 data/human_fair_price.jsonl
:40   ── ② bot process_signal 下单前读取建议（config 开关控制，可随时关闭）
:41   ── bot 按现有 round52.4 逻辑下单（或按开关应用 AI 建议）
```

### 新增组件清单（4 项）

| # | 组件                               | 说明                                                           | 关键点                                                |
| - | -------------------------------- | ------------------------------------------------------------ | -------------------------------------------------- |
| 1 | `analyze_hour_for_trade.py`（新脚本） | 每窗 :39 触发；读模型演变+引擎信号+实时盘口；**调用 AI 生成建议**                     | 建议输出保持结构化 JSON                                     |
| 2 | `data/human_fair_price.jsonl`    | 每窗建议存档                                                       | 与 ml\_shadow\_predictions.jsonl 同 slug+decision 幂等 |
| 3 | bot 读取点                          | `hermes_1h_bot.py` `process_signal()` L1016 下单前插入"读 AI 建议"步骤 | 受 config 开关控制，默认关                                  |
| 4 | 计划任务 `HermesBTC1HHumanAdvice`    | 每小时 :39 触发第 1 项                                              | 触发点早于 bot :40 分析                                   |

### 关键概念对齐：fair\_price 的用途

**当前 bot 是"直接吃价成交"（round52.4），所以 fair\_price 不是"挂单价"，而是"是否接受吃价的判断线"：**

```
if ask ≤ fair_price:  → 吃价成交（模型认为价合理，正 EV）
if ask >  fair_price: → 建议 WAIT / SKIP（模型认为价偏贵，负 EV）
```

- fair\_price(买涨方向) = 模型校准胜率 p\_up（盈亏平衡价）

- fair\_price(买跌方向) = 1 - p\_up

- 8pm 教训映射：ask 冲到 0.695 而 fair 仅 \~0.5 → 正确动作 = 不追

***

## 五、三个必须拍板的决策点

### ① 信任深度（推荐 B 门控 SKIP）

- **A 纯影子对照**：AI 建议只记录不干预，攒 N 周对比"按 AI 建议 vs bot 实际执行"孰优。最稳，见效慢

- **B 门控 SKIP（推荐）**：AI 说"价太贵/方向不对"→ bot 本窗不下单。立刻减少负 EV 单，风险有限可随时关

- **C 定价覆盖**：AI 的 fair\_price 直接取代 ask 作为下单价。高风险高回报，需 A/B 数据支撑才能上

### ② AI 形态（推荐 A+B 混合）

- **A 真 AI 每窗介入**：AI 每窗读数据给建议。灵活、能看演变，但有延迟、偶尔失败、建议不可 100% 复现。适合第一周验证

- **B 规则化**：把看演变的核心判据固化成确定性规则（如"连续 N 时点方向一致才强信""盘口相对模型价偏离 ≥X 不追"）。快、稳、可复盘。适合打底

### ③ 谁当最终裁决

- **AI 建议只作参考线**（bot 引擎信号仍是主体）→ 平滑演进

- **AI 建议可否决引擎方向**（分歧时以模型演变+盘口为准）→ 更激进，需先看几天分歧统计

***

## 六、推荐组合（供拍板）

> **第一步**：真 AI 每窗 :39 出建议 + **门控 SKIP**（①B + ②A），bot 现有链路只加一个"读建议"开关 → 跑 1-2 周积累"AI 建议 vs 实际"数据 → 再决定是否升级到规则化 + 定价覆盖。

**设计原则**：bot 现有交易链路几乎不动，新增环节全部可选开关控制，随时可整体关闭。

***

## 七、参考文件清单

- bot 主文件：`d:\hermes-btc\hermes_1h_bot.py`（process\_signal L1016 起；order\_price 计算 L1246-1297）

- 信号队列：`data\claude_signal.jsonl` / 最近信号 `data\hermes_last_signal.json`

- 模型预测：`model\predict_hourly.py`（12 时点推理）、`model\backfill_data.py`（数据）、`model\train_lgbm.py`（LGBM）、`model\train_lstm.py`（LSTM）

- 影子记录：`data\ml_shadow_predictions.jsonl`

- 报告：`tmp\ml_shadow_report.py`（时点统计/小时聚合/EV 信号/预测演变）

- 模型 registry：`model\registry\lgbm_dm0~55_v1.txt` + `lstm_dm0~55_v1.pt`

- 交易硬约束：`d:\hermes-btc\.trae\rules\trading-hard-constraints.md`

***

## 八、实施前置检查（拍板后按此执行）

1. 确认 AI 调用方式（本项目环境内可用的模型推理通道）
2. 定义 AI 建议 JSON schema（direction/fair\_price/action/confidence/reason + slug + decision\_min + ts）
3. `analyze_hour_for_trade.py` 幂等设计（同 slug 建议不重复覆盖）
4. 实时盘口来源确认：gamma 标记价有延迟（8pm 教训），需用 CLOB 实时报价（bot 内已有 best\_ask 逻辑可参考）
5. bot 读取点开关：config 加 `ai_advice_enabled`（默认 false），随时回滚
6. 每改一处立即交叉验证 + py\_compile + pytest tests\ 全量回归（无回归才提交）

