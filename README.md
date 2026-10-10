# Hermes BTC 1H · Polymarket BTC 每小时涨跌预测与自动交易系统

面向 **Polymarket「Bitcoin Up or Down — 每小时一场」** 二元合约的端到端量化交易系统：
从行情 / 盘口采集、因子打分与规则判定、机器学习影子预测，到自动下单、成交跟踪、逐小时结算复盘，**全流程自动化**。

> 一句话：把「采集 → 引擎判定 → 信号 → 下单 → 成交 → 结算 → 复盘」做成一条无人值守的生产线。

---

## 一、项目亮点

| 亮点 | 说明 |
|---|---|
| **端到端闭环** | 每周期的采集、预测、下单、成交核对、结算、复盘全自动，无人值守运行 |
| **引擎驱动，非自由裁量** | 方向 / 置信度 / 概率 / 仓位全部由「因子打分 + 规则门 + 回测校准」机算，每个阈值都有回测证据 |
| **双模型影子系统** | LightGBM + LSTM 在 dm15 / dm35 两个决策点并行做**影子预测（只记录不下单）**，与规则引擎 A/B 对比、持续攒样本 |
| **以证据为准** | 订单是否成交**必须**由交易所成交流水佐证，查不到一律按 UNKNOWN 处理，禁止默认判为成交 |
| **工程健壮性** | 文件锁 + 同标的去重、进程看门狗自动拉起、Windows 任务计划调度、Web 实时仪表盘 |
| **失败安全分层** | 校验层 fail-open（不阻塞），执行层真拦截（共识反转 / 流动性守门），方向字段非法则拒单 |

---

## 二、系统架构（每小时信号链路）

```
                    Windows Task Scheduler（定时驱动）
                                 │
:02  ──▶ hermes_collect.py      采集 BTC 行情(Binance K线/逐笔) + Polymarket 盘口/订单簿
        hermes_review.py        结算上一小时交易（对错 / 盈亏 / 因子归因 / 教训库）
                                 │
:16  ──▶ model/predict_hourly.py --decision-min 15   影子预测（不下单）
:36  ──▶ model/predict_hourly.py --decision-min 35   影子预测（不下单）
                                 │
:40  ──▶ scripts/engine_signal.py   ★ 信号生产主线 ★
         ├─ signal_validator.py   因子打分 extract_factors()
         └─ rule_engine.py        规则门 judge() → 方向/置信度/概率/仓位
         └─ write_signal.py       校验/加锁/去重 → 写信号队列
                                 │
:40+ ──▶ hermes_1h_bot.py（常驻）读取信号 → 白名单/side 校验 → 实时守门
         → 下单 → 追价/撤单/成交跟踪 → 以交易所流水核对成交
                                 │
结算后  ──▶ hermes_review.py  结算归因，闭环
```

**决策时点**：`ANALYZE_AT_MIN = 40` —— 收到信号后推迟到整点后第 40 分钟才分析下单，
在「早分析噪声多」与「晚分析价格高」之间取平衡；引擎链路与之对齐。

---

## 三、技术栈

| 层 | 技术 |
|---|---|
| 语言 / 运行 | Python 3.11，Windows Server + Task Scheduler |
| 数据与建模 | pandas / NumPy，LightGBM（梯度提升），LSTM（PyTorch），时间序列特征工程 |
| 交易执行 | Polymarket CLOB API（现货二元合约），Binance 行情 API |
| 服务与可视化 | Flask Web 仪表盘（含模板化前端） |
| 工程 | 文件锁 / 幂等去重、进程看门狗、pytest 测试套件、A/B 影子评估 |

---

## 四、目录结构（核心）

```
hermes-btc/
├── AI_PROJECT_GUIDE.md        项目说明书（系统总览 / 信号链路 / 硬约束）
├── SYNC_MAP.md                逐轮变更编年史（规则成因可溯源）
├── hermes_1h_bot.py         ★ 执行层：常驻交易 bot（消费信号/下单/追价/结算）
├── hermes_collect.py        ★ 采集器：BTC 行情 + Polymarket 盘口/聪明钱标记 → 快照
├── hermes_review.py         ★ 复盘模块：逐小时结算、统计、教训库
├── hermes_common.py            公共函数（时间 / slug 构建等）
├── bot_watchdog.py             看门狗：bot 进程死亡后自动拉起
├── dashboard.py                Web 仪表盘（Flask）
├── hermes_config.json          运行配置（mode / bet_amount 等）
├── scripts/
│   ├── engine_signal.py     ★ 引擎信号流水线主入口
│   ├── signal_validator.py  ★ 因子打分 + validate()
│   ├── rule_engine.py       ★ 规则门 judge()
│   ├── write_signal.py      ★ 信号写入器（校验/锁/去重）
│   ├── smart_money_pool.py     聪明钱池滚动筛选
│   └── test_*.py / backtest_*.py  研究层回测与审计脚本
├── model/                      影子 ML（LightGBM / LSTM）
│   ├── predict_hourly.py       每小时影子推理
│   ├── train_lgbm.py / train_lstm.py
│   └── registry/               训练好的模型产物
├── tests/                      pytest 测试套件
├── templates/                  仪表盘前端模板
└── docs/                       设计提案与评审文档
```

---

## 五、如何运行

> 本项目依赖外部交易所凭据与运行数据（均**不包含在仓库内**）。

```bash
# 1) 依赖
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2) 配置：在 .env 中提供交易所 / 环境变量（参考 .env.example，切勿提交真实密钥）
#    HERMES_DIR / POLYMARKET_PK / POLYMARKET_API_KEY / ...

# 3) 手动跑一次采集与信号
python hermes_collect.py --force
python scripts/engine_signal.py

# 4) 启动常驻交易进程与仪表盘
python hermes_1h_bot.py
python dashboard.py            # 浏览器打开 http://localhost:5050

# 5) 测试
pytest -q
```

生产环境通过 Windows 任务计划（Task Scheduler）按上述时点自动调度。

---

## 六、说明

- 本仓库为**个人研究与工程能力的公开作品集**：包含代码、文档与训练产物；
  **运行数据、交易记录、账户凭据一律不在此仓库**。
- 项目用于个人量化研究与工程实践，**不构成任何投资建议**。市场有风险，据此操作后果自负。

---

## 七、作者

**唐俊（Tang Jun）** · Python / 机器学习 / 量化交易
- GitHub：https://github.com/tyj147454413-cmd
- 邮箱：tyj147454413@gmail.com