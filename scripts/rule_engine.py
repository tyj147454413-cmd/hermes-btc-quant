#!/usr/bin/env python3
"""
Hermes 规则判定器 - prompt.md 门槛层的可执行化（round41.3，2026-08-22 审计修复 + A 方案：G3 恢复硬覆盖 + 宏观趋势族 + H13 守卫 + 概率表全表重校 + 版本防漂移检查）

定位：把 prompt.md 中**确定性的部分**（步骤8 两步计分 → H12 降权/逆势防线 → G1-G6 → 9.7）
写成可测试代码。因子打分本身（Taker 是否"暴力"、订单流是否"双正"）仍是 AI/人工判断，
本引擎接收**已评定的因子最终分** + 原始门槛输入（PM、偏差、MACD hist 等）。

同步策略（诚实声明）：本文件不自动跟随 prompt.md；改 prompt.md 门槛层时须人工同步本文件，
并跑 `python3 scripts/rule_engine.py --selftest` 回归。断言失败 = 规则回归或同步遗漏。

实盘校验（8/11 防错补丁）：scripts/signal_validator.py 在 write_signal.py 写入前调用本引擎
复算 AI 信号（机算因子从 market_data 快照重算，AI 专属因子保守默认），不一致时向保守方向
修正（方向翻转必锁低、引擎 SKIP 优先、fail-open 永不阻塞），校验轨迹落盘 engine_check。

round26 变更（2026-08-17 P0 修复）：
- _probability 函数：固定边缘缩放 → 回测历史胜率直接映射（117场实盘匹配样本校准）。
  旧公式系统性低估胜率17-25%，新映射：（|净分|≥6,低）81.5%、（3≤|净分|<6,低）71.8%、
  （|净分|<3,低）76.7%。样本不足的组合保守估计。
- 移除死代码：G5.4 cover_to（0/267触发）、F1U/BB3 候选翻转型（0/267触发）、
  9.7#5 scale=min（0/267触发）。相关 selftest 案例已同步移除。

已知口径说明：
- PM 行梯度按 H7 当前表：同向一律 0；反向时 50-60% -> 0（round13 修正：50-55% 档 +1 取消，
  该档未经回测、统计负价值），60-70/70-80/≥80% -> 向 PM 方向拉 1/2/3 分。
- H12 降权"四舍五入"按 ROUND_HALF_UP（验证报告 9AM 复推保留 1.5 未入整，结论不受影响）。
- H12 逆势防线仅在贴线模式正式激活时判定（|偏差|<0.05% + 前置门通过），与 G4 区间互斥。
- SKIP 场景概率口径（prompt.md 步骤10"SKIP 场景概率口径"，第六轮文档化）：H12/G6 SKIP 时
  probability = _probability(净分口径, SKIP 判定时刻置信度)——P₀ 取区间中值直接边缘缩放，
  不经 9.7 调整项与步骤10 前置约束取最严；H12 SKIP 用原始净分 d.net + 基准置信度，
  G6 SKIP 用 net_for_prob（G3 硬覆盖后回退为 S0）+ 门槛层处理后置信度（round41.3 恢复）。
- G4 顺共识强信号豁免的 |净分|≥6 判定用原始净分 d.net（S0+PM行），不用 G3 回退后的 S0
  （G3 的 S0 回退仅服务概率映射与 G6 判定；round41.3 A 方案恢复该口径）。
- G5.4 非深度档/中性档"无"行 = 维持当前方向（direction 不修改，即步骤8 初始方向经 G2/G3/G4
  处理后的结果），不回退为步骤8 净分方向；该行逆大趋势判定用原始净分方向 d.net_direction，
  仅作判定参考变量，非最终方向（prompt.md G5.4"当前方向"定义，第七轮文档化）。
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

# 规则版本锚点：prompt.md 2026-08-11 第十轮口径（8/10 重构 + 8/11 七轮修复 + P0 权重微调 + round10 废除 1H MACD 方向计分）
# 8/11 审查修复: zone_cover 覆盖受 H9 前置守卫约束（|偏差|≥0.15% 逆价格位置拦截，仅锁低+0.5），
# 与 prompt.md H9 总览/514/567/631 行对齐；吸筹/派发行同步补充 H9 拦截说明。
# 8/11 P0 权重微调（仅 prompt.md 评分表，本引擎不实现权重乘法，factors 来自上游 AI）：
#   价格位置 ×2→×3（一致率 80.9%）、Taker B/S 分级 ×2→×1（一致率 48.1%）、15m RSI ×1→仅参考（一致率 46.7%）。
# 8/11 round10（用户决策）: 评分表 1H MACD ×2 → 仅参考（单独一致率 28.6% 最误导），不再产生方向分。
#   本引擎 factors 求和由上游决定，不在此删 key；H12/G5.1 的 macd_1h_hist 入参（同向深负组合判定）保留。
# 8/12 round11（用户决策，务实缩仓方案）: 评分表新增「短周期反弹先兆」×2 因子（1m&5m 连续同向≥2 +
#   15m MACD 同号，仅常规矩阵非贴线生效，由 signal_validator 机算为 ±2 进 factors）；G5.4 新增缩仓分支
#   ——深负大趋势(深度空头) + 方向DOWN + short_bounce=+1 + |偏差|<0.15% → 不翻向，锁低+scale 0.5。
#   依据 3PM 场：S0=-4 盲跟深负做空被反弹打脸；全样本回测深负+三重组合 UP率 59-83% 不足以翻向（7PM 反例），
#   加权重至 +3 不足翻向（S0 仅 -4→-1），覆盖为 UP 则 1对1错——故只缩仓不翻向。对称：深正+回落先兆同理。
# 8/12 round12（8/12 6AM 场复盘，G3 摆动市贴线豁免）: G3 在摆动市+贴线（|偏差|<0.15%）场景下
#   维持反向的"价格突破对家锚定≥0.15%"门槛永不满足 → 硬覆盖必然、强证据全废。新增豁免分支：
#   摆动市 + |偏差|<0.15% + 强证据（AI evidence_dims≥2 或 机算 short_bounce 与原始方向同向）→
#   不硬覆盖翻向，保留原始方向 + 锁低 + scale 0.5；net_for_prob 不回退（G6 弱信号 SKIP 判定用含 PM 行的
#   原始净分，8/12 6AM 场 +1 低置信逆共识 → SKIP 规避亏损）；g3_overrode 不置位（G6"G3已改判"例外不适用）。
#   依据：6AM 场 PM 13 分钟从 UP61.5% 反转为 DOWN65.5% 不可靠；PM60-80% 逆共识 10 场反事实维持 60%>硬覆盖 40%；
#   但 G3"顺共识"大方向仍成立（222 场各档顺共识胜率全面优于逆共识），故仅加豁免不废 G3。
# 8/13 round13（用户决策：全量交易，不允许跳过）: 取消 H12 逆势防线 SKIP 与 G6/H11 弱信号 SKIP，
#   只要有分析方向一律 ORDER（唯一例外 G1 数据失效保留 SKIP）。原弱信号 SKIP 场景降级为 9.7 缩仓承接
#   （锁低 + position_scale=0.5）。同时修正 H7 弱档（Y）：PM 50-55% 逆共识 +1 → 0（该档未经回测、
#   统计负价值，与 55-60% 同档归零）。依据：8/12 三单输家（3pm/6pm/7pm）均弱 PM 共识，但用户明确
#   "分析出来了涨和跌，都要交易"，故不以 SKIP 规避，以缩仓控风险。
# 注意：本引擎直接 d.s0=sum(factors.values())，不验证权重正确性。selftest 中的 factor 数值（如 taker_bs ±6）
# 是历史真实场景快照，反映当时执行权重（Taker 分级 ×3 下机构级基础 ±2×3=±6），仅用于验证规则逻辑链路
# （G3/G4/G5/H12/G6 判定），不代表当前权重。勿据 selftest 数值断言当前权重正确性。
# 8/14 round18（反手预警分级：精确翻转，不再无差别缩仓）: 用户质疑 round17 A 方案
#   "锁低+缩仓0.5 误伤正确场次"。深入分析（scripts/countertrend_*.py +
#   data/hermes_countertrend_redesign_20260814.md）发现：
#   (a) round17 的 4 维组合有缺陷——21 场触发中"弱信号/顺共识60-80%/4H衰竭"三常维 21/21
#       全命中，只有"反向历史"有区分度，导致触发面太宽，把 18 场判断正确的场次也拉来降级；
#   (b) 3 个真实错误场（8/13 12am/2am、8/14 1am，预测DOWN实际UP）无任何 G3/G2/G4 覆盖，
#       是纯"贴线+顺共识+4H浅势衰竭+做空依据弱"陷阱，共享 F1 特征；
#   (c) F1 在全集 72 场验证：命中 3 场 = 恰好 3 个目标错误场，0 误伤，净 +3。
#       三个过滤器各排除一个误伤场：|4H hist|<50 排除深势正确跟随；净分≤-3 排除随机游走；
#       顺共识60-80% 排除深度共识(PM≥80%)。
#   round22（8/15 贴线优化回测）：F1 净分阈值 ≤-3 → ≤-4。92 场重放 F1 翻转 4 场
#       （净分 -5/-3/-4/-5），唯一错判 8/14 11pm（净分 -3 擦线），收紧 ≤-4 恰好剔除该场，
#       前 3 场对的不受影响 → F1 3/3=100%，0 误伤；贴线模式最终方向 73.1%→76.9%。
#   故 round18 落地三档分级（净分阈值 round22 收紧为 ≤-4）：
#     翻转级：F1 全满足（预测DOWN+贴线模式+顺共识60-80%+4H衰竭+|4H hist|<50+净分≤-4）
#             → 方向翻转 + 置信度锁低；【不缩仓】（用户：仓位本就小，缩仓无意义）。
#     观察级：反手预警 ≥3/4 维但不满足 F1 → 方向/置信/仓位完全不动，仅 risks 标注（不误伤）。
#     不触发：<3/4 维 → 不动。
#   修正 round17 错误认知：8/13 9pm 是"PM逆共识(UP54.5%)+衰竭"型错误，非顺共识型，n=1 不单独设规则。
#   8/14 bug 审查修复（round18_bug_audit.py + round18_f1_boundary.py 验证）：
#     (1) F1 翻转后新方向必须不逆价格位置——补 G4/H9 前置：|dev|≥0.15% 且翻转目标逆 price_dir 时
#         禁止翻转（降为观察级），防 F1 绕过 G4/H9 硬约束。3 目标场 |dev|~0.08%<0.15% 不受影响。
#     (2) F1 翻转后清理 9.7 注入的 scale（scales.clear()）——否则贴线激活(|dev|<0.05%)场会残留
#         scale=0.5，违反"翻转级不缩仓"设计（round17 误伤教训重演）。
#     (3) 翻转后顺共识风险标注语义错位（翻转前 DOWN 顺共识降级/收割标注，翻转后实为逆共识）
#         ——翻转级 risk 追加"顺共识标注作废"澄清，避免复盘误导。
#     观察级 9.7 缩仓（如 G5.4 逆大趋势/H12/贴线+共识注入 0.5）属其他独立规则，非反手预警干预，
#     观察级"反手预警不动"语义成立，不视为 bug。
# 8/15 候选翻转型别观察级标注（F-框架阶段1，不改变 round19 方向/置信/仓位语义）：
#   依据 data/hermes_flip_conditions_design_20260815.md——翻转合法 = F-框架四条件
#   （证伪层/时机/方向基础薄/样本背书）。新增两个候选型别，n<30 只观察级标注收集样本：
#     F1U：F1 对称化（预测UP+贴线+顺共识60-80%+4H动能衰竭+|4H hist|<50+净分≥+3，不逆价格位置）
#     BB3：深跌末端反转（预测DOWN+4H bb_pos≤10+4H跌势衰竭+顺共识60-80%+历史反向+护栏2 |4H hist|<50）
#   护栏2 教训（flip_conditions_replay 验证）：BB3 初版缺 |4H hist|<50，8/11 1am（hist4=-159.91 深空势）
#   误伤 → 已加护栏（与 F1 同源，F-框架护栏2：深势正确跟随禁止翻转）。
#   回测结论（data/hermes_flip_conditions_replay_20260815.md）：F1U n=0、BB3 n=0 → 均保持观察级；
#   另发现 8/14 3pm（accel=-0.01 微负）是 F1 翻转级 accel≈0 边界漏网，放宽衰竭口径可翻对，
#   属阈值调整需单独回测，不并入候选型别。
#   ⚠️ 监控铁律：实盘观察级翻转胜率 <50% 立即回退（不升级）；n≥30 且回测 Wilson95 下界显著>保本线才升翻转级。
# RULE_VERSION 已同步至 round41.3（2026-08-22 审计修复 + 用户决策 A 方案，对齐 skill/prompt_exec.md 锚点）。
# round41.2 同步内容（相对 round33 的 8 个版本缺口，一次性补齐 4 个逻辑级）：
#   - 宏观趋势族 ×1 进 S₀（factors 由上游供给，本引擎求和自动包含）
#   - H13 顶部收敛摆动日守卫引擎化（4H RSI 极端 + 3 根已收 1H 交替 + 涨跌幅均 <0.7%）
#   - 概率表四维全表重校（239 场，矛盾格 82.1%"高"修正为保守回退 30.8%"低"）
#   - 版本锚点防漂移：selftest 末尾自动比对文档锚点与本常量。
# round41.3（A 方案，用户决策）：G3 证据不全分支恢复硬覆盖翻向 PM（round40"保留 S₀"经 240 场
#   回测证伪：12 场分歧场翻向 PM 9胜3负 75% vs 保留 S₀ 3胜9负 25%），保留锁低 + scale 0.5
#   建议值（实盘仓位由用户手动设定）；概率/G6 口径回退 S₀（恢复旧口径）。
#   非逻辑级（round34~37 等，数据源/文档层变更）不涉本引擎判定。
# round44（2026-08-26 用户四件套指令，评分层+采集层变更；judge 判定逻辑不变）：
#   ① K线序列拆分双行：原 factors["kline"] 15m/1H 各4根共8根混计×2 → kline_15m/kline_1h 各自独立计票
#     （阳多+1/阴多-1/平0），权重各 ×1（用户「分开评分，权重都改成1」）。依据=两窗口样本相关
#     （15m 4根全落在最后1小时内与 1H 末根重叠），混计给最近一小时双倍计票且两窗矛盾被掩盖。
#   ② 贴线墙背离 ×2→×1：wall_divergence ±2.0×W_WALL_DIV(2)=±4 → ±1×W_WALL_DIV(1)=±1
#     （用户「wall_divergence ×1权重」）。依据=归因 57.1%（n=14 小样本）配最高档 ×2 最不匹配。
#   ③ macro_trend_1h_4h 计算挪进采集主体（hermes_collect collect 步骤11.1，SCHEMA_VERSION 5→6）：
#     此前只在 _apply_slim 计算→全量快照恒缺该字段（实测 12/12 缺失），validator 从快照读取恒走
#     consecutive_up 降级口径（6根含进行中K线），AI 层与机算两层口径不一致 + 归因失真。
#   ④ 权重再归因：factor_attribution.py 重跑（kline 拆分后按新键归因；弱因子去留待新样本再裁决）。
#   验证：validator selftest 6/6 + rule_engine selftest 45/45 + mock(test_round44_kline_split_walldiv_macro.py)。
RULE_VERSION = "exec-round53=新增reversal_intent深跌反转意向单边+1因子(9/3用户拍板「先回测验证→权重±1」:引擎口径回测 scripts/reversal_intent_engine_caliber.py 深跌反UP n=52 胜率65.4% 时间分段65.4/65.4 阈值0.02-0.05参数稳健64-67% EV@0.5入场+0.154, 对称深涨反DOWN n=43 46.5% EV-0.108 无效→无负分档; 代码级变更: hermes_collect.py[根级+scripts双份]新增polymarket.up_price_delta_prev字段[上小时末快照delta,SCHEMA_VERSION 7→8], signal_validator.py新增W_REVERSAL_INTENT=1+REVERSAL_PREV_THRESHOLD=0.03+REVERSAL_CUR_THRESHOLD=0.02+extract_factors reversal_intent单边机算[旧快照缺字段计0 fail-open,不进H12降权键表], judge零逻辑改动factors求和自动含; prompt_exec.md评分表新增深跌反转意向行+版本锚点round53; selftest=signal_validator 6/6+rule_engine 45/45+mock冒烟6/6含边界值+引擎闭环dry-run实测reversal_intent进机算10项; 5min粒度对称版初测57.4%不达标系口径差异,按引擎真实口径单边化落地);前exec-round52.1=贴线模式recent_dir_5m/15m不计分(8/30用户决策「你去修复」,依据scripts/round52_factor_backtest.py变体B回测):贴线盘整市5m动量47.5%反向噪音[80场贴线快照实测,常规67.0%对照],全模式计分致贴线-3场;改为贴线计0后全量72.2%→73.5%[+5场]、贴线66.2%→72.5%[+3场],pm_consensus贴线保持计分[有益];代码级变更:signal_validator recent_dir循环加tie_mode分支计0,prompt_exec评分表/矩阵贴线列改禁用;selftest=signal_validator 6/6+rule_engine 45/45;详见SYNC_MAP六十七段round52.1附记);前exec-round52=P0-1.3阈值敏感性分析驱动因子增删(8/30用户三指令「先验证→加证实最强因子→换掉证伪因子」):删4因子[order_flow 56.7% n=307 EV-2.1噪音 / taker_bs_15m 62.0% EV-15.6滞后 / open_micro 57.9% n=19 EV-9.3冗余 / macro_trend 57.1% n=184 EV-8.9噪音],加3因子全×1[recent_dir_5m 62.9% EV+9.9 / recent_dir_15m 68.0% EV+4.1 / pm_consensus PM绝对价共识78.9% EV+3.2],taker_bs(59.1%)与kline_1h(59.3%)经验证反转保留(研究口径误判,引擎口径真信号);依据docs/factor_sensitivity_p013_20260830.md+scripts/verify_engine_factors.py引擎口径387场;用户拍板「只加不撞铁律的3个+换4个+全部×1保守」;代码级变更:signal_validator删taker_bs_15m/order_flow/macro_trend/open_micro机算+加recent_dir_5m/15m/pm_consensus机算(W_RECENT_DIR_5M/15M/W_PM_CONSENSUS=1)+结算期降权键去双窗,rule_engine H12键表去taker_bs_15m;prompt_exec评分表同步增删+pm_imbalance行补录(round48引擎在计分但文档缺失的同步缺口);selftest=signal_validator 6/6+rule_engine 45/45;详见SYNC_MAP六十七段);前exec-round51.4=Taker B/S ±2 极端档代码落地(8/30用户决策「现在就修复」--复盘4am ET发现prompt_exec评分表Taker B/S 5m/15m两行已声明取消±2极端档[round51],但signal_validator._taker_grade()未实际落地仍返回±2[>2.33→+2 / <0.43→-2],导致本轮taker_bs_15m ratio=0.363实际得-2而非规则的-1,S₀被多扣1分;代码级变更:scripts/signal_validator.py第253-263行_taker_grade()删除>2.33/+2与<0.43/-2两个机构级档,合并为±1/0三档,docstring标注round51.4;双窗最大贡献从±4降到±2生效;selftest验证=signal_validator.py _selftest()6/6+rule_engine.py _selftest()45/45+4am ET快照extract_factors回放taker_bs_15m -2→-1,S₀ -1→0[本场若按新规则跑仍DOWN/低/0.5scale,方向同,标签从'净分-1→DOWN'变'S₀=0→以PM侧DOWN决定+S₀=0观察级标注'];WF baseline继承round51.3+;备份scripts/signal_validator.py.bak_20260830_round514_taker_extreme_removal[md5:0297fda8b5f42bb964a69a4b475f225e];详见SYNC_MAP六十六段);前exec-round51.3+=8/30逐项因子明细落盘(scoring_compare增强:signal_validator check dict加factors完整逐项[L969-971],scoring_compare engine_score块带出factors[L165-166];验证sum(factors)==engine_s0[12项明细与汇总对齐];补丁层,主版本号仍round51.2)[注:实际strategy_version以runtime_meta为准为round51.2,本RULE_VERSION层级是子补丁号round51.3+=见六十五段];前exec-round51.3=8/30定时任务纯引擎简化(新增scripts/engine_signal.py纯机关闭环[collect→signal_validator.validate→write_signal→hermes_review],取代Claude SKILL AI推理外壳,方向/置信度/概率/仓位全引擎裁决;launchd com.hermes.engine_signal :40触发[scripts/com.hermes.engine_signal.plist+SYNC_MAP六十三];前置停用旧桌面端btc-1h-hourly-analysis-v2任务[否则同一分钟双触发];评分规则与权重不变[仍round51.2],仅自动化管道改造;__dry-run__已验4am ET场UP/净分+6落信正确);前exec-round51.2=8/30用户指令PM动量在贴线模式也进引擎计分(signal_validator.py pm_momentum从phase_scored块提取为顶层全模式机算,贴线同样计±2进S₀[此前贴线恒0与AI报+2分歧亏损];口径/阈值/同源/掉头逻辑不变;prompt_exec评分表L369去'成型/博弈期计分'歧义改全模式;回归test_round41810/test_round41816/test_round441全过+贴线场实测+2/-2;SYNC_MAP六十二);前exec-round51.1=8/30报告3.因子读数与计分增加'引擎机算'列+AI不能越过引擎机算改单铁律(全局铁律新增第6条+路由铁律新增第4条;5处定时任务SKILL.md#4 prompt模板编辑;见SYNC_MAP五十三v2);前exec-round50.1=提示词瘦身(8/29用户决策「系统提示太大，分析哪些没用」，纯文档重构零规则语义变更，零代码逻辑改动；230,686B→107,980B/-53.2%；剥离历史版本链+bot执行细节+采集器演进至prompt.md〇版本区/瘦身方案/.trae/documents/；保留11步推理+评分表+G1-G6+9.7+四维概率表+§11自检+复盘四块铁律；同步执行层协同项trading_paused/momentum_burst/smart_money引用恢复；锚点round49.1→round50.1与CLAUDE.md主版本round50对齐；详见SYNC_MAP六十三段+瘦身方案文件；WF baseline继承round49 WF-20260828-0057-8f41；validator selftest 6/6+rule_engine 45/45+全套mock零回归)；前exec-round49=PM动量权重回退×3→×2(8/28用户决策「PM 动量 还是都回到+2」, 代码级变更: signal_validator W_PM_MOMENTUM=3→2, 覆盖round47加权, 判据口径book-book/阈值0.02不变仅回退, 引擎judge零逻辑改动factors自动含; 季节性退出记分项目round47保留不动; WF baseline继承round48; validator selftest正; 见SYNC_MAP五十一); 前exec-round48=聪明钱PM订单簿不平衡pm_imbalance×1入评分(8/28用户决策方案A[|book_imbalance|≥0.3计±1, 权重×1, 仅成型/博弈期计分同pm_momentum口径], 代码级变更: signal_validator W_PM_IMBALANCE=1+PM_IMBALANCE_THRESHOLD=0.3+extract_factors phase_scored块机算factors['pm_imbalance'], 数据源polymarket.book_imbalance[采集器round41.8.11起影子采集本轮转正计分], 回测=data/hermes_factor_candidate_backtest_20260828_smart_money.md A1档81.2%[39/48, join 65场8/25-8/27三天单一市况样本时间集中已披露, 30+结算场后归因复核]; judge零逻辑改动factors求和自动含新键, 概率表不重校待新样本; WF baseline WF-20260828-0027-c831[round47]; validator selftest 6/6+rule_engine 45/45+mock test_round48_pm_imbalance.py; 见SYNC_MAP五十); 前exec-round47=PM动量加权×2→×3+历史季节性退出记分项目(8/27用户决策「PM 动量加大权限。+3。同步引擎机算 PM 动量 +3, 历史季节性 不进人记分项目」, 代码级变更: signal_validator W_PM_MOMENTUM=3[与短反弹先兆/贴线1m taker同档最强信号]; signal_validator 删除历史季节性计分[factors.pop('seasonality')退出评分表, historical_up_rate 字段仍注入供 round17 反手预警组合判定使用]; validator selftest 6/6[case4 修订=round47季节性删除后3PM场S0减少+方向翻向检查+锁低; case5 修订=S0=0临界由价格位置决定]; **round47 PM动量×3已被round49回退×2**; 见SYNC_MAP四十七); prompt.md@2026-08-26-round44.2-G4双向核对(8/26用户决策「C: G4翻向后做双向核对, 翻UP后检查24h位置+宏观趋势+4H MACD是否至少2维支持UP, 否则保留原方向」, 代码级变更: scripts/rule_engine.py 第477-505行 G4 翻向段加双向核对块[3维证据计数+≥2才执行翻向, 否则保留+锁低缩仓], 原2个selftest用例更新[case17/G4翻UP后G5.4维持→G4双向核对0/3不通过保留DOWN; case18/round13弱信号逆位置→G4双向核对1/3不通过保留DOWN, 不再翻UP]; validator selftest 6/6+rule_engine 45/45[2个用例名称+断言更新反映round44.2新规则]+回归持续观测; 见SYNC_MAP四十六); 前exec-round44.1=采集层方向字段已收盘口径(tf_summary recent_dir/consecutive_up-down剔除进行中K线[SCHEMA_VERSION 7], 与41.8.5/MACD/K线序列口径统一)+评分层PM动量×2(W_PM_MOMENTUM=2, 归因重跑73.3% n=30与momentum_burst先例同档); prompt.md@2026-08-26-round44.1-瑕疵②修复+pm动量×2(8/26用户决策「瑕疵②现在就修+mock」「③B观察不动」「pm_momentum权重修改+2」); 前exec-round44=评分层round44(K线序列拆分15m/1H双行各×1+贴线墙背离×2→×1+macro_trend_1h_4h入采集主体[SCHEMA_VERSION 6]+权重再归因重跑); prompt.md@2026-08-26-round44-K线拆分双行各×1+墙背离×1(8/26用户四件套指令「15m/1H分开评分权重都改成1」「wall_divergence ×1权重」「macro_trend计算挪进采集主体」「权重再归因重跑」, 代码级变更: signal_validator W_KLINE_15M/W_KLINE_1H=1替代旧kline±2混计+W_WALL_DIV语义并入基准幅度记分±4→±1+hermes_collect步骤11.1全量快照含macro_trend字段; judge判定逻辑不变, factors求和自动包含新键); 前exec-round43.2=评分层同主版本round43.2(贴线1m-taker权重×3→×1); prompt.md@2026-08-26-round43.2-贴线1m-taker权重×3→×1(8/26用户决策「权重改小一点试试」, 代码级变更: signal_validator W_TAKER_1M_TIE=1+贴线分支不再乘W常量直接写±1, 依据=round43.1诊断性回测贴线场1m taker命中率45.8%<50%原×3权重放大弱势信号拖累概率表0-2贴线+PM=T格-7.2pp漂移, 降到×1后-3.9pp漂移减半且整体S0命中率维持56.8%/+0.9pp改善, 待更准样本观察; validator selftest 6/6+rule_engine 45/45+mock 12/12[test_round431_taker_dual_window.py 含权重×1断言更新]; 见SYNC_MAP四十二+四十三); 前exec-round43.1(评分层Taker双窗+贴线1m-taker机算[原×3已被×1回退, 用户8/26决策]; 见SYNC_MAP四十二); exec-round43(bot执行层无变动=round43双闸收紧[MAKER_CAP/TAKER_MAX 0.80/0.88→0.65]口径沿用, 评分层round43.1/43.2对bot零影响); exec-round43(validator层_consensus_flip: 最终方向逆PM≥60%强共识→翻向PM, 概率=PM方向快照价, 锁低; round41.8.13共识价来源修正=优先真实订单簿book_up_mid[0-1×100对齐pm_up, 缺失退回gamma标记价up_price, 标记价偏离盘口≥10pt记⚠️标记价失真], 修复8/24 3pm场标记价0.705 vs盘口0.44/实时0.28失真骗翻向; 实时价复核防线: 实时共识价优先book_up_mid[缺失退回标记价], 实时共识未反向≥55%且快照≤5min保持快照判定; 弱共识绝不翻向; judge内部逻辑自round41.6后未变[至41.8.13, 41.8.14起judge共识源改book_up_mid见下]) | 8/24文档对齐: G2三条件维持/G3强证据维持/贴线豁免/价格门槛拦截均仅引擎中间态, validator G3.5无≤80%豁免必翻向PM(注释标注, 零行为变更) | 执行层exec-round42.2(K线序列4根+价格位置阈值0.10%: 8/25用户指令「K线序列修改成4根」+「价格位置阈值放宽0.10%进入记分项目」--hermes_collect tf_summary新增last_4_candles[已收盘4根], signal_validator kline因子优先读last_4缺退last_3兼容旧快照[判分口径=15m+1H各4根共8根阴阳计数]; signal_validator PP_ANCHOR_DEV=0.15→0.10[dev 0.10-0.15%区间从灰区计0改为计±1锚定侧, G4/H9硬约束G4_DEVIATION仍0.15%未动分层]; validator selftest 6/6+rule_engine 45/45+回归141/141[test_round41820_pricepos_anchor T5a/T5b边界0.15→0.10同步修正]; 见SYNC_MAP三十八); 前exec-round42.1(短周期反弹先兆降权×2→×1: 8/25用户指令「短周期反弹先兆。降权，成1」--signal_validator W_SHORT_BOUNCE=2→1, 评分表短周期反弹先兆行权重×2→×1[判据口径不变仅降权], 依据=8/25 6AM ET场short_bounce=-2误导方向[实际UP LOSS]; validator selftest 6/6[case5 3PM场期望S0 +2→+1]+rule_engine 45/45+回归107/107; 见SYNC_MAP三十七); 前exec-round42(G3.5翻向禁用: 8/25用户决策「G3.5翻向给禁止使用」--validator _consensus_flip翻向执行段no-op[仅观测]+judge G3证据不全分支改保留S₀方向+锁低+scale0.5+net_for_prob不回退, 依据=实盘11笔翻向36.4%胜率-$67.65 vs回测+$69.90完全反向; 见SYNC_MAP三十六); 前exec-round41.8.20(价格位置因子锚定口径统一: 8/25用户指令「都改成锚定价格-24h分位不使用」--signal_validator extract_factors价格位置因子与G4/H9同源同基准[|deviation_pct|>=0.15%计±1锚定侧,<0.15%计0未选边,PM弱共识两侧<0.60计0仅参考沿用41.8.9且共识源book_up_mid优先缺失退标记价],24h分位current_percentile/in_upper_half/in_lower_half退出该因子[旧极端位<15/>55仅参考规则废除],G5.4吸筹/派发区标记与sr_proximity参考口径不变[均不改方向]; prompt_exec评分表/矩阵/结算期规则/H9行/G4节/自检清单/两报告模板同步锚定口径,消除AI层[按分位记0]与机算[按dev记±1]两层S0不一致[实证=8/25 2am场AI记0 vs机算+1; 1am场AI按分位85.6%恐高计-1被复盘判误]; mock 20/20(test_round41820_pricepos_anchor.py含2AM/3AM事故场快照回归)+selftest 45/45+validator 6/6[selftest期望S0重同步:case1 -1->0/case5 +1->+2; 6AM场终局路径G3顺共识改round41.6共识翻向,断言不变]+回归4188 11/11+41819 16/16+41816 13/13+4185 9/9; 见SYNC_MAP三十五); 前exec-round41.8.19(翻向后PM基准回填+max_price口径拆分: 8/25用户指令「管道层缺陷修复+交叉验证」--①signal_validator _validate_with_md 包装出口统一回填: 凡ORDER且final side的pm_{side}_price缺失/非法时从快照gamma标记价回填[41.8.17同源口径,绝不填book_up_mid],已有有效值不覆盖/SKIP不处理/标记价缺失不造值,覆盖方向翻转+AI-SKIP→ORDER+round41.6共识翻向三路径;事故实证=8/25 1am场AI原始DOWN写pm_down_price=0.335,引擎S₀+4翻UP后pm_up_price=None,bot一般波动检测+C4影子按final side取pm_up_price双双静默失效;②bot save_last_signal展示副本max_price改写实际挂单价时保留分析基准进max_price_signal,修「max_price=0.8与ev=(P−0.60)×100同框」; mock 16/16(test_round41819_pm_refill.py含1am事故全路径复现)+回归58/58+selftest 45/45+validator 6/6; 见SYNC_MAP三十四); 前exec-round41.8.18(AI层momentum_burst因子行强制读数: 8/25用户指令「直接改prompt_exec.md并记SYNC_MAP」, 纯文档层零代码--评分表行备注+7.3双处落地: 每轮因子表必含momentum_burst行且写实际15m vol_ratio+recent_dir[采集器恒产字段], 禁标数据缺[整份快照缺失属G1域], ≤1.5写明数值+不触发, 权重标签一律×2; 违规实证=8/24 11pm ET场报告标数据缺实为vol_ratio=1.12<1.5不触发+9pm/10pm两轮整行漏写; 见SYNC_MAP三十三); 前exec-round41.8.17(AI层PM共识判定读数同源: 8/25用户决策, prompt_exec核心规则/步骤8 PM行/关键字段定义/报告模板S3.6--AI层所有PM共识判定[PM行计分/9.7共识区收割区/步骤7庄家视角PM定价读数/报告PM共识X%表述]优先读book_up_mid×100[缺失退gamma标记价×100, 偏离≥10pt标注⚠️标记价失真]; 信号字段pm_up_price/pm_down_price仍写标记价不变[bot一般波动检测对比基准=gamma实时价须同源]; 纯文档+模板层变更引擎采集器零代码, 至此四层同源[judge41.8.14/validator翻向+实时复核41.8.13/pm_momentum delta41.8.16/AI读数41.8.17]全部以真实订单簿为共识事实源, 见SYNC_MAP三十二); 前exec-round41.8.16(pm动量delta口径同源切订单簿: hermes_collect up_price_delta改book-book同源[当前与上一快照book_up_mid均有效], 任一侧缺失整体退mark-mark绝不混合, 计算挪至订单簿采集后, 新增up_price_delta_source[book|mark]字段SCHEMA_VERSION 4->5, 配套signal_validator掉头检测绝对价与delta同源[book源优先book_up_mid防假掉头]; 事故实证=8/24 3pm场标记价0.705[假]vs盘口0.44, 4pm场旧口径mark delta=-0.265假暴跌, 真实book-book仅-0.025; mock 13/13, 见SYNC_MAP三十一); 前exec-round41.8.15(价格位置权重×2→×1: 8/25用户决策「×1价格位置都改成1」, signal_validator W_PRICE_POS=2→1, 极端位/PM弱共识维持×1无额外降权空间, judge判定逻辑不变; 前exec-round41.8.14(judge共识源无条件切真实订单簿: 8/25用户决策「直接上无条件版不重放」, judge入口单点替换pm_up←book_up_mid[extract_factors注入0-100, 有效即无条件采用, 标记价偏离≥10pt记trace⚠️标记价失真, 缺失退回gamma标记价up_price], PM行/H12逃生门/G2/G3/G4豁免/G5.4护栏/G6/概率表顺PM维度/_flip_target_price价格闸门同源全覆盖, 配套signal_validator实时复核live_inp同步传book_up_mid防旧快照盘口覆盖live共识; 依据=8/24 3pm场标记价0.705 vs盘口0.44 vs实时0.28, judge层是最后一个读假标记价的口子[validator翻向层41.8.13已修]; 回测豁免=用户决策风险可控; 前exec-round41.8.13=翻向共识价改用订单簿book_up_mid修复标记价失真骗翻向, 前41.8.12=三因子权重修正[1H Taker方向计分废除仅参考/订单流×2→×1/K线序列×1→×2], judge判定逻辑不变; 前exec-round41.8.10=PM动量因子pm_momentum[polymarket.up_price_delta采集器算当前up_price−上一slug快照, |Δ|≥0.02计±1×1, 独立于PM绝对价捕捉聪明钱流向, 掉头时notes标⚠️PM动量掉头]; 前exec-round41.8.9=归因驱动四条权重修正[sr_proximity方向计分废除不记分仅参考/momentum_burst×1→×2/15m MACD废除方向计分仅参考/PM弱共识[两侧<0.60]时price_position×2→×1]; 前exec-round41.8.8=阻力支撑位邻近因子sr_proximity, 已被41.8.9废除; 前exec-round41.8.7=前瞻观察因子momentum_burst+全局暂停开关trading_paused; 前exec-round41.8.6=AI层因子计分口径四条铁律入部署技能表[开盘微观None禁编造/订单流双窗共振/K线序列last_3_candles口径/概率引擎回写禁旧值93.3%], 零行为变更; 前exec-round41.8.5=采集器K线序列已收盘口径修复: hermes_collect tf_summary().last_3_candles剔除Binance返回最后一根进行中K线, 8/24 8AM ET假末阴事故[进行中1H暂阴被计-1, 该小时最终收+0.91%大阳, DOWN亏$10], judge判定逻辑不变; 前exec-round41.8.3=G2/G3管辖权文档对齐+consensus_flip_bucket分桶插桩零行为变更; 同日并行轮41.8.2=EV统一每$100美元口径[write_signal+validator机器层]+盘面时段表删除; bot层hermes_1h_bot.py逻辑为41.7两段式TAKER_MAX=0.88 / 41.8规则A / 41.8.7暂停开关; 引擎judge判定逻辑不变)"

CONF_LEVELS = ["低", "中", "高"]  # 索引越大越自信
H12_DEPTH_THRESHOLD = 30.0       # |hist| 深度固定阈值（G5.1 与 H12 统一口径）
G4_DEVIATION = 0.15              # G4/H9 价格位置硬约束偏差阈值 %
TIE_LINE_DEVIATION = 0.05        # 贴线偏差阈值 %
# round18 反手预警分级阈值
CW_HIST_BULL = 0.55              # historical.up_rate > 0.55 时段偏多（预测 DOWN = 反向历史）
CW_HIST_BEAR = 0.45              # historical.up_rate < 0.45 时段偏空（预测 UP = 反向历史）
CW_MIN_DIMS = 3                  # 观察级触发最小维度数（4 维中命中 ≥3 触发）
CW_F1_DOWN_NET = -4.0            # F1 翻转级：净分 ≤ -4（round22 收紧；-3 擦线场 8/14 11pm 唯一翻错，剔除后 F1 3/3）
CW_F1_HIST4_MAX = 50.0           # F1 翻转级：|4H hist| < 50（4H 大趋势浅，排除深势正确跟随）
# round23 反转价格门槛：反转目标方向 Polymarket 价格必须 ≥0.23 才允许翻转（用户决策）。
# 依据：历史贴线 6 场翻转目标价全部 ≥0.23（0.305/0.315/0.375/0.605/0.735/0.785），6 场全对 0 误伤 0 漏杀；
# 目标价<0.23 说明市场给该方向<23% 隐含概率（赔率极差/极度不看好），反转买入性价比差甚至危险。
# 作用口子：F1 翻转级 / G3 硬覆盖顺共识 / G5.4 cover_to；G4 回退到价格位置除外（非反转）。
REV_PRICE_MIN = 0.23             # 反转目标方向价格硬门槛（round23）
# 8/15 候选翻转型别观察级阈值（F-框架阶段1：先观察级收集样本，n≥30 且回测胜率显著才升翻转级）
CW_F1U_NET = 3.0                 # F1U 观察级：净分 ≥ +3（预测 UP 依据中等偏弱，F1 对称）
CW_BB3_BBPOS_MAX = 10.0          # BB3 观察级：4H bb_pos ≤ 10（布林极低位，深跌末端）
CW_BB3_HIST_UP = 0.55            # BB3 观察级：historical.up_rate > 0.55（时段偏多，预测 DOWN=反向）


def _round_half_up(x: float) -> int:
    return int(Decimal(str(x)).quantize(0, rounding=ROUND_HALF_UP))


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _dir_of(sign: int) -> str | None:
    return {1: "UP", -1: "DOWN"}.get(sign)


@dataclass
class Decision:
    s0: float = 0.0
    pm_row: float = 0.0
    net: float = 0.0
    net_direction: str | None = None
    final_direction: str | None = None
    action: str = "ORDER"          # ORDER / SKIP
    confidence: str = "低"
    position_scale: float | str | None = None  # None / 0.5 / "min"
    probability: float | None = None           # 方向方概率 %
    ev: float | None = None                    # 期望值 %（round30.1 新增）
    h12_triggered: bool = False
    trace: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


def _cap(conf: str, cap: str) -> str:
    return CONF_LEVELS[min(CONF_LEVELS.index(conf), CONF_LEVELS.index(cap))]


def _downgrade(conf: str) -> str:
    return CONF_LEVELS[max(0, CONF_LEVELS.index(conf) - 1)]


def _flip_target_price(inp: dict, target_dir: str) -> float | None:
    """round23 反转目标方向价格（Polymarket 0-1）。

    target_dir 为目标方向（'UP'/'DOWN'）。优先取显式传入的 flip_target_price，
    否则由 inp['pm_up']（0-100 百分比）换算：UP 价=pm_up/100，DOWN 价=(100-pm_up)/100。
    价格缺失/异常返回 None（由调用方 _price_blocked 以 fail-closed 语义处理：缺价禁反转）。
    """
    p = inp.get("flip_target_price")
    if isinstance(p, (int, float)) and p > 0:
        return float(p)
    pm_up = inp.get("pm_up")
    if isinstance(pm_up, (int, float)) and pm_up is not None and 0 <= pm_up <= 100:
        if target_dir == "UP":
            return pm_up / 100.0
        if target_dir == "DOWN":
            return (100.0 - pm_up) / 100.0
    return None


def _price_blocked(d: object, target_dir: str) -> bool:
    """round25 反转价格门槛：目标方向价缺失或 < REV_PRICE_MIN → 禁止反转（fail-closed）。

    round25（8/16）：缺价 fail-open → fail-closed。统计（全样本 115 场 + 翻转 11 场）缺价
    场景 n=0，改 fail-closed（缺价禁反转）零误伤——反转是全系统最高风险操作，缺价时放行
    会在数据异常时买入极端低胜率方向（此前 fail-open 方向放反了）。
    """
    p = _flip_target_price(d, target_dir)
    if p is None:
        return True  # fail-closed：缺价禁反转
    return p < REV_PRICE_MIN


def _flip_price_str(inp: dict, target_dir: str) -> str:
    """round25：反转目标价字符串（缺价显示"缺失"，供 trace/risks 格式化，避免 None:.3f 崩溃）。"""
    p = _flip_target_price(inp, target_dir)
    return "缺失" if p is None else f"{p:.3f}"


def judge(inp: dict, *, legacy_pm_weak_row: bool = False,
          _disable_pm_cover_guard: bool = False,
          max_price: float = 0.50) -> Decision:
    """单方向源流水线判定。inp 字段见模块 docstring 与 selftest 示例。

    max_price：Polymarket 买入价上限（默认 0.50），用于 EV 计算。EV = P(win) - price。
    round31：EV 仅日志复盘，不设 SKIP 门槛（唯一 SKIP 场景 = G1 数据失效）。

    legacy_pm_weak_row：仅供审查对比的插桩参数（默认 False 不影响实盘）。
    为 True 时模拟旧规则——PM 50-55% 逆共识档 pm_row=+1（顺 S0 方向），
    而非 round13 的归零。用于 round13 复查量化 Y 修正的 net=0 边界方向翻转。

    _disable_pm_cover_guard：仅供回测对比的插桩参数（默认 False 不影响实盘）。
    为 True 时跳过 G5.4 吸筹/派发覆盖的 PM 深度共识护栏（模拟修复前 legacy），
    用于 verify_g5_guard_replay 量化护栏净效果。
    """
    d = Decision()
    tr = d.trace.append

    factors: dict[str, float] = dict(inp.get("factors", {}))
    pm_up = inp.get("pm_up")                       # PM UP 侧百分比 0-100，None=无PM
    # round41.8.14（8/25 用户决策「直接上无条件版，不重放」）：judge 共识源无条件切真实订单簿。
    # book_up_mid（extract_factors 注入，0-100）有效时无条件作为 PM 共识唯一事实源，gamma 标记价
    # up_price 降级为缺失回退值。单点替换全覆盖：PM 行/H12 逃生门/G2/G3/G4 豁免/G5.4 护栏/
    # G6/概率表顺PM 维度/_flip_target_price 价格闸门全部消费替换后的 pm_up。
    # 依据=8/24 3pm 场标记价 0.705 vs 盘口 0.44 vs 实时 0.28（validator 41.8.13 已修翻向层，
    # judge 层是最后一个读假标记价的口子）；偏离 ≥10pt 记 trace「⚠️标记价失真」供复盘归因。
    _book_mid = inp.get("book_up_mid")
    if isinstance(_book_mid, (int, float)) and 0.0 < _book_mid < 100.0:
        if isinstance(pm_up, (int, float)) and abs(_book_mid - pm_up) >= 10.0:
            tr(f"⚠️标记价失真：mark_up={pm_up:.0f} vs book_mid={_book_mid:.0f}"
               f"（差{abs(_book_mid - pm_up):.0f}pt）→ 共识判定无条件改用盘口（round41.8.14）")
        pm_up = _book_mid
        inp = dict(inp, pm_up=pm_up)  # _flip_target_price 价格闸门同源（否则闸门仍读假标记价）
    dev = float(inp.get("deviation_pct", 0) or 0)  # 价格 vs 锚定偏差%（带符号）
    tie_line = bool(inp.get("tie_line_active"))
    macd1 = float(inp.get("macd_1h_hist", 0) or 0)
    macd4 = float(inp.get("macd_4h_hist", 0) or 0)

    pm_side = None
    pm_dir = None
    if isinstance(pm_up, (int, float)):
        pm_side = max(pm_up, 100 - pm_up)
        pm_dir = "UP" if pm_up > 50 else ("DOWN" if pm_up < 50 else None)
    price_dir = _dir_of(_sign(dev))

    # ---- G1 数据失效 ----
    if not inp.get("data_ok", True):
        d.action = "SKIP"
        tr("G1 数据失效 → SKIP，流程终止")
        return d

    # ---- 步骤8.1：S0 ----
    # round26 P1: 移除 macd_4h 因子（回测一致率 47.9%，移除后胜率 +1.7%）
    removed = []
    if factors.get("macd_4h", 0) != 0:
        removed.append(f"macd_4h {factors['macd_4h']:+.1f}")
        del factors["macd_4h"]
    if removed:
        tr(f"P1 低效因子移除：{'、'.join(removed)}")
    d.s0 = sum(factors.values())
    tr(f"步骤8 S0={d.s0:+.1f}（因子 {len(factors)} 项）")

    # ---- 步骤8.2：H12 降权（贴线模式 + 1H/4H MACD 同向 + 至少一个 |hist|>=30）----
    trend_dir = _dir_of(_sign(macd1)) if _sign(macd1) == _sign(macd4) != 0 else None
    h12 = (
        tie_line
        and trend_dir is not None
        and max(abs(macd1), abs(macd4)) >= H12_DEPTH_THRESHOLD
    )
    d.h12_triggered = h12
    if h12:
        halved = []
        for key in ("taker_bs", "taker_1m"):  # P0-1.3: taker_bs_15m 已删（双窗降为单窗，round51.4 后 5m 单窗）
            v = factors.get(key, 0)
            if v != 0:
                factors[key] = _round_half_up(v * 0.5)
                halved.append(f"{key} {v:+.1f}→{factors[key]:+.1f}")
        v1h = factors.get("taker_1h", 0)
        if v1h != 0 and _dir_of(_sign(v1h)) != trend_dir:  # 1H Taker 与大趋势矛盾才降权
            factors["taker_1h"] = _round_half_up(v1h * 0.5)
            halved.append(f"taker_1h {v1h:+.1f}→{factors['taker_1h']:+.1f}（逆势）")
        # round26: H12 降权扩展到 price_position（Bug#5 修复，price_position 未被降权导致深势时主导 S0）
        vpp = factors.get("price_position", 0)
        if vpp != 0 and _dir_of(_sign(vpp)) != trend_dir:
            factors["price_position"] = _round_half_up(vpp * 0.5)
            halved.append(f"price_position {vpp:+.1f}→{factors['price_position']:+.1f}（逆势）")
        # round51.2：pm_momentum 自本轮起在贴线模式也计分（±2，贴线 S₀ 唯一 ±2 幅值因子）。
        # 须与 taker_bs/price_position 同口径纳入 H12 逆势半权——否则深势双线下一个逆势最大
        # 幅值动量因子被放行，直接主导贴线 S0，违背 H12"权威大趋势、逆势因子减权"设计。
        vpm = factors.get("pm_momentum", 0)
        if vpm != 0 and _dir_of(_sign(vpm)) != trend_dir:
            factors["pm_momentum"] = _round_half_up(vpm * 0.5)
            halved.append(f"pm_momentum {vpm:+.1f}→{factors['pm_momentum']:+.1f}（逆势）")
        d.s0 = sum(factors.values())
        tr(f"H12 触发（MACD 同向{trend_dir}，|hist|≥30）：降权 {halved} → S0={d.s0:+.1f}")
        d.risks.append(f"⚠️H12 MACD 同向深{trend_dir}≥30，贴线单边信号降权，大趋势为权威")
        # round27: H12 触发时额外锁低（贴线+深势=庄家易反手，回测 H12 触发场胜率仅 55.6%）
        d.confidence = "低"

    # ---- 步骤8.3：PM 行（H7 连续梯度，相对 S0 方向）----
    s0_dir = _dir_of(_sign(d.s0))
    if pm_dir and s0_dir and pm_dir != s0_dir:
        # round13（Y 修正）：PM 50-55% 逆共识 +1 取消（该档未经回测、统计负价值），与 55-60% 同档归零。
        # legacy_pm_weak_row 插桩：审查对比用，恢复旧规则 50-55% 档 +1 顺 S0。
        if legacy_pm_weak_row and pm_side < 55 and pm_side >= 50:
            d.pm_row = -_sign(d.s0) * 1
        elif pm_side < 60:
            d.pm_row = 0
        elif pm_side < 70:
            d.pm_row = -_sign(d.s0) * 1
        elif pm_side < 80:
            d.pm_row = -_sign(d.s0) * 2
        else:
            d.pm_row = -_sign(d.s0) * 3
        tr(f"PM {pm_dir} {pm_side:.1f}% 逆 S0 → PM行 {d.pm_row:+.0f}")
    else:
        tr("PM 与 S0 同向（或缺 PM/S0=0）→ PM行 0")
    d.net = d.s0 + d.pm_row
    d.net_direction = _dir_of(_sign(d.net)) or price_dir  # 净分 0 → 价格位置决定
    tr(f"净分={d.net:+.1f} → 初始方向 {d.net_direction}")

    # 基准置信度（round30：统计置信度由 _probability 四维校准决定，不再用 |净分| 粗粒度基准）
    # round31 修复：H12 触发即锁低，不能被基准置信度覆盖
    d.confidence = "低" if h12 else "高"

    # ---- 步骤8.4：H12 逆势防线 ----
    h12_scale_registered = False
    if h12 and d.net_direction != trend_dir:
        if abs(d.net) >= 8:
            escape = (inp.get("h12_macro_evidence", 0) or 0) >= 2 or (
                pm_side is not None and pm_side >= 80 and pm_dir == d.net_direction
            )
            # round13：取消 H12 逆势 SKIP（全量交易），≥8 逆深势同样锁低+缩仓照常 ORDER
            d.confidence = "低"
            h12_scale_registered = True
            d.risks.append(f"⚠️H12 逆势防线：净分{d.net:+.1f} 逆深{trend_dir}大趋势（round13 全量交易，缩仓下单）")
            tr(f"H12 逆势防线 |净分|={abs(d.net):.1f}≥8 逆{trend_dir} → round13 取消 SKIP，锁低+scale 0.5 照常 ORDER")
            if escape:
                tr("H12 逆势防线 ≥8 且满足逃生门（≥2 宏观证据 / PM≥80% 同向）→ 仍缩仓继续")
        else:
            d.confidence = "低"
            h12_scale_registered = True
            tr(f"H12 逆势防线 |净分|≤7 -> 锁低 + 登记 scale=0.5（9.7 统一写入）")

    # ---- 步骤8.5：H13 顶部收敛摆动日守卫（round38 引擎化，2026-08-22 审计 Fix 5）----
    # 三条件（全满足）：4H RSI>90/<10 + 最近 3 根已收 1H 方向交替 + 涨跌幅均 |chg|<0.7%。
    # 动作：锁"低" + scale 0.5，不翻向不 SKIP（风控守卫非方向覆盖，8/21 五连错实证：
    # 深多顶部收敛摆动是趋势类因子系统性失灵行情，5 连错中至少 4 场会被本守卫覆盖）。
    h13_triggered = False
    rsi4 = inp.get("rsi_4h")
    prev_dirs = inp.get("prev_dirs")
    prev_chgs = inp.get("prev_chgs")
    if (
        isinstance(rsi4, (int, float)) and (rsi4 > 90 or rsi4 < 10)
        and isinstance(prev_dirs, list) and len(prev_dirs) >= 3
        and prev_dirs[-1] != prev_dirs[-2] != prev_dirs[-3]  # 链式=相邻两两不等（UP/DOWN/UP 合法）
        and isinstance(prev_chgs, list) and len(prev_chgs) >= 3
        and all(isinstance(c, (int, float)) and abs(c) < 0.7 for c in prev_chgs[-3:])
    ):
        h13_triggered = True
        d.confidence = "低"
        d.risks.append(f"⚠️H13 顶部收敛摆动日：深{'多' if rsi4 > 90 else '空'}顶部横盘收敛，趋势类因子失灵，锁低缩仓（round38）")
        tr(f"H13 顶部收敛摆动日触发（4H RSI={rsi4:.1f} + 最近3根已收1H交替 + 涨跌幅均<0.7%）-> 锁低+scale 0.5，方向照常由净分决定")

    direction = d.net_direction
    # 8/23 trace 增强（无规则语义变化）：方向来源追踪（最终方向由哪个环节决定，供 trace/复盘精确归因，
    # 防止 AI 报告把"PM 行计分翻转"误写成"G4 回退翻向"、把"H12 缩仓"误写成"G6 锁仓"）
    dir_source = f"净分{d.net:+g}（S0{d.s0:+g}+PM行{d.pm_row:+g}）"
    net_for_prob = d.net  # G3 硬覆盖时回退为 S0（round41.3 A 方案恢复该口径）
    # round54.1 概率口径修复（9/3）：resonance/pm_pair 组合因子只做方向确认加分，不参与
    # 概率表档位判定。_probability 的档位胜率（0-2/3-5/6+）为旧净分口径统计（round41.2
    # 重跑 239 场），共振分会把 3-5 档挤进 6+ 档读 84.6%"高"，审计 26 场升档实际命中仅
    # 76.9%（高估约 8pp → EV 高估 → 高价吃价+仓位放大风险）。查表前剔除这两项恢复统计
    # 口径；方向计分（S0/net 照常含共振分）保留不动。G6 弱信号（|净分|≤5 缩仓）同口径
    # 恢复——共振分不再把弱信号"救"过缩仓线（更保守）。
    _inp_factors = inp.get("factors") or {}
    net_for_prob = net_for_prob - float(_inp_factors.get("resonance") or 0) - float(_inp_factors.get("pm_pair") or 0)

    # ---- G2：PM≥80% 深度共识反向保护 ----
    g2_full_evidence = False
    if pm_side is not None and pm_side >= 80 and pm_dir and direction != pm_dir:
        price_breaks = (direction == "DOWN" and dev < 0) or (direction == "UP" and dev > 0)
        ok = (inp.get("evidence_dims", 0) or 0) >= 2 and price_breaks and not inp.get("orderflow_contradiction", False)
        if ok:
            # round41.6 管辖权（8/24 文档对齐）：本"维持反向"仅引擎中间态——validator
            # _consensus_flip 对逆 PM≥60%（含 ≥80%）最终方向无条件翻向 PM，无 ≤80% 豁免。
            # 中间态仍影响概率映射/G4 g2_exception/G5.2 防护链，照常置位。
            g2_full_evidence = True
            tr("G2 触发但满足全部 3 条 → 维持反向")
        else:
            tr(f"G2 覆盖：PM≥80% 反向证据不足 → 跟随 PM {pm_dir}（上限中）")
            direction = pm_dir
            dir_source = f"G2反向保护跟PM{pm_dir}"
            d.confidence = _cap(d.confidence, "中")
            d.risks.append("✅G2 PM≥80% 反向保护：未满足强证据，跟随 PM 深度共识方向")

    # ---- G3：逆 PM 60-80% 共识改判 ----
    g3_overrode = False   # round41.3 A 方案：证据不全恢复硬覆盖翻向 PM（round40 曾废除，240 场回测证伪后恢复）
    g3_exempt_scale = None
    g3_scale = None       # round41.3：证据不全翻向 PM + 锁低 + 建议 scale 0.5（仓位建议，实盘手动设定）
    if pm_side is not None and 60 <= pm_side < 80 and pm_dir and direction != pm_dir:
        price_breaks_015 = (direction == "DOWN" and dev <= -G4_DEVIATION) or (direction == "UP" and dev >= G4_DEVIATION)
        ok = (inp.get("evidence_dims", 0) or 0) >= 2 and price_breaks_015
        # 8/12 round12: G3 摆动市贴线豁免 —— 摆动市 + |偏差|<0.15%（贴锚定，价格未选择方向）+ 强证据
        # （AI evidence_dims≥2 或 机算 short_bounce 与原始方向同向，两路任一满足）→ 不硬覆盖翻向。
        # 机制缺陷修复：贴线场景下"价格突破对家锚定≥0.15%"门槛永不满足，若仍硬覆盖则摆动市贴线必然
        # 跟随 PM（PM 在摆动市剧烈波动不可靠），强证据全废（8/12 6AM 场 13 分钟 PM UP61.5%→DOWN65.5%）。
        short_bounce = int(inp.get("short_bounce", 0) or 0)
        g3_exempt = (
            inp.get("regime") == "摆动市"
            and abs(dev) < G4_DEVIATION
            and ((inp.get("evidence_dims", 0) or 0) >= 2 or (short_bounce != 0 and _dir_of(short_bounce) == direction))
        )
        if ok:
            # round41.6 管辖权：强证据维持反向仅中间态，validator G3.5 必翻向 PM（8/24 文档对齐）
            d.confidence = "低"
            d.risks.append("⚠️逆PM共识（强证据维持）")
            tr("G3 触发但证据充足 → 维持反向，锁低")
        elif g3_exempt:
            # round41.6 管辖权：贴线豁免"保留原始方向"仅中间态，validator G3.5 必翻向 PM（8/24 文档对齐）
            # 不翻向：保留原始方向 + 锁低 + 缩仓 0.5；net_for_prob 不回退（PM 行保留），
            # G6 用含 PM 行的原始净分判定弱信号 SKIP；g3_overrode 不置位（G6"G3已改判"例外不适用）。
            d.confidence = "低"
            g3_exempt_scale = 0.5
            d.risks.append("⚠️G3 摆动市贴线豁免：摆动市+贴锚定+强证据，保留原始方向不翻向，锁低+缩仓0.5")
            tr(f"G3 摆动市贴线豁免（摆动市 + |偏差|{abs(dev):.3f}%<0.15% + 强证据）→ 保留 {direction}，锁低+scale 0.5，net_for_prob 不回退")
        elif _price_blocked(inp, pm_dir):
            # round23 反转价格门槛：顺共识目标方向价格 <0.23 → 禁止 G3 硬覆盖翻向。
            # 目标方向市场隐含概率<23%（赔率极差），跟随可能买入极端低胜率方向。
            # 处置：维持原方向 + 锁低 + 缩仓 0.5（不硬翻向），g3_overrode 不置位（G6 例外不适用）。
            # round41.6 管辖权：本拦截"维持原方向"仅中间态，validator G3.5 必翻向 PM（8/24 文档对齐）。
            d.confidence = "低"
            g3_exempt_scale = 0.5
            d.risks.append(f"⚠️G3 顺共识目标价缺失或<0.23 禁止翻向（round25 反转价格门槛 fail-closed），维持原方向+锁低+缩仓0.5")
            tr(f"G3 硬覆盖被反转价格门槛拦截（目标 {pm_dir} 价 {_flip_price_str(inp, pm_dir)}<{REV_PRICE_MIN}）→ 维持 {direction}，锁低+scale 0.5")
        else:
            # round42（2026-08-25 用户决策「G3.5 翻向给禁止使用」）：G3 证据不全硬覆盖**禁用**，
            # 保留原 S₀ 方向 + 锁低 + scale 0.5 + net_for_prob 不回退。
            #
            # 历史背景：
            # - round41.3（8/22 A 方案）：240 场反事实回测证伪 round40 后恢复 G3 硬覆盖，12 场
            #   分歧场 9胜3负（75%）vs 保留 S₀ 仅 3胜9负（25%）。
            # - round42（8/25）：实盘打脸——bot_launchd.log 8/22-8/25 共 11 笔翻向样本（已结算），
            #   胜率 36.4% / PnL -$67.65。其中 8 笔 G3/G3.5+G4+G2 多门槛叠加触发胜率仅 25.0%
            #   / -$50.49（含本场 8/25 3AM ET S₀=-5 六维共振 + |偏差|=0.074%<0.15% 摆动市案例）。
            #   标记价失真（41.8.13 才修复）+ G2 共同触发是偏差主因。
            # - 结论：G3 硬覆盖路径在实盘不仅未产生回测的 +$25.35 收益，反而净亏 $50+，
            #   用户决策回退到 round40「保留 S₀」路径。
            #
            # 新行为（round42）：证据不全分支不再翻向 PM，仅锁低 + scale 0.5 + 标注「G3 改判禁用」
            # + net_for_prob 不回退（保持原始含 PM 行的净分，供四维概率表机算）。
            d.confidence = "低"
            g3_scale = 0.5
            # net_for_prob 不回退 = 保持 d.s0 + d.pm_row 的原始值
            d.risks.append("⚠️G3 逆PM共识证据不足 → round42 禁用改判：保留 S₀ 方向 + 锁低 + scale 0.5"
                           f"（实盘 8/22-8/25 11 笔翻向 36.4% 胜率 -$67.65 实证，round41.3 A 方案 240 场回测失真）")
            tr(f"G3 证据不全 -> round42 禁用改判：保留 {direction} 方向，锁低+scale 0.5（不翻向 PM，net_for_prob 不回退）")
            # 插桩字段：仅观测 G3 硬覆盖若启用会触发多少次
            d.risks.append(f"[G3硬覆盖若启用会翻向 PM{pm_dir}，round42 已禁用]")

    # ---- G4：价格位置回退（|偏差|≥0.15%）----
    g4_scale = None
    g4_rolled = False  # 8/23 trace 增强：G4 回退是否实际发生（方向来源追踪用）
    settlement = bool(inp.get("is_settlement_phase"))
    if abs(dev) >= G4_DEVIATION and price_dir and direction != price_dir:
        exempt = (
            pm_side is not None and pm_side >= 60 and pm_dir == direction and abs(d.net) >= 6
        )
        g2_exception = g2_full_evidence and not (settlement and abs(dev) > G4_DEVIATION)
        if exempt:
            d.confidence = "低"
            g4_scale = 0.5
            d.risks.append("⚠️G4 豁免：顺共识强信号逆价格位置，不翻向仅缩仓")
            tr("G4 顺共识强信号豁免（顺PM≥60% 且 |净分|≥6）→ 不翻向，锁低+scale 0.5")
        elif g2_exception:
            # round41.6 管辖权（8/24 文档对齐）：本分支实际行为=维持 G2 反向方向不回退（不改 direction），
            # trace 文案"跟随 PM"系历史措辞失实（prompt_exec.md G4 节已标注作废该旧例外表述）；
            # 且该方向最终仍被 validator G3.5 翻向 PM。文案不改以保护既有 trace 断言。
            tr("G4 的 G2 例外：G2 已满足全部 3 条 → 跟随 PM 深度共识方向")
        else:
            # round44.2 双向核对(8/26 8AM ET 误判修复): G4 翻向后, 24h位置 + 宏观趋势 + 4H MACD
            # 至少 2 维支持新方向 price_dir 才执行翻向, 否则保留原方向仅锁低缩仓。
            # 依据: 8/26 8AM ET 翻 UP 误判, 24h位置=45.3%下半区(吸筹) + 宏观趋势=DDUD down=3 +
            # 4H MACD hist=-439.4 都偏空, G4 一票否决了 K线+MACD+宏观 4 维空头证据。
            new_dir = price_dir
            support_dims = 0
            support_detail = []
            # 24h 位置: 上半区支持 UP / 下半区支持 DOWN
            pos24 = inp.get("position_24h_percentile") or inp.get("price_position_24h_percentile")
            if pos24 is not None:
                if new_dir == "UP" and pos24 >= 50:
                    support_dims += 1
                    support_detail.append(f"24h位置{pos24:.1f}%上半区")
                elif new_dir == "DOWN" and pos24 < 50:
                    support_dims += 1
                    support_detail.append(f"24h位置{pos24:.1f}%下半区")
            # 宏观趋势 1H4H: 涨≥3 支持 UP / 跌≥3 支持 DOWN
            macro = inp.get("macro_trend_1h_4h") or {}
            if isinstance(macro, dict):
                up_n = macro.get("up_count") or 0
                down_n = macro.get("down_count") or 0
                if new_dir == "UP" and up_n >= 3:
                    support_dims += 1
                    support_detail.append(f"宏观1H4H涨{up_n}")
                elif new_dir == "DOWN" and down_n >= 3:
                    support_dims += 1
                    support_detail.append(f"宏观1H4H跌{down_n}")
            # 4H MACD hist: >0 支持 UP / <0 支持 DOWN
            macd4_hist = inp.get("macd_4h_hist") or macd4
            if macd4_hist is not None:
                if new_dir == "UP" and macd4_hist > 0:
                    support_dims += 1
                    support_detail.append(f"4H MACD hist={macd4_hist:+.1f}>0")
                elif new_dir == "DOWN" and macd4_hist < 0:
                    support_dims += 1
                    support_detail.append(f"4H MACD hist={macd4_hist:+.1f}<0")
            tr(f"G4 双向核对: 新方向={new_dir} 支持维度={support_dims}/3 [{','.join(support_detail)}]")
            if support_dims >= 2:
                tr(f"G4/H9 回退：方向 {direction} 逆价格位置 {price_dir}（|偏差|{abs(dev):.2f}%≥0.15%）→ 回退为 {price_dir}（上限中, 双向核对{support_dims}/3支持）")
                direction = price_dir
                g4_rolled = True
                dir_source = f"G4/H9价格位置回退→{price_dir}(双向核对{support_dims}/3)"
                d.confidence = _cap(d.confidence, "中")
                d.risks.append(f"✅G4/H9 价格位置硬约束+双向核对{support_dims}/3：回退为{price_dir}")
            else:
                # 双向核对不过 → 保留原方向, 仅锁低缩仓
                g4_scale = 0.5
                d.confidence = _cap(d.confidence, "低")
                d.risks.append(f"⚠️G4 双向核对仅{support_dims}/3支持新方向{new_dir} → 不翻向, 保留{direction}+锁低+scale 0.5 (round44.2, 8/26 8AM ET 误判修复)")
                tr(f"G4 双向核对不通过({support_dims}/3<2) → 保留{direction}, 锁低+scale 0.5")
            if pm_side is not None and pm_side >= 60 and pm_dir and direction != pm_dir:
                d.confidence = "低"
                d.risks.append("⚠️G4 回退逆PM共识，跟随价格位置")

    # ---- G5：大趋势 + 诱多/诱空 ----
    g5_scale = None
    trend_bucket = _trend_bucket(inp, macd1, macd4)
    fakeout = inp.get("fakeout")  # None / "诱多"(DOWN信号) / "诱空"(UP信号)
    tr(f"G5.1 大趋势档位：{trend_bucket}")

    maintain_trend = False
    # 8/15 覆盖翻向去激活：吸筹区/派发区"极端墙+半区+衰竭→反转"假设经全量回测（89场）证伪——
    # 吸筹覆盖翻向 DOWN→UP 历史 0/2 翻对（极端卖墙更多是真实卖压非庄家吸筹），故不再产生 zone_cover 翻转。
    # 仅保留：PM≥80% 护栏（有效）+ accel<0 健康空头仅标注 + fakeout/深度档 cover_to（另有 H9 守卫）。
    accel4 = inp.get("macd_4h_accel")
    extreme_wall = bool(inp.get("extreme_wall"))
    # 8/15 flip_gates_replay：吸筹/派发覆盖加 PM 深度共识护栏（与 F-框架护栏4 对齐）。
    # 回测实证（data/hermes_flip_gates_replay_20260815.md）：8/13 4am 场 PM=16.5%（DOWN 83.5% 深度看空）
    # + 15m/kline/taker_bs/short_bounce 全偏空，仅因"极端卖墙+低位+4H accel>0"被 zone_cover 硬翻 UP，
    # 实际 DOWN → 覆盖误伤。PM 深度共识是权威护栏（G2 管辖），吸筹覆盖此前绕过了它。
    # 修复：PM 任一侧 ≥80%（深度共识）时禁止吸筹/派发覆盖翻向（维持原方向 + 强标注）。
    _pm_deep = (
        isinstance(pm_side, (int, float)) and pm_side is not None
        and pm_side >= 80
    )
    if extreme_wall and accel4 is not None:
        if _pm_deep and not _disable_pm_cover_guard:
            # 深度共识护栏：不覆盖翻向，维持原方向（8/13 4am 修复）
            d.confidence = "低"
            g5_scale = 0.5
            d.risks.append("⚠️PM≥80% 深度共识，吸筹/派发覆盖被护栏拦截（维持原方向），仅锁低+缩仓")
            tr(f"G5.4 吸筹/派发覆盖被 PM 深度共识护栏拦截（PM {pm_side:.1f}%≥80%）→ 不翻向，锁低+scale 0.5")
        elif direction == "DOWN" and inp.get("in_lower_half") and accel4 > 0:
            # 8/15 G5.4 覆盖翻向去激活（吸筹）：全量回测（89场，data/hermes_g54_cover_disable_replay_20260815.md）
            # 实证吸筹覆盖翻向 DOWN→UP 历史 0/2 翻对（8/11 9am 被 H9 拦截碰巧未翻错、8/14 8pm 翻错），
            # 极端卖墙+低位+跌势衰竭更多是真实卖压而非庄家吸筹反转 → 不翻向，维持 DOWN+锁低+缩仓（用户二元市不缩仓仅日志）。
            d.confidence = "低"
            g5_scale = 0.5
            d.risks.append("⚠️跌势衰竭+极端卖墙+24h下半区：庄家吸筹嫌疑但覆盖翻向无历史背书（0/2），维持原方向+锁低+缩仓不翻向")
            tr(f"G5.4 吸筹区覆盖已禁用：DOWN 不翻向（in_lower_half + 4H accel={accel4}>0 + 极端卖墙，但覆盖翻向历史0/2）→ 锁低+scale 0.5")
        elif direction == "UP" and inp.get("in_upper_half") and accel4 < 0:
            # 8/15 G5.4 覆盖翻向去激活（派发）：n=0 无样本，对称禁用（与吸筹同风险假设）。
            d.confidence = "低"
            g5_scale = 0.5
            d.risks.append("⚠️动能衰竭+极端买墙+24h上半区：庄家派发嫌疑但覆盖翻向无历史背书，维持原方向+锁低+缩仓不翻向")
            tr(f"G5.4 派发区覆盖已禁用：UP 不翻向（in_upper_half + 4H accel={accel4}<0 + 极端买墙）→ 锁低+scale 0.5")
        # 健康方向但极端墙+半区：不覆盖，但强风险标注（3AM 场景：accel<0 健康空头）
        elif direction == "DOWN" and inp.get("in_lower_half"):
            d.risks.append("⚠️极端卖墙+24h底部，庄家吸筹嫌疑，顺深空需更强证据")
            tr(f"G5.4 极端卖墙+24h底部，4H accel={accel4}<0 健康空头不覆盖，吸筹嫌疑标注")
        elif direction == "UP" and inp.get("in_upper_half"):
            d.risks.append("⚠️极端买墙+24h顶部，庄家派发嫌疑，顺做多需更强证据")
            tr(f"G5.4 极端买墙+24h顶部，4H accel={accel4}>0 健康多头不覆盖，派发嫌疑标注")
    if trend_bucket in ("深度多头", "深度空头"):
        tdir = "UP" if trend_bucket == "深度多头" else "DOWN"
        fakeout_dir = {"诱多": "DOWN", "诱空": "UP"}.get(fakeout)
        # G5.4 cover_to 已移除（round26，0/267 触发）：深度档不再覆盖翻向。
        # round31：深度档衰竭+诱空/诱多/派发吸筹标记仅锁低+缩仓，不翻向。
        _exhausted = (
            (tdir == "UP" and isinstance(accel4, (int, float)) and accel4 < 0)
            or (tdir == "DOWN" and isinstance(accel4, (int, float)) and accel4 > 0)
        )
        if fakeout_dir and fakeout_dir != tdir:
            if _exhausted:
                d.confidence = "低"
                g5_scale = 0.5
                if tdir == "DOWN":
                    d.risks.append("⚠️跌势衰竭+诱空=庄家底部吸筹嫌疑，维持空头+锁低缩仓")
                else:
                    d.risks.append("⚠️动能衰竭+诱多=庄家顶部派发嫌疑，维持多头+锁低缩仓")
                tr(f"G5.4 深度{tdir}衰竭+反向诱空/诱多 → 不翻向，维持 {tdir}，锁低+scale 0.5")
            else:
                d.risks.append("诱空嫌疑但大趋势空头优先" if tdir == "DOWN" else "诱多嫌疑但大趋势多头优先")
                tr(f"G5.4 深度{tdir}健康，诱空/诱多不翻向，维持 {tdir}")
            maintain_trend = True
        elif not fakeout_dir and _exhausted and direction == tdir:
            if (tdir == "UP" and inp.get("in_upper_half")) or (tdir == "DOWN" and inp.get("in_lower_half")):
                d.confidence = "低"
                g5_scale = 0.5
                if tdir == "UP":
                    d.risks.append("⚠️动能衰竭+24h上半区=庄家顶部派发嫌疑，维持多头+锁低缩仓")
                else:
                    d.risks.append("⚠️跌势衰竭+24h下半区=庄家底部吸筹嫌疑，维持空头+锁低缩仓")
                tr(f"G5.4 深度{tdir}衰竭+半区标记 → 不翻向，维持 {tdir}，锁低+scale 0.5")
    elif trend_bucket in ("偏多非深度", "偏空非深度"):
        tdir = "UP" if trend_bucket == "偏多非深度" else "DOWN"
        fakeout_dir = {"诱多": "DOWN", "诱空": "UP"}.get(fakeout)
        # 8/16 修复 Bug#1 + Bug#2：非深度档翻向 tdir 同样受
        #  ① G3/G2 已覆盖豁免（Bug#2：G3 硬覆盖顺共识后，G5 不得再改回大趋势）
        #  ② H9 价格位置守卫（Bug#1：|dev|≥0.15% 且 tdir 逆价格位置 → 不翻向，仅缩仓）
        # 此前非深度档直接 direction=tdir，既绕过 G3 覆盖又绕过 H9。
        if fakeout_dir and fakeout_dir != tdir:
            if not (g3_overrode or g2_full_evidence):
                if abs(dev) >= G4_DEVIATION and price_dir and tdir != price_dir:
                    d.confidence = "低"
                    g5_scale = 0.5
                    d.risks.append("⚠️非深度档 maintain 目标逆价格位置（偏差≥0.15%），尊重 H9 不翻向，锁低+缩仓0.5")
                    tr(f"非深度档 {tdir} 被 H9 拦截（逆价格位置 {price_dir}，|偏差|{abs(dev):.2f}%≥0.15%）→ 不翻向，锁低+scale 0.5")
                else:
                    direction = tdir
                    dir_source = f"G5大趋势跟随{tdir}"
            d.confidence = "低"
            g5_scale = 0.5
            maintain_trend = True
            d.risks.append("⚠️诱多/诱空逆非深度大趋势，维持大趋势方向+缩仓")
        elif fakeout_dir:
            if not (g3_overrode or g2_full_evidence):
                if abs(dev) >= G4_DEVIATION and price_dir and tdir != price_dir:
                    d.confidence = "低"
                    g5_scale = 0.5
                    d.risks.append("⚠️非深度档 maintain 目标逆价格位置（偏差≥0.15%），尊重 H9 不翻向，锁低+缩仓0.5")
                    tr(f"非深度档 {tdir} 被 H9 拦截（逆价格位置 {price_dir}，|偏差|{abs(dev):.2f}%≥0.15%）→ 不翻向，锁低+scale 0.5")
                else:
                    direction = tdir
                    dir_source = f"G5大趋势跟随{tdir}"
            d.confidence = _cap(d.confidence, "中")
            maintain_trend = True
        else:
            # round29: 非深度趋势无 fakeout 不再 blanket cap 到"中"（统计模型已反映趋势市/摆动市差异）
            if d.net_direction != tdir:
                d.confidence = "低"
                g5_scale = 0.5
                d.risks.append("⚠️逆非深度大趋势，缩仓减亏")
    else:  # 中性
        fakeout_dir = {"诱多": "DOWN", "诱空": "UP"}.get(fakeout)
        if fakeout_dir:
            # 8/16 修复 Bug#1：中性档 fakeout 翻转同样受 H9 价格位置 + round23 反转价格门槛 + G3/G2 覆盖豁免约束。
            # 此前中性档直接 direction=fakeout_dir 绕过了 H9（H9 只拦深度档 cover_to），
            # fakeout（AI 易错的主观因子）可能完胜价格位置（全系统一致率最高因子）。
            # G3/G2 已覆盖（Bug#2）时不翻向，尊重硬覆盖顺共识方向。
            _fb_target = fakeout_dir
            if (g3_overrode or g2_full_evidence):
                tr("G3/G2 已覆盖，中性档 fakeout 放弃改向（尊重硬覆盖方向）")
            elif abs(dev) >= G4_DEVIATION and price_dir and _fb_target != price_dir:
                d.confidence = "低"
                g5_scale = 0.5
                d.risks.append("⚠️中性档 fakeout 目标逆价格位置（偏差≥0.15%），尊重 H9 不翻向，锁低+缩仓0.5")
                tr(f"中性档 fakeout {_fb_target} 被 H9 拦截（逆价格位置 {price_dir}，|偏差|{abs(dev):.2f}%≥0.15%）→ 不翻向，锁低+scale 0.5")
            elif _price_blocked(inp, _fb_target):
                d.confidence = "低"
                g5_scale = 0.5
                d.risks.append(f"⚠️中性档 fakeout 目标价缺失或<0.23 禁止翻向（round25 反转价格门槛 fail-closed），锁低+缩仓0.5")
                tr(f"中性档 fakeout {_fb_target} 被反转价格门槛拦截（目标价 {_flip_price_str(inp, _fb_target)}<{REV_PRICE_MIN}）→ 不翻向，锁低+scale 0.5")
            else:
                direction = fakeout_dir
                dir_source = f"G5诱多/诱空覆盖{fakeout_dir}"
                d.confidence = _cap(d.confidence, "中")

    if maintain_trend:
        # 8/16 修复 Bug#2 + Bug#1：
        # ① Bug#2：G3 硬覆盖（g3_overrode）或 G2 满足全部强证据（g2_full_evidence）后，
        #    maintain_trend 不得再把 direction 改回大趋势方向——G3 的"逆PM共识硬覆盖顺共识"是
        #    回测 7/7=100% 可靠的最强翻转，G5 的次要约束不能静默吃掉它。
        # ② Bug#1：非深度档 maintain_trend 改向 tdir 时，若 tdir 逆价格位置且 |dev|≥0.15%，
        #    尊重 G4/H9（价格位置优先）不翻向，仅缩仓。
        _mt_target = trend_bucket and ("UP" if "多" in trend_bucket else "DOWN")
        if g3_overrode or g2_full_evidence:
            tr("G3/G2 已覆盖，maintain_trend 放弃改向（仅保留缩仓）")
        elif abs(dev) >= G4_DEVIATION and price_dir and _mt_target and _mt_target != price_dir:
            d.confidence = "低"
            g5_scale = 0.5
            d.risks.append("⚠️maintain_trend 目标逆价格位置（偏差≥0.15%），尊重 H9 不翻向，锁低+缩仓0.5")
            tr(f"maintain_trend 被 H9 拦截：目标 {_mt_target} 逆价格位置 {price_dir}（|偏差|{abs(dev):.2f}%≥0.15%）→ 不翻向，锁低+scale 0.5")
        else:
            direction = _mt_target
            dir_source = f"G5大趋势跟随{_mt_target}"

    # ---- G5.4 深负/深正大趋势 + 短周期反弹/回落先兆 → 缩仓不翻向（8/12 round11）----
    # 触发：大趋势明确偏空/偏多（深度档 或 非深度档，MACD 同向且 |hist|≥30）+ 方向与大趋势同向
    # + short_bounce 与大趋势相反 + |偏差|<0.15%（贴锚定，价格未选择方向）。
    # 不翻向（尊重大趋势优先+H12），仅锁低+缩仓 0.5。依据：3PM 场 S0=-4 盲跟深负做空被反弹打脸（实际 UP）。
    # 注意：3PM 场 G5.1 判"偏空非深度"（1H Taker 48.5% 未<30% 确认，深度档降级）——若缩仓分支只覆盖
    # 深度档会漏掉目标场景，故扩展到非深度档。判定用 1H/4H MACD 同向 + |hist|≥30（H12 同口径），
    # 不依赖 1H Taker 确认（与 H12"大趋势"口径一致，独立于 G5.1 档位）。
    short_bounce = int(inp.get("short_bounce", 0) or 0)  # +1 反弹先兆 / -1 回落先兆 / 0
    deep_trend_dir = None
    if _sign(macd1) == _sign(macd4) != 0 and max(abs(macd1), abs(macd4)) >= H12_DEPTH_THRESHOLD:
        deep_trend_dir = _dir_of(_sign(macd1))  # 1H/4H 同向深势方向（H12 口径，不依赖 G5.1 档位）
    if short_bounce != 0 and abs(dev) < G4_DEVIATION and deep_trend_dir is not None:
        # 方向与大趋势同向（顺深势做空/做多），但短周期先兆与大趋势相反 → 动能背离，缩仓
        if direction == deep_trend_dir and _dir_of(short_bounce) != deep_trend_dir:
            d.confidence = "低"
            g5_scale = 0.5
            d.risks.append(
                f"⚠️深{'负' if deep_trend_dir == 'DOWN' else '正'}大趋势但短周期{'反弹' if short_bounce > 0 else '回落'}"
                f"启动（1m/5m连阳+15m MACD翻正），方向存疑缩仓避险"
            )
            tr(f"G5.4 深{deep_trend_dir}大趋势+短周期先兆{short_bounce:+d} 反向背离 + 贴锚定 → 不翻向，锁低+scale 0.5")

    # ---- G6：弱信号（round13 取消 SKIP → 改 ORDER + 缩仓，全量交易）----
    # round13 用户决策"分析出方向就要交易，不允许跳过"：弱信号不再 SKIP 终止流程，
    # 改为锁低 + position_scale=0.5 照常 ORDER（风险用缩仓承接，不放弃交易）。
    g6_weak_scale = None  # round13 取消 SKIP：弱信号不再终止流程，仅缩仓承接
    g6_whys = []
    if abs(net_for_prob) <= 5 and d.confidence == "低" and not g3_overrode:
        # 8/23 trace 增强（无规则语义变化）：G6 触发原因精确到"逆什么"——逐项判定并写明最终方向 vs 共识/位置的
        # 实际方向，替代旧口径含糊的"逆共识/逆位置"（8/23 3AM 场 AI 报告曾把顺位置 UP 误写成逆位置）
        if pm_side is not None and pm_side >= 60 and pm_dir and direction != pm_dir:
            g6_whys.append(f"最终方向{direction}逆PM共识{pm_dir}（{pm_side:.0f}%）")
        if abs(dev) >= G4_DEVIATION and price_dir and direction != price_dir:
            g6_whys.append(f"最终方向{direction}逆价格位置{price_dir}（|偏差|{abs(dev):.2f}%≥0.15%）")
        if g6_whys:
            g6_weak_scale = 0.5
            d.risks.append("⚠️G6/H11 弱信号+" + "+".join(g6_whys) + "（round13 全量交易，缩仓下单）")
            tr(f"G6 弱信号（round13 不 SKIP）：|净分|{abs(net_for_prob):.1f}≤5 + 低置信 + {'；'.join(g6_whys)} → 缩仓 0.5 照常 ORDER")

    # ---- 9.7 置信度/仓位调整项 ----
    scales = [s for s in (g3_exempt_scale, g3_scale, g4_scale, g5_scale, g6_weak_scale, 0.5 if h12_scale_registered else None, 0.5 if h13_triggered else None) if s is not None]
    mins = inp.get("minutes_to_settle")
    tie_dev = abs(dev) < TIE_LINE_DEVIATION
    pm_same = pm_dir and direction == pm_dir
    if pm_same and pm_side is not None and 60 <= pm_side < 80:
        # round29: 置信度不再由 9.7#1 降级（顺共识风险已在统计胜率中反映）
        d.risks.append("⚠️顺共识区，庄家收割风险")
        tr("9.7#1 同向共识区 → 风险标注（置信度由统计模型决定）")
    if pm_same and pm_side is not None and pm_side >= 80:
        d.risks.append("⚠️庄家收割区")
        tr("9.7#2 同向深度共识区 → 风险标注（置信度由统计模型决定）")
    if tie_dev and mins is not None and mins < 15 and inp.get("deviation_trend") == "expanding":
        d.confidence = "低"
        scales.append(0.5)
        d.risks.append("⚠️尾盘庄家操控启动")
        tr("9.7#3 尾盘操控 → 锁低+0.5")
    if tie_dev and pm_same:
        scales.append(0.5)
        d.risks.append("⚠️贴线+共识=高收割风险")
        tr("9.7#4 贴线+顺PM → scale 0.5")
        # round28: 贴线+|S0|≥3+顺PM 子组胜率仅 64.7%（17场回测），额外锁低减少高风险错单损失
        if abs(d.s0) >= 3:
            d.confidence = "低"
            d.risks.append("⚠️贴线+强信号+顺共识=极高收割风险，锁低")
            tr("9.7#4 子组 |S0|≥3 → 锁低")
    if (inp.get("consecutive_same_dir", 0) or 0) >= 3:
        # round29: 置信度不再由 9.7#6 降级（反手时点风险已在统计胜率中反映）
        d.risks.append("⚠️接近庄家反手时点")
        tr("9.7#6 反手时点 → 风险标注（置信度由统计模型决定）")

    # ---- 9.7#8 贴线高不确定降级（8/15 综合诊断落地）----
    # 依据 data/hermes_pm3losses_rootcause_20260815.md：贴线模式 + 极端墙 + 极端Taker
    # 并存时方向正确率仅 62%（vs 未命中 82%），覆盖 8/14 下午 3 场错判中的 2 场（8am/4pm）
    # 及 11-3am。正确处置：方向不变，锁低 + 缩仓 0.5（减少低置信错单损失），而非翻转。
    # 极端墙 = 买墙或卖墙比值 >3；极端Taker = taker_bs_ratio >2.33 或 <0.43（深度档阈值）。
    if inp.get("_tie_mode") and direction:
        _bwr = inp.get("_buy_wall_ratio")
        _swr = inp.get("_sell_wall_ratio")
        _tr = inp.get("taker_bs_ratio")
        _extreme_wall = (
            (isinstance(_bwr, (int, float)) and _bwr is not None and _bwr > 3.0)
            or (isinstance(_swr, (int, float)) and _swr is not None and _swr > 3.0)
        )
        _taker_ext = isinstance(_tr, (int, float)) and _tr is not None and (_tr > 2.33 or _tr < 0.43)
        if _extreme_wall and _taker_ext:
            d.confidence = "低"
            scales.append(0.5)
            d.risks.append("⚠️贴线高不确定（极端墙+极端Taker背离，8/15 诊断）：方向判断可靠性 62%，锁低+缩仓0.5")
            tr(f"9.7#8 贴线高不确定：极端墙({_bwr}:{_swr}) + 极端Taker({_tr}) → 锁低+scale 0.5（不翻转）")

    # ---- round18 反手预警分级（翻转级精确翻转 / 观察级仅标注，不再无差别缩仓）----
    # 依据：data/hermes_countertrend_redesign_20260814.md + scripts/countertrend_*.py。
    # round17"锁低+缩仓0.5"对 18 场判断正确的场次是误伤（用户质疑成立）；4 维组合三常维
    # 必命中致触发面过宽。round18 改为三档：
    #   [翻转级] F1 全满足：预测DOWN + 贴线模式 + 顺共识60-80% + 4H衰竭 + |4H hist|<50 + 净分≤-4
    #             → 方向翻转 + 锁低；【不缩仓】。round22 收紧（≤-3→≤-4）后 92 场重放 3 对 0 错。
    #   [观察级] 反手预警 ≥3/4 维但不满足 F1 → 方向/置信/仓位完全不动，仅 risks 标注。
    #   [不触发] <3/4 维 → 不动。
    # 4 维独立判定（观察级触发口径，命中 ≥CW_MIN_DIMS 维）：
    #   1. 弱信号贴线：贴线激活 或 当前置信度已低
    #   2. PM 顺共识：PM 60-80% 且与预测方向同向
    #   3. 反向历史时段：historical_up_rate 主倾向与预测方向相反
    #   4. 4H 衰竭反向：macd_4h_accel 指向大趋势衰竭
    if direction:
        _hist_rate = inp.get("historical_up_rate")
        _accel4 = inp.get("macd_4h_accel")
        _hist4 = inp.get("macd_4h_hist")
        _cw_weak = bool(tie_line or d.confidence == "低")
        _cw_pm = bool(pm_side is not None and 60 <= pm_side < 80 and pm_dir == direction)
        _cw_hist = bool(
            isinstance(_hist_rate, (int, float))
            and ((_hist_rate > CW_HIST_BULL and direction == "DOWN") or (_hist_rate < CW_HIST_BEAR and direction == "UP"))
        )
        _cw_exhaust = bool(
            isinstance(_accel4, (int, float))
            and ((direction == "DOWN" and _accel4 > 0) or (direction == "UP" and _accel4 < 0))
        )
        cw_dims = 0
        cw_hits = []
        for _ok, _name in ((_cw_weak, "弱信号贴线"), (_cw_pm, "PM顺共识60-80%"),
                           (_cw_hist, "反向历史时段"), (_cw_exhaust, "4H衰竭反向")):
            if _ok:
                cw_dims += 1
                cw_hits.append(_name)

        if cw_dims >= CW_MIN_DIMS:
            # ---- F1 翻转级判定（精确陷阱特征）----
            # 预测 DOWN + 贴线模式 + 顺共识60-80% + 4H衰竭 + |4H hist|<50 + 净分≤-4
            # 8/14 bug 审查补充（round18_bug_audit.py）：
            #   ① 翻转后新方向必须不逆价格位置（防 F1 绕过 G4/H9 硬约束）——F1 只处理预测 DOWN，
            #      翻转目标 UP；若 |dev|≥0.15%（价格已选择方向 price_dir）且 price_dir≠UP，则翻转
            #      逆价格位置，禁止翻转（降为观察级）。3 个目标场 |dev|~0.08%<0.15% 不受影响。
            #   ② 翻转后必须清理 9.7 注入的 scale（9.7#3/#4/#5 基于翻转前 DOWN 顺共识判定），
            #      否则贴线激活(|dev|<0.05%)场会残留 scale=0.5，违反"翻转级不缩仓"设计。
            #   ③ 翻转后 9.7#1/#2"顺共识收割"风险标注语义错位（翻转后实为逆共识），追加澄清标注。
            _tie_mode_any = bool(inp.get("_tie_mode"))   # 贴线激活/维持/失效预警任一
            _flip_to = "UP" if direction == "DOWN" else "DOWN"
            # G4/H9 价格位置前置：|dev|≥0.15% 且翻转目标逆 price_dir → 禁止翻转
            _price_block = (
                abs(dev) >= G4_DEVIATION
                and price_dir is not None
                and _flip_to != price_dir
            )
            _f1 = (
                direction == "DOWN"
                and _tie_mode_any
                and _cw_pm
                and _cw_exhaust
                and isinstance(_hist4, (int, float))
                and abs(_hist4) < CW_F1_HIST4_MAX
                and d.net <= CW_F1_DOWN_NET
                and not _price_block
                and not _price_blocked(inp, _flip_to)  # round23 反转价格门槛：翻转目标价≥0.23 才允许 F1 翻转
            )
            if _f1:
                tr(f"round18 反手预警【翻转级】F1 命中（{'/'.join(cw_hits)}，|4H hist|{abs(_hist4):.1f}<{CW_F1_HIST4_MAX}，净分{d.net:+.1f}≤{CW_F1_DOWN_NET}）")
                # 方向翻转 + 锁低，缩仓由置信度联动（round30.1）
                # 清理门槛层 scale，统一由置信度决定
                scales.clear()
                d.risks.append(f"⚠️反手预警翻转级（F1：{cw_dims}/4 维 {'/'.join(cw_hits)} + 贴线 + |4H hist|<50 + 净分≤-4）"
                               f"→ 精确陷阱，翻转为 {_flip_to}，锁低缩仓0.5"
                               f"（翻转后为逆共识，规避顺共识陷阱；9.7 顺共识标注作废）")
                direction = _flip_to
                dir_source = f"F1反手翻转→{_flip_to}"
                d.confidence = "低"
            else:
                # ---- 观察级：仅标注，完全不动（不误伤 18 场正确场）----
                d.risks.append(f"⚠️反手预警观察级（{cw_dims}/4 维：{'/'.join(cw_hits)}）→ 未达 F1 翻转条件，维持原判不干预")
                tr(f"round18 反手预警观察级命中 {cw_dims}/4 维（{'/'.join(cw_hits)}）→ 不满足 F1，方向/置信/仓位不变")

    # ---- 步骤10：前置约束取最严（波动率/regime/贴线/第三层否决）----
    vol = inp.get("volatility_5m")
    if isinstance(vol, (int, float)):
        if vol > 0.5:
            d.confidence = _cap(d.confidence, "低")
        elif vol > 0.3:
            d.confidence = _cap(d.confidence, "中")
    if inp.get("regime") == "摆动市":
        # round29: 摆动市不再 cap 置信度（摆动市胜率 79.6% vs 趋势市 76.8%，已在统计模型中反映）
        pass
    if tie_line:
        # round29: 贴线不再 cap 置信度（贴线胜率 73.8% vs 常规 80.3%，已在统计模型中反映）
        pass
    if inp.get("veto_triggered"):
        d.confidence = _downgrade(d.confidence)

    d.position_scale = ("min" if "min" in scales else (0.5 if scales else None))
    d.final_direction = direction
    # round29: 统计置信度 vs 风险置信度，取较严者
    stat_prob, stat_conf = _probability(net_for_prob, tie_line, pm_same, inp.get("regime", "?"))
    d.confidence = _cap(stat_conf, d.confidence)
    d.probability = stat_prob
    # round30.1: 置信度→仓位联动（中/低置信度默认缩仓 0.5，与门槛层取较严者）
    if d.confidence != "高" and d.position_scale is None:
        d.position_scale = 0.5
    # round31: EV 计算（P(win) - max_price），EV 仅日志复盘，不设 SKIP 门槛
    if d.probability is not None:
        d.ev = round(d.probability - max_price * 100, 1)
        if d.ev < 5.0:
            d.risks.append(f"⚠️EV={d.ev:.1f}%<5%（round31 仅日志，不拦截下单）")
            tr(f"EV={d.ev:.1f}%<5%（round31 仅日志，不 SKIP）")
    # 8/23 trace 增强（无规则语义变化）：最终方向/仓位来源汇总（一行看清"方向谁定的、缩仓谁给的"）
    _scale_gate_names = [nm for nm, sv in (
        ("G3豁免", g3_exempt_scale), ("G3", g3_scale), ("G4", g4_scale), ("G5", g5_scale),
        ("G6", g6_weak_scale), ("H12", 0.5 if h12_scale_registered else None),
        ("H13", 0.5 if h13_triggered else None),
    ) if sv is not None]
    if _scale_gate_names:
        _scale_src = "/".join(_scale_gate_names)
    elif scales:
        _scale_src = "9.7调整项"
    elif d.position_scale is not None:
        _scale_src = "置信度联动(round30.1)"
    else:
        _scale_src = "无缩仓"
    tr(f"方向来源：{direction} ← {dir_source}；仓位来源：{_scale_src}（scale={d.position_scale}）")
    tr(f"最终：{d.action} {direction} 置信度{d.confidence} scale={d.position_scale} P={d.probability}% EV={d.ev}%")
    return d


def _trend_bucket(inp: dict, macd1: float, macd4: float) -> str:
    """G5.1 五档判定；1H Taker 确认字段缺失/失真时深度档降级。"""
    taker1h = inp.get("taker_1h_buy_pct")
    degraded = taker1h is None or inp.get("taker_1h_degraded", False)
    deep = max(abs(macd1), abs(macd4)) >= H12_DEPTH_THRESHOLD
    bull = int(macd1 > 0) + int(macd4 > 0) + int(isinstance(taker1h, (int, float)) and taker1h > 70)
    bear = int(macd1 < 0) + int(macd4 < 0) + int(isinstance(taker1h, (int, float)) and taker1h < 30)
    if macd1 > 0 and macd4 > 0 and deep and not degraded and taker1h > 70:
        return "深度多头"
    if macd1 < 0 and macd4 < 0 and deep and not degraded and taker1h < 30:
        return "深度空头"
    if bull >= 2:
        return "偏多非深度"
    if bear >= 2:
        return "偏空非深度"
    return "中性"


def _probability(net: float, is_tie: bool, pm_same: bool, regime: str = "?") -> tuple[float, str]:
    """概率+置信度：基于 |净分|×贴线×PM×Regime 四维交叉胜率校准。

    round41.2 重跑（2026-08-22 概率表审计 Fix 4）：样本 118→239 场（当前引擎口径复算），
    全表重校。核心修正：
    - 0-2 贴线+逆PM 格原 82.1%"高"为数学不可能值（全量高于全部子组 67.6/60.0/77.8），
      n=3 样本不足 → 保守回退同档最低子组 30.8%（0-2 常规+逆PM n=13）；
    - 0-2 常规+逆PM 60.0%→30.8%（n=13，重大修正：0-2 弱信号+逆PM 为负 EV 区）；
    - 3-5 常规+顺PM 降档（摆动 88.9高→73.3中 n=30；趋势 75.0中→65.7低 n=35）；
    - 6+ 常规+逆PM 60.0%→50.0%（n=10）；
    - 0-2 常规+顺PM 重跑 74.2%（n=62）但保守维持 round35 降档决策 67.6%"低"
      （升档=放大仓位，时间切分后半 67.7% 走弱，不升）。
    重跑明细见 data/hermes_probability_table_recheck_20260822.md。
    round41.3 口径备注：本表为 round41.2 口径（G3 不翻向）统计。A 方案恢复翻向后 G3 场
    迁入顺 PM 格，重跑对比大格子变化 <7pp（顺 PM 73.9/70.1/86.0 vs 表值 67.6/65.7-73.3/84.6），
    均在保守余量内且方向性结论不变；逆 PM 格样本缩小（n=7/16/8）统计噪声增大，表值偏保守。
    暂不回填，待样本累积后统一重校。

    round30：|净分|3-5 时 Regime 区分度最大（摆动市>趋势市，方向不变幅度收敛）。

    返回 (概率%, 置信度)。置信度三档：高 ≥80% | 中 70-80% | 低 <70%。
    样本≥5场的细粒度组合直接用历史胜率；样本不足回退到粗粒度（保守取低）。
    """
    an = abs(net)
    is_swing = regime == "摆动市"

    # 四维交叉：样本≥5场直接用历史胜率，不足回退三维/粗粒度
    if an >= 6:
        if not is_tie and pm_same:
            return 84.6, "高"    # 41场重跑85.4%（<1pp 维持；Regime 不显著：趋势93.3%/摆动80.8%）
        if not is_tie and not pm_same:
            return 50.0, "低"    # 10场重跑50.0%（原60.0%）
        # 贴线+≥6 → 顺PM n=4(75.0%)、逆PM 0场，样本不足回退全量
        return 78.2, "中"        # 55场 |净分|≥6 全量（原79.4%）
    elif an >= 3:
        # 常规+顺PM：Regime 区分（摆动 73.3% n=30 > 趋势 65.7% n=35）
        if not is_tie and pm_same:
            if is_swing:
                return 73.3, "中"    # 30场重跑（原88.9%"高"为9场小样本高估）
            return 65.7, "低"        # 35场重跑（原75.0%）
        if not is_tie and not pm_same:
            return 66.7, "低"    # 18场重跑（原62.5%）
        if is_tie and pm_same:
            return 62.5, "低"    # 8场重跑（原56.2%）
        if is_tie and not pm_same:
            # 1场样本不足，回退 |净分|3-5 全量
            return 68.5, "低"        # 92场 |净分|3-5 全量（原68.9%）
        return 68.5, "低"        # 92场 |净分|3-5 全量
    else:  # 0-2
        if not is_tie and pm_same:
            # round35（2026-08-21）降档决策维持：重跑 74.2%（n=62）可升"中"，但时间切分
            # 后半 67.7% 走弱 + round35 刚降档，保守不升（升档=放大仓位）。
            return 67.6, "低"
        if not is_tie and not pm_same:
            # 13场重跑30.8%（原60.0%）——重大修正：0-2 弱信号+逆PM 为负 EV 区
            return 30.8, "低"
        if is_tie and pm_same:
            return 71.4, "中"    # 14场重跑（原77.8%，仍"中"档位不变）
        # 贴线+逆PM+0-2 → n=3 样本不足（原82.1%"高"为数学不可能值），
        # 保守回退同档已知子组最低胜率（0-2 常规+逆PM 30.8% n=13，同为弱信号逆PM性质）
        return 30.8, "低"


# ================= selftest：8/10 三场 + 第五/六轮场景 =================

def _selftest() -> int:
    # ⚠️ 通用说明（勿改数值）：以下各案例的 factors 为历史真实场景快照，factor 分值已按当时执行权重
    # 算好（如 taker_bs ±6 = 旧 Taker 分级 ×3 下机构级基础 ±2×3；taker_1h ±6 = 旧 1H Taker ×3 下 ±2×3）。
    # 当前 prompt.md（round9-P0权重）Taker 分级已降为 ×1、1H Taker 为 ×2，故这些数值不代表当前权重下的分值。
    # 本引擎只做求和与规则判定，不重算权重——改数值会破坏案例净分方向与 G3/G4/G5/H12/G6 断言语义，
    # 因此保持数值不动，仅作规则逻辑链路回归。
    cases = []

    # 案例1：8/10 7AM（验证报告第二章案例1 字段）。round41.3 A 方案口径（8/22 回测后用户决策）：
    # PM 63.5%∈[60,80) 逆向下 + 证据不全 -> 恢复 G3 硬覆盖翻 UP（该场碰巧避错）。
    # round40 曾改"保留 S₀ DOWN"（单场 6pm-et 实证），但 240 场回测显示证据不全场 12 场分歧场
    # 翻向 PM 9胜3负（75%）vs 保留 S₀ 3胜9负（25%）-> 方向跟数据走，恢复翻向 + 保留锁低+缩仓建议。
    cases.append(("7AM 逆共识弱信号 -> round42 G3 禁用改判：保留 DOWN 方向 + 锁低 + scale 0.5", {
        "pm_up": 63.5, "deviation_pct": 0.1127, "tie_line_active": False,
        "macd_1h_hist": -5.0, "macd_4h_hist": -8.0, "taker_1h_buy_pct": 63.6,
        "regime": "摆动市", "evidence_dims": 0,
        "factors": {"price_position": 2, "taker_bs": -6, "taker_1h": 0, "trend_pulse": 0,
                    "order_flow": 2, "macd_15m": -1, "macd_1h": -1, "macd_4h": -1,
                    "kline": 0, "rsi": 0, "open_micro": 1, "seasonality": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN" and d.confidence == "低"
       and d.position_scale == 0.5 and any("round42 禁用改判" in r for r in d.risks)))

    # 案例2：8/10 9AM（验证报告案例2）→ round13 取消 H12 逆势 SKIP：锁低+缩仓 0.5 照常 ORDER
    cases.append(("9AM 贴线逆深势 → round13 H12 取消 SKIP，ORDER UP 锁低+缩仓0.5", {
        "pm_up": 65.5, "deviation_pct": 0.0942, "tie_line_active": True,
        "macd_1h_hist": -27.8, "macd_4h_hist": -50.4, "taker_1h_buy_pct": 70.6,
        "h12_macro_evidence": 0,
        "factors": {"price_position": 2, "taker_bs": 6, "taker_1h": 6,
                    "order_flow": 2, "taker_1m": 3},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP" and d.h12_triggered
       and d.position_scale == 0.5 and d.confidence == "低"))

    # 案例3：8/10 10AM（验证报告案例3）→ 最终 UP
    cases.append(("10AM 逆 PM79.5% → 改判 UP", {
        "pm_up": 79.5, "deviation_pct": 0.41, "tie_line_active": False,
        "macd_1h_hist": -3.0, "macd_4h_hist": -6.0, "taker_1h_buy_pct": 55.0,
        "evidence_dims": 0,
        "factors": {"price_position": 2, "taker_bs": -6, "order_flow": 2},
    }, lambda d: d.final_direction == "UP" and d.action == "ORDER"))

    # 案例4（第五轮新场景）：H12 ≤7 档 → 锁低 + scale 0.5 登记，流程继续
    cases.append(("H12 逆势 |净分|≤7 → 锁低+0.5，不 SKIP", {
        "pm_up": 50.0, "deviation_pct": 0.03, "tie_line_active": True,
        "macd_1h_hist": -25.0, "macd_4h_hist": -40.0, "taker_1h_buy_pct": 50.0,
        "factors": {"price_position": 2, "taker_bs": 6, "taker_1h": 2, "taker_1m": 1},
    }, lambda d: d.action == "ORDER" and d.confidence == "低" and d.position_scale == 0.5
       and d.final_direction == "UP"))

    # 案例5（第五轮：区间互斥）：偏差 0.20%（非贴线）+ 顺 PM70% + |净分|≥6 → G4 豁免不翻向
    cases.append(("G4 豁免：顺共识强信号逆位置不翻向（H12 不触发）", {
        "pm_up": 70.0, "deviation_pct": -0.20, "tie_line_active": False,
        "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "taker_1h_buy_pct": 65.0,
        "factors": {"price_position": -2, "taker_bs": 6, "taker_1h": 4,
                    "macd_15m": -1, "rsi": 1},
    }, lambda d: d.final_direction == "UP" and d.confidence == "低" and d.position_scale == 0.5
       and not d.h12_triggered))

    # 案例7：G3 强证据维持反向 + 弱净分 → round13 取消 G6 SKIP：锁低+缩仓 0.5 照常 ORDER
    cases.append(("G3 维持反向 + |净分|≤5 低置信 → round13 取消 G6 SKIP，ORDER DOWN 缩仓0.5", {
        "pm_up": 65.0, "deviation_pct": -0.20, "tie_line_active": False,
        "macd_1h_hist": -4.0, "macd_4h_hist": -7.0, "taker_1h_buy_pct": 45.0,
        "evidence_dims": 2,
        "factors": {"price_position": -2, "taker_bs": -2, "order_flow": -2, "macd_15m": 1,
                    "seasonality": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.position_scale == 0.5 and d.confidence == "低"))

    # 案例8（第六轮：G4 豁免净分口径=原始净分，非 G3 回退 S0）：
    # S0=-6（DOWN），PM UP 65% 逆 S0 → PM行+1（60-70 档）→ 净分-5。G3 证据不足硬覆盖 UP（净分回退 S0=-6 仅供概率/G6），
    # 随后 G4 发现 UP 逆价格位置（dev=-0.20%）→ 豁免判定用原始净分 |net|=5<6 → 不豁免，照常回退 DOWN。
    # 若误用回退 S0（|S0|=6≥6）会错误豁免保持 UP + scale 0.5——本断言锁定口径区分。
    cases.append(("G3 硬覆盖后 G4 豁免用原始净分 |净分|=5<6 → 照常回退", {
        "pm_up": 65.0, "deviation_pct": -0.20, "tie_line_active": False,
        "macd_1h_hist": -3.0, "macd_4h_hist": -5.0, "taker_1h_buy_pct": 50.0,
        "evidence_dims": 0,
        "factors": {"price_position": -2, "taker_bs": -4},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.confidence == "低" and d.position_scale == 0.5))

    # 案例9（第七轮：G5.4 非深度档"无"行维持=当前方向，非步骤8 净分方向）：
    # S0=-6（DOWN），PM UP 52% 逆 S0 → round13 弱档归零 PM行0（原 <55 档 +1 已取消）→ 净分-6，净分方向 DOWN。
    # G4：|偏差|0.20%≥0.15% 且 DOWN 逆价格位置 UP，PM52%<60% 无豁免 → 回退为 UP。
    # round44.2 双向核对(8/26 8AM ET 误判修复): G4 翻向需 24h位置+宏观+4H MACD ≥2 维支持.
    # 本用例 S₀=-6 极强空头 + 24h位置缺 + 4H MACD=-7.0<0 + 宏观DDUD缺位 = 0/3 支持 UP
    # → 双向核对不通过, 保留原 DOWN+锁低缩仓. 测试期望更新为反映新规则.
    # 原期望"G4 翻 UP 后 G5.4 维持 UP"作废, 因 G4 不再硬翻.
    cases.append(("round44.2 G4 双向核对 0/3 不通过 → 保留 DOWN+锁低（原\"翻 UP 后 G5.4 维持 UP\"作废）", {
        "pm_up": 52.0, "deviation_pct": 0.20, "tie_line_active": False,
        "macd_1h_hist": -4.0, "macd_4h_hist": -7.0, "taker_1h_buy_pct": 45.0,
        "factors": {"price_position": 2, "taker_bs": -6, "order_flow": -2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.net_direction == "DOWN" and d.position_scale == 0.5))

    # 案例10（round8 原 4AM 吸筹区覆盖，8/15 去激活后语义变更）。4AM 实际：1H hist=+30.32 正、4H=-148.98 负（不同向→中性档），
    # 净分 DOWN、in_lower_half=true、4H accel=+8.43>0（跌势衰竭）、卖墙38.4:1 extreme_wall=true、偏差-0.0094%（贴线）。
    # 8/15 吸筹覆盖翻向去激活：不再 DOWN→UP 翻转，改为维持 DOWN + 锁低 + 缩仓 0.5。
    cases.append(("4AM 吸筹区覆盖（已禁用翻向）：极端卖墙+24h底部+4H衰竭 → 维持 DOWN+锁低+缩仓", {
        "pm_up": 49.5, "deviation_pct": -0.0094, "tie_line_active": True,
        "macd_1h_hist": 30.32, "macd_4h_hist": -148.98, "taker_1h_buy_pct": 50.0,
        "in_lower_half": True, "macd_4h_accel": 8.43, "extreme_wall": True,
        "factors": {"price_position": -2, "taker_bs": 0, "order_flow": -2,
                    "macd_1h": -1, "macd_4h": -1, "taker_1h": 0},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.position_scale == 0.5 and d.confidence == "低"
       and any("吸筹嫌疑" in r for r in d.risks)
       and not any("覆盖为" in r for r in d.risks)))

    # 案例11（round8：3AM 健康空头不覆盖但标注吸筹嫌疑）。3AM 实际：1H=+15.8 正、4H=-166.79 负（中性档），
    # PM DOWN 75.5%（引擎 pm_up=24.5 同向净分 DOWN）、净分 DOWN、in_lower_half=true、
    # 4H accel=-6.78<0（健康空头）、卖墙15.1:1 extreme_wall=true。
    # 不覆盖（accel<0），维持 DOWN，但 risk 含"吸筹嫌疑"强标注。
    cases.append(("3AM 健康空头不覆盖：极端卖墙+底部但4H accel<0 → 维持DOWN+吸筹嫌疑标注", {
        "pm_up": 24.5, "deviation_pct": -0.0623, "tie_line_active": True,
        "macd_1h_hist": 15.8, "macd_4h_hist": -166.79, "taker_1h_buy_pct": 50.0,
        "in_lower_half": True, "macd_4h_accel": -6.78, "extreme_wall": True,
        "factors": {"price_position": -2, "taker_bs": 0, "order_flow": -2,
                    "macd_1h": -1, "macd_4h": -1, "taker_1h": 0},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and any("吸筹嫌疑" in r for r in d.risks)))

    # 案例12（round8 原 zone_cover H9 拦截；8/15 吸筹覆盖去激活后语义变更）。
    # 吸筹区条件全命中（极端卖墙+in_lower_half+4H accel=8.43>0 衰竭）且方向 DOWN、偏差 -0.20%。
    # 8/15 后 zone_cover 不再产生翻转 → 不再有"覆盖被 H9 拦截"路径，直接维持 DOWN + 锁低 + 缩仓。
    cases.append(("8/15 吸筹覆盖已禁用（原 zone_cover H9 拦截场景）：极端卖墙+低位+衰竭+大偏差 → 直接维持 DOWN+缩仓不翻向", {
        "pm_up": 50.0, "deviation_pct": -0.20, "tie_line_active": False,
        "macd_1h_hist": 5.0, "macd_4h_hist": -20.0, "taker_1h_buy_pct": 50.0,
        "in_lower_half": True, "macd_4h_accel": 8.43, "extreme_wall": True,
        "factors": {"price_position": -2, "taker_bs": -2, "order_flow": -2,
                    "macd_1h": 1, "macd_4h": -1, "taker_1h": 0},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.position_scale == 0.5
       and not any("覆盖为" in r for r in d.risks)))

    # 案例13（8/11 6AM 错单场机算口径，signal_validator 实盘校验层同源场景）：
    # AI 曾误判 UP/中（S₀声称+7：4H accel 误当 hist、墙背离同向误计+4、PM行自造"无共识"、漏 Taker分级、跳过G3）。
    # 机算因子：价格位置-3（×3权重，dev=-0.0529%锚下）+ Taker分级-1（0.589<0.8）+ 1H Taker 0（53.4%）+
    # 订单流+2（双正>0.05）+ 墙背离0（买墙159.9:1但1m上涨=同向不背离）→ S0=-2。
    # PM DOWN 75.5% 与 S0 同向 → PM行0；净分-2 DOWN。1H+60.32/4H-135.82 不同向 → H12 不触发；
    # G3 前提（逆共识）不成立；G4 |dev|<0.15% 不触发；G5.1 中性（1多1空）；方向DOWN+极端买墙（非卖墙）→ 吸筹区不覆盖；
    # G6 顺共识不触发 → ORDER DOWN。
    # round30: |净分|0-2+常规+顺PM 胜率 93.3% → round35（8/21）重算 37 场 67.6% 降级"低"
    # （210 场 regime_split_replay：Wilson95 上界 80.4% 已排除 93.3%，时间切分后半 52.6% 恶化；
    #   9.7#1/#6 降级已在 round29 移除，统计模型已反映）。
    cases.append(("8/11 6AM 错单场机算口径：S0=-2 顺 PM 共识 → ORDER DOWN 低（round35 0-2档降级；AI 曾误判 UP/中）", {
        "pm_up": 24.5, "deviation_pct": -0.0529, "tie_line_active": False,
        "macd_1h_hist": 60.32, "macd_4h_hist": -135.82, "taker_1h_buy_pct": 53.4,
        "macd_1h_accel": 7.85, "macd_4h_accel": 21.59,
        "in_lower_half": True, "extreme_wall": False,
        "regime": "趋势市", "volatility_5m": 0.1734, "consecutive_same_dir": 3,
        "minutes_to_settle": 31.6, "deviation_trend": "stable",
        "evidence_dims": 0,
        "factors": {"price_position": -3, "taker_bs": -1, "taker_1h": 0,
                    "order_flow": 2, "wall_divergence": 0},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.confidence == "低" and not d.h12_triggered and d.s0 == -2 and d.pm_row == 0
       and abs(d.probability - 67.6) < 1e-9))

    # 案例14（8/12 round11：3PM 场深负大趋势+短周期反弹先兆 → 缩仓不翻向）。
    # 3PM 实际：1H hist=-59.4/4H=-172.75 同向深负（H12 口径 |hist|≥30），但 G5.1 判"偏空非深度"
    # （taker_1h 48.5% 未<30% 确认，深度档降级）；1m 4连阳+5m 3连阳+15m MACD+8.53 → short_bounce=+1；
    # 方向 DOWN 顺深势、|偏差|0.014%<0.15% 贴锚定。修复前：S0=-4 满仓 DOWN 被反弹打脸（实际 UP）。
    # 修复后：短周期因子 +2 进 S0（-4→-2），方向仍 DOWN（不翻向，A 务实方案），G5.4 缩仓分支触发 scale=0.5。
    # 关键：缩仓分支用 H12 口径（1H/4H 同向深势）而非 G5.1 档位判定，否则偏空非深度场景会漏触发。
    cases.append(("3PM 深负大趋势+短周期反弹先兆+贴锚定 → 不翻向，锁低+缩仓0.5（round11）", {
        "pm_up": 41.0, "deviation_pct": -0.0143, "tie_line_active": False,
        "macd_1h_hist": -59.4, "macd_4h_hist": -172.75, "taker_1h_buy_pct": 48.5,
        "macd_1h_accel": -6.72, "macd_4h_accel": -24.14,
        "in_lower_half": True, "extreme_wall": True,  # 卖墙3.39:1 极端卖墙（DOWN 方向）
        "regime": "摆动市", "volatility_5m": 0.2807,
        "short_bounce": 1,  # signal_validator 机算：1m&5m 连阳≥2 + 15m MACD>0
        "factors": {"price_position": -3, "taker_bs": 1, "taker_1h": 0, "order_flow": -2,
                    "macd_15m": 1, "macd_4h": -2, "kline": 0, "seasonality": 1,
                    "short_bounce_factor": 2},  # 含短周期因子 +2
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.confidence == "低" and d.position_scale == 0.5 and d.s0 == 0
       and any("短周期反弹" in r for r in d.risks)))

    # 案例15（8/12 round12：6AM 场 G3 摆动市贴线豁免 → round13 取消 G6 SKIP：照常 ORDER UP 缩仓0.5）。
    # 6AM 实际：PM DOWN 65.5%（13 分钟前还是 UP 61.5%，剧烈反转）；偏差 -0.0448% 贴线；Regime 摆动市（flips=2）；
    # 原始方向 UP（S0=+2 含短周期反弹先兆+2/订单流双正+2/季节性+1，PM行-1 → 净分+1）。修复前：G3 硬覆盖
    # 改判 DOWN（价格未突破对家锚定≥0.15%）→ 满仓 DOWN 错单（实际 UP 结算）。
    # round12 修复：摆动市+贴锚定+short_bounce=+1（与原始方向 UP 同向）→ G3 豁免保留 UP、锁低+缩仓0.5、
    # net_for_prob 不回退（+1）。round12 时 G6 |净分|+1 低置信逆 PM DOWN≥60% → SKIP（signal=UP）规避亏损；
    # round13 用户决策取消 SKIP → 该弱信号改为 ORDER UP + 缩仓0.5（全量交易）。
    cases.append(("6AM 摆动市贴线 G3 豁免 → 保留UP，round13 取消 G6 SKIP，ORDER UP 缩仓0.5", {
        "pm_up": 34.5, "deviation_pct": -0.0448, "tie_line_active": False,
        "regime": "摆动市", "evidence_dims": 0, "short_bounce": 1,
        "macd_1h_hist": 64.54, "macd_4h_hist": -64.72, "taker_1h_buy_pct": 58.5,
        "macd_1h_accel": 7.38, "macd_4h_accel": 37.86,
        "in_upper_half": True, "extreme_wall": True,
        "factors": {"price_position": -3, "taker_bs": 1, "taker_1h": 0, "order_flow": 2,
                    "macd_15m": 1, "macd_4h": -2, "kline": 0, "seasonality": 1,
                    "short_bounce_factor": 2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "低" and d.position_scale == 0.5 and d.s0 == 4 and d.pm_row == -1
       and not d.h12_triggered
       and any("摆动市贴线豁免" in r for r in d.risks)))

    # 案例16（round12 回归 + round41.3 A 方案口径恢复：摆动市+贴线但无强证据（short_bounce=0）-> 豁免不触发）。
    # 对应 8/11 5pm 模式：PM DOWN 68.5%、偏差 -0.11%（贴线）、摆动市、原始方向 UP（逆 PM 共识）、
    # 但 1m/5m 无连续同向（short_bounce=0）-> 强证据不成立 -> G3 豁免不触发。
    # round41.3 A 方案：证据不全恢复硬覆盖翻向 PM DOWN（5pm 实际 DOWN 正确；round40 曾保留 UP，
    # 经 240 场回测证伪后回退）。锁定边界：豁免确实不触发（无豁免 risk），走硬覆盖分支。
    cases.append(("摆动市贴线无强证据（short_bounce=0）-> 豁免不触发，round42 G3 禁用保留原方向（净分 UP）", {
        "pm_up": 31.5, "deviation_pct": -0.11, "tie_line_active": False,
        "regime": "摆动市", "evidence_dims": 0, "short_bounce": 0,
        "macd_1h_hist": 0.0, "macd_4h_hist": 0.0, "taker_1h_buy_pct": 50.0,
        "factors": {"price_position": -3, "taker_bs": 1, "taker_1h": 0, "order_flow": 2,
                    "macd_15m": 1, "macd_4h": 0, "kline": 1, "seasonality": 1,
                    "short_bounce_factor": 0},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "低" and d.position_scale == 0.5
       and not any("摆动市贴线豁免" in r for r in d.risks)
       and any("round42 禁用改判" in r for r in d.risks)))

    # 案例17（round13：PM<60% 弱共识强信号 → 照常 ORDER，不 SKIP）。
    # 场景：PM 55%（弱共识，逆 S0 方向）+ S0=-11 强信号 DOWN。PM 弱档归零（Y）→ PM行 0 → 净分-11。
    # round13 前 G6 只对"逆共识≥60% 或逆位置"的弱信号 SKIP，强信号本就 ORDER；本轮锁定弱共识强信号
    # 仍交易（用户要求全量交易）。断言：ORDER + 最终方向 DOWN + |净分|≥6。
    cases.append(("round13 弱共识强信号：PM55% 逆S0 |净分|11 → ORDER DOWN（全量交易）", {
        "pm_up": 55.0, "deviation_pct": -0.25, "tie_line_active": False,
        "macd_1h_hist": 12.0, "macd_4h_hist": 18.0, "taker_1h_buy_pct": 60.0,
        "factors": {"price_position": -3, "taker_bs": -2, "taker_1h": -2,
                    "order_flow": -2, "macd_15m": -1, "seasonality": -1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and abs(d.net) >= 6 and d.pm_row == 0))

    # 案例18（round44.2 修订：弱共识弱信号逆位置 → G4 双向核对不过，保留 DOWN）。
    # 场景：PM 55%（弱共识，逆 S0 方向）+ S0=-1 弱信号 + 偏差 +0.30%（逆价格位置，price_dir=UP）。
    # 4H MACD=+8 支持 UP(+1维)，24h位置/宏观缺位 → 1/3 < 2 翻向条件不满足
    # → 保留 DOWN + scale 0.5 + 置信度低。风险标 G4 双向核对不通过。
    cases.append(("round44.2 弱共识弱信号逆位置：G4 双向核对 1/3 不通过 → 保留 DOWN+锁低（不翻 UP）", {
        "pm_up": 55.0, "deviation_pct": 0.30, "tie_line_active": False,
        "macd_1h_hist": 5.0, "macd_4h_hist": 8.0, "taker_1h_buy_pct": 60.0,
        "factors": {"price_position": 3, "taker_bs": -2, "order_flow": -2,
                    "macd_15m": -1, "seasonality": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.position_scale == 0.5 and d.confidence == "低"))

    # 案例19（round13：PM 60-65% 档保持现状不扩改动面——弱共识防线只收紧 <60%）。
    # 场景：PM 62%（逆 S0，净分 -3 弱信号低置信）。60-65% 档保持现状：逆共识弱信号 → 缩仓 ORDER（非 SKIP）。
    # 断言：ORDER + 缩仓 0.5 + 置信度低（G6 弱信号分支统一缩仓承接）。
    cases.append(("round13 PM62% 逆共识弱信号 → 缩仓 ORDER（60-65% 档保持）", {
        "pm_up": 62.0, "deviation_pct": -0.10, "tie_line_active": False,
        "macd_1h_hist": -3.0, "macd_4h_hist": -5.0, "taker_1h_buy_pct": 50.0,
        "factors": {"price_position": 3, "taker_bs": 2, "taker_1h": 0,
                    "order_flow": -2, "macd_15m": 0, "seasonality": 1},
    }, lambda d: d.action == "ORDER" and d.position_scale == 0.5
       and d.confidence == "低"))

    # 案例20（round14：价格位置近贴线降权）：8/12 3pm 场——|偏差|<0.15% 时 price_position ×3→×2。
    # 降权前 price_position=-3 → S0=0 → 方向由价格位置 DOWN（错，实际 UP）；降权后 -2 → S0=+1 → UP（对）。
    # 断言：降权输入（price_position=-2）下方向 UP，S0=+1（临界场翻转修正）。
    cases.append(("round14 价格位置近贴线降权：8/12 3pm 场 price_position -2 → S0+1 UP（未降权 -3 → S0 0 DOWN 错）", {
        "pm_up": 56.5, "deviation_pct": -0.0289, "tie_line_active": True,
        "macd_1h_hist": -47.86, "macd_4h_hist": -79.47, "taker_1h_buy_pct": 54.7,
        "factors": {"price_position": -2, "taker_bs": 1, "order_flow": 2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP" and d.s0 == 1))

    # 案例21（round18 反手预警翻转级正例：8/14 1AM ET 错单场）。1AM 快照：偏差 -0.0735%（贴线维持，
    # 非激活）+ PM DOWN 62.5%（60-80 顺共识）+ historical.up_rate=0.571（时段偏多，预测 DOWN=反向历史）
    # + 4H accel +2.57（跌势衰竭→反弹，逆预测 DOWN）。机算 S0=-4（price_pos-1/taker_bs-1/order_flow-2）
    # 顺 PM 共识 → 方向 DOWN，净分 ≤-4（round22 收紧后仍满足），|4H hist|=9.44<50，贴线模式。
    # F1 全满足 → 【翻转级】：方向 DOWN→UP + 置信度锁低 + 缩仓0.5（round30.1 置信度联动）
    cases.append(("round18 反手预警翻转级：8/14 1AM ET 贴线+顺共识+衰竭+浅势+净分≤-4 → 翻转 UP，锁低缩仓0.5", {
        "pm_up": 37.5, "deviation_pct": -0.0735, "tie_line_active": False,
        "_tie_mode": True, "macd_1h_hist": -5.69, "macd_4h_hist": -9.44,
        "taker_1h_buy_pct": 47.3, "macd_4h_accel": 2.57, "historical_up_rate": 0.571,
        "factors": {"price_position": -1, "taker_bs": -1, "taker_1h": 0, "order_flow": -2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "低" and d.position_scale == 0.5
       and any("翻转级" in r for r in d.risks)))

    # 案例22（round18 反手预警观察级反例）：命中 ≥3/4 维但不满足 F1 → 方向/置信/仓位完全不动。
    # 构造：贴线模式 + 顺共识 + 衰竭（观察级触发），但 |4H hist|=150≥50（深势，非 F1）
    # → 仅 risks 标注，方向保持 DOWN、置信度不强制锁低、scale 不变（不误伤正确场次）。
    cases.append(("round18 反手预警观察级：≥3/4维但深势(|4H hist|≥50)非F1 → 维持原判不干预", {
        "pm_up": 37.5, "deviation_pct": -0.09, "tie_line_active": True,
        "_tie_mode": True, "macd_1h_hist": -80.0, "macd_4h_hist": -150.0,
        "taker_1h_buy_pct": 45.0, "macd_4h_accel": 3.2, "historical_up_rate": 0.58,
        "factors": {"price_position": -2, "taker_bs": -2, "taker_1h": -2, "order_flow": -2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and any("观察级" in r for r in d.risks)
       and not any("翻转级" in r for r in d.risks)))

    # 案例23（round18 反手预警不触发反例）：强信号顺大趋势场，仅 1 维命中（或 <3 维）→ 不触发观察级，
    # 维持原始方向/仓位（确保不误伤正常强信号场次）。构造：PM 逆共识强信号 + 顺 4H 大趋势，无贴线、
    # 无反向历史、4H 衰竭方向与预测一致 → 0 维命中 → 反手预警不触发，无相关 risk 标注。
    cases.append(("round18 反手预警不触发：强信号顺大趋势（<3维）→ 维持原方向/仓位不误伤", {
        "pm_up": 55.0, "deviation_pct": -0.30, "tie_line_active": False,
        "_tie_mode": False, "macd_1h_hist": -40.0, "macd_4h_hist": -60.0,
        "taker_1h_buy_pct": 25.0, "macd_4h_accel": -5.0, "historical_up_rate": 0.40,
        "evidence_dims": 2,
        "factors": {"price_position": -3, "taker_bs": -2, "taker_1h": -4,
                    "order_flow": -2, "macd_4h": -2, "seasonality": -1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and not any("反手预警" in r for r in d.risks)))

    # 案例27（8/15 flip_gates_replay 修复：吸筹/派发覆盖加 PM 深度共识护栏）。
    # 回测误伤场 8/13 4am：PM=16.5%（DOWN 83.5% 深度看空）+ 极端卖墙 + in_lower_half
    # + 4H accel=18>0（强衰竭），旧逻辑 zone_cover 硬翻 UP，实际 DOWN → 误伤。
    # 修复：PM 任一侧≥80% 深度共识 → 禁止吸筹/派发覆盖翻向，维持原方向 DOWN，仅锁低+缩仓 0.5。
    cases.append(("8/15 PM深度共识护栏：吸筹/派发覆盖被拦（8/13 4am 误伤修复）→ 维持原方向+缩仓", {
        "pm_up": 16.5, "deviation_pct": -0.1156, "tie_line_active": False,
        "macd_1h_hist": -4.0, "macd_4h_hist": -148.0, "taker_1h_buy_pct": 58.0,
        "in_lower_half": True, "macd_4h_accel": 18.0, "extreme_wall": True,
        "factors": {"price_position": -2, "taker_bs": -1, "order_flow": -2,
                    "macd_15m": -1, "macd_4h": 2, "kline": -1, "seasonality": 0,
                    "short_bounce_factor": -2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.position_scale == 0.5
       and any("护栏拦截" in r for r in d.risks)
       and not any("覆盖为" in r for r in d.risks)))

    # 案例28（8/15 贴线高不确定降级：9.7#8；round41.3 A 方案方向口径恢复）。
    # 8/14 4pm 场特征：贴线激活 + 极端卖墙(买1.17 vs 卖11.49=9.8:1) + taker_bs_ratio=5.627 极端买。
    # 极端墙+极端Taker 并存 -> 方向判断可靠性仅 62%（综合诊断），应锁低+缩仓0.5，不翻转。
    # round41.3：PM UP 64.5% 逆 S₀ + 证据不全 -> G3 硬覆盖翻 UP（本案例核心锁定 9.7#8 降级，方向为次）。
    cases.append(("8/15 贴线高不确定降级：极端墙+极端Taker -> 锁低+缩仓0.5（9.7#8，round42 G3 禁用保留 DOWN）", {
        "pm_up": 64.5, "deviation_pct": -0.0001, "tie_line_active": True,
        "_tie_mode": True, "macd_1h_hist": -21.5, "macd_4h_hist": -222.0,
        "taker_1h_buy_pct": 50.0, "taker_bs_ratio": 5.627,
        "_buy_wall_ratio": 1.17, "_sell_wall_ratio": 11.49,
        "factors": {"price_position": -2, "taker_bs": 2, "taker_1h": 4,
                    "order_flow": -2, "wall_divergence": 0},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.position_scale == 0.5 and d.confidence == "低"
       and any("贴线高不确定" in r for r in d.risks)))

    # 案例29（8/15 G5.4 吸筹覆盖翻向去激活核心回归：8/14 8PM ET 错判场）。
    # 本场：pm_up=34.5（PM DOWN65.5%，<80% 护栏不触发）、in_lower_half=true、4H accel=9.29>0（跌势衰竭）、
    # 极端卖墙38.7:1、偏差-0.0644%（<0.15% H9 不拦截）、净分-6→DOWN。
    # 旧逻辑 zone_cover 硬翻 DOWN→UP → 实际 DOWN 错判。修复后：不翻向，维持 DOWN+锁低+缩仓0.5。
    cases.append(("8/15 G5.4 吸筹覆盖去激活：8/14 8PM 场（极端卖墙+低位+4H衰竭+PM65.5%）→ 维持 DOWN 不翻向", {
        "pm_up": 34.5, "deviation_pct": -0.0644, "tie_line_active": False,
        "macd_1h_hist": 35.95, "macd_4h_hist": -4.24, "taker_1h_buy_pct": 56.2,
        "macd_4h_accel": 9.29, "in_lower_half": True, "extreme_wall": True,
        "factors": {"price_position": -2, "taker_bs": -1, "order_flow": -2,
                    "macd_15m": 1, "macd_4h": -2, "kline": 0, "seasonality": 0,
                    "short_bounce_factor": 0},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.position_scale == 0.5 and d.confidence == "低"
       and any("吸筹嫌疑" in r for r in d.risks)
       and not any("覆盖为" in r for r in d.risks)))

    # 案例30（round22 F1 阈值收紧回归：8/14 11PM ET 擦线场，净分=-3 不再触发翻转级）。
    # 本场：贴线维持 + PM DOWN 68.5%（顺共识 60-80）+ 4H accel=+14.95（跌势衰竭）+ |4H hist|=1.42<50
    # + 引擎机算 S0=-3（price_pos-1/taker_bs-2），PM 行 0 → 净分 -3。
    # round22 前（≤-3）：F1 翻转 DOWN→UP → 实际 DOWN 错判（唯一翻错场）。
    # round22 收紧（≤-4）：-3 不满足 → 不翻转，降观察级，保持 DOWN。
    cases.append(("round22 F1 阈值收紧：8/14 11PM 场净分-3擦线 → 不翻转级，维持 DOWN（观察级）", {
        "pm_up": 31.5, "deviation_pct": -0.0676, "tie_line_active": False,
        "_tie_mode": True, "macd_1h_hist": 46.53, "macd_4h_hist": 1.42,
        "taker_1h_buy_pct": 58.1, "macd_4h_accel": 14.95, "historical_up_rate": 0.476,
        "factors": {"price_position": -1, "taker_bs": -2, "taker_1h": 0, "order_flow": 0, "wall_divergence": 0},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.net == -3.0
       and not any("翻转级" in r for r in d.risks)
       and any("观察级" in r for r in d.risks)))

    # 案例31（round23 反转价格门槛反例：F1 翻转目标 UP 价 <0.23 → 禁止翻转，降观察级）。
    # 复用案例21（8/14 1AM 场，pm_up=37.5→UP 价 0.375≥0.23 本应 F1 翻转）全部其他条件，
    # 仅显式注入 flip_target_price=0.15（模拟写信号实时查价该场 UP 已跌到 0.15<0.23）。
    # round23 门槛：目标价<0.23 → F1 不触发翻转级，降为观察级，维持 DOWN + 缩仓0.5（round30.1 置信度联动；
    # round41.2 概率表重校后该组合（3-5 常规顺PM 趋势市）置信度"低"，仍锁缩仓）。
    cases.append(("round23 反转价格门槛反例：F1 翻转目标价0.15<0.23 → 禁止翻转，维持 DOWN（观察级）", {
        "pm_up": 37.5, "deviation_pct": -0.0735, "tie_line_active": False,
        "_tie_mode": True, "macd_1h_hist": -5.69, "macd_4h_hist": -9.44,
        "taker_1h_buy_pct": 47.3, "macd_4h_accel": 2.57, "historical_up_rate": 0.571,
        "flip_target_price": 0.15,
        "factors": {"price_position": -1, "taker_bs": -1, "taker_1h": 0, "order_flow": -2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.confidence == "低" and d.position_scale == 0.5
       and not any("翻转级" in r for r in d.risks)
       and any("观察级" in r for r in d.risks)))

    # 案例32（round23 反转价格门槛正例：F1 翻转目标 UP 价 ≥0.23 → 正常翻转）。
    cases.append(("round23 反转价格门槛正例：F1 翻转目标价0.375≥0.23 → 正常翻转 UP，锁低缩仓0.5", {
        "pm_up": 37.5, "deviation_pct": -0.0735, "tie_line_active": False,
        "_tie_mode": True, "macd_1h_hist": -5.69, "macd_4h_hist": -9.44,
        "taker_1h_buy_pct": 47.3, "macd_4h_accel": 2.57, "historical_up_rate": 0.571,
        "flip_target_price": 0.375,
        "factors": {"price_position": -1, "taker_bs": -1, "taker_1h": 0, "order_flow": -2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "低" and d.position_scale == 0.5
       and any("翻转级" in r for r in d.risks)))

    # 案例33（8/16 修复 Bug#2 + round41.3 A 方案路径恢复：G3 硬覆盖后 G5 maintain_trend 不得改回）。
    # 场景：S0=-6 DOWN（含PM行+1净分-5）、PM UP65% 逆共识、证据不足（evidence_dims=0）、偏空非深度 + fakeout=诱空。
    # Bug#2：G3 硬覆盖 direction=UP（顺共识），G5 非深度档设 maintain_trend -> 曾被静默改回 DOWN（bug）。
    # round41.3 A 方案：恢复硬覆盖（g3_overrode=True）-> Bug#2 保护路径重新生效：maintain_trend 放弃改向，
    # direction 保持 G3 的 UP + 锁低 + 缩仓 0.5。锁定该保护链路。
    cases.append(("round42：G3 禁用后保留原 DOWN 方向（净分向下），G5 maintain_trend 路径不触发", {
        "pm_up": 65.0, "deviation_pct": 0.05, "tie_line_active": False,
        "macd_1h_hist": -10.0, "macd_4h_hist": -12.0, "taker_1h_buy_pct": 25.0,
        "evidence_dims": 0, "fakeout": "诱空",
        "factors": {"price_position": -2, "taker_bs": -2, "order_flow": -2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and d.position_scale == 0.5
       and any("round42 禁用改判" in r for r in d.risks)))

    # 案例34（8/16 修复 Bug#1：中性档 fakeout 逆价格位置被 H9 拦）。
    # 场景：dev=+0.3%（price_dir=UP）、MACD 一正一负（中性档）、fakeout=诱多(DOWN)。
    # 修复前：中性档直接 direction=DOWN（绕过 H9，逆价格位置翻向）。
    # 修复后：H9 拦截（|dev|≥0.15% 且 fakeout 目标逆 price_dir）→ 不翻向，维持 UP + 锁低 + 缩仓0.5。
    cases.append(("8/16 修复Bug#1：中性档 fakeout=诱多 逆价格位置(dev+0.3%) → H9 拦，维持 UP 锁低缩仓0.5", {
        "pm_up": 50.0, "deviation_pct": 0.30, "tie_line_active": False,
        "macd_1h_hist": 15.0, "macd_4h_hist": -15.0, "taker_1h_buy_pct": 50.0,
        "fakeout": "诱多",
        "factors": {"price_position": 2, "taker_bs": 1, "macd_15m": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.position_scale == 0.5 and d.confidence == "低"
       and any("fakeout" in r and "H9" in r for r in d.risks)))

    # 案例35（8/16 修复 Bug#1：非深度档 maintain_trend 逆价格位置被 H9 拦）。
    # 场景：dev=+0.3%（price_dir=UP）、偏空非深度（tdir=DOWN）、fakeout=诱空(UP) → 非深度档维持 DOWN + maintain_trend。
    # 修复前：maintain_trend 472 行直接 direction=DOWN（逆价格位置 UP 翻向）。
    # 修复后：H9 拦截（|dev|≥0.15% 且 maintain 目标 DOWN 逆 price_dir）→ 不翻向，维持 UP + 锁低 + 缩仓0.5。
    cases.append(("8/16 修复Bug#1：非深度 maintain_trend=DOWN 逆价格位置(dev+0.3%) → H9 拦，维持 UP 锁低缩仓0.5", {
        "pm_up": 50.0, "deviation_pct": 0.30, "tie_line_active": False,
        "macd_1h_hist": -10.0, "macd_4h_hist": -12.0, "taker_1h_buy_pct": 25.0,
        "fakeout": "诱空",
        "factors": {"price_position": 2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.position_scale == 0.5 and d.confidence == "低"
       and any("maintain_trend" in r and "H9" in r for r in d.risks)))

    # 案例36（round28 P2修复：9.7#4 贴线+|S0|≥3+顺PM 子组锁低）。
    # 回测17场该子组胜率仅64.7%，远低于全量77.8%。贴线+强信号+顺共识=庄家收割最优场景。
    # 修复：在9.7#4 scale 0.5基础上，额外锁低置信度。
    cases.append(("round28 9.7#4子组锁低：贴线+|S0|≥3+顺PM → 锁低+scale 0.5", {
        "pm_up": 65.0, "deviation_pct": 0.02, "tie_line_active": True,
        "macd_1h_hist": 5.0, "macd_4h_hist": 8.0, "taker_1h_buy_pct": 50.0,
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "低" and d.position_scale == 0.5
       and any("贴线+强信号+顺共识" in r for r in d.risks)))

    # 案例37（round29 置信度重构验证：常规+顺PM+强信号 → 应输出"高"置信度）。
    # 旧方案：9.7#1 + 步骤10摆动市/贴线cap 会把几乎所有信号锁低，导致"高"置信度几乎不出现。
    # 新方案：统计模型驱动，|净分|≥6+常规+顺PM 回测胜率 84.6% → 置信度应为"高"。
    cases.append(("round29 置信度重构：常规+顺PM+强信号 → 置信度高", {
        "pm_up": 65.0, "deviation_pct": 0.10, "tie_line_active": False,
        "macd_1h_hist": 20.0, "macd_4h_hist": 15.0, "taker_1h_buy_pct": 65.0,
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 2},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "高" and d.probability >= 80.0))

    # 案例38（round30 四维置信度：|净分|3-5 + 常规+顺PM，趋势市 → 中，摆动市 → 高）。
    # S0=+5（price_position+2, taker_bs+2, order_flow+1），PM 60% UP 同向 PM行=0，净分=+5。
    # |净分|=5∈3-5，常规（|dev|=0.10），顺PM。
    # 趋势市：round41.2 重跑 65.7%（35场）→ 置信度"低"（原 75.0%"中"为旧表值）。
    # 摆动市：round41.2 重跑 73.3%（30场）→ 置信度"中"（原 88.9%"高"为 9 场小样本高估）。
    cases.append(("round41.2 四维置信度：3-5+常规+顺PM+趋势市 → 置信度低（65.7%）", {
        "pm_up": 60.0, "deviation_pct": 0.10, "tie_line_active": False,
        "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "taker_1h_buy_pct": 55.0,
        "regime": "趋势市",
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "低" and d.probability == 65.7))
    cases.append(("round41.2 四维置信度：3-5+常规+顺PM+摆动市 → 置信度中（73.3%）", {
        "pm_up": 60.0, "deviation_pct": 0.10, "tie_line_active": False,
        "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "taker_1h_buy_pct": 55.0,
        "regime": "摆动市",
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "中" and d.probability == 73.3))

    # 案例39（round31 EV 仅日志：低概率+高价格 → EV<5% 仍 ORDER）。
    # 贴线+顺PM+3-5 净分 → round41.2 概率 62.5%；max_price=0.60（封顶价）→ EV=62.5-60=2.5%<5% → 仍 ORDER。
    cases.append(("round31 EV 仅日志：贴线+顺PM+3-5+max_price=0.60 → EV=2.5%<5% ORDER", {
        "pm_up": 60.0, "deviation_pct": 0.02, "tie_line_active": True,
        "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "taker_1h_buy_pct": 55.0,
        "regime": "趋势市",
        "_max_price": 0.60,
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 1},
    }, lambda d: d.action == "ORDER" and d.ev is not None and d.ev < 5.0
       and any("EV" in r and "仅日志" in r for r in d.risks)))

    # 案例40（round31 EV 高价值：高概率+高价格 → EV>5% → ORDER）。
    # 强信号+常规+顺PM → 概率 84.6%；max_price=0.55 → EV=84.6-55=29.6%>5% → ORDER。
    cases.append(("round31 EV 高价值：强信号+max_price=0.55 → EV=29.6%>5% ORDER", {
        "pm_up": 65.0, "deviation_pct": 0.10, "tie_line_active": False,
        "macd_1h_hist": 20.0, "macd_4h_hist": 15.0, "taker_1h_buy_pct": 65.0,
        "_max_price": 0.55,
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 2},
    }, lambda d: d.action == "ORDER" and d.ev is not None and d.ev > 5.0))

    # 案例41（round31 H12 锁低修复）：H12 触发且净分顺深势 → 仍锁低，不被基准置信度覆盖。
    cases.append(("round31 H12 触发顺深势仍锁低", {
        "pm_up": 50.0, "deviation_pct": 0.02, "tie_line_active": True,
        "macd_1h_hist": 35.0, "macd_4h_hist": 40.0, "taker_1h_buy_pct": 80.0,
        "regime": "趋势市", "volatility_5m": 0.1,
        "factors": {"price_position": 1, "taker_bs": 2, "taker_1h": 4,
                    "taker_1m": 2, "order_flow": 2},
    }, lambda d: d.action == "ORDER" and d.h12_triggered and d.confidence == "低"
       and d.position_scale == 0.5 and d.final_direction == "UP"))

    # 案例42（round38 H13 顶部收敛摆动日守卫引擎化，8/22 审计 Fix 5）。
    # 场景：4H RSI=92.5（深多顶部）+ 最近 3 根已收 1H 交替（UP/DOWN/UP）+ 涨跌幅均 <0.7%。
    # 8/21 五连错复盘实证：该场景趋势类因子系统性失灵，5 连错中至少 4 场会被覆盖。
    # 动作：锁低 + scale 0.5，不翻向不 SKIP（风控守卫），方向照常由净分决定（本例净分+5->UP）。
    cases.append(("H13 顶部收敛摆动日：4H RSI92.5+3根交替+收敛 -> 锁低缩仓0.5，方向不变", {
        "pm_up": 55.0, "deviation_pct": 0.10, "tie_line_active": False,
        "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "taker_1h_buy_pct": 55.0,
        "regime": "趋势市",
        "rsi_4h": 92.5, "prev_dirs": ["UP", "DOWN", "UP"], "prev_chgs": [0.3, -0.2, 0.4],
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and d.confidence == "低" and d.position_scale == 0.5
       and any("H13 顶部收敛" in r for r in d.risks)))

    # 案例43（H13 反例：RSI 极端但涨跌幅超 0.7% 未收敛 -> 不触发）。
    cases.append(("H13 反例：4H RSI92.5 但涨跌幅含 0.9%>0.7% 未收敛 -> 不触发守卫", {
        "pm_up": 55.0, "deviation_pct": 0.10, "tie_line_active": False,
        "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "taker_1h_buy_pct": 55.0,
        "regime": "趋势市",
        "rsi_4h": 92.5, "prev_dirs": ["UP", "DOWN", "UP"], "prev_chgs": [0.3, 0.9, 0.4],
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "UP"
       and not any("H13 顶部收敛" in r for r in d.risks)))

    # 案例44（H13 反例：RSI 不极端 -> 不触发；字段缺失（None）-> fail-open 不触发）。
    cases.append(("H13 反例：RSI 55 不极端 -> 不触发；prev_chgs 缺失 fail-open", {
        "pm_up": 55.0, "deviation_pct": 0.10, "tie_line_active": False,
        "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "taker_1h_buy_pct": 55.0,
        "regime": "趋势市",
        "rsi_4h": 55.0, "prev_dirs": ["UP", "DOWN", "UP"], "prev_chgs": None,
        "factors": {"price_position": 2, "taker_bs": 2, "order_flow": 1},
    }, lambda d: d.action == "ORDER" and not any("H13 顶部收敛" in r for r in d.risks)))

    # ---- round41.8.14（8/25 用户决策「直接上无条件版不重放」）：judge 共识源无条件切 book_up_mid ----
    # 案例45（大偏离切换）：标记价 70.5% 假强共识 vs 盘口 44%（8/24 3pm 事故同构）。
    # 旧口径（读标记价）pm_side=70.5 → G3 证据不全硬覆盖翻 UP；新口径 pm_side=56<60 →
    # G3 不触发，方向保持 S0 负向 DOWN，trace 记「⚠️标记价失真」。
    cases.append(("41.8.14 大偏离：mark 70.5 vs book 44 → G3 不触发保持 DOWN", {
        "pm_up": 70.5, "book_up_mid": 44.0, "deviation_pct": -0.046, "tie_line_active": False,
        "macd_1h_hist": 113.0, "macd_4h_hist": -351.0, "taker_1h_buy_pct": 65.3,
        "regime": "趋势市", "evidence_dims": 0,
        "factors": {"price_position": -2, "order_flow": -2, "taker_trend": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and not any("改判顺共识" in r for r in d.risks)
       and any("标记价失真" in t for t in d.trace)))

    # 案例46（小偏离也切换=无条件）：mark 63.5 vs book 56（差 7.5pt<10，不记失真 trace）。
    # 旧口径 G3 触发翻 UP；新口径 pm_side=56<60 → G3 不触发保持 DOWN——证明切换无条件、
    # 不设偏离门槛。
    cases.append(("41.8.14 小偏离 7.5pt：无条件切 book 56 → G3 不触发保持 DOWN（无失真标注）", {
        "pm_up": 63.5, "book_up_mid": 56.0, "deviation_pct": 0.1127, "tie_line_active": False,
        "macd_1h_hist": -5.0, "macd_4h_hist": -8.0, "taker_1h_buy_pct": 63.6,
        "regime": "摆动市", "evidence_dims": 0,
        "factors": {"price_position": 2, "taker_bs": -6, "taker_1h": 0, "trend_pulse": 0,
                    "order_flow": 2, "macd_15m": -1, "macd_1h": -1, "macd_4h": -1,
                    "kline": 0, "rsi": 0, "open_micro": 1, "seasonality": 1},
    }, lambda d: d.final_direction == "DOWN" and d.action == "ORDER"
       and not any("改判顺共识" in r for r in d.risks)
       and not any("标记价失真" in t for t in d.trace)))

    # 案例47（book 缺失回退标记价）：round42 G3.5 禁用后保留原方向 DOWN，不再翻向 PM UP。
    cases.append(("round42 book 缺失 → 回退标记价，G3 禁用保留 DOWN（案例1 复刻改为禁用版本）", {
        "pm_up": 63.5, "book_up_mid": None, "deviation_pct": 0.1127, "tie_line_active": False,
        "macd_1h_hist": -5.0, "macd_4h_hist": -8.0, "taker_1h_buy_pct": 63.6,
        "regime": "摆动市", "evidence_dims": 0,
        "factors": {"price_position": 2, "taker_bs": -6, "taker_1h": 0, "trend_pulse": 0,
                    "order_flow": 2, "macd_15m": -1, "macd_1h": -1, "macd_4h": -1,
                    "kline": 0, "rsi": 0, "open_micro": 1, "seasonality": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN" and d.confidence == "低"
       and d.position_scale == 0.5 and any("round42 禁用改判" in r for r in d.risks)))

    # 案例48（8/24 3pm 场回归）：mark 70.5 vs book 44 + 摆动市贴线 + S0=-3。
    # 旧口径靠「G3 摆动市贴线豁免」侥幸保住 DOWN（豁免仅引擎中间态，终被 validator
    # _consensus_flip 读假标记价翻成 UP 实亏 -2.8）；新口径 pm_side=56 → G3 根本不触发，
    # DOWN 从源头稳固，无需豁免救场。
    cases.append(("41.8.14 3pm 场回归：book 44 → G3 不触发（豁免无需救场），DOWN 稳固", {
        "pm_up": 70.5, "book_up_mid": 44.0, "deviation_pct": -0.046, "tie_line_active": False,
        "macd_1h_hist": 113.0, "macd_4h_hist": -351.0, "taker_1h_buy_pct": 65.3,
        "regime": "摆动市", "evidence_dims": 0, "short_bounce": -2,
        "factors": {"price_position": -2, "kline": -1, "order_flow": -2, "taker_trend": 1},
    }, lambda d: d.action == "ORDER" and d.final_direction == "DOWN"
       and not any("改判顺共识" in r for r in d.risks)
       and not any("贴线豁免" in r for r in d.risks)))

    failed = 0
    for name, inp, check in cases:
        mp = inp.pop("_max_price", 0.50)
        d = judge(inp, max_price=mp)
        ok = bool(check(d))
        status = "✅" if ok else "❌"
        print(f"{status} {name}")
        print(f"   → {d.action} {d.final_direction} 置信度{d.confidence} scale={d.position_scale} "
              f"P={d.probability}% EV={d.ev}% 净分={d.net:+.1f}（S0={d.s0:+.1f} PM行={d.pm_row:+.0f}）")
        if not ok:
            failed += 1
            for line in d.trace:
                print(f"     trace: {line}")
    print(f"\n规则版本锚点：{RULE_VERSION}")

    # ---- 版本锚点防漂移检查（2026-08-22 审计 Fix 6；2026-08-24 round41.8.1 升级双轨比对）----
    # 读 prompt_exec.md 头部版本锚点，与本引擎 RULE_VERSION 比对。
    # 双轨口径（round41.8.1，用户批准方案A）：文档主锚点跟随最新轮次（含 bot 执行层轮），
    # 引擎 judge 逻辑版本独立演化 → 比对时优先取 RULE_VERSION 中 exec-round 标记
    # （执行层轮次由 hermes_1h_bot.py 承载、judge 不变），无 exec 标记才退回首个 round 号。
    # 不一致只打印 ⚠️ 告警（不 assert 失败、不阻断实盘，fail-open 精神）--
    # 根因修复：round34~41.1 八个版本 4 个逻辑级缺口全部源于"文档改版引擎没跟上"且无告警。
    import re as _re
    from pathlib import Path as _Path
    _doc = _Path(__file__).resolve().parent.parent / "prompt_exec.md"
    if _doc.exists():
        try:
            _head = _doc.read_text(encoding="utf-8")[:2000]
            _m = _re.search(r"round(\d+(?:\.\d+)?)", _head)
            _me = _re.search(r"exec-round(\d+(?:\.\d+)?)", RULE_VERSION)
            _m2 = _re.search(r"round(\d+(?:\.\d+)?)", RULE_VERSION)
            if _m and (_me or _m2):
                _doc_v = _m.group(1)
                _eng_v = (_me.group(1) if _me else _m2.group(1))
                _layer = "执行层exec" if _me else "judge层"
                if _doc_v == _eng_v:
                    print(f"版本一致性：✅ 文档 round{_doc_v} == 引擎{_layer} round{_eng_v}"
                          f"（judge层 round{_m2.group(1) if _m2 else '?'} 未变）")
                else:
                    print(f"版本一致性：⚠️ 漂移！文档 round{_doc_v} != 引擎{_layer} round{_eng_v}"
                          f" -- 文档改版后须人工同步引擎判定逻辑并抬升 RULE_VERSION")
        except OSError:
            print("版本一致性：⚠️ prompt_exec.md 读取失败，跳过漂移检查")
    else:
        print("版本一致性：⚠️ prompt_exec.md 不存在，跳过漂移检查")

    print(f"selftest：{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：python3 scripts/rule_engine.py --selftest")
    print("程序内调用：from rule_engine import judge; judge({...}) -> Decision")
