# 新会话交接：继续打造完整量化交易系统

> 创建时间：2026-08-22  
> 用途：下次新开对话时先读本文件，确认上下文后继续推进。  
> 完整计划书：`/Users/tangyijun/Desktop/hermes btc/HERMES_量化交易升级计划书.md`

---

## 1. 当前系统状态

- 项目：`/Users/tangyijun/Desktop/hermes btc`
- 当前规则版本：`prompt.md@2026-08-22-round41.3-G3证据不全恢复硬覆盖`
- 规则标准：`prompt_exec.md`（AI 执行版唯一权威）
- 引擎状态：
  - `rule_engine.py --selftest`：41/41 通过
  - `signal_validator.py --selftest`：6/6 通过
- 实盘链路：
  - CodeBuddy automation 已退役，`status=PAUSED`
  - **触发 = Claude Desktop 计划任务 `btc-1h-hourly-analysis-v2`，每小时 :28**（8/23 用户决策由 :10 迁 :28，依据 `data/hermes_timing_sunday_analysis_20260823.md`；触发时刻在 App UI 内管理）
  - 节奏：:28 触发 → ~:30 快照落盘 → ~:31-33 信号写入 `data/hermes_last_signal.json` → bot 接单
  - 成交/已处理记录：`data/hermes_done.jsonl`
- SKILL：
  - `.codebuddy/skills/hermes-btc-analysis/SKILL.md`，版本 `v2.1.1`
  - 每轮必须读 `prompt_exec.md` 全文
- 已知问题：
  - `position_scale` 目前仅日志标注，不实际影响下单金额
  - `bet_amount` 依赖 UI，曾出现 `$0.50 / $5.0` 抖动
  - 回测多为场景重放，缺少完整事件驱动回测框架
  - 大量阈值未做敏感性分析，存在过拟合风险

---

## 2. 目标

将 Hermes 从“规则驱动的自动交易工具”升级为：

> 数据驱动、可研究、可验证、可风控、可监控的完整量化交易系统

完整路线图见计划书。

---

## 3. 下一步要做的第一件事

### 阶段 0.1：真实化 bet_amount 和 position_scale

**为什么先做这个？**

- 这是直接影响真金白银的问题。
- 当前 `position_scale=0.5` 只产生日志，不实际缩仓。
- `bet_amount` 依赖 UI，曾导致下单金额异常。

**待确认参数（新对话需要用户确认）：**

1. 默认下单金额：建议 `$5.0`，待确认。
2. `position_scale=0.5` 是否要真实减半金额：建议“是”，待确认。
3. 最小/最大金额：建议 `min=$3.0`、`max=$20.0`，待确认。
4. 是否保留 UI bet_amount 作为兜底：建议保留，但用配置默认值覆盖异常值。

**涉及文件：**

- `hermes_config.json`
- `hermes_1h_bot.py`
- `scripts/write_signal.py`
- 文档同步：`prompt_exec.md` / `prompt.md` / `MEMORY.md`

---

## 4. 后续推进顺序（按优先级）

| 优先级 | 事项 | 阶段 |
|---|---:|---|
| P0 | 真实化 bet_amount 和 position_scale | 阶段 0.1 |
| P0 | 版本一致性固化（机器可读 rule_version + check_rule_anchor.sh） | 阶段 0.2 |
| P0 | 数据快照版本化（Feature Store） | 阶段 0.3 |
| P0 | 每日健康检查 | 阶段 0.4 |
| P0 | 事件驱动回测器 | 阶段 1.1 |
| P0 | 阈值敏感性分析 | 阶段 1.3 |
| P1 | 组合级风控（日亏损熔断、漂移监控） | 阶段 5 |
| P1 | 执行层升级（动态挂价、未成交处理） | 阶段 4 |
| P1 | 数据与特征升级（盘口、跨市场） | 阶段 2 |
| P1 | 模型层升级（ML 概率模型、Meta-Labeling） | 阶段 3 |
| P1 | 监控仪表盘、自动报告、再训练、A/B 测试 | 阶段 6 |

---

## 5. 关键文件地图

| 文件 | 作用 |
|---|---|
| `prompt_exec.md` | AI 执行唯一权威规则 |
| `prompt.md` | 人工档案与版本历史 |
| `scripts/rule_engine.py` | 门槛层引擎 |
| `scripts/signal_validator.py` | 写入前校验 |
| `scripts/write_signal.py` | 信号写入 |
| `scripts/hermes_collect.py` | 数据采集 |
| `hermes_1h_bot.py` | 自动执行 |
| `data/snapshots/` | 原始快照 |
| `.codebuddy/skills/hermes-btc-analysis/SKILL.md` | Claude 桌面链路 SKILL |
| `.codebuddy/memory/MEMORY.md` | 长期记忆 |
| `HERMES_量化交易升级计划书.md` | 完整计划书 |

---

## 6. 新对话开场建议

新对话可以直接说：

> 继续打造 Hermes 量化交易系统。先读 `HERMES_量化交易升级计划书.md` 和 `HERMES_新会话交接.md`。  
> 开始阶段 0.1：真实化 bet_amount 和 position_scale。  
> 默认下单金额建议 $5.0，position_scale=0.5 真实减半，请确认。

---

## 7. 纪律提醒

- 改规则前必须读 `prompt_exec.md`。
- 改引擎后必须跑：
  - `python3 scripts/rule_engine.py --selftest`
  - `python3 scripts/signal_validator.py --selftest`
- 文档、SKILL、引擎锚点要保持一致。
- 回测未验证的新规则不要直接上实盘。
- 先小仓位验证，再逐步加仓。

---

## 8. 8/23 起观察项（:28 新调度 + AI/引擎偏差周检）

> 8/23 用户安排：调度 :10→:28 已生效（首轮 9pm ET 实证 :28 触发/:30:49 快照/:33:32 信号）。先不改代码，跑一周看数据。

**每周从当周报告抽两次统计（方法只读、零改动）：**

1. **AI vs 引擎偏差**：`grep -H -o "机算[^,；]\*" archive/reports/root-hourly/*.md` + 当周新报告，对照 AI 净分（`S₀ =`）。
   - 已知基线（8/21-23，22/118 场留痕）：偏差常 ±3~5；8/22 06:14 出现过方向打架（AI +1 vs 引擎 -2）。
   - ⚠️ 触发条件：若方向打架或 ≥5 分偏差频率明显上升 → 给 `signal_validator.py` 加"每轮全量记录 ai_s0/engine_s0"补丁（走 SYNC_MAP）。
2. **:28 时点准确率 vs 基线**：新报告的 side==actual 命中率，对比基线——旧 :10 时点平日 70.4%、周末 56.5%、周日 53.6%（BJT 口径，见 `data/hermes_timing_sunday_analysis_20260823.md`）。预期 :28 后平日 ~75%+、周末改善。
   - 样本不足 50 场前不下结论。

**其他 8/23 变更备忘**：openclaw cron 残留已清（备份 `~/.openclaw/backup/`）；fixnextrun watchdog 已退役；历史报告归档 `archive/reports/`；文件盘点 `data/file_inventory_20260823.md`。

---

## 9. 8/23 15:58 修复交接指针（本会话最后更新）

> 完整【已完成/未完成】全景见 **`HERMES_修复状态清单_20260823.md`**（含 UI 重启核实、bug 复查、续做指南）。git log 最近提交：
> - `f64ae5b` 状态清单
> - `74c9727` 三轮(复查修3bug + D6 pytest + B6 + B7 + C1核实 + D7纠误)
> - `d4e3fe8` B4/B5
> - `b3a963e` 二轮(B1/B2/B3/A6/P0-5/D2/D3/D4/D5)

**待你拍板优先master**：LIVE 近11场真实单均亏−2.30/胜率45.5% → 降仓/收熔断/暂回PAPER；D1 git push(10提交未推)；147笔amount=0错账&version历史回填。
**新会话续做直接可做**：C2(fills真实成交价 avg_price)、每日数据备份。当前锚点=round41.3。
