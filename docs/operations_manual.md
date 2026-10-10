---
title: Hermes BTC 操作手册
type: operations_manual
date: 2026-08-21
audience: 分析师 + AI agent(任何接手者)
frequency: 每次分析前必读
---

# Hermes BTC 操作手册

> 接手 Hermes BTC 1H 涨跌合约分析的完整工作流。所有铁律来自 CLAUDE.md + 三轮 AI 审查融合结论(round35+round35.1)。

## 目录

1. [系统全景](#1-系统全景)
2. [每小时工作流(标准流程)](#2-每小时工作流标准流程)
3. [信号写盘 SOP](#3-信号写盘-sop)
4. [复盘写盘 SOP](#4-复盘写盘-sop)
5. [诊断与排错](#5-诊断与排错)
6. [工具命令速查](#6-工具命令速查)
7. [铁律速记卡](#7-铁律速记卡)

---

## 1. 系统全景

### 1.1 角色分工

| 角色 | 干什么 | 输出 |
|---|---|---|
| **你**(人) | 拍板、看结果、批准规则改动 | 决策 |
| **AI agent**(我) | 11 步推理、写信号、写复盘、归档 | 信号 JSON + 笔记 |
| **bot**(自动) | 读 claude_signal.jsonl → Polymarket 下单 | 成交结果 |

### 1.2 三个仓库各司其职

| 仓库 | 位置 | 谁能改 | 内容 |
|---|---|---|---|
| **规则权威** | `prompt_exec.md` + `prompt.md` | 仅大改 + SYNC_MAP 全链同步 | 11 步规则 + 四维表 + G1-G6 |
| **数据/代码** | `scripts/` + `data/` | 受 SYNC_MAP 约束 | 引擎、采集、信号、复盘脚本 |
| **归档/笔记** | `BTC_1H_Trade_Journal/` + `docs/` | 自由 | 复盘笔记 + 架构文档 + 案例研究 |

### 1.3 关键数字(快照)

| 指标 | 值 | 备注 |
|---|---|---|
| 样本库(已结算) | 399 场 | 全结算口径 |
| 样本库(可复算) | 211 场 | 引擎复算口径 |
| 漂移告警 | 4 条 | 全 n<30,仅告警 |
| 当前 round | **round35 + round35.1** | 锚点 |

---

## 2. 每小时工作流(标准流程)

**执行顺序固定,不可乱**:

```
采集 → 同 slug 检测 → 11步推理 → 写信号前 last-check → 写信号 → 复盘上一小时 → 报告 → 本轮笔记
```

### Step 1: 采集 + 同 slug 检测(一条命令)

```bash
cd "$(ls -d /sessions/*/mnt/'hermes btc' 2>/dev/null | head -1 || echo '/Users/tangyijun/Desktop/hermes btc')" && source scripts/hermes_env.sh && "$HERMES_VENV" hermes_collect.py --force --slim && echo "--- 双触发检测 ---" && grep -c "<本时段slug>" data/hermes_done.jsonl || echo "0 → 非重复"
```

**判断**:
- `grep -c` 返回 **>0** → 已 FILLED → **跳过 Step 2-4,直接 Step 5 复盘**
- `grep -c` 返回 **0** → 继续

### Step 2: 读数据 + 11 步推理

读 `/tmp/hermes_market_data.json` 或 `$HERMES_DATA/hermes_market_data.json`。**完整字段**:
- `slug`(本时段标识)
- `timeframes.{1m/5m/15m/1H/4H/1D}`(MACD/RSI/K线/recent_dir)
- `volatility_5m`、`anchor_price`、`current_price`、`deviation`、`deviation_trend`
- `capital_flow`(Taker B/S、订单流、资金费率)
- `polymarket.{up_price, down_price}`
- `historical.up_rate`(本时段历史)
- `opening.dir` + `prev_hour.flips`(摆动市判定)

**11 步速查**(详细见 skill):

```
1-6 情境:阶段+波动率+Regime(摆动市/趋势市)+贴线+PM
7   计分:8 族因子按权重累加 → S₀
8   PM 行:S₀ 与 PM 同向 → 0;反向 → PM 表逆共识梯度
9   净分=S₀+PM → 方向
10  门槛层 G1-G6 + H9/H12
11  四维表概率映射 + EV 计算
```

### Step 3: 写信号前 last-check(round33.2 铁律)

```bash
cd "$(ls -d /sessions/*/mnt/'hermes btc' 2>/dev/null | head -1 || echo '/Users/tangyijun/Desktop/hermes btc')" && source scripts/hermes_env.sh && curl -s 'https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT' | python3 -c "
import json, sys
d=json.load(sys.stdin)['data'][0]
last=float(d['last'])
import json as j
md=j.load(open('$HERMES_DATA/hermes_market_data.json'))
cur=md['current_price']; anchor=md['anchor_price']
delta=abs(last-cur)/anchor*100
cross=(cur<anchor and last>=anchor) or (cur>=anchor and last<anchor)
print(f'last={last} cur={cur} Δdev={delta:.4f}% 跨轴={cross}')
print('→ v1 直接写' if (delta<0.10 and not cross) else '→ 需 refresh')
"
```

**判断**:
- `Δdev<0.10%` 且**未跨轴** → 写信号,reason 标 `"v1 未刷新"`
- `Δdev≥0.10%` 或**跨轴** → refresh 重算(详见 SKILL round33.2)
- refresh 后**仍振荡** → SKIP + risk 标"信号面振荡"

### Step 4: 写信号(原子动作)

```bash
cd "$(ls -d /sessions/*/mnt/'hermes btc' 2>/dev/null | head -1 || echo '/Users/tangyijun/Desktop/hermes btc')" && source scripts/hermes_env.sh && "$HERMES_VENV" scripts/write_signal.py '<JSON>'
```

**JSON 必含字段**:
```json
{
  "slug": "bitcoin-up-or-down-august-21-2026-5pm-et",
  "signal": "UP",            // 或 DOWN
  "side": "UP",              // 同 signal
  "action": "ORDER",         // 或 SKIP
  "source": "btc-agent",     // ⚠️ 必为 btc-agent,否则 bot 忽略
  "probability": 0.57,       // 四维表小数口径
  "confidence": "中",        // 高/中/低
  "max_price": 0.60,         // 统一封顶
  "position_scale": 0.5,     // 高=1.0/中=0.5/低=0.5
  "ev": 0.16,                // 百分数/100
  "pm_up_price": 0.42,       // 快照值
  "pm_down_price": 0.58,     // 快照值
  "reason": "净分+4顺共识...",
  "risk": "⚠️..."
}
```

### Step 5: 写后核对(权威)

```bash
bash scripts/check_order.sh <本时段slug>
```

**不要靠队列判断**(claude_signal.jsonl 几分钟就被清空),权威核对看 done.jsonl + last_signal.json。

### Step 6: 复盘上一小时(强制铁律,**不可跳**)

详见 §4 复盘 SOP。

### Step 7: 报告 + 本轮笔记

报告放 `$HERMES_DIR/analysis_report_<时点>_<日期>.md`。笔记放 `BTC_1H_Trade_Journal/YYYY-MM-DD_{HHMM}_复盘.md`(canonical 路径由 `scripts/journal_path.py <slug>` 输出)。

---

## 3. 信号写盘 SOP

### 3.1 同 slug 双触发检测(第一步优先)

**问题背景**: cron/手动可能同小时段二次触发。当前 slug 若已在 `data/hermes_done.jsonl` 且为 FILLED → 本轮**禁止再写信号**。

**检测命令**(并入采集):

```bash
grep -c "<本slug>" data/hermes_done.jsonl
```

- `>0` → **跳过 Step 2-4,直接 Step 5 复盘**,回复注明"二次触发,未新写信号"
- `0` → 继续正常流程

### 3.2 EV 阈值

EV < 5% **不再 SKIP**(round31.1 取消),EV 仅作日志字段,记录到 reason 供复盘。

### 3.3 source 字段铁律

`source` 必须 = `"btc-agent"`(hermes_1h_bot.py:35 白名单只认 btc-agent/BTC Sentinel/btc_signal_watcher)。**写错 bot 不消费**。

### 3.4 reason 字段必含刷新标记

- `"v1 未刷新"` 表示 last-check 通过直接写
- `"v2 refreshed"` 表示经过 refresh 重算

### 3.5 risk 字段必含

- G1-G6 逐项结论(✅通过 或 ⚠️触发项)
- 数据缺失/延迟标注

### 3.6 调试模式(绝不入实时队列)

```bash
# 预览不落盘
"$HERMES_VENV" scripts/write_signal.py --dry-run '<JSON>'
```

**严禁用 write_signal.py 写假 slug 探针**(会污染实时队列)。

---

## 4. 复盘写盘 SOP

### 4.1 复盘上一小时(写信号后**立即**,不可跳)

**canonical 路径**:
```bash
python3 scripts/journal_path.py <本轮slug> --prev
```
输出含 `prev_slug` + `prev_canonical`(24h %H%M 命名,跨日自动处理)。**禁止 am/pm 别名与手算跨日**。

### 4.2 找成交数据

```bash
grep "<上小时slug>" data/hermes_done.jsonl | tail -1
```

字段:
- `order_id` / `entry_price`(`buy_price`)/ `shares` / `cost` / `actual` / `pnl`
- 若 done 无该 slug(下单失败)→ 改查 `data/hermes_bot_launchd.log`

### 4.3 结算方向判定

```python
上小时锚定 = data/snapshots/<上slug>*.json 的 anchor_price
本小时锚定 = current slug 的 anchor_price
涨:本小时锚定 > 上小时锚定 → UP
跌:本小时锚定 < 上小时锚定 → DOWN
```

### 4.4 WIN/LOSS + P&L

- WIN: `pnl = shares - cost`(正)
- LOSS: `pnl = -cost`(负)
- 未成交: `pnl = 0`

### 4.5 因子归因(必须 4 块,**不可省**)

```
✅ 对的因子表:读数 + 贡献分 + 实际验证
❌ 错的/误导因子表:读数 + 误导方向 + 教训
🎯 关键决策点回顾:G5/H9/H12/G1-G6/PM顺逆
📚 3-5 条教训:具体可操作
```

**省略归因 = 复盘价值归零,与漏复盘同等级违规**(详见 `memory/feedback-back-review-mandatory.md`)。

### 4.6 落盘铁律(§7.3/7.4)

1. **归因读数与快照一致**: Taker 字段有值不许标"数据缺";订单流/15m MACD 引用对齐最新快照
2. **先读后写**: obsidian_read 拿 etag,存在才 edit,不存在才 create(用 canonical 名,禁止 am/pm 别名)
3. **写后必重读验证**: result/p_l/actual 已入档才算落盘成功
4. **"调用了工具" ≠ "落盘成功"** — 必须 read 验证

### 4.7 成交状态判定

- **以 done.jsonl + Obsidian canonical 笔记回填为准**
- bot 日志的"订单超时取消"行**不能**直接判未成交(8/19 9am 单 timeout 但实为 WIN)
- 交叉 done + 笔记,单行日志不算数

---

## 5. 诊断与排错

### 5.1 信号没生效

| 症状 | 可能原因 | 检查 |
|---|---|---|
| `claude_signal.jsonl` 没新行 | write_signal.py 未执行成功 | 检查返回 `ok:true` |
| bot 没下单 | source 字段错 | 必为 `btc-agent` |
| bot 下了但 timeout | Polymarket best_ask 缺失 | 查 bot log 的 `⏳ 撤单` 行 |
| 信号方向被改 | 校验层覆盖 | 查 last_signal.json 的 `corrected` |

### 5.2 复盘笔记找不到

```bash
python3 scripts/journal_path.py <slug> --prev
```

跨日自动处理,**禁止 am/pm 别名**(6am→0600,7am→0700)。

### 5.3 字段数据缺失

**核心字段**(anchor_price/current_price)缺失 → G1 SKIP,**唯一 SKIP 场景**。

**其他字段缺失** → 不阻塞,降级处理 + risk 标注。

### 5.4 跨轴抖动(5pm-et 类)

**症状**: last-check 通过但 :13-:15 价格跨轴。

**当前(未落表)**: 已实测 0.10% 阈值是 81.2% 胜率,**不收紧**(见 `docs/case_studies/refresh_threshold_round35.1.md`)。

**真药方**(待立项): 监控环方案 A/B/C——

- A. 写信号前强制 engine 重算校验
- B. 贴线抖动场快速 5 步推理
- C. 信号 JSON 加 `engine_verified_direction` 字段

### 5.5 AI 手算 vs 引擎机算不一致

**症状**: 写出去的信号方向与引擎 `judge()` 返回的 `final_direction` 不同。

**当前**: 校验层读 AI 同源数据,无法纠偏。**进监控环待立项清单**。

---

## 6. 工具命令速查

### 6.1 采集
```bash
source scripts/hermes_env.sh && "$HERMES_VENV" hermes_collect.py --force --slim
```

### 6.2 写信号
```bash
"$HERMES_VENV" scripts/write_signal.py '<JSON>'           # 写入
"$HERMES_VENV" scripts/write_signal.py --dry-run '<JSON>'  # 预览
```

### 6.3 核对订单
```bash
bash scripts/check_order.sh <slug>
```

### 6.4 canonical 路径
```bash
python3 scripts/journal_path.py <slug>            # 本小时
python3 scripts/journal_path.py <slug> --prev    # 上一小时
```

### 6.5 健康检查(统计层)
```bash
python3 scripts/rule_engine.py --selftest         # 38 断言
python3 scripts/signal_validator.py --selftest    # 6 断言
```

### 6.6 周日体检(每周日跑)
```bash
python3 scripts/regime_split_replay.py            # 漂移告警 + Wilson + 时间切分
```

### 6.7 监控环 #1 回测(刷新阈值,非生产工具)
```bash
python3 scripts/refresh_threshold_backtest.py     # 一次性,归档为案例研究
```

---

## 7. 铁律速记卡

**任何时候出问题,先核这 8 条**:

1. ⚠️ **每个 bash 先 cd 自定位** — `cd "$(ls -d /sessions/*/mnt/'hermes btc' 2>/dev/null | head -1 || echo '...')"`
2. ⚠️ **同 slug 已 FILLED → 不写信号,只复盘**
3. ⚠️ **source 必为 `btc-agent`**
4. ⚠️ **EV <5% 不再 SKIP,仅日志**
5. ⚠️ **写信号前必 last-check(round33.2)**
6. ⚠️ **写信号后必复盘上小时(含归因)**
7. ⚠️ **canonical 路径用 `journal_path.py`,禁止 am/pm 别名**
8. ⚠️ **改规则必看 SYNC_MAP.md,锚点 round35**

---

## 附录 A:配置文件位置

| 配置 | 位置 |
|---|---|
| 规则权威 | `prompt_exec.md` |
| 版本历史 | `prompt.md` |
| 项目入口 | `CLAUDE.md` |
| 同步铁律 | `SYNC_MAP.md` |
| 定时任务 | `~/Claude/Scheduled/btc-1h-hourly-analysis-v2/SKILL.md`(MCP 管理) |
| 学习架构 | `docs/hermes_learning_architecture.md` |
| 状态快照 | `docs/self_learning_system_state_round35.md` |
| 案例研究 | `docs/case_studies/refresh_threshold_round35.1.md` |

## 附录 B:快速决策树

```
信号计算完 → direction 是否确定?
  ├─ 否(G1 数据失效) → action=SKIP,终止流程
  └─ 是 → 检查 G2/G3/G4/G5 是否触发翻向
     ├─ 触发 → 覆盖方向,confidence 锁对应档
     └─ 不触发 → 用 S₀ 方向
        → 四维表查概率 → max_price=0.60 → EV 计算
        → action=ORDER(除非 EV<5% 但已不 SKIP)
```

---

**手册维护**: 每次 round 抬升(锚点变化)需更新 §1.3 + §7。
**维护者**: 用户 + AI agent
**下次更新触发**: round36 落表 / 监控环立项 / 阶段 4 启动 之一发生。