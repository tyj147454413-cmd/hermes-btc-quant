# Hermes BTC 1H — 项目说明书（AI 接手必读）

> **本文档定位**：面向 AI（或新接手的工程师）的系统级总览。读完本文即可理解项目全貌、信号链路、
> 各文件职责、运维方式与修改纪律。细节口径以代码为准，规则铁律以 `.trae/rules/` 为准。
> 快照日期：2026-09-03（round54）。项目持续演化，读档后请以磁盘现状为准。

***

## 0. 新 AI 接手 5 分钟上手清单

1. 读本文档全文（约 15 分钟）。
2. 读 `.trae/rules/trading-hard-constraints.md`（交易硬约束，违反即事故）。
3. 确认运行态：`data\hermes_bot.log` 尾部有 `🚀 Bot | LIVE` 横幅 + 进程启动时间晚于代码文件修改时间。
4. 确认计划任务齐全：`schtasks /Query /FO LIST | findstr Hermes`（见 §10 任务表）。
5. 改任何代码前，先读 §11 修改纪律（每改一处即验、全套回归、重启核对时间戳）。

***

## 1. 项目是什么

**一句话**：对 Polymarket 的 "Bitcoin Up or Down — 每小时一场" 二元合约做量化预测并自动下单的完整系统。

- **标的**：Polymarket BTC 1H up/down 合约（slug 形如 `bitcoin-up-or-down-september-3-2026-8am-et`）。

- **预测目标**：本小时 ET 整点 vs 下一整点的 BTC 价格涨跌（结算价 = Binance BTC/USDT 整点价）。

- **信号来源**：**纯规则引擎**（因子打分 + 规则门 + 回测校准），**不是 AI 自由裁量**。
  早期版本由 Claude Desktop 定时任务写信号，round51.x 起已改为 `scripts/engine_signal.py` 纯引擎流水线。

- **交易模式**：当前 `mode: live`（2026-09-03 00:17 用户显式确认切 live）。切 live/paper 由
  `hermes_config.json` 的 `mode` 字段控制，**切换必须用户显式确认，AI 不得自行切模式**。

- **运行环境**：Windows Server（Task Scheduler 驱动）+ Python 3.11 venv（`.venv\`）。

**与钱的距离**：本系统直连 Polymarket CLOB 用真实资金下单（live 模式下）。任何改动都可能造成真金白银
的损益，因此 §11 的修改纪律是硬性的，不是建议。

***

## 2. 核心设计哲学（五条铁律）

1. **引擎算信号，AI 不拍脑袋**：方向/置信度/概率/仓位全部由 `signal_validator.py`（因子打分）

   - `rule_engine.py`（规则门）机算得出。每个因子、每个权重、每个阈值都有回测证据，
     历史决策记录在 `SYNC_MAP.md`（60+ 轮 round 编年史）。
2. **信号单向流动**：采集 → 引擎判定 → 写信号队列 → bot 消费 → 下单 → 成交 → 结算 → 复盘。
   每一环只做自己的事，通过 `data/` 下的 JSON/JSONL 文件解耦。
3. **成交以证据为准**：订单是否成交**必须**以 `list_account_trades`（交易所成交记录）佐证；
   `get_order` 查不到一律按 UNKNOWN 处理，禁止默认判 FILLED。
4. **fail-open 只属于校验层**：引擎校验层（validator）异常时放行信号不阻塞；但执行层守门
   （共识反转、流动性）是真拦截。方向字段（side）禁止编造默认值——非法即拒收，宁可不交易。
5. **一切修改必须当场验证**：每改一处代码立即交叉验证 + 全套回归 + 重启核对时间戳（详见 §11）。

***

## 3. 系统全景与信号链路（每小时时序）

```
                          ┌──────────────────────────────────────────────┐
                          │  Windows Task Scheduler（见 §10 任务表）      │
                          └──────────────────────────────────────────────┘
:02  HermesBTC1HCollect ──▶ hermes_collect.py
                              │ 采集 BTC 行情(Binance K线/aggTrades)、Polymarket
                              │ 盘口/价格/订单簿、聪明钱池标记
                              │ ▶ 归档快照 data/snapshots/{slug}_{ts}.json
                              │ ▶ 写 data/hermes_market_data.json
:02  HermesBTC1HReview ───▶ hermes_review.py
                              │ 结算已到期的上一小时交易（对错/盈亏/因子归因/教训库）

:16  HermesBTC1HModelPred15 ▶ model/predict_hourly.py --decision-min 15（影子，不下单）
:36  HermesBTC1HModelPred35 ▶ model/predict_hourly.py --decision-min 35（影子，不下单）
                              │ ▶ 追加 data/ml_shadow_predictions.jsonl（A/B 攒样本中）

:40  HermesBTC1HEngine ────▶ scripts/engine_signal.py   ★信号生产主线★
                              │ 1) hermes_review.main()        复盘上一小时
                              │ 2) hermes_collect.py --force   强制采集当前小时
                              │ 3) signal_validator.validate() 引擎判定
                              │    ├─ extract_factors()  因子打分（§6 因子表）
                              │    └─ rule_engine.judge() 规则门 → 方向/置信度/概率/仓位
                              │ 4) write_signal.write_signal()
                              │    ├─ side 校验（非法直接 raise，拒绝写入）
                              │    ├─ 引擎复验（fail-open）
                              │    ├─ 概率天花板 0.67 / 高概率通道 price_cap=0.80
                              │    └─ 追加写 data/claude_signal.jsonl（文件锁+同slug去重）

:40+ hermes_1h_bot.py（常驻进程，ANALYZE_AT_MIN=40）
                              │ 读队列 → 白名单校验(source 精确匹配) → side 校验
                              │ → 实时守门（§7）→ 下单 → 追价/撤单/成交跟踪
                              │ ▶ 恢复单/追价单重启后自动续管（SIGTERM 保留）
结算后                        │ list_account_trades 取成交证据 → 写 fills/results
:40(下小时)                   │ hermes_review 结算归因，闭环
```

**关键时点**：`ANALYZE_AT_MIN = 40` —— bot 收到信号后推迟到整点后 **:40** 才分析下单（平衡
"早分析噪音多" 与 "晚分析价格高"）。引擎链也同步在 :40 跑，两者对齐。

***

## 4. 目录结构

```
d:\hermes-btc\
├── AI_PROJECT_GUIDE.md          ← 本文档
├── CLAUDE.md / prompt_exec.md / SYNC_MAP.md
│      ⚠️ Mac 时代遗留文档。CLAUDE.md 的路径/技能路由已过时（指向 macOS）；
│      SYNC_MAP.md 是逐 round 的变更编年史（查"某个规则为什么是这样"时读它）。
│
├── hermes_1h_bot.py            ★执行层：常驻交易 bot（消费信号、下单、追价、结算）172KB 主文件
├── hermes_collect.py           ★采集器：BTC 行情 + Polymarket 盘口/价格/聪明钱标记 → 快照
│      （scripts/hermes_collect.py 是同一文件的同步副本，两处都改，口径一致）
├── hermes_review.py            ★复盘模块：逐小时结算、统计、教训库
├── hermes_review_feedback.py     复盘权重刷新（regime + source 隔离）
├── hermes_common.py              公共函数：now_et() / build_slug() 等（slug 规则在 build_slug）
├── bot_watchdog.py               看门狗：bot 进程死亡 5 分钟内自动拉起（防风暴 10 分钟）
├── dashboard.py                  Web 仪表盘（Flask，端口 5050，templates/ 下有 UI）
├── hermes_config.json          ★运行配置（mode/bet_amount/min_bet_amount 等，UI 可改）
├── .env                          环境变量（HERMES_DIR 等；含密钥，勿提交勿外传）
│
├── scripts/
│   ├── engine_signal.py        ★引擎信号流水线（:40 定时任务入口，见 §3）
│   ├── signal_validator.py     ★因子打分 + validate()（引擎核心之一，96KB）
│   ├── rule_engine.py          ★规则门 judge()（引擎核心之二，140KB）
│   ├── write_signal.py         ★信号写入器（校验/锁/去重/落盘）
│   ├── smart_money_pool.py       聪明钱池滚动筛选（读账本 → data/hermes_smart_money_pool.json，
│   │                              采集器每小时读池文件标记 smart_money_pool_direction）
│   ├── smart_money_settle.py     聪明钱账本结算
│   ├── *.bak_2026082*            改版前备份（round 历史锚点，勿删勿改）
│   └── 其余 test_*.py / backtest_*.py / *_replay.py
│          一次性或专项回测/审计脚本（研究层，不进生产线；改动生产线前用它验证）
│
├── model/                        影子 ML（LightGBM/LSTM，dm15/dm35 双口径）
│   ├── predict_hourly.py          每小时影子推理（:16/:36，只记录不下单）
│   ├── train_lgbm.py / train_lstm.py / TRAINING_PLAN.md
│   └── registry/                  训练好的模型（lgbm_v1.txt / lstm_dm35_v1.pt）
│
├── data/                       ★全部运行时数据（见 §5 地图）
├── tests/                        pytest 套件（147 个，改码后必须全过，见 §11）
├── templates/                    dashboard 的前端模板
├── docs/                         历史提案/评审文档
├── paper_trades/ predictions/ result/ outputs/ tmp/
│          早期 paper 时代遗留目录（已不活跃，新数据都进 data/）
└── .trae/rules/
    ├── trading-hard-constraints.md   交易硬约束（违反即事故，必读）
    ├── exchange-troubleshooting.md  交易所故障排查（503/425/geofence）
    └── windows-env.md                Windows 环境纪律
```

***

## 5. 数据流与关键文件（data/ 地图）

| 文件                                                                  | 读方                | 写方                    | 说明                                                   |
| ------------------------------------------------------------------- | ----------------- | --------------------- | ---------------------------------------------------- |
| `claude_signal.jsonl`                                               | bot               | write\_signal         | **信号队列**（bot 唯一消费入口）。同 slug 去重只留最新；`*.lock` 文件锁防读写竞争 |
| `snapshots/{slug}_{ts}.json`                                        | 引擎/回测             | hermes\_collect       | 每小时快照归档（因子输入的全量原始数据），回测与复盘的事实源                       |
| `hermes_market_data.json`                                           | signal\_validator | hermes\_collect       | 当前小时最新快照（`--force` 采集即写）                             |
| `hermes_analysis.json` / `hermes_hourly_prediction.json`            | dashboard         | write\_signal         | 仪表盘展示用                                               |
| `hermes_bot.pid`                                                    | bot               | bot                   | 进程单实例锁；**残留会误判已运行导致新实例退出**（故障见 §12）                  |
| `hermes_bot.log`                                                    | 人/AI              | bot                   | bot 主日志（看 `🚀 Bot \| LIVE` 横幅、下单/追价/结算全轨迹）           |
| `hermes_engine_signal.log`                                          | 人/AI              | engine\_signal        | 引擎流水线日志（:40 每轮采集→判定→写入结果）                            |
| `hermes_pending.jsonl`                                              | bot               | bot                   | 挂单跟踪（含恢复单，必须带 price/token\_id/chase\_count 等键）       |
| `hermes_fills.jsonl` / `hermes_done.jsonl` / `hermes_results.jsonl` | 复盘                | bot/review            | 成交证据 / 已处理 slug 终态 / 结算结果账本                          |
| `hermes_smart_money_pool.json`                                      | 采集器               | smart\_money\_pool    | 聪明钱池（滚动 7 天盈利钱包）                                     |
| `ml_shadow_predictions.jsonl`                                       | 分析                | model/predict\_hourly | ML 影子预测（A/B 攒样本，目标 ≥300 样本）                          |
| `pm_guard_shadow.jsonl`                                             | 复盘                | bot                   | PM 斜率守卫影子账（不拦截，攒数据定阈值）                               |
| `hermes_watchdog.log` / `watchdog_state.json`                       | 人/AI              | 看门狗                   | 拉起记录与防风暴状态                                           |
| `hermes_runtime_meta.json`                                          | write\_signal     | 人工维护                  | strategy\_version/model\_version 版本戳（分段统计用）          |
| `review-lessons.json` / `review-stats.json` / `review-log.md`       | 复盘                | hermes\_review        | 教训库 / 统计 / 复盘日志                                      |

***

## 6. 引擎层详解（signal\_validator.py + rule\_engine.py）

### 6.1 两趟结构

```
validate(seed)                          # seed 只需 slug + source + action
  ├─ load_market_data(slug)            # 读 data/hermes_market_data.json（缺失→fail-open 放行）
  ├─ extract_factors(snapshot)         # 第一趟：因子打分 → factors dict + notes
  ├─ rule_engine.judge(factors, ...)   # 第二趟：规则门 → 方向/置信度/概率/仓位/风险标注
  └─ 出口：corrected 信号（方向可能被引擎翻向，概率被强制回写机算值）
```

**核心概念**（引擎输出里的字段）：

- **S0（engine\_s0）**：因子净分基线（extract\_factors 的合计）。

- **PM 行（engine\_pm\_row）**：PM（Polymarket）侧因子小计。

- **净分（engine\_net）**：进入方向判定的最终分（正→UP，负→DOWN）。

- **方向**：`engine_direction` = UP/DOWN；**置信度**：高/中/低（三档）。

- **概率**：`engine_probability`（0-100）；**仓位**：`position_scale`（1.0/0.5/"min"）。

### 6.2 在役因子表（extract_factors 实际写 factors[] 的键）

| 因子键 | 中文名 | 判据（简） | 权重 | 备注 |
|---|---|---|---|---|
| `price_position` | 价格位置 | 现价相对小时锚定价位置（dev 绝对值 <0.15% 计 0） | ±3 | 全场最强（86.3%） |
| `pm_momentum` | PM 动量 | PM up_price 小时内变化量，绝对值 ≥0.30 才计 | ±2 | 全模式计分；阈值 9/3 从 0.02 提到 0.30 |
| `pm_consensus` | PM 共识 | book_up_mid>0.60→+1 / <0.40→-1 | ±1 | 全模式 |
| `pm_imbalance` | PM 挂单失衡 | book_imbalance 绝对值 ≥0.3 | ±1 | round48 |
| `data_completeness` | 聪明订单 | book_imbalance × smart_money_pool_direction 两票同向按方向 ±2；矛盾/缺任一→0 | ±2 | round54 两票制（详见 §6.3 修正1-3） |
| `reversal_intent` | 深跌反转 | Δ_prev≤-0.03 且 Δ_cur≥+0.02 | +1 | 只做深跌反UP单边（回测不对称）；仅退出共振池仍独立计分（修正4） |
| `recent_dir_5m` | 5m 动量 | timeframes.5m.recent_dir | ±1 | 贴线模式不计分 |
| `recent_dir_15m` | 15m 动量 | timeframes.15m.recent_dir | ±1 | 贴线模式不计分；兼共振池第 5 票（修正4） |
| `kline_15m` / `kline_1h` | K线双行 | 各自 4 根阳多+1/阴多-1 | 各±1 | round44 拆双行（旧 `kline` 键停用） |
| `taker_bs` | Taker 5m | taker_bs_ratio 分级 | ±1 | |
| `taker_1m` | 贴线 1m Taker | 买占比>0.6→+1/<0.4→-1 | ±1 | 仅贴线模式 |
| `momentum_burst` | 动量爆发 | 高波动+方向一致 | ±2 | |
| `wall_divergence` | 墙背离 | 买墙大价跌/卖墙大价涨 | ±1 | 仅贴线模式 |
| `short_bounce_factor` | 短反弹先兆 | consecutive_up/down + 短窗回撤形态 | ±1 | 消费 consecutive_*（已改真连续计数） |
| `wall_settlement` | 结算期墙 | 结算期墙形态 | ±1 | 结算期专列（非贴线分支） |
| `resonance` | 共振 | 5 个强因子同向个数：≥4→±2，=3→±1 | ±2 | round54；票池见 §6.3 修正4 |
| `pm_pair` | 强配对 | pm_momentum 与强搭档同向数 k：≥2→±2，=1→±1 | ±2 | round54；搭档 PM_PAIR_PARTNERS |

**休眠/已删因子**（读旧文档别被误导）：

- 休眠（代码保留、不进 S0）：`acceleration`、`exhaustion`、`momentum_health`、`s_r_swap`（9/3 回测证伪关停）、`taker_1h`（仅参考）、旧 `kline` 合计键。

- 已删（round52 P0-1.3）：`order_flow`、`taker_bs_15m`、`open_micro`、`macro_trend`。

### 6.3 round54 引擎修正记录（9/3，P2-8/P2-9）

四个修正全部落盘于 [signal_validator.py](file:///d:/hermes-btc/scripts/signal_validator.py)，有对应测试与 OOS 回测依据：

**修正1（P2-8）聪明订单方向符号**：原实现 book+pool 两票同向时**一律记 +2**，把偏 DOWN 的同向共识错记成加分（与体系 up=+/down=- 相左）。修正后按共识方向记分：看 UP 共识 +2 / 看 DOWN 共识 -2。事故实证：9/3 6am 场 book=-0.498、pool=-1（双看 DOWN）曾被记 +2 反向误导。

**修正2（P2-8）矛盾档记 0（不记 -2）**：book 与 pool 方向矛盾时引擎不给分。依据：矛盾无方向，方向性因子无法记"无向 -2"；回测所谓「矛盾档 75% 命中」实为矛盾场次**引擎净分方向**的描述统计（与因子取值无关），不构成惩罚依据——其它因子仍有效不应扣死。矛盾档样本 n=24 偏小留观察。

**修正3（P2-8）摘除 net_up_imbalance 证伪链**：原第三票 net 字段结构性恒正（只统计当前 UP 持仓大户的 taker 买入，卖出离场钱包不再入榜 → 幸存者偏差），用它证伪等价于单边扑杀 DOWN 票（实测 33 次 DOWN 共识全被压 0，UP 共识 0 次被压）——聪明订单因子退化成 UP 单边。修正后 net 不参与本因子任何分支，只做两票制（book × pool）。

**修正4（P2-9）共振池第 5 票改回 recent_dir_15m**：`RESONANCE_STRONG = (price_position, pm_momentum, pm_consensus, data_completeness, recent_dir_15m)`。曾短暂用 reversal_intent 作第 5 票，OOS 回测（462 场最晚 25% 样本外）对比定夺：

- 池B(reversal_intent)：≥3 同向 n=82 命中 89.0% / ≥4 n=37 命中 97.3%——但 reversal_intent 88% 场次=0 票不参与（触发场仅 71/462），≥4 档实为「4 核心全同向」的换皮，几乎不贡献独立票
- 池A(recent_dir_15m)：≥3 n=88 命中 85.2% / ≥4 n=66 命中 92.4%——双向对称、触发率高、与回测口径一致

改回池A。reversal_intent **仍作独立因子 ±1 保留**，仅退出共振池。

**配套（round54.1）概率口径剔除组合因子**：[rule_engine.py](file:///d:/hermes-btc/scripts/rule_engine.py) L408 在查概率表前从净分中减去 resonance/pm_pair 分——组合因子只做**方向确认加分**（S0/net 照常含），不参与概率表档位判定。依据：概率表档位胜率为旧净分口径统计（round41.2 239 场），共振分会把 3-5 档挤进 6+ 档读 84.6%"高"（审计 26 场升档实际命中仅 76.9%，高估约 8pp → EV 高估 → 高价吃价风险）；G6 弱信号缩仓线同口径恢复，共振分不再把弱信号"救"过缩仓线（更保守）。

### 6.4 概率与 EV 口径（铁律）

- **概率天花板 0.67**：模型宣称 >67% 一律压回 0.67（实测方向胜率校准上限，8/13+8/21 双源）。

- **EV 单位**：全链路统一 = **每 $100 本金期望美元**（如 16.8 = +16.8%），`|ev|>400` 视为公式错误拒收。

- **EV 不做 SKIP 门槛**（round31）：分析确认方向即写 ORDER，EV 仅日志参考。

- **高概率通道**：`probability_raw ≥ 0.70` 时信号显式带 `price_cap=0.80`（bot 侧同 `HIGH_PROB_CAP=0.80`，两端口径必须一致）。

***

## 7. 执行层详解（hermes\_1h\_bot.py）

### 7.1 关键常量（改前先读注释里的历史依据）

| 常量                | 值                                               | 含义                                       |
| ----------------- | ----------------------------------------------- | ---------------------------------------- |
| `ANALYZE_AT_MIN`  | 40                                              | bot 推迟到 :40 才分析下单                        |
| `ALLOWED_SOURCES` | btc-agent / BTC Sentinel / btc\_signal\_watcher | 信号来源白名单（**精确匹配**，大小写归一后比对）               |
| `MAX_ORDER_PRICE` | 0.60                                            | 基础挂价封顶                                   |
| `HIGH_PROB_CAP`   | 0.80                                            | 高概率通道封顶（praw≥0.70）                       |
| `CHASE_AFTER_MIN` | 2                                               | 首挂静置 2 分钟后开始追价（**与 TTL 独立，禁止混用**）        |
| `CHASE_TARGETS`   | 0.65→0.85 步进 0.05                               | 阶梯追价档位，用尽后继续 +0.05 延伸至 CLOB 上限 0.99（无封顶） |
| `ORDER_TTL_MIN`   | 25                                              | 挂单总超时（首挂起算），超时撤单                         |
| `RECHECK_PM_MIN`  | 0.50                                            | 守门线：Gamma 实时中间价 <0.50（共识反转）→ 撤单 SKIP     |

### 7.2 信号消费与守门（下单前）

1. **白名单**：`source` 精确匹配白名单，不匹配即拒收（终态写 done，防重试刷日志）。
2. **side 校验**：side/signal 必须是 UP/DOWN，非法值拒收（`skip_reason: invalid side`）——
   防 side=None 落盘导致 bot 崩溃连环重试。
3. **UI 金额开关**：`bet_amount < min_bet_amount(1.0)` 视为用户主动 SKIP 本窗口。
4. **实时 PM 极端反转拦截**：我方方向实时价 <0.25（市场 ≥75% 定价对家胜出）→ SKIP 不成交。
5. **PM 大幅波动**：相对快照跌 >15% 只记录不拦截（`pm_guard_shadow.jsonl` 影子账）。
6. **流动性预检（B12）**：盘口无有效卖单/价差异常 → SKIP。
7. **规则 A 观察账**：「ask<0.55 × 16-21 ET」历史最大失血点，当前**暂停拦截只记账**（攒样本）。

### 7.3 下单与追价状态机

```
首挂（round52.4，成交率优先）：
  有实时 ask（≤0.99）→ 直接按 ask 跨价吃（taker 立即成交）
  book 缺失 → 按信号价封顶兜底挂 maker

追价链（仅作 book 缺失/撤单重挂兜底）：
  首挂未成交 → 静置 2 分钟（CHASE_AFTER_MIN）
  → 每 2 分钟上移一档（0.65→0.70→…→0.85 用尽后继续 +0.05 直至 0.99）
  → 期间 Gamma mid < 0.50（RECHECK_PM_MIN，方向反了）→ 撤单 SKIP
  → 25 分钟（ORDER_TTL_MIN）超时 → 撤单终态

已知代价（用户知情确认）：成交价 > 引擎概率的部分是负 EV，成交率优先于单笔 EV。
```

### 7.4 进程管理

- **单实例**：`data/hermes_bot.pid` 文件锁；残留 PID 文件会让新实例误判退出（排查见 §12）。

- **重启恢复**：启动时读 `hermes_pending.jsonl` 续管挂单/追价单（恢复键必须含
  `price`/`token_id`/`chase_count`/`price_cap`/`max_price` 等，缺键=追价分支 KeyError）。

- **SIGTERM 保留**：进程被停时把在途订单写回 pending 文件（含全部续管键，round54 P1-4 补齐 5 键）。

- **看门狗**：`bot_watchdog.py` 每 5 分钟由计划任务调用，bot 死亡自动 `schtasks /Run` 拉起，
  10 分钟防风暴保护期。

### 7.5 结算纪律

- 成交判定**必须** `list_account_trades` 佐证；maker 份额从 `maker_orders[].matched_amount` 取，
  **不可**用整笔 trade 的 size/price。

- 结算后写 `hermes_done.jsonl` / `hermes_results.jsonl`，复盘层（hermes\_review）再结算对错与盈亏。

***

## 8. 复盘与账本（hermes\_review\.py）

- 触发：`HermesBTC1HReview`（每小时 :02）+ `engine_signal.py` 开头（:40 再跑一遍，幂等）。

- 职责：结算已到期交易 → 更新胜率/盈亏统计（`review-stats.json`）→ 因子归因（哪几个因子
  指错了方向）→ 教训库（`review-lessons.json`）→ `review-log.md` 逐时日志。

- `hermes_review_feedback.py`：按 regime（市况）+ source 隔离刷新指标权重。

- 复盘产出是因子增删/改权重的回测输入之一（另一半是 scripts/ 下的专项回测脚本）。

***

## 9. 影子模型（model/）

- **定位**：LightGBM/LSTM 的**影子预测**，只记录不下单（`data/ml_shadow_predictions.jsonl`）。

- **双口径**：dm15（:16 决策）/ dm35（:36 决策），分别由计划任务 ModelPred15/ModelPred35 驱动。

- **里程碑**：攒 ≥300 影子样本后做 model vs engine A/B 对比，再谈是否集成（当前样本量约 120+，
  进度见 `scripts/check_shadow_progress.py`）。

- **不要**让模型直接驱动交易——引擎是唯一信号源（用户明确决策）。

***

## 10. 运维（Windows Task Scheduler 任务表）

| 任务名                      | 触发      | 执行                                                                         | 说明                                          |
| ------------------------ | ------- | -------------------------------------------------------------------------- | ------------------------------------------- |
| `HermesBTC1HBot`         | 登录时     | `scripts\migration\bat\start_bot.bat` → `start /B python hermes_1h_bot.py` | 常驻 bot；日志重定向到 `data\hermes_bot.log`         |
| `HermesBotWatchdog`      | 每 5 分钟  | `start_watchdog.bat` → `bot_watchdog.py`                                   | bot 死亡自动拉起（防风暴 10 分钟）                       |
| `HermesBTC1HCollect`     | 每小时 :02 | `start_collect.bat` → `hermes_collect.py`                                  | 例行采集+快照归档                                   |
| `HermesBTC1HReview`      | 每小时 :02 | `hermes_review.py`                                                         | 逐小时复盘结算                                     |
| `HermesBTC1HEngine`      | 每小时 :40 | `engine_signal.bat` → `scripts\engine_signal.py`                           | **信号生产主线**（复盘→强制采集→判定→写队列）                  |
| `HermesBTC1HModelPred15` | 每小时 :16 | `model\predict_hourly.py --decision-min 15`                                | ML 影子预测                                     |
| `HermesBTC1HModelPred35` | 每小时 :36 | `model\predict_hourly.py --decision-min 35`                                | ML 影子预测                                     |
| `HermesBTC1HDashboard`   | 登录时     | `start_dashboard.bat` → `dashboard.py`                                     | Web 仪表盘 <http://localhost:5050>             |
| `HermesBTC1HSignalCron`  | 每小时 :40 | `signal_cron.bat`（Claude CLI 占位）                                           | **遗留未启用**（Mac 时代 AI 信号外壳，已被 Engine 任务替代，忽略） |

**常用运维命令**（PowerShell）：

```powershell
# 查 bot 进程与启动时间（venv shim 会派生子进程，两个 python 是正常父子关系）
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match "hermes_1h_bot" } | Select ProcessId, CreationDate

# 重启 bot（先杀旧进程树，再拉起，再核对启动时间戳）
taskkill /PID <pid> /T /F ; schtasks /Run /TN HermesBTC1HBot

# 看 bot 日志尾部
Get-Content data\hermes_bot.log -Tail 20 -Encoding UTF8

# 手动跑一轮引擎（不下单调试）
.venv\Scripts\python.exe scripts\engine_signal.py --dry-run
```

***

## 11. 修改纪律与验证标准（硬性，违反=事故）

来自 `.trae/rules/trading-hard-constraints.md` 与用户规则，**逐条强制**：

1. **每修改一处代码，立即交叉验证**检查是否留下 BUG；禁止攒到最后一次性验证。
2. **改完必须全套回归**：`pytest tests\`（当前 147 个）——新场景全过 + 既有测试无回归，缺一不可。
3. **改 .py 后必须** **`py_compile`** **语法检查**；改 bot 主文件后必须**重启进程并核对启动时间戳**
   （旧进程不算数：文件修改时间 < 进程启动时间 = 改动未生效）。
4. 测试失败先定位归属：**产品 BUG 还是测试自身 BUG**（mock 时序/参数名/场景设定），
   测试自身错了也要修，不许只改到"看起来能过"。
5. 涉及编码的修改，**实测磁盘既有数据文件的实际编码**（本项目全部 UTF-8）。
6. **验证结论必须列证据**（测试通过数、进程启动时间、日志输出行），不接受"应该没问题"。
7. 所有 `open()` 读写必须显式 `encoding="utf-8"`（Windows 默认 GBK 会 UnicodeDecodeError 崩溃）。
8. 交易约束：paper 模式测试优先，切 live 必须用户显式确认；`bet_amount ≥ $1.00` 才执行；
   订单状态以 `list_account_trades` 为准。
9. 追价链变量必须有赋值（禁用未定义变量，如曾经的 `_eff_cap`）；`CHASE_AFTER_MIN` 与
   `ORDER_TTL_MIN` 语义独立，禁止混用。

***

## 12. 常见故障速查（历史教训浓缩）

| 症状                                                            | 根因                                | 处置                                                                    |
| ------------------------------------------------------------- | --------------------------------- | --------------------------------------------------------------------- |
| 新 bot 启动即退                                                    | 残留 `data/hermes_bot.pid` 误判已运行    | 删 PID 文件后重新拉起                                                         |
| bot 每 tick 崩溃连环重试                                             | 信号 side=None 落盘（上游缺字段）            | 已加 side 校验拒收；新链路写入前先验 side                                            |
| 交易所 425 "order manager not ready" / 503 "trading is disabled" | Polymarket 平台级故障（AWS 副本延迟），非账户问题  | bot 有静默期守卫（10 分钟一探测自动恢复）；详见 `.trae/rules/exchange-troubleshooting.md` |
| 改码后行为没变                                                       | 旧进程还在跑（没重启/没核对时间戳）                | 重启 + 核对进程 CreationDate > 文件 LastWriteTime                             |
| UnicodeDecodeError 启动崩溃                                       | open() 缺 encoding="utf-8"（GBK 环境） | 补显式编码（9/2 `_load_risk_stats` 实证）                                      |
| tasklist 两个 python 疑似双实例                                      | venv python.exe 是 shim，派生子进程是正常现象 | 查 ParentProcessId 确认父子再动手，勿盲杀                                         |
| schtasks /End 杀不干净                                            | start /B 派生的进程树                   | `taskkill /PID <pid> /T /F` 按命令行匹配兜底                                  |
| 强趋势日持续踏空                                                      | 死守低价封顶（历史 0.70→已无封顶追价）            | 现行无封顶追价链即为此教训产物                                                       |
| 追价分支 KeyError                                                 | 恢复挂单缺 price/chase\_count 等键       | 恢复单必须带全套键（round54 P1-4 已补齐）                                           |

***

## 13. 历史文档导读（优先级与陷阱）

| 文档                            | 状态                               | 怎么用                           |
| ----------------------------- | -------------------------------- | ----------------------------- |
| **AI\_PROJECT\_GUIDE.md**（本文） | 当前权威总览                           | 接手先读                          |
| **.trae/rules/\*.md**         | 当前权威约束                           | 改交易/执行逻辑前必读                   |
| `SYNC_MAP.md`                 | 变更编年史（60+ round）                 | 查"某规则为什么是这样"的决策依据与测试证据；只读不抄   |
| `prompt_exec.md`              | Mac/AI 外壳时代规则正文                  | 历史口径参考，引擎现行为以代码为准             |
| `CLAUDE.md`                   | **过时**（指向 macOS 路径与 Claude 技能路由） | 勿按它操作；round51.x 起信号已由纯引擎流水线生产 |
| `docs/` 各提案                   | 历史评审记录                           | 考古用                           |
| `图表分析.md` / `prompt.md`       | 早期分析笔记                           | 低优先级                          |

***

## 14. 术语表

| 术语                  | 含义                                                                                          |
| ------------------- | ------------------------------------------------------------------------------------------- |
| **slug**            | 一场 1H 合约的唯一标识，如 `bitcoin-up-or-down-september-3-2026-8am-et`（`hermes_common.build_slug` 生成） |
| **ET**              | 美东时间；全系统窗口/小时判定统一用 ET                                                                       |
| **PM / Polymarket** | 预测市场交易所（CLOB 挂单撮合，USDC 本位）                                                                  |
| **快照（snapshot）**    | 某小时 :40 时点采集的全量因子输入（K线/盘口/PM 价格/聪明钱标记），归档于 `data/snapshots/`                                |
| **因子（factor）**      | 引擎的机算打分项（§6.2 表），产出 ±N 分进净分                                                                 |
| **S0 / 净分 / PM 行**  | 因子净分基线 / 方向判定最终分 / PM 侧因子小计                                                                 |
| **贴线模式（tie\_mode）** | 锚定价附近小波动市况（盘整），部分因子计分口径不同                                                                   |
| **praw**            | probability\_raw，引擎原始概率（校准前）                                                                |
| **吃价 / taker**      | 按 ask 价立即成交（与 maker 挂单相对）                                                                   |
| **追价链（chase）**      | 首挂未成交后逐档上移挂价的兜底机制                                                                           |
| **影子（shadow）**      | 只记录不执行的观察账（ML 影子预测/守卫影子/规则A影子账）                                                             |
| **paper / live**    | 模拟盘 / 真实资金（由 `hermes_config.json` mode 控制）                                                  |
| **round**           | 策略版本迭代编号（编年史见 SYNC\_MAP.md），如 round54 = 当前                                                  |

***

*本文档由 2026-09-03 round54 会话整理。若与代码/规则文件冲突，以* *`.trae/rules/`* *与代码现状为准，
并请顺手更新本文档。*
