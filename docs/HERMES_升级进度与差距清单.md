# Hermes 量化系统升级进度与差距清单

> 版本：v1.0 ｜ 更新：2026-08-23 01:20 BJT
> 依据：`HERMES_量化交易升级计划书.md` v1.0 逐任务对照
> 进展快照：**阶段 0' 零风险区 ✅ ｜ 阶段 0 低风险改造区 ✅ ｜ 阶段 1 核心框架 ✅ ｜ 阶段 2-6 ⬜ 未开始**
> 关联提交：`b41257e`（零风险批）、`dddfd4a9`（改造批）
> 里程碑：**08-23 00:58 切 LIVE 实盘（$10/单），01:14 首笔真实成交 UP 20股@0.50**

---

## 一、已完成交付清单

### 批次一：零风险区（commit b41257e，纯新增 29 文件）

| 计划书任务 | 交付物 | 实测结果 |
|---|---|---|
| 0.3 快照结构化 | `scripts/feature_store.py` | 286 合约 × 96 特征列，全量+日切分 CSV |
| 0.3 特征字典 | `docs/features.md` | 从代码 SPEC 自动生成，零漂移 |
| 0.2 锚点检查(只读) | `scripts/check_rule_anchor.sh` | 首跑即发现 CLAUDE.md 补丁号漂移 |
| 0.4 健康检查 | `scripts/daily_health_check.py` | 六项巡检（采集/信号/成交/结算/时钟/锚点） |
| 1.1 事件驱动回测器 | `scripts/backtest_engine.py` | 286 事件回放；引擎重放一致性 **79.7%** |
| 1.4 基准对照 | `scripts/benchmark_strategies.py` | **Hermes EV +9.8%/单 vs 最强基准 +2.6% = 超额 +7.1pt** |
| 1.3 阈值敏感性 | `scripts/threshold_sensitivity.py` | 因子单调性分层（见第二节） |
| 1.2 Walk-forward | `scripts/walk_forward.py` | IS→OOS 滚动框架就绪（OOS 样本尚少） |

### 批次二：低风险改造区（commit ddfd4a9）

| 计划书任务 | 交付物 | 安全设计 |
|---|---|---|
| 健康检查定时化 | crontab `50 * * * *` | 试跑→正式均验证；launchd EX_CONFIG 弃用，plist 存档 `launchd/` |
| CLAUDE.md 锚点对齐 | round41.1→round41.3 | 有备份，检查器警告清零 |
| 0.1 UI 抖动防护 | bot `_load_risk_cfg()` + `min_bet_amount=1.0` 地板 | $0.50 异常改为"拦截→回退信号金额" |
| 0.1 缩仓能力 | `_apply_position_scale(scale_enabled)` + config 开关 | **默认 false=8/12 决策原样**；开启须走 SYNC_MAP 全链路 |
| 0.2 rule_version 字段 | SKILL.md frontmatter 机读字段 | 可被脚本解析 |
| 0.2 启动比对 | 健康检查第 6 项 `rule_anchor` | 每小时自动比对三文件主版本 |

### 运维事实

- 全程备份四份 `*.bak_20260823_*`；SYNC_MAP 已登记变更与回滚路径；bot 重启加载验证 OK
- LIVE 首单链路实测：新代码零干扰，金额 $10 分毫未动

---

## 二、首轮数据分析关键发现

1. **Hermes 有正 alpha**：引擎重放 EV +9.8%/单（n=106）；顺 PM 跟随 −5.1% —— 规则链路有真实信息增量。
2. **因子可靠性分层**：
   - ✅ 单调可靠：`taker_bs_ratio`、`flow_imbalance_5m`、`rsi_1m`、`macd_hist_4h`
   - ⚠️ 疑似过拟合：**`deviation_pct`(贴线族核心)**、`rsi_5m/15m`、`macd_hist_15m`、`pos24h_percentile`、`fear_greed`、`long_short_ratio`
3. **策略近期衰减警示**：最近 30 场模拟 EV ≈ −3.8%（全量 +7%）——恰在上实盘前，需重点观察。
4. **引擎重放一致性 79.7%**：分歧来自历史版本差异+AI 盘感因子不可机算。
5. **新异常待查**：01:14 周期 AI 信号缺 `max_price/ev` → bot 回退 0.69 上限（旧逻辑非本轮引入）；复现则修上游。

---

## 三、未完成任务总账

### 阶段 0 剩余（收尾级）
- 🔄 0.1 验收「连续 3 天无异常金额」（进行中）
- ⬜ 0.1 write_signal 层 bet_amount_override 校验（低优先，bot 层地板已兜住）
- 🔄 max_price 缺失异常观察（复现→修上游）
- ◑ 0.2 每周全量 CI（cron :50 锚点项已覆盖大半，独立周检可选）

### 阶段 1 剩余（框架有、深度不足）
- ◑ 1.1 撮合精度：按 ask 简化成交，无盘口深度/排队/部分成交模拟
- 🔄 1.2 OOS 样本积累（现仅 15 单，500+ 才有统计功效）
- ◑ 1.2 新规则上线流程固化（候选入 CANDIDATES→OOS 达标才上线）
- ⬜ 费率/滑点参数扫描（fee 参数位已留）

### 阶段 2 数据与特征升级（⬜ 未开始，计划 3-6 周）
- ⬜ 完整盘口历史 10-20 档落盘 `data/orderbook/`
- ⬜ 盘口特征 OBI / Micro price / Wall pressure
- ⬜ PM 成交特征：价差/成交速度/大单方向 → 情绪因子
- ⬜ BTC 跨市场：基差/资金费率历史/跨所价差/GK-YZ 波动率
- ⬜ Parquet Feature Store（依赖 pyarrow 未装，CSV 现阶段够用）

### 阶段 3 模型层（⬜ 未开始，计划 4-8 周）
- ⬜ 规则 Baseline 权益曲线报告（回测器可直接出，约半天）
- ⬜ ML 概率模型 P(UP)：XGBoost/LightGBM，Brier 对标随机
- ⬜ Meta-Labeling（只学该不该下/下多大）
- ⬜ 规则+ML 加权集成 + A/B
- ⚠️ 前置：样本 ≥1000 场再启动，否则拟合噪声

### 阶段 4 执行层升级（⬜ 未开始，计划 2-4 周）
- ⬜ 动态挂价 `execution_pricing.py`
- ⬜ 未成交处理 `order_management.py`（30s/1min 撤重挂，<5min 弃追）
- ⬜ 止损止盈研究（tp_* 配置雏形已有但 disabled）
- ⬜ 订单审计 `order_audit.jsonl`（执行成本归因）

### 阶段 5 组合级风控（⬜ 未开始，计划 2-4 周）⚠️ 实盘后优先级实质提升
- ⬜ 固定风险金额（总资金×0.5%-1%/单）
- ⬜ 日亏损熔断 `risk_manager.py`
- ⬜ 波动率目标仓位
- ⬜ 漂移监控 `drift_monitor.py`（drift_alerts.jsonl 已有雏形）

### 阶段 6 监控与迭代（⬜ 长期）
- ⬜ Dashboard 健康/权益/P&L 卡片（数据源已就绪差 UI）
- ⬜ 每日自动报告 `daily_report.py`
- ⬜ 再训练管线 + 四维表定期重校
- ⬜ A/B 测试框架

### 基础设施债
- ⬜ pyarrow 安装（Parquet 前置，需用户点头才动 .venv）
- ⬜ launchd EX_CONFIG 根因（已绕过不阻塞）
- ◑ prompt.md 档案滞后 round36.1 / 定时任务#4 描述锚点 round33.2（均注明无行为风险）

---

## 四、建议下一步顺序（实盘语境优先级）

```
第 1 优先（本周 · 实盘保护）
  ① 实盘首周观察：每日滚动 EV / done-results 对账 / max_price 缺失是否复现
  ② 5.2 日亏损熔断 + 5.4 漂移监控最小版 —— 真金安全网，1-2 天工作量，
     drift_alerts 已有基础可加速

第 2 优先（下周 · 看得见）
  ③ 6.1 Dashboard 健康卡片 + LIVE/PAPER 模式显示（health_report.json 直读）

第 3 优先（样本到位后 · 再谈精度与模型）
  ④ 特征库攒到 500 场 → 回测/WF 重跑校准
  ⑤ 攒到 1000 场 → 启动阶段 3 ML
  ⑥ 阶段 4 执行层（成交率数据攒够后做动态挂价才有依据）
```

> 统计口径：完成度粗估——阶段0 ≈95%，阶段1 ≈70%（框架齐、深度欠），阶段2-6 ≈0%。
> 整体对照计划书"综合 3/10"起点，当前基础设施分约 **5/10**：研究闭环（回测/敏感性/WF/基准）
> 已建立，但数据深度、模型层、组合风控、可视化四块仍是空白。
