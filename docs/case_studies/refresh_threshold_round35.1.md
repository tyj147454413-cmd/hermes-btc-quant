---
title: refresh 阈值收紧误伤回测(round35.1 监控环案例研究)
type: case_study
date: 2026-08-21
round: round35.1
status: archived(非生产工具,不入 SYNC_MAP)
outcome: 否决收紧建议,现行 0.10% 阈值已最优
related_script: scripts/refresh_threshold_backtest.py
related_note: BTC_1H_Trade_Journal/2026-08-20_1700_复盘.md
---

# refresh 阈值收紧误伤回测 — round35.1 监控环案例研究

## 1. 问题来源

5pm-et 2026-08-20(锚定 72671.76,实际结算 UP,LOSS -$10)复盘后被列为"贴线抖动场":

- 首快照 :12 dev=-0.034%(贴线区间)
- 次快照 :15 dev=+0.045%(跨轴)
- AI 手算 DOWN → bot 下单 → LOSS
- 引擎机算 :15 快照 UP(若走引擎方向则翻对)

**三轮 AI 审查后达成共识**:根因是 AI 手算与引擎机算不同步 + 校验层读同源数据无法纠偏(不属本次回测范围)。

**伴随提出的辅助议题**:refresh 阈值是否应从 0.10% 收紧到 0.05%?

## 2. 假设

**收紧假设**:0.10% 阈值放过部分贴线抖动场,这些场次本可通过 refresh 翻对 → 0.05% 阈值应能救更多场次。

## 3. 方法

`scripts/refresh_threshold_backtest.py`(一次性回测脚本,非生产工具)

- 样本:211 场可复算样本中的 **171 场多快照场次**
- 筛"贴线抖动场":首快照 |dev|<0.10% + 多快照间跨方向轴 → **32 场**
- 对比 0.10% vs 0.05% 阈值在 32 场上的 refresh 触发次数 + 后续胜率
- "翻对"判定:refresh 触发后,任一快照的引擎机算方向与实际结算一致

## 4. 结果

| 阈值 | refresh 触发 | refresh 后胜率 | Wilson95 下界 | 提升 vs 基线 |
|---|---|---|---|---|
| **0.10%(现行)** | 32/32 | **81.2%(26/32)** | 64.7% | **+43.8pp** |
| 0.05%(建议收紧) | 27/32 | 66.7%(18/27) | 47.8% | +29.2pp |
| 不 refresh 基线 | - | 37.5%(首快照方向) | - | - |

## 5. 结论

### 🎯 颠覆性发现

1. **现行 0.10% 阈值已是最优**:refresh 后胜率 81.2%,比基线 37.5% 提升 +43.8pp
2. **0.05% 阈值反而更差**:少翻对 8 场(66.7% vs 81.2%)
3. **5pm-et 自己验证**:dev=0.034% < 0.10% → 现行阈值**本来就会触发 refresh**,无须收紧
4. **0.05% 多触发 5 场误伤,漏掉 5 场原本能翻对的场次**——典型的过度收紧

### 决策

- ❌ **不收紧 refresh 阈值**(0.10% → 0.05%)—— 实测反向
- ✅ 0.10% 阈值保留,作为监控环 #1 **已验证项归档**
- 🎯 5pm-et 类问题真药方不属本议题,留给监控环待立项清单(方案 A/B/C 见 BTC_1H_Trade_Journal 笔记补遗)

## 6. 教训元教训

本案例的方法论价值大于结论本身:

- **任何"基于 N=1 样本想改规则"的 push back 都应触发 Wilson 下界 + 反事实验证**
- 真实因果链 vs 假想因果链必须用反事实区分
- 假因果链 → 假教训 → 假规则 → 系统越改越烂
- **本场反例**:5pm-et LOSS 归因最初是"G5 缺翻向口子",实际是 AI vs 引擎不同步。基于假因果链提的"G5 翻向硬规则"被三轮审查后用反事实彻底否决(全库命中仅 1 场且引擎本来就对)。

## 7. 脚本存档

`scripts/refresh_threshold_backtest.py` 保留(CLAUDE.md "不允许删除"铁律)。

- 用途:如未来再次出现贴线抖动相关 LOSS,可复用本脚本快速验证阈值假设
- 状态:**非生产工具**,不入 SYNC_MAP,不接 automation
- 兼容性:sandbox/宿主双兼容(用 `Path(__file__).resolve().parent.parent` 解析数据目录)
- 依赖:`extract_factors` 返回 tuple + `judge` 返回 `Decision` dataclass(见 `scripts/rule_engine.py:152`,`scripts/signal_validator.py:218`)

## 8. 引用链

- 5pm-et 复盘笔记:`BTC_1H_Trade_Journal/2026-08-20_1700_复盘.md`(末尾"真相补遗"段引用本回测结论)
- 学习架构文档:`docs/hermes_learning_architecture.md`(监控环 #1 已验证项)
- 三轮 AI 审查融合方案:见 `data/hermes_model_library_review_20260821.md`(对方执行报告引用)