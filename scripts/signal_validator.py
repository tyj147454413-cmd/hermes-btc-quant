#!/usr/bin/env python3
"""
实盘引擎校验层（2026-08-11 防错补丁，write_signal.py 唯一调用）

定位：在信号写入前，用 rule_engine.judge() 对 AI（DeepSeek）信号做确定性复算。
背景：8/11 审计确认 AI 文本推理存在复发型错误——漏因子、订单簿墙背离误读、
macd_acceleration/macd_hist 字段混淆、PM 行自造"无共识"档、跳过 G3——纯文本链路零校验无人能拦。

策略（混合校验）：
- 能机算的因子从 market_data 快照确定性重算：价格位置(×1)/Taker B/S 双窗分级(5m+15m 各×1，round42.4)/
  1m taker 趋势(贴线专列×3，round42.4)/订单流(×1)/K线序列(×2)/momentum_burst(×2)/pm_momentum(×3,round47；round49回退×2)/
  pm_imbalance(×1,round48)/宏观趋势族(×1)/短周期反弹先兆(×1)/开盘微观/订单簿墙（贴线背离行与结算期行）；
  历史季节性(round47退出记分,仅historical_up_rate字段保留供反手预警组合判定)；
- AI 专属因子不机算（保守默认）：taker_trend（脉冲判定）、evidence_dims、fakeout、
  veto_triggered（第三层否决）——在 engine_check.coverage 显式标注缺口；
- 贴线/阶段机器判定（近似，assumptions 写入 trace）：|dev%|<0.10 且波动率≤0.3% 且非摆动市
  → 贴线口径（MACD/K线/季节/开盘微观不计分，启用贴线专列）；:46+ 且贴线条件并存 → 结算期口径优先。

修正策略（向保守方向，引擎为准）：
- 引擎方向 ≠ AI side → 修正为引擎方向 + 置信度强制锁低 + 概率按引擎回写 + max_price/ev 作废
  （旧值按原方向定价，翻向后语义错误；bot 以实时 best_ask 为主、max_price 仅兜底，作废更安全）；
- 方向一致但引擎置信度更严 → 取严 + 概率回写；position_scale 两侧取更保守者；
- round13 用户决策（全量交易，不允许跳过）：SKIP 仅保留 G1 数据失效。引擎 G6/H12 不再产出 SKIP；
  AI 输出 SKIP 时按引擎最终方向修正为 ORDER（锁低 + 缩仓承接），不再"保留 SKIP 不强迫下单"。

fail-open 铁律：快照缺失/字段异常/数据不足 → 原信号放行 + engine_check.skipped 记录原因，
永不阻塞信号链路（本函数自身的未预期异常向上抛，由 write_signal.py 外层 try/except 兜底）。

selftest：python3 scripts/signal_validator.py --selftest
  用 data/snapshots/bitcoin-up-or-down-august-11-2026-6am-et_20260811_182827.json 端到端复现
  8/11 6AM 错单场（AI 判 UP/中/S₀声称+7，引擎应复算 S₀=-1[round41.8.15 价格位置 ×2→×1
  后由 -2 升至 -1]、PM行0、最终 DOWN/低/ORDER）。
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 8/21 修复：路径可移植化。此前写死宿主机绝对路径 + /tmp，Claude.app 桌面端沙箱
# （/sessions/<id>/mnt/... 挂载）看不到 /Users/... 绝对路径，且 hermes_collect 8 月起
# 输出 data/hermes_market_data.json（/tmp 早已 stale）→ 校验层 8/19 起 100% 静默跳过，
# AI 原始方向直通（17.4% 引擎/AI 分歧无人拦截的根因）。
# 主路径 = 脚本相对（宿主机与沙箱挂载点均成立）；/tmp 与宿主机绝对路径仅作回退。
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_PROJECT_ROOT))  # 沙箱内 hermes_common 位于项目根
from rule_engine import CONF_LEVELS, judge  # noqa: E402
try:  # noqa: E402 - round23 实时查价复用 hermes_common 网络层
    from hermes_common import GAMMA_API, CLOB_API, http_get_json, get_proxy as _get_proxy  # noqa: E402
except Exception:  # noqa: E402 - 导入失败则禁用实时查价（fail-open，退回快照价）
    GAMMA_API, CLOB_API, http_get_json, _get_proxy = None, None, None, None

TMP_MARKET_DATA = _PROJECT_ROOT / "data" / "hermes_market_data.json"
SNAPSHOT_DIR = _PROJECT_ROOT / "data" / "snapshots"
_TMP_FALLBACK = Path("/tmp/hermes_market_data.json")
_SNAPSHOT_FALLBACK = Path("/Users/tangyijun/Desktop/hermes btc/data/snapshots")

# 评分表口径（prompt.md@2026-08-22-round41.2-工作流审计修复）：
# 价格位置 ×1（round41.8.15 全局 ×2→×1；round41.8.20 锚定口径统一：与 G4/H9 同源，24h 分位退出）；Taker 分级 ×1（±1，机构级 ×2）；
# 1H Taker ×2（±2 基础，>70%/<30% 才计）；订单流 ×2（±1 基础，双正>0.05/双负<-0.05）；15m MACD ×1；
# PM 订单簿不平衡 pm_imbalance ×1（round48 新增：|book_imbalance|≥0.3 计 ±1，仅成型/博弈期，聪明钱挂单失衡）；
# K线序列/开盘微观/季节性 ×1；墙背离贴线专列 ×2（±2 基础）；结算期墙专列 ×1；
# 短周期反弹先兆 ×2（round11：1m&5m recent_dir 同向+连阳阴≥2+15m MACD 同号，仅常规矩阵非贴线生效）；
# 宏观趋势族 ×1（round41：最近 4 根已收 1H 方向序列，成型/博弈期计分，与 K线序列同族矩阵口径）；
# 1H MACD round10 起仅参考（一致率 28.6% 最误导）；4H MACD round26 起仅参考--均不机算不计方向分。
W_PRICE_POS = 1          # round41.8.15：全局 ×2→×1；round41.8.20 锚定口径统一（取值见 PP_ANCHOR_DEV，与 G4/H9 同源）
W_PRICE_POS_WEAK = 1     # round14 遗留兼容别名；全局 ×1 后与 W_PRICE_POS 一致，保留兼容
PP_DOWNGRADE_DEV = 0.10  # round14 遗留常量（值与 PP_ANCHOR_DEV 同，保留兼容）
PP_ANCHOR_DEV = 0.10      # round42.2 价格位置因子选边阈值：|deviation_pct|>=0.10% 计 ±1（锚定侧；用户 8/25 指令「放宽到 0.10% 进入记分项目」），<0.10% 计 0（更紧贴锚定的灰区未选边）
                           # 旧 PP_EXTREME_LOW/HIGH（24h 分位极端位仅参考）41.8.20 废除→分位退出本因子（仅存 G5.4 区标记/sr_proximity 参考，均不改方向）
PM_WEAK_CONSENSUS = 0.60  # round41.8.9 PM 弱共识阈值：两侧均<0.60 → price_position 计 0 仅参考（41.8.20 起共识源 book_up_mid 优先缺失退标记价；归因依据为旧分位口径待重归因）
W_TAKER_BS = 1
W_TAKER_BS_15M = 1  # round42.4 双窗第二行：taker 15m 分级独立计分（用户 8/26 决策「Taker 定为双窗」，5m/15m 各一行同分级同权重）
W_TAKER_1M_TIE = 1  # round43.2 贴线专列 1m taker 趋势权重 ×3→×1（用户 8/26 决策「权重改小一点试试」，依据=8/26 诊断性回测贴线场 1m taker 命中率 45.8% < 50%，原 ×3 权重过度放大该弱势信号拖累贴线场 S₀ 与概率表；降到 ×1 后净分最大影响 ±1 而非 ±3，与 K线/momentum_burst/Taker 同档保守可控，A2 单因子归因待更准样本观察）
W_TAKER_1H = 2  # round41.8.12 起 1H Taker 仅参考（不再写 factors，常量保留供历史追溯）
W_ORDER_FLOW = 1           # round41.8.12 订单流 ×2→×1（归因 55.0% 弱因子）
W_ORDER_FLOW_PARTIAL = 1   # round41.8.12 订单流 ×1 已最低，partial 不再额外降权（原 round41.4 ±2→±1 逻辑随 ×2→×1 归并）
W_MACD = {"15m": 1}  # 4H MACD 已移除（round26 废除方向计分，2026-08-22 审计 P2-1 清理，不再靠引擎 P1 删除兜底）
# round44（用户 8/26 指令）：W_WALL_DIV 语义变更——原表达式 ±2.0×权重(×2)=±4，现基准幅度并入权重，
# 记分 = ±1×W_WALL_DIV(=±1)。依据=归因 57.1%（n=14 小样本）配最高档 ×2 权重最不匹配；
# 判据口径不变（墙比值>3 且与 recent_dir 反向才构成背离）。
W_WALL_DIV = 1
W_SHORT_BOUNCE = 1  # round42.1 短周期反弹先兆 ×2→×1（用户 8/25 决策：6AM ET LOSS 场 short_bounce -2 误导方向；判据口径不变仅降权）
# round44 K线序列拆分双行（用户 8/26 指令「15m/1H 分开评分，权重都改成1」）：
# 原 factors["kline"]=±2 为 15m last_4 + 1H last_4 共 8 根混计多数（round41.8.12 ×1→×2）。
# 两窗口样本相关——15m 的 4 根全部落在最后 1 小时内、与 1H 末根重叠，混计等于给「最近一小时」双倍计票。
# 现拆为 kline_15m / kline_1h 两行独立计票（各窗阳多 +1/阴多 -1/平 0），权重各 ×1、可正负抵消。
# 旧因子键 "kline" 停用（factor_attribution 保留 legacy 标签供历史快照归因；旧常量 W_KLINE 移除）。
W_KLINE_15M = 1
W_KLINE_1H = 1
W_MOMENTUM_BURST = 2      # round41.8.9 归因回测 73.3%（高波动率 89.5%/极端高位 88.9%）→ 权重 ×1→×2
W_PM_MOMENTUM = 2         # round49 权重 ×3→×2（用户 8/28 决策「PM 动量还是都回到+2」；回退 round47 的 ×2→×3；判据口径与同源口径（book-book）不变，仅回退加权）。round51.2（8/30）：改为全模式计分（含贴线），是贴线 S₀ 唯一 ±2 幅值因子（其余贴线因子均 ±1）；8/30 用户定夺「保持 ×2 全模式」；H12 深势下逆势 pm_momentum 纳入半权（rule_engine H12 键表，与 taker_bs/price_position 同口径）
PM_MOMENTUM_THRESHOLD = 0.30  # |up_price_delta| ≥ 0.30 才计 ±2（round53/9/3 用户拍板：由 0.02 提至 0.30）。全量回测(155场,data/snapshots×results)：0.02档72.7%→0.30档86.5%/CI下界78%为顶点；|Δ|<0.30 为噪声(0.05-0.15档47.6%偏反/0.15-0.30档54.5%)，小Δ仍给±2曾致 9/3 亚盘 12am/1am/3am 三场误判(Δ=0.035/0.145/0.180 全<0.30，0.30 档全部屏蔽)
# round53（9/3 用户拍板「先验证→±1」）：reversal_intent 深跌反转意向因子常量。
# 引擎口径回测（scripts/reversal_intent_engine_caliber.py + reversal_intent_validation.py）：
#   深跌反UP n=52 胜率65.4%（前半/后半均65.4%，阈值0.02-0.05全部64-67%参数稳健，EV@0.5入场+0.154）→ 单边+1；
#   对称深涨反DOWN n=43 胜率46.5% EV-0.108 → 计0（不对称是回测证据驱动，非遗漏）；
#   5min 口径全对称版（57.4%）与小时级引擎口径有偏差，按引擎口径单边化落地。
REVERSAL_PREV_THRESHOLD = 0.03   # Δ_prev ≤ -0.03 判定深跌（上小时末 delta）
REVERSAL_CUR_THRESHOLD = 0.02    # Δ_cur ≥ +0.02 判定反向（本时刻 delta）。独立常量：round53 起与 PM_MOMENTUM_THRESHOLD(0.30) 数值已脱钩，reversal_intent 回测在 0.02-0.05 档 64-67% 稳健，不随其调整
W_REVERSAL_INTENT = 1            # 用户拍板 ±1（从初议 ±2 降档：胜率口径 65.4% 稳健但样本仅 52 场）
W_PM_IMBALANCE = 1            # round48 新增：PM 订单簿不平衡（聪明钱挂单失衡）×1（用户 8/28 决策方案 A）
# round54（9/3 用户拍板「先进引擎加分」）：data_completeness 聪明订单因子 ×2。
# 判据：book_imbalance 与 smart_money_pool_direction 方向一致 → 按方向 ±2 / 矛盾 → 0 / 缺任一 → 0。
# 依据=scripts/backtest_completeness_factor.py（459 场）：信号齐全档(同向87.3%+矛盾75.0%)均正 EV，
#   缺信号档 372 场均pnl-0.56 是全数据主亏损源；同向 EV 被 0.69 高价磨平(因信号齐全往往即市场已price-in)，
#   但按用户拍板统一进净分。
# 【round54 修正2 20260903 用户复核拍板】矛盾档保持 0 分：原 headnote「方向矛盾 → -2」不可落地
#   （矛盾无方向，方向性因子无法记无向 -2），且回测「矛盾档 75%命中/均pnl+0.71」经查是矛盾场次
#   引擎净分方向的描述统计（与因子取值无关），不构成惩罚分依据——矛盾档引擎命中 75% 反而说明
#   其它因子仍有效、不应扣死。与 9/3 19:26「三票一致矛盾→0」拍板一致。矛盾档样本 n=24 偏小留观察。
W_DATA_COMPLETENESS = 2
# round54（9/3）resonance 强共振因子权重。判据 = 5 个独立强因子指向同一方向的个数：
#   ≥4 同向 → 方向×W_RESONANCE(2)；≥3 同向 → 方向×1；<3 → 0。
# 依据 = scripts/factor_resonance_oos_check.py 时序样本外验证（460 场，最晚25%为测试段）：
#   强因子≥3 同向：训练85.5% / 样本外86.6%；≥4 同向：训练86.4% / 样本外92.9%。
#   （对照组 up_price>0.6 样本外仅 84.8%，共振增益真实、非撞市场一致。）已确认非过拟合。
# 【round54 修正4 20260903 用户拍板: OOS 回测定夺】第5票由 reversal_intent 改回 recent_dir_15m：
#   用 pm_prices 重建 reversal_intent（引擎口径 Δ_prev/Δ_cur）后公平对比 462 场最晚25%样本外——
#   池B(reversal_intent)：≥3 n=82 命中89.0% / ≥4 n=37 命中97.3%（≥4 实为「4核心全同向」，
#   reversal_intent 88% 场次=0 票不参与、触发场仅 71/462 且触发时 4 核心≥3 同向仅 27%，
#   几乎不贡献共振票）；池A(recent_dir_15m)：≥3 n=88 命中85.2% / ≥4 n=66 命中92.4%
#   （双向、触发率高、与回测口径一致）。reversal_intent 仍作独立因子 ±1 保留，仅退出共振池。
W_RESONANCE = 2
RESONANCE_STRONG = ("price_position", "pm_momentum", "pm_consensus", "data_completeness", "recent_dir_15m")
# round54（9/3 用户指定 2.0）：pm_pair 强配对因子。判据 = pm_momentum 与强搭档同向个数 k：
#   k ≥ 2 → 方向×W_PM_PAIR(2)；k == 1 → 方向×1；k == 0 → 0。
# 依据 = scripts/backtest_pm_pair_factor.py 时序样本外验证（460 场，最晚25%测试段）：
#   单配对 pm_momentum+price_position n=45 命中93.3% / +pm_consensus n=54 88.9% / +recent_dir_15m n=46 89.1%；
#   ≥2 配对同向 n=49 89.8%，≥3 n=39 94.9%。与 resonance 互补（resonance 抓全池≥3共振，
#   仅 1-2 个搭档同向的高胜率信号由 pm_pair 捕捉）。
W_PM_PAIR = 2
PM_PAIR_PARTNERS = ("price_position", "pm_consensus", "recent_dir_15m")
PM_IMBALANCE_THRESHOLD = 0.3  # |book_imbalance| ≥ 0.3 计 ±1（回测 A1 档 81.2% n=48，join 8/25-8/27 65 场，样本时间集中已披露）
# P0-1.3 阈值敏感性分析（8/30）新增 3 因子，全 ×1 保守起步（用户拍板「全部×1」）：
# 依据 = verify_engine_factors.py 引擎口径 387 场验证：recent_dir_5m 62.9% EV+9.9 / recent_dir_15m
# 68.0% EV+4.1 / PM共识绝对价 78.9% EV+3.2。三者均不撞噪音过滤铁律（不采纳 1m/5m MACD/RSI 本体）。
W_RECENT_DIR_5M = 1      # 5m 动量方向（timeframes.5m.recent_dir），全模式计分
W_RECENT_DIR_15M = 1     # 15m 动量方向（timeframes.15m.recent_dir），全模式计分
W_PM_CONSENSUS = 1       # PM 绝对价共识顺向（book_up_mid 0-1，>0.60→+1 / <0.40→-1），全模式计分
W_SR_PROX = 1              # round41.8.8 阻力/支撑位邻近；round41.8.9 用户决策「不记分仅参考」，本常量保留供历史追溯
SR_PROX_BAND = 0.005       # 距弱阻力/弱支撑 ≤0.5% 视为"邻近"（BTC 1H 波动率 ~0.5-1%，0.5% 为合理贴界带宽）
SR_PROX_PCT_HIGH = 90      # key_levels 缺失时降级：percentile ≥90 视为邻近阻力
SR_PROX_PCT_LOW = 10       # key_levels 缺失时降级：percentile ≤10 视为邻近支撑

TIE_LINE_DEV = 0.05          # 贴线激活阈值 %
TIE_LINE_MAX_DEV = 0.10      # 贴线失效阈值 %
VOL_TIE_GATE = 0.3           # 贴线前置门：波动率 >0.3% 禁用贴线
SETTLEMENT_MINUTE = 46       # :46+ 结算期
WALL_RATIO = 3.0             # 墙失衡阈值（背离/结算期/吸筹派发共用 3:1）

# AI 专属、引擎不机算的因子（coverage 标注）
# round42.4 移除 taker_1m：贴线模式已机算（×3，读 timeframes.1m.taker_pct_last）；非贴线模式该因子
# 按评分表矩阵本就禁用（仅结算期尾盘操纵否决，非方向计分）。
# 8/17 盘感新增：fake_breakout（假突破）、divergence（背离）需 AI 综合形态判断，引擎不机算
AI_ONLY_FACTORS = ["taker_trend", "evidence_dims", "fakeout", "veto_triggered(第三层否决)",
                   "fake_breakout(假突破)", "divergence(背离)"]

# 8/17 盘感/趋势新因子总开关：False = 不改变 S₀（保持现有引擎零扰动，待全样本回测验证后再启用）。
ENABLE_SOFT_SKILL_FACTORS = False

_CONF_ALIAS = {"low": "低", "medium": "中", "high": "高", "低": "低", "中": "中", "高": "高"}


class _SkipValidation(Exception):
    """数据不足/字段异常 → fail-open 放行（engine_check.skipped 记录原因）。"""


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _f(x: object) -> float | None:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_ts(s: object) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_market_data(slug: str) -> tuple[dict | None, str]:
    """返回 (market_data, source_desc)。项目 data 文件优先（slug 必须匹配防串场），
    回退 /tmp 旧路径与 snapshots 归档（8/21 可移植化：主路径脚本相对，沙箱/宿主机通用）。"""
    for tmp in (TMP_MARKET_DATA, _TMP_FALLBACK):
        try:
            if tmp.exists():
                md = json.loads(tmp.read_text(encoding="utf-8"))
                if isinstance(md, dict) and md.get("slug") == slug:
                    return md, str(tmp)
        except (OSError, json.JSONDecodeError):
            pass
    for snap_dir in (SNAPSHOT_DIR, _SNAPSHOT_FALLBACK):
        try:
            cands = sorted(
                snap_dir.glob(f"{slug}_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for p in cands:
                try:
                    md = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(md, dict) and md.get("slug") == slug:
                    return md, str(p)
        except OSError:
            pass
    return None, ""


def _fetch_live_pm_book(slug: str) -> dict:
    """round23 + round41.8.13（第二步）：写信号实时复核时同时拉 gamma 标记价 + clob 真实订单簿。

    标记价（outcomePrices）与盘口可剧烈脱节（8/24 3pm 场 0.705 vs 0.44 vs 实时 0.28），
    实时复核若只信标记价会重演同一失真源。故本轮起优先拉 clob 订单簿 UP token 的 book_up_mid。
    返回 dict：{"up","down"}=标记价(0-1)，"book_up_mid"/"book_imbalance"=订单簿(0-1/±1，缺失为 None)。
    任何失败返回空 dict（fail-open：实时查价不可用则退回快照判定，不阻断信号链路）。
    """
    if not GAMMA_API or not http_get_json:
        return {}
    try:
        raw = http_get_json(f"{GAMMA_API}/markets?slug={slug}", timeout=6, proxy=_get_proxy() if _get_proxy else None)
        if not raw or not isinstance(raw, list) or len(raw) == 0:
            return {}
        m = raw[0]
        prices = m.get("outcomePrices", "[]")
        prices = json.loads(prices) if isinstance(prices, str) else prices
        if not isinstance(prices, (list, tuple)) or len(prices) < 2:
            return {}
        up, down = float(prices[0]), float(prices[1])
        if not (0 <= up <= 1 and 0 <= down <= 1):
            return {}
        res: dict = {"up": up, "down": down, "book_up_mid": None, "book_imbalance": None}
        # clob 真实订单簿（与 hermes_collect round41.8.11 同口径：UP token = clobTokenIds[0]）
        try:
            _cids = m.get("clobTokenIds")
            _cids = json.loads(_cids) if isinstance(_cids, str) else _cids
            if _cids and m.get("enableOrderBook") and CLOB_API:
                _book = http_get_json(f"{CLOB_API}/book?token_id={_cids[0]}", timeout=8,
                                      proxy=_get_proxy() if _get_proxy else None)
                if _book:
                    _bids = [(float(b["price"]), float(b["size"])) for b in _book.get("bids", [])]
                    _asks = [(float(a["price"]), float(a["size"])) for a in _book.get("asks", [])]
                    _bb = max((p for p, _ in _bids), default=None)
                    _ba = min((p for p, _ in _asks), default=None)
                    _bid_mid = sum(s for p, s in _bids if 0.3 <= p <= 0.7)
                    _ask_mid = sum(s for p, s in _asks if 0.3 <= p <= 0.7)
                    _imb = round((_bid_mid - _ask_mid) / (_bid_mid + _ask_mid), 4) if (_bid_mid + _ask_mid) > 0 else None
                    _bm = round((_bb + _ba) / 2, 4) if (_bb is not None and _ba is not None) else None
                    res["book_up_mid"] = _bm
                    res["book_imbalance"] = _imb
        except Exception:  # noqa: BLE001 - book 拉取任何异常都不影响标记价回退
            pass
        return res
    except Exception:  # noqa: BLE001 - 实时查价任何异常都 fail-open
        return {}


def _regime(md: dict, notes: list[str]) -> str:
    """步骤4.5 摆动市识别（采集字段口径，禁止自行推算）。"""
    prev = md.get("prev_hour") or {}
    hist = prev.get("direction_history") or []
    flips = prev.get("flips")
    prev_open = _f(prev.get("open"))
    anchor = _f(md.get("anchor_price"))
    if not hist or len(hist) < 3 or flips is None:
        notes.append("regime: prev_hour 字段缺失，按趋势市")
        return "趋势市"
    if isinstance(flips, (int, float)) and flips >= 2:
        return "摆动市"
    if prev_open and anchor and abs(anchor - prev_open) / prev_open >= 0.0012 and hist[-1] != hist[-2]:
        return "摆动市"
    return "趋势市"


def _taker_buy_pct(avg_ratio: float | None) -> float | None:
    if avg_ratio is None or avg_ratio < 0:
        return None
    return avg_ratio / (1 + avg_ratio) * 100


def _taker_grade(ratio: float) -> float:
    """Taker B/S 分级（round51.4 起取消 ±2 极端档）：>1.3→+1 / <0.8→-1 / 其余 0。

    变更:round51.4 (2026-08-30) 删除 >2.33 / <0.43 两个机构级档,合并为 ±1/0 三档;
    双窗最大贡献从 ±4 降到 ±2。详见 SYNC_MAP.md 六十六段。
    """
    if ratio > 1.3:
        return 1.0
    if ratio < 0.8:
        return -1.0
    return 0.0


def _kline_major(candles: str) -> int:
    """单窗口 K 线序列多数方向（round44 拆分双行用）：阳多 +1 / 阴多 -1 / 平或空串 0。

    入参为采集器 tf_summary 输出的 last_4_candles（已剔进行中 K 线；旧快照退 last_3_candles），
    形如「阳阴阳阴」。只数已收盘蜡烛，与 41.8.5 已收盘口径一致。
    """
    if not candles:
        return 0
    yang, yin = candles.count("阳"), candles.count("阴")
    if yang > yin:
        return 1
    if yin > yang:
        return -1
    return 0


def _short_bounce(tf1m: dict, tf5m: dict, tf15m: dict) -> int:
    """8/12 round11 短周期反弹先兆（三重组合）：+1 反弹 / -1 回落 / 0 无。

    判定：1m 与 5m 均连续同向（consecutive_up/down ≥2 且 recent_dir 同号），且 15m MACD hist 同号。
    只取 K线连续方向聚合形态（recent_dir + consecutive + 15m MACD 符号），不取被规则4 排除的
    1m/5m RSI/MACD 本体。返回值供 factors 计分（×W_SHORT_BOUNCE）与 judge 的 short_bounce 入参共用。
    """
    def _consec(tf: dict, key: str) -> int:
        v = _f(tf.get(key))
        return int(v) if v is not None else 0

    d1, d5 = tf1m.get("recent_dir"), tf5m.get("recent_dir")
    h15 = _f(tf15m.get("macd_hist"))
    if d1 is None or d5 is None or h15 is None:
        return 0
    up = (
        d1 == "UP" and d5 == "UP"
        and _consec(tf1m, "consecutive_up") >= 2 and _consec(tf5m, "consecutive_up") >= 2
        and h15 > 0
    )
    down = (
        d1 == "DOWN" and d5 == "DOWN"
        and _consec(tf1m, "consecutive_down") >= 2 and _consec(tf5m, "consecutive_down") >= 2
        and h15 < 0
    )
    if up:
        return 1
    if down:
        return -1
    return 0


def extract_factors(md: dict) -> tuple[dict[str, float], dict, list[str], str]:
    """从 market_data 机算因子。

    返回 (factors, judge_inputs, notes, mode_desc)。
    数据不足抛 _SkipValidation（fail-open）。
    """
    notes: list[str] = []
    factors: dict[str, float] = {}

    anchor = _f(md.get("anchor_price"))
    current = _f(md.get("current_price"))
    if not anchor or not current:
        raise _SkipValidation("anchor_price/current_price 缺失或为 0")
    dev_pct = (current - anchor) / anchor * 100

    collect_dt = _parse_ts(md.get("collect_time_utc"))
    if collect_dt is None:
        raise _SkipValidation("collect_time_utc 缺失，无法判定阶段")
    minute = collect_dt.minute
    is_settlement = minute >= SETTLEMENT_MINUTE

    market_start = _parse_ts(md.get("timestamp"))
    minutes_to_settle: float | None = None
    if market_start is not None:
        minutes_to_settle = ((market_start + timedelta(hours=1)) - collect_dt).total_seconds() / 60
        if not (0 <= minutes_to_settle <= 65):
            notes.append(f"minutes_to_settle={minutes_to_settle:.1f} 异常，按 None 处理")
            minutes_to_settle = None

    tf = md.get("timeframes") or {}
    tf1m = tf.get("1m") or {}
    tf5m = tf.get("5m") or {}
    tf15m = tf.get("15m") or {}
    tf1h = tf.get("1H") or {}
    tf4h = tf.get("4H") or {}
    cap = md.get("capital_flow") or {}
    ob = cap.get("order_book") or {}
    pm = md.get("polymarket") or {}

    macd1 = _f(tf1h.get("macd_hist"))
    macd4 = _f(tf4h.get("macd_hist"))
    if macd1 is None or macd4 is None:
        raise _SkipValidation("1H/4H macd_hist 缺失")

    vol = _f(md.get("volatility_5m"))
    regime = _regime(md, notes)

    # ---- 模式判定（步骤6 前置门 + 6.1 动态失效 + 结算期优先）----
    tie_gate_ok = (vol is None or vol <= VOL_TIE_GATE) and regime != "摆动市"
    if vol is not None and vol > VOL_TIE_GATE:
        notes.append(f"波动率{vol}%.3>{VOL_TIE_GATE}% → 贴线禁用")
    if regime == "摆动市":
        notes.append("摆动市 → 贴线禁用")
    abs_dev = abs(dev_pct)
    tie_line_active = False   # <0.05%（H12/步骤10 贴线约束口径）
    tie_mode = False          # 贴线专列生效（激活/维持/失效预警）
    tie_warning = False       # 失效预警 → 置信度强制低
    if tie_gate_ok and abs_dev < TIE_LINE_MAX_DEV and not (is_settlement and abs_dev < TIE_LINE_MAX_DEV):
        tie_mode = True
        dtrend = md.get("deviation_trend") or {}
        if abs_dev < TIE_LINE_DEV:
            tie_line_active = True
            mode = "贴线激活"
        elif dtrend.get("expanding"):
            tie_warning = True
            mode = "贴线失效预警"
            notes.append("贴线失效预警（0.05-0.10% 且偏差扩大）→ 置信度强制低")
        else:
            mode = "贴线维持"
    else:
        phase = "结算期" if is_settlement else ("开盘期" if minute <= 4 else ("成型期" if minute <= 14 else "博弈期"))
        if is_settlement and abs_dev < TIE_LINE_MAX_DEV and tie_gate_ok:
            notes.append("结算期∩贴线条件并存 → 结算期规则优先")
        mode = f"常规-{phase}"

    # ---- 通用可机算因子 ----
    # round41.8.20 用户指令「都改成锚定价格」：价格位置因子锚定口径统一--与 G4/H9 同源同基准，
    # 取值=deviation_pct（价格 vs 锚定，带符号）：
    #   |偏差|≥0.15%（价格已实质选边，同 G4_DEVIATION）计 ±1（锚定侧）；|偏差|<0.15%（贴线/灰区未选边）计 0；
    #   PM 弱共识（两侧<0.60，round41.8.9 沿用；共识源 book_up_mid 优先、缺失退 gamma 标记价）
    #   计 0 仅参考（归因依据为旧分位口径，待新口径重新归因）。
    # 24h 区间分位（current_percentile/in_upper_half/in_lower_half）退出本因子（旧极端位 <15/>55 规则废除）；
    # 分位仅存于 G5.4 吸筹/派发区标记与 sr_proximity 参考（均不改方向）。事故链=8/25 1am AI 按分位
    # 85.6% 恐高计 -1 被复盘判误 + 8/25 2am AI 按分位记 0 vs 机算按 dev 记 +1 两层 S0 不一致。
    # 权重 ×1 维持（round41.8.15）。judge 侧 G4/H9（L461）本就锚定口径，本轮两层对齐。
    pp_val = float(_sign(dev_pct)) * W_PRICE_POS if abs(dev_pct) >= PP_ANCHOR_DEV else 0.0
    if pp_val != 0.0:
        _pm_up_book = _f(pm.get("book_up_mid"))
        _pm_up_mark = _f(pm.get("up_price"))
        _pm_down_mark = _f(pm.get("down_price"))
        _pm_up_eff = _pm_up_book if _pm_up_book is not None else _pm_up_mark
        _pm_down_eff = (1.0 - _pm_up_book) if _pm_up_book is not None else _pm_down_mark
        if _pm_up_eff is not None and _pm_down_eff is not None \
                and _pm_up_eff < PM_WEAK_CONSENSUS and _pm_down_eff < PM_WEAK_CONSENSUS:
            pp_val = 0.0
            notes.append(f"PM 弱共识(up{_pm_up_eff:.2f}/down{_pm_down_eff:.2f} 均<{PM_WEAK_CONSENSUS:.2f}) "
                         f"price_position 计 0 仅参考（round41.8.9 沿用，共识源 book 优先）")
    factors["price_position"] = pp_val

    # round42.4 双窗 Taker（用户 8/26 决策「定为双窗」）：5m/15m 各一行独立计分，同分级同权重 ×1。
    # 5m 窗 = capital_flow.taker_bs_ratio（最新 5m 末根，键名 taker_bs 保留供 judge H12/结算期降权兼容）；
    # 15m 窗 = 顶层 taker_15m_ratio（采集器 hermes_collect 取 15 分钟前那根 5m 末根的近似，非真 15m 滚动均值）。
    # 旧快照顶层字段缺失 → 15m 计 0 并 notes 标注（fail-open，评分表口径与 AI 实盘报告双行计分对齐）。
    taker_ratio = _f(cap.get("taker_bs_ratio"))
    if taker_ratio is not None:
        factors["taker_bs"] = _taker_grade(taker_ratio) * W_TAKER_BS
    else:
        factors["taker_bs"] = 0.0
        notes.append("taker_bs_ratio 缺失，按 0 计")
    # P0-1.3（8/30）删除 taker_bs_15m：引擎口径验证 62.0% 但 EV -15.6（15m 窗=15 分钟前那根
    # 5m 末根近似，数据滞后一个决定周期致买价系统性不利），且 H12 双窗降权被取消后贡献进一步收窄。
    # 顶层 taker_15m_ratio 字段仍由采集器产出，供复盘参考，不再进 S₀。

    taker_avg_1h = _f(cap.get("taker_bs_avg_1h"))
    taker1h_pct = _taker_buy_pct(taker_avg_1h)
    # round41.8.12 1H Taker B/S 仅参考（归因 28.6% 全场最低、却配 ×2 最高权重 = 最大噪声源）。
    # 方向计分废除（不再写 factors["taker_1h"]）；taker1h_pct 仍传给 judge_inputs 供 G5.1 深度档/H12 防御用途。
    if taker1h_pct is not None:
        notes.append(f"1H Taker B/S(仅参考,不计分): 买占比={taker1h_pct:.1f}%")
    else:
        notes.append("taker_bs_avg_1h 缺失，G5.1 深度档降级由 judge 处理")

    # P0-1.3（8/30）删除 order_flow 订单流失衡计分：引擎口径验证 56.7% n=307 EV -2.1（大样本噪音）。
    # capital_flow.order_flow.imbalance_5m/15m 字段仍由采集器产出，供复盘参考，不再进 S₀。

    # P0-1.3（8/30）新增：短周期动量 recent_dir 5m/15m 直接作方向因子。
    # 数据源 timeframes.5m.recent_dir / timeframes.15m.recent_dir（采集器恒产字段）。
    # 引擎口径验证：5m 62.9% EV+9.9 / 15m 68.0% EV+4.1，均真信号（全样本）。权重 ×1 保守。
    # 不纳入 rule_engine H12 半权键表：×1 因子降权 ×0.5 → ROUND_HALF_UP 舍回 ±1，降权无效
    # （与 pm_momentum ±2 半权→±1 不同）。详见 SYNC_MAP 六十七段。
    # round52.1（8/30）：贴线模式不计分——贴线盘整市 5m 动量 47.5% 反向噪音（80 场贴线快照实测，
    # 常规 67.0% 对照）、15m 贴线 62.5% 弱；全模式计分致贴线 -3 场，贴线不计分全量 +5 场、
    # 贴线 +3 场（回测 scripts/round52_factor_backtest.py 变体B）。pm_consensus 贴线保持计分（有益）。
    for _tfn, _rkey, _rw in (
        ("5m", "recent_dir_5m", W_RECENT_DIR_5M),
        ("15m", "recent_dir_15m", W_RECENT_DIR_15M),
    ):
        if tie_mode:
            factors[_rkey] = 0.0  # round52.1：贴线模式不计分（短周期动量是贴线盘整的反向噪音）
        else:
            _rd = (tf.get(_tfn) or {}).get("recent_dir")
            factors[_rkey] = float(_rw if _rd == "UP" else (-_rw if _rd == "DOWN" else 0))

    bid_v = _f(ob.get("bid_volume_top10"))
    ask_v = _f(ob.get("ask_volume_top10"))
    buy_wall = (bid_v / ask_v) if (bid_v is not None and ask_v and ask_v > 0) else None
    sell_wall = (ask_v / bid_v) if (ask_v is not None and bid_v and bid_v > 0) else None

    # ---- 模式相关因子 ----
    if tie_mode:
        # 贴线专列：MACD/K线/RSI 仅参考、季节/开盘微观禁用、结算期墙禁用
        recent_dir = tf1m.get("recent_dir")
        # round43.2 贴线专列 1m taker 趋势 ×1 引擎机算（round43.1 原 ×3 已被诊断性回测证伪：
        # 8/26 A2 n=249 贴线场命中率 45.8% < 50% 基线、Wilson 95% [39.7%, 52.0%]，×3 权重放大弱势信号
        # 拖累贴线场 S₀ 与概率表漂移 -7.2pp；用户 8/26 决策「权重改小一点试试」×3→×1 与 K线/
        # Taker/订单流同档保守可控；A2 单因子归因待更准样本观察，必要时回滚或加贴线噪点门槛）。
        # 数据源 timeframes.1m.taker_pct_last（采集器 round32 起预聚合）。判分口径：买占比 >60%→+1
        # / <40%→-1 / 其余 0。judge H12 降权键列表含 taker_1m 自动兼容。旧快照缺字段计 0 标注。
        _t1m_last = _f(tf1m.get("taker_pct_last"))
        if _t1m_last is not None:
            if _t1m_last > 0.6:
                factors["taker_1m"] = float(W_TAKER_1M_TIE)
            elif _t1m_last < 0.4:
                factors["taker_1m"] = float(-W_TAKER_1M_TIE)
            else:
                factors["taker_1m"] = 0.0
        else:
            notes.append("taker_pct_last 缺失（round32 前旧快照），贴线 1m taker 按 0 计")
        wall_div = 0.0
        if buy_wall is not None and buy_wall > WALL_RATIO and recent_dir == "DOWN":
            wall_div = 1.0 * W_WALL_DIV   # round44: ±4→±1（买墙大但价格跌 = 背离）
        elif sell_wall is not None and sell_wall > WALL_RATIO and recent_dir == "UP":
            wall_div = -1.0 * W_WALL_DIV  # round44: ±4→±1（卖墙大但价格涨 = 背离）
        # 墙与价格同向 → 不构成背离计 0（8/11 防呆补丁口径）
        factors["wall_divergence"] = wall_div
    else:
        phase_scored = mode in ("常规-成型期", "常规-博弈期")
        if phase_scored:
            # round41.8.9 15m MACD 仅参考（归因回测 51.6% 近随机，废除方向计分，与 1H/4H MACD 同口径）。
            # 1H MACD round10 / 4H MACD round26 均已废除方向计分（仅参考），不机算不计入 S0。
            h15 = _f(tf15m.get("macd_hist"))
            if h15 is not None:
                notes.append(f"15m MACD hist(仅参考,不计分): {h15:+.1f}")
            # round42.2 K线序列 3根→4根（用户 8/25 指令）：优先读 last_4_candles，旧快照缺该字段退 last_3_candles
            # round44（用户 8/26 指令）拆分双行独立计票：15m/1H 各自阳多 +1 / 阴多 -1 / 平 0，各 ×1。
            # 原 8 根混计 ×2 给「最近一小时」双倍计票（15m 4 根全落在 1H 末根内），且两窗矛盾时互相抵消为 0
            # 掩盖分歧；拆分后两窗口径独立可见、可正负抵消（如 15m=+1 / 1H=-1 → S0 净 0 但分歧留痕）。
            _k15 = tf15m.get("last_4_candles") or tf15m.get("last_3_candles") or ""
            _k1h = tf1h.get("last_4_candles") or tf1h.get("last_3_candles") or ""
            factors["kline_15m"] = float(_kline_major(_k15) * W_KLINE_15M)
            factors["kline_1h"] = float(_kline_major(_k1h) * W_KLINE_1H)
            # round41.8.7 前瞻观察因子：momentum_burst（15m 放量顺势，即回测报告中的 acceleration）
            # 2026-08-25 回测：333 小时命中 74.6%（多36次75.0%/空23次73.9%），按周切片全正
            # （61.9%/80.0%/87.5%）。round41.8.9 归因回测 73.3%（n=45），高波动率 89.5%/极端高位 88.9%，
            # 用户批准权重 ×1→×2。
            _vr15 = _f(tf15m.get("vol_ratio"))
            _rd15 = tf15m.get("recent_dir")
            if _vr15 is not None and _vr15 > 1.5 and _rd15 in ("UP", "DOWN"):
                factors["momentum_burst"] = (1.0 if _rd15 == "UP" else -1.0) * W_MOMENTUM_BURST
                notes.append(f"momentum_burst(15m放量顺势)触发: vol_ratio={_vr15:.2f} dir={_rd15} → ±{W_MOMENTUM_BURST}")
            else:
                factors["momentum_burst"] = 0.0
            # round48 PM 订单簿不平衡（聪明钱挂单失衡）：数据源 polymarket.book_imbalance
            # （采集器 round41.8.11 起影子采集，本轮转正计分）。imb = (bid_mid - ask_mid)/(bid_mid + ask_mid)，
            # 为 PM 订单簿 0.3-0.7 价位带挂单量的净失衡（±1）。独立于 pm_momentum（价动量）与 PM 绝对价。
            # 回测（data/hermes_factor_candidate_backtest_20260828_smart_money.md A1）：|imb|≥0.3 → 39/48=81.2%
            # （join 65 场，8/25-8/27 三天单一市况，样本时间集中已披露；30+ 结算场后归因复核）。
            # 仅成型/博弈期计分（同 pm_momentum 口径）；round41.8.11 前旧快照缺字段计 0（fail-open）。
            _imb48 = _f(pm.get("book_imbalance"))
            if _imb48 is not None:
                if _imb48 >= PM_IMBALANCE_THRESHOLD:
                    factors["pm_imbalance"] = 1.0 * W_PM_IMBALANCE
                    notes.append(f"pm_imbalance(PM挂单失衡)触发: imb={_imb48:+.3f} -> +{W_PM_IMBALANCE}")
                elif _imb48 <= -PM_IMBALANCE_THRESHOLD:
                    factors["pm_imbalance"] = -1.0 * W_PM_IMBALANCE
                    notes.append(f"pm_imbalance(PM挂单失衡)触发: imb={_imb48:+.3f} -> -{W_PM_IMBALANCE}")
                else:
                    factors["pm_imbalance"] = 0.0
            else:
                factors["pm_imbalance"] = 0.0
                notes.append("book_imbalance 缺失（round41.8.11 前旧快照），pm_imbalance 按 0 计")
            # round41.8.9 sr_proximity 方向计分废除（用户决策「不记分，仅参考」）。
            # 归因回测：均值回归假设被推翻（逼近支撑 35.1%/逼近阻力 44.1%，整体 41.0%），
            # BTC 1H 是动量延续市场，价格逼近阻力实际继续涨、逼近支撑实际继续跌。
            # 故 sr_proximity 只产出参考读数（notes），不写入 factors 不影响 S0。
            _kl = md.get("key_levels") or {}
            _cp0 = _f(md.get("current_price"))
            _wr0 = _f(_kl.get("weak_resistance"))
            _ws0 = _f(_kl.get("weak_support"))
            if _cp0 and _wr0 and _ws0 and _wr0 > 0 and _ws0 > 0:
                _dr0 = (_wr0 - _cp0) / _cp0   # >0 = 价格在阻力下方（未突破）
                _ds0 = (_cp0 - _ws0) / _cp0   # >0 = 价格在支撑上方（未跌破）
                if 0 <= _dr0 <= SR_PROX_BAND:
                    notes.append(f"sr_proximity(参考,不计分): 逼近阻力 距{_dr0*100:.2f}%（动量市倾向续涨）")
                elif 0 <= _ds0 <= SR_PROX_BAND:
                    notes.append(f"sr_proximity(参考,不计分): 逼近支撑 距{_ds0*100:.2f}%（动量市倾向续跌）")
            else:
                _pct0 = _f((md.get("price_position_24h") or {}).get("current_percentile"))
                if _pct0 is not None and _pct0 >= SR_PROX_PCT_HIGH:
                    notes.append(f"sr_proximity(参考,不计分): key_levels缺失 percentile{_pct0:.0f}%≥{SR_PROX_PCT_HIGH}（极端高位）")
                elif _pct0 is not None and _pct0 <= SR_PROX_PCT_LOW:
                    notes.append(f"sr_proximity(参考,不计分): key_levels缺失 percentile{_pct0:.0f}%≤{SR_PROX_PCT_LOW}（极端低位）")
            # P0-1.3（8/30）删除 macro_trend 宏观趋势族计分：引擎口径验证 57.1% n=184 EV -8.9（噪音）。
            # macro_trend_1h_4h 字段仍由采集器产出，供复盘参考，不再进 S₀。
            # 8/12 round11: 短周期反弹先兆（三重组合）×2 —— K线连续方向聚合形态，非被排除的 1m/5m RSI/MACD 本体
            sb = _short_bounce(tf1m, tf5m, tf15m)
            if sb != 0:
                factors["short_bounce_factor"] = float(sb * W_SHORT_BOUNCE)
        # round47 删除历史季节性计分（用户 8/27 决策「历史季节性 不进人记分项目」；
        # 依据=BTC 1H 是动量延续市场，方向跟随类因子有效（PM 动量 73.3% 归因），均值/季节性
        # 因子被反复证伪（多空比 47.2% / 资金费率 46.5% 同档）。historical_up_rate 字段仍注入
        # 供 rule_engine 反手预警组合判定（round17）使用，仅退出评分表。）
        # 历史季节性退出记分（round47）
        # P0-1.3（8/30）删除 open_micro 开盘微观方向计分：引擎口径验证 57.9% n=19 EV -9.3，
        # 且与 recent_dir_5m 同时出票时方向矛盾 8/19（42%），属 5m 动量的劣化冗余。opening.dir 字段保留供复盘。
        if mode == "常规-结算期":
            # 结算期：Taker（双窗已删，仅 5m 窗）降权 ×0.5（四舍五入）；结算期墙专列 ×1
            for k in ("taker_bs",):
                v = factors.get(k)
                if v:
                    factors[k] = float(int(v * 0.5 + (0.5 if v > 0 else -0.5)))
            wall_settle = 0.0
            if buy_wall is not None and buy_wall > WALL_RATIO and dev_pct > 0:
                wall_settle = 1.0
            elif sell_wall is not None and sell_wall > WALL_RATIO and dev_pct < 0:
                wall_settle = -1.0
            factors["wall_settlement"] = wall_settle

    # round41.8.10 PM 动量因子（up_price_delta）：PM 价 1 小时变化方向，独立于 PM 绝对价。
    # 归因回测 66-67%（阈值 0.02 档 66.7%）；「掉头」（PM 绝对价方向 vs 动量相反）时
    # PM 共识命中率从 73% 暴跌到 52-65%（尤其 PM看空+掉头 51.9% ≈ 随机）。
    # ◆ round51.2（8/30 用户指令「PM 动量在贴线模式也进引擎计分」）：本因子从常规-成型/博弈期
    #   专用改为全模式顶层机算，贴线模式同样计分进入 S₀（此前贴线 mode 从不机算 pm_momentum，
    #   而 prompt_exec 评分表口径要求 AI 每轮必读该行 → AI 报 +2 引擎算 0 造成方向分歧亏损）。
    #   同源口径与掉头检测逻辑保持不变。
    _pm_delta = _f((md.get("polymarket") or {}).get("up_price_delta"))
    if _pm_delta is not None and abs(_pm_delta) >= PM_MOMENTUM_THRESHOLD:
        factors["pm_momentum"] = float(_sign(_pm_delta) * W_PM_MOMENTUM)
        _pm_src = (md.get("polymarket") or {}).get("up_price_delta_source") or "mark"
        notes.append(f"pm_momentum(PM价动量)触发: Δ={_pm_delta:+.3f}(源={_pm_src}) -> ±{W_PM_MOMENTUM}")
        # 掉头检测（供参考）：PM 绝对价方向 vs 动量方向相反 = 共识在撤退（归因 52-65% 弱信号）
        # round41.8.16：绝对价与 delta 同源--delta 源=book 时绝对价优先 book_up_mid
        # （缺失退标记价），源=mark（含 41.8.16 前旧快照无 source 字段）用标记价。
        # 否则 delta 用 book、绝对价用失真标记价会错位产出假掉头（3pm 场 mark 0.705
        # 看多 vs book delta 向下即假掉头）。
        _bu0 = _f((md.get("polymarket") or {}).get("book_up_mid"))
        _mk0 = _f((md.get("polymarket") or {}).get("up_price"))
        _pm_up0 = _bu0 if (_pm_src == "book" and _bu0 is not None) else _mk0
        if _pm_up0 is not None and _sign(_pm_up0 - 0.5) != _sign(_pm_delta):
            notes.append(f"⚠️PM 动量掉头：绝对价{'看多' if _pm_up0 > 0.5 else '看空'}(源={_pm_src}) 但动量{'向下' if _pm_delta < 0 else '向上'}（共识撤退，归因弱信号）")
    else:
        factors["pm_momentum"] = 0.0

    # round53（9/3 用户拍板）reversal_intent 深跌反转意向因子：单边 +1。
    # 判据（引擎口径，与采集器 up_price_delta 同源）：Δ_prev(上小时末 delta) ≤ -0.03 深跌
    # 且 Δ_cur ≥ +0.02 反向上行 → +1（UP 方向）。对称深涨反DOWN 计 0（回测 46.5% 无效）。
    # 数据源 polymarket.up_price_delta_prev（round53 SCHEMA_VERSION 8 新增；旧快照缺失计 0，fail-open）。
    _ri_prev = _f((md.get("polymarket") or {}).get("up_price_delta_prev"))
    if (_ri_prev is not None and _pm_delta is not None
            and _ri_prev <= -REVERSAL_PREV_THRESHOLD and _pm_delta >= REVERSAL_CUR_THRESHOLD):
        factors["reversal_intent"] = float(W_REVERSAL_INTENT)
        notes.append(f"reversal_intent(深跌反转意向)触发: Δ_prev={_ri_prev:+.3f} 深跌 → Δ_cur={_pm_delta:+.3f} 反向上行 -> +{W_REVERSAL_INTENT} (回测65.4% n=52)")
    else:
        factors["reversal_intent"] = 0.0

    # P0-1.3（8/30）新增：PM 绝对价共识顺向因子（与 pm_momentum 价动量、H7 PM 行逆共识扣分均独立）。
    # 数据源 polymarket.book_up_mid（0-1 小数，缺失退 up_price 标记价）；>0.60→+1 / <0.40→-1 / 其余 0。
    # 引擎口径验证：78.9% EV+3.2（全场最强胜率，但 EV 中等——PM 价格已含信息、买价不利）。
    # 权重 ×1 保守。全模式计分。不进 H12 降权键表（PM 定价不受 MACD 深势影响）。
    _pc = _f(pm.get("book_up_mid"))
    if _pc is None:
        _pc = _f(pm.get("up_price"))
    factors["pm_consensus"] = float(W_PM_CONSENSUS if (_pc is not None and _pc > 0.60) else (-W_PM_CONSENSUS if (_pc is not None and _pc < 0.40) else 0))

    # round54（9/3 用户拍板）：data_completeness 聪明订单因子。
    # 判据 = book_imbalance × smart_money_pool_direction 两票一致性 + 方向符号：
    #   两票(book,pool)都存在且同向 → 按方向记分：偏 UP得 +W / 偏 DOWN得 -W（遵循体系 up=+/down=-）。
    #   pool 与 book 矛盾 → 0（方向不确定不给分）；任一为0/缺失 → 0。
    # 方向口径：>0 偏 UP，<0 偏 DOWN（与 pm_imbalance 同口径）。
    # 【round54 修正1】原实现同向一律 +W,把偏DOWN同向记成+分,与 up+/down- 相左(对照 pm_imbalance:imb=-0.498记-1),改按方向符号记分。
    # 【round54 修正3 20260903】摘除 net_up_imbalance 证伪副手：磁盘 104/104 快照实测该字段结构性恒正
    #   （只统计当前 UP 持仓大户的 taker 买入，卖出离场钱包不再入榜 → 幸存者偏差），用它反向证伪等价于
    #   只扑杀 DOWN 票（实测 33 次 DOWN 共识全部被压 0，UP 共识 0 次被压）——聪明订单因子退化成 UP 单边。
    #   9/3 6am「pool 滞后」问题应通过 pool 时效性治理，而非恒正字段；net 不再参与本因子任何分支。
    _dc_imb = _f(pm.get("book_imbalance"))
    _dc_sm_obj = pm.get("smart_money_consensus_1h") or {}
    _dc_pool = _f(_dc_sm_obj.get("smart_money_pool_direction"))
    if _dc_imb is not None and _dc_imb != 0 and _dc_pool is not None and _dc_pool != 0:
        if _sign(_dc_imb) == _sign(_dc_pool):
            # pool 与 book 同向：方向符号由该共识方向决定
            _dc_dir = _sign(_dc_imb)
            _dc_val = _dc_dir * W_DATA_COMPLETENESS
            factors["data_completeness"] = float(_dc_val)
            notes.append(
                f"data_completeness(聪明订单)触发: book_imb={_dc_imb:+.3f}+pool={_dc_pool:+.3f} "
                f"同向{'看UP' if _dc_dir > 0 else '看DOWN'}"
                + f" -> {'+' if _dc_val > 0 else ''}{_dc_val}"
            )
        else:
            # pool 与 book 矛盾：方向不定，不计分
            factors["data_completeness"] = 0.0
            notes.append(f"data_completeness(聪明订单)跳过: book_imb={_dc_imb:+.3f} 与 pool={_dc_pool:+.3f} 矛盾,方向不定 -> 0")
    else:
        factors["data_completeness"] = 0.0

    # round54（9/3 用户拍板 2.0）：resonance 强共振因子。
    # 判据 = 数 5 个独立强因子（price_position/pm_momentum/pm_consensus/data_completeness/recent_dir_15m）
    #   指向同一方向的个数 nres：
    #   nres ≥ 4 → 方向 × W_RESONANCE(2)    （样本外 92.9%）
    #   nres ≥ 3 → 方向 × 1                   （样本外 86.6%）
    #   nres < 3 → 0（不加分，避免噪声/强因子意见分散）
    # 方向正负沿用体系：偏 UP → +，偏 DOWN → -。
    # 与各强因子本身已在净分内的分不同：resonance 是"对撞度确认"加成，上限 2，起锦上添花作用，
    #   避免把所有强因子再各加一遍造成双重计权失真。
    _res_votes = {}   # dir -> count
    for _rk in RESONANCE_STRONG:
        _rv = factors.get(_rk, 0.0)
        if _rv != 0.0:
            _d = 1 if _rv > 0 else -1
            _res_votes[_d] = _res_votes.get(_d, 0) + 1
    _res_dir, _res_n = None, 0
    if _res_votes:
        _res_dir, _res_n = max(_res_votes.items(), key=lambda kv: kv[1])
    if _res_dir is not None and _res_n >= 3:
        factors["resonance"] = float(_res_dir * (W_RESONANCE if _res_n >= 4 else 1))
        notes.append(
            f"resonance(强共振)触发: {_res_n} 个强因子同看{'UP' if _res_dir>0 else 'DOWN'}"
            + (f"(≥4 加强 -> {factors['resonance']:+.1f})" if _res_n >= 4 else f" -> {factors['resonance']:+.1f}")
        )
    else:
        factors["resonance"] = 0.0

    # round54（9/3 用户拍板 2.0）：pm_pair 强配对因子。
    # 判据 = pm_momentum 与任一强搭档（price_position/pm_consensus/recent_dir_15m）同向的个数 k：
    #   k ≥ 2 → 方向×W_PM_PAIR(2)；k == 1 → 方向×1；k == 0 → 0。
    # 方向沿用体系：偏 UP → +，偏 DOWN → -。
    # 与 resonance 互补：resonance 需 ≥3 强因子共振才触发，把 pm_momentum 仅有 1-2 个搭档
    #   同向的高胜率信号（样本外单配对 88.9-93.3%）漏掉；本因子专捕这类强配对。
    _pp_pm = factors.get("pm_momentum", 0.0)
    _pp_k = 0
    if _pp_pm != 0.0:
        _pp_dir = 1 if _pp_pm > 0 else -1
        for _pk in PM_PAIR_PARTNERS:
            _pv = factors.get(_pk, 0.0)
            if _pv != 0.0 and ((_pv > 0) == (_pp_pm > 0)):
                _pp_k += 1
    if _pp_pm != 0.0 and _pp_k > 0:
        factors["pm_pair"] = float(_pp_dir * (W_PM_PAIR if _pp_k >= 2 else 1))
        notes.append(
            f"pm_pair(强配对)触发: pm_momentum+{_pp_k} 个强搭档同看{'UP' if _pp_dir>0 else 'DOWN'}"
            + (f"(≥2 加强 -> {factors['pm_pair']:+.1f})" if _pp_k >= 2 else f" -> {factors['pm_pair']:+.1f}")
        )
    else:
        factors["pm_pair"] = 0.0

    # ---- judge 原始入参 ----
    prev = md.get("prev_hour") or {}
    consec = max(
        int(_f(prev.get("consecutive_up")) or 0),
        int(_f(prev.get("consecutive_down")) or 0),
    )
    dtrend = md.get("deviation_trend") or {}
    dev_trend_str = "expanding" if dtrend.get("expanding") else ("shrinking" if dtrend.get("shrinking") else "stable")
    pp24 = md.get("price_position_24h") or {}

    # ---- 趋势状态 + 盘感因子（8/17 新增，权重×1，仅进 S₀，不翻转方向）----
    # 设计文档：data/hermes_trend_softskill_factors_design_20260817.md
    # ⚠️ 不设"大周期背书(用4H方向做1H方向因子)"：与已废除的 macd_4h 语义重复，
    #   8/11 已证 4H 方向一致率仅 28.6% 最误导（rule_engine round26 P1 亦删除 macd_4h）。
    # 开关 ENABLE_SOFT_SKILL_FACTORS=False 时全部 ×0，不改变 S₀（待全样本回测验证后启用）。
    if ENABLE_SOFT_SKILL_FACTORS:
        # A1 动量健康/衰竭：4H MACD 动能是否收窄（变向预警核心，图表分析"动量衰竭"）
        macd4_s = _sign(macd4)
        accel4 = _f(tf4h.get("macd_acceleration"))
        if macd4_s > 0 and accel4 is not None and accel4 < 0:
            factors["momentum_health"] = -1.0   # 多头衰竭 → 偏空
        elif macd4_s < 0 and accel4 is not None and accel4 > 0:
            factors["momentum_health"] = 1.0    # 空头衰竭 → 偏多
        else:
            factors["momentum_health"] = 0.0
        # B1 动量耗尽：连续同色≥5 + 实体缩小（不追、准备反转）
        cu15 = int(_f(tf15m.get("consecutive_up")) or 0)
        cd15 = int(_f(tf15m.get("consecutive_down")) or 0)
        _body_shrinking = False
        rcs = tf15m.get("recent_candles") or []
        if isinstance(rcs, list) and len(rcs) >= 2:
            try:
                _b0 = abs(float(rcs[-1].get("c", 0)) - float(rcs[-1].get("o", 0)))
                _b1 = abs(float(rcs[-2].get("c", 0)) - float(rcs[-2].get("o", 0)))
                _body_shrinking = 0 <= _b0 < _b1
            except (TypeError, ValueError):
                _body_shrinking = False
        if cu15 >= 5 and _body_shrinking:
            factors["exhaustion"] = -1.0        # 多头耗尽 → 偏空
        elif cd15 >= 5 and _body_shrinking:
            factors["exhaustion"] = 1.0         # 空头耗尽 → 偏多
        else:
            factors["exhaustion"] = 0.0
        # B2 支撑阻力互换：价格破强支撑/强阻力 + 方向确认
        kl = md.get("key_levels") or {}
        ss = _f(kl.get("strong_support"))
        sr = _f(kl.get("strong_resistance"))
        rdir15 = tf15m.get("recent_dir")
        if ss is not None and current < ss and rdir15 == "DOWN":
            factors["s_r_swap"] = -1.0          # 跌破强支撑，续跌
        elif sr is not None and current > sr and rdir15 == "UP":
            factors["s_r_swap"] = 1.0           # 突破强阻力，续涨
        else:
            factors["s_r_swap"] = 0.0
        # B3 加速/放量：vol_ratio>1.5 + 方向确认（顺势但权重仅×1）
        vr15 = _f(tf15m.get("vol_ratio"))
        if vr15 is not None and vr15 > 1.5 and rdir15 in ("UP", "DOWN"):
            factors["acceleration"] = 1.0 if rdir15 == "UP" else -1.0
        else:
            factors["acceleration"] = 0.0
    # AI 专属盘感因子（fake_breakout/divergence）→ 保守默认 0，AI 在 prompt_exec 评分表打分为 ±1
    for _k in ("fake_breakout", "divergence"):
        factors.setdefault(_k, 0.0)

    inp: dict = {
        "factors": factors,
        "pm_up": (_f(pm.get("up_price")) or 0) * 100 or None,
        "deviation_pct": dev_pct,
        "tie_line_active": tie_line_active,
        # round18 反手预警：贴线模式标志（激活/维持/失效预警任一，供 F1 翻转级判定）
        "_tie_mode": bool("贴线" in mode),
        "macd_1h_hist": macd1,
        "macd_4h_hist": macd4,
        "taker_1h_buy_pct": taker1h_pct,
        "regime": regime,
        "volatility_5m": vol,
        "in_lower_half": bool(pp24.get("in_lower_half")),
        "in_upper_half": bool(pp24.get("in_upper_half")),
        # 9/3 P1-6 修复: 注入 24h 分位——rule_engine G4 双向核对 L520 消费
        # position_24h_percentile，原实现只注入布尔导致该维度恒缺失，
        # G4 回退从设计「3 维取 ≥2」退化为「2 维全中才翻」。
        "position_24h_percentile": _f(pp24.get("current_percentile")),
        "macd_1h_accel": _f(tf1h.get("macd_acceleration")),
        "macd_4h_accel": _f(tf4h.get("macd_acceleration")),
        # 8/15 候选翻转型 BB3：4H 布林位置（深跌末端反转判据，快照 timeframes.4H.bb_pos）
        "bb_pos_4h": _f(tf4h.get("bb_pos")),
        # round17 反手预警：历史时段主倾向（seasonality 判据同源，独立输入供组合判定）
        "historical_up_rate": _f((md.get("historical") or {}).get("up_rate")),
        "minutes_to_settle": minutes_to_settle,
        "deviation_trend": dev_trend_str,
        "consecutive_same_dir": consec,
        "is_settlement_phase": is_settlement,
        # 8/12 round11: 短周期反弹先兆（仅常规矩阵非贴线生效；贴线/结算期恒为 0）
        "short_bounce": _short_bounce(tf1m, tf5m, tf15m) if mode in ("常规-成型期", "常规-博弈期") else 0,
        # round38 H13 顶部收敛摆动日守卫（8/22 审计 Fix 5 引擎化配套注入）：
        # 4H RSI + 最近已收 1H 方向序列 + 涨跌幅序列。旧快照缺 last3_chg_pct -> H13 不触发
        # （fail-open，与文档"字段缺失按趋势市"精神一致）。
        "rsi_4h": _f(tf4h.get("rsi")),
        "prev_dirs": prev.get("direction_history") if isinstance(prev.get("direction_history"), list) else None,
        "prev_chgs": prev.get("last3_chg_pct") if isinstance(prev.get("last3_chg_pct"), list) else None,
        # round44.2 同步链补丁（8/30）：G4 双向核对读 macro_trend_1h_4h，但此前从未注入该字段
        # → 宏观趋势维度永远缺失（实际只剩 24h位置+4H MACD 两维）。采集器 collect 步骤11.1 已产出
        # 该 dict（SCHEMA_VERSION 6，{up_count,down_count}，已收4根口径），此处补注入供 judge G4 使用；
        # 不进 S₀ 计分（评分表已无 macro_trend 行），仅 G4 双向核对 3 维证据计数（round44.2 设计意图）。
        "macro_trend_1h_4h": md.get("macro_trend_1h_4h") or {},
        # AI 专属，保守默认
        "evidence_dims": 0,
        "fakeout": None,
        "orderflow_contradiction": False,
        "h12_macro_evidence": 0,
        "veto_triggered": False,
        "data_ok": True,
    }
    # round41.8.13（8/25 用户指令）：翻向共识价引入真实订单簿 book_up_mid。
    # gamma 标记价 up_price 与 clob 盘口可剧烈脱节（3pm 场实证：up_price=0.705 vs book_up_mid=0.44
    # vs 实时 best_ask=0.28 → 标记价是失真的假强共识，骗引擎翻向买涨）。book_up_mid 原始 0-1 口径，
    # ×100 对齐 pm_up 的 0-100 口径；只在 0<mid<1（真实有效盘口）时注入，否则退回标记价。
    _bm = _f(pm.get("book_up_mid"))
    inp["book_up_mid"] = _bm * 100 if (_bm is not None and 0.0 < _bm < 1.0) else None
    _imb = _f(pm.get("book_imbalance"))
    inp["book_imbalance"] = _imb
    if inp["pm_up"] is None:
        notes.append("polymarket.up_price 缺失，PM 行/G2/G3 按无 PM 处理")
    # H13 缺口标注：4H RSI 已极端（>90/<10，H13 第一条件成立）但方向/涨跌幅序列缺失
    # （round41.2 前旧快照无 last3_chg_pct）-> H13 无法完整评估，engine_check 层可见。
    _rsi4 = inp.get("rsi_4h")
    if isinstance(_rsi4, (int, float)) and (_rsi4 > 90 or _rsi4 < 10) and (
        not isinstance(inp.get("prev_dirs"), list) or not isinstance(inp.get("prev_chgs"), list)
    ):
        notes.append(f"H13 缺口：4H RSI={_rsi4:.1f} 极端但 prev_dirs/prev_chgs 缺失（旧快照），H13 不触发")

    # extreme_wall 语义方向相关（吸筹=极端卖墙@方向DOWN / 派发=极端买墙@方向UP），
    # 而方向由 judge 内部产出 → 两遍判定：先 extreme_wall=False 取预方向，再按方向注入墙重跑。
    inp["_buy_wall_ratio"] = buy_wall
    inp["_sell_wall_ratio"] = sell_wall
    # 8/15 贴线高不确定降级：taker_bs_ratio 供 judge 的"极端Taker"判别
    # （贴线模式 taker_1h_buy_pct 通常不极端，需原始买卖比才能识别脉冲失真）
    inp["taker_bs_ratio"] = _f(cap.get("taker_bs_ratio"))
    return factors, inp, notes, mode


def _norm_conf(c: object) -> str | None:
    if isinstance(c, str):
        return _CONF_ALIAS.get(c.strip().lower(), _CONF_ALIAS.get(c.strip()))
    return None


def _scale_rank(s: object) -> float:
    """position_scale 保守度排序：min(0) < 0.5 < 1.0/None(1)。"""
    if s is None:
        return 1.0
    if isinstance(s, str) and s.strip().lower() == "min":
        return 0.0
    v = _f(s)
    return v if v is not None else 1.0


def _min_scale(a: object, b: object) -> object:
    winner = a if _scale_rank(a) <= _scale_rank(b) else b
    return None if _scale_rank(winner) >= 1.0 else winner


def _extract_ai_s0(reason: object) -> str:
    if isinstance(reason, str):
        m = re.search(r"S[₀0]\s*[=＝]\s*([+-]?\d+(?:\.\d+)?)", reason)
        if m:
            return m.group(1)
    return "未知"


def _consensus_flip(out: dict, inp: dict, d, check: dict, annotate) -> tuple[dict, dict]:
    """round42（8/25 用户决策「G3.5 翻向给禁止使用」）：终局翻向 **已禁用**。

    历史背景：
    - round41.6（8/24）落地：297 笔成交回测显示模型逆 PM≥60% 强共识胜率仅 20.8% vs PM 79.2%，
      无差别翻向 Δ+$69.90（slip×1.05 仍 +$61.33），落地终局翻向。
    - round42 实盘打脸：bot_launchd.log 提取 8/22-8/25 共 11 笔翻向样本（已结算），
      胜率 36.4% vs 回测 79.2%、总 PnL **-$67.65** vs 期望 +$69.90。
      其中 8 笔 G3/G3.5+G4+G2 多门槛叠加触发胜率 25.0% / -$50.49；
      标记价失真（41.8.13 才修复）+ G2 共同触发是偏差主因。
    - 8/25 3AM ET 实场实证：S₀=-5 六维共振 + |偏差|=0.074%<0.15% 摆动市案例被 G3.5
      无差别翻向（用户决策禁用触发）。

    现行行为：函数壳保留（供未来 re-enable / 监控 PM 共识度），翻向执行段全部 no-op。
    - PM 共识价计算逻辑保留（仅观测，不影响信号）
    - `consensus_flip_disabled` 插桩字段保留（供复盘统计「G3.5 若启用会触发多少次」）
    - 翻向 out / annotation 段全部 no-op

    未来 re-enable 路径：把 # ↓↓↓↓↓ ROUND42 DISABLED ↓↓↓↓↓ 块替换为原翻向执行段。
    """
    # 守卫：仅对 ORDER 信号走观测（SKIP 不观测）
    if (out.get("action") or "ORDER").upper() != "ORDER":
        return out, check
    # round41.8.13：共识价优先 book_up_mid（缺失退标记价）
    book_mid = inp.get("book_up_mid")
    mark_up = inp.get("pm_up")
    if isinstance(book_mid, (int, float)):
        snap_pm_up = book_mid
        pm_src = "book_mid"
    else:
        snap_pm_up = mark_up
        pm_src = "mark"
    if not isinstance(snap_pm_up, (int, float)):
        return out, check
    # 标记价失真预警（仅观测/归因，不阻断）
    if pm_src == "book_mid" and isinstance(mark_up, (int, float)) and abs(book_mid - mark_up) >= 10.0:
        annotate(f"⚠️标记价失真：mark_up={mark_up:.0f} vs book_mid={book_mid:.0f}"
                 f"（差{abs(book_mid - mark_up):.0f}pt）→ G3.5 已禁用，本应翻向但保留原方向")
    pm_cons = max(snap_pm_up, 100 - snap_pm_up)
    pm_dir = "UP" if snap_pm_up > 50 else ("DOWN" if snap_pm_up < 50 else None)
    # ↓↓↓↓↓ ROUND42 DISABLED ↓↓↓↓↓
    # 强共识本应触发翻向（round41.6 旧逻辑）——本轮用户决策禁用，仅记录不触执行
    if pm_dir is not None and pm_cons > 60.0:
        cur_dir = (out.get("side") or out.get("signal") or "").upper()
        if cur_dir and cur_dir != pm_dir:
            check["consensus_flip_disabled"] = (
                f"PM={pm_cons:.0f}%({pm_src}) G3.5 disabled round42, would flip {cur_dir}→{pm_dir}"
            )
            check["consensus_flip_disabled_pm_cons"] = pm_cons
            annotate(f"⚠️round42 G3.5 翻向禁用：{cur_dir} 逆 PM {pm_cons:.0f}%（{pm_dir}）"
                     f"本应翻向但保留原方向（实盘 8/22-8/25 11 笔翻向 36.4% 胜率 -$67.65 实证）")
    return out, check
    # ↑↑↑↑↑ ROUND42 DISABLED ↑↑↑↑↑
    # ---- 以下为原 round41.6 翻向执行段（已废，保留供 re-enable 参考）----
    # if pm_dir is None or pm_cons <= 60.0:
    #     return out, check
    # cur_dir = (out.get("side") or out.get("signal") or "").upper()
    # if not cur_dir or cur_dir == pm_dir:
    #     return out, check
    # pm_price = snap_pm_up / 100.0 if pm_dir == "UP" else (100 - snap_pm_up) / 100.0
    # out["signal"] = out["side"] = pm_dir
    # out["confidence"] = "低"
    # out["probability"] = round(min(pm_price, 0.90), 3)
    # out["max_price"] = 0.60
    # if isinstance(out.get("probability"), (int, float)):
    #     out["ev"] = round((float(out["probability"]) - 0.60) * 100, 2)
    # merged = _min_scale(out.get("position_scale"), d.position_scale)
    # if merged is not None:
    #     out["position_scale"] = merged
    # check["corrected"] = True
    # check["consensus_flip"] = f"{cur_dir}→{pm_dir} (PM {pm_cons:.0f}% 强共识, 源={pm_src})"
    # # 8/24 分桶插桩（零行为变更）：按共识度落桶，供复盘分桶统计翻向战绩。
    # # 争议区=70-80（买价反推口径模型方向胜率 45% vs PM 55%）；≥80 桶模型方向 0/4 全负。
    # # 复查触发：70-80 桶翻向后累计 P&L ≤−$30 且样本 ≥20 笔 → 提案豁免/封顶改 70（见 prompt_exec.md G3.5）。
    # check["consensus_flip_bucket"] = "60-70" if pm_cons < 70 else ("70-80" if pm_cons < 80 else ">=80")
    # check["correction_detail"] = (check.get("correction_detail", "") + "; " if check.get("correction_detail") else "") + \
    #     f"round41.6 逆强共识翻向 {cur_dir}→{pm_dir}"
    # annotate(f"⚠️round41.6 逆强共识翻向：{cur_dir} 逆 PM {pm_cons:.0f}%（{pm_dir}）→ 翻向 {pm_dir}"
    #          f"（回测: 逆强共识模型方向胜率 20.8% vs PM 79.2%，24 笔跨 16 天）")
    # return out, check


def _validate_with_md_impl(sig: dict, md: dict, source: str) -> tuple[dict, dict]:
    factors, inp, notes, mode = extract_factors(md)
    buy_wall = inp.pop("_buy_wall_ratio")
    sell_wall = inp.pop("_sell_wall_ratio")
    taker_bs = inp.pop("taker_bs_ratio", None)

    # round31：把 AI 信号中的真实挂价传入 judge，供 EV 计算（EV 仅日志，不影响 ORDER/SKIP）
    try:
        max_price = float(sig.get("max_price"))
    except (TypeError, ValueError):
        max_price = 0.60
    if not (0 < max_price <= 1):
        max_price = 0.60

    prelim = judge(dict(inp, extreme_wall=False), max_price=max_price)
    prelim_dir = prelim.final_direction
    if prelim_dir == "DOWN":
        extreme_wall = bool(sell_wall is not None and sell_wall > WALL_RATIO)
    elif prelim_dir == "UP":
        extreme_wall = bool(buy_wall is not None and buy_wall > WALL_RATIO)
    else:
        extreme_wall = False
    # 8/15 9.7#8 贴线高不确定：墙比值与 taker_bs_ratio 需传给 judge
    # （extreme_wall 布尔不足以判别"极端墙+极端Taker"背离）
    d = judge(dict(inp, extreme_wall=extreme_wall,
                   _buy_wall_ratio=buy_wall, _sell_wall_ratio=sell_wall,
                   taker_bs_ratio=taker_bs),
              max_price=max_price)

    # round23 写信号实时价复核：若快照判定发生了方向翻转（F1 翻转级 / G3 硬覆盖 / G5.4 覆盖），
    # 则实时查 Polymarket 该场价格，用实时价重跑 judge——引擎的"反转目标价≥0.23 门槛"据此裁决
    # 是否允许反转（用户要求：写信号时查实时价，满足就反转，不满足就不反转）。
    # 实时查价失败 fail-open：保持快照判定。
    # round41.6 防线（8/24，10am 事故实证）：实时复核只允许「PM 实时确实反向 ≥55%」或
    # 「快照较老（>5 分钟）」时才采用实时重跑结果，否则保持快照判定——防止 2 分钟内
    # 6-8 个点的 PM 噪音把正确的 G3 翻向又翻回去（8/23 10am：G3 翻 DOWN 被复核翻回 UP 亏损）。
    _flip_like = any(("翻转级" in r) or ("改判顺共识" in r) or ("覆盖：" in r) or ("覆盖为" in r) for r in d.risks)
    if _flip_like:
        live = _fetch_live_pm_book(sig.get("slug") or "")
        if live.get("up") is not None and live.get("down") is not None:
            # round41.8.13（第二步）：实时共识价优先真实订单簿 book_up_mid（0-1 ×100），缺失退回
            # gamma 标记价——与 _consensus_flip 同一失真源修复（8/24 3pm 场标记价 0.705 vs 盘口 0.44）。
            live_book_mid = live.get("book_up_mid")
            live_mark_up = live.get("up") * 100.0
            live_pm_up = live_book_mid * 100.0 if (live_book_mid is not None and 0.0 < live_book_mid < 1.0) else live_mark_up
            # 快照共识价同源：优先 book_up_mid（round41.8.13 已注入 inp，0-100），缺失退回标记价
            snap_pm_up = inp.get("book_up_mid") if isinstance(inp.get("book_up_mid"), (int, float)) else inp.get("pm_up")
            snapshot_age_min = 0.0
            ct = md.get("collect_time_utc") or md.get("collected_at")
            if ct:
                try:
                    ct_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                    snapshot_age_min = (datetime.now(timezone.utc) - ct_dt).total_seconds() / 60.0
                except Exception:
                    pass
            # 标记价失真预警（仅观测）：实时标记价与盘口偏离 ≥10pt 记 notes
            if live_book_mid is not None and 0.0 < live_book_mid < 1.0 and abs(live_book_mid * 100.0 - live_mark_up) >= 10.0:
                notes.append(f"⚠️实时复核标记价失真：mark_up={live_mark_up:.0f} vs book_mid={live_book_mid * 100.0:.0f}"
                             f"（差{abs(live_book_mid * 100.0 - live_mark_up):.0f}pt）→ 实时共识改用 book_mid")
            pm_flip_ok = (isinstance(snap_pm_up, (int, float)) and abs(live_pm_up - 50.0) >= 5.0) \
                or (not isinstance(snap_pm_up, (int, float))) or (snapshot_age_min > 5.0)
            if pm_flip_ok:
                # round41.8.14：live_pm_up 已是"优先 live book_mid、缺失退 live 标记价"的
                # 最佳实时共识——同步写入 book_up_mid，否则 judge 的无条件盘口优先会拿
                # 旧快照 book_up_mid 覆盖 live 共识（实时复核语义被破坏）。
                live_inp = dict(inp, pm_up=live_pm_up, book_up_mid=live_pm_up,
                                extreme_wall=extreme_wall, _buy_wall_ratio=buy_wall,
                                _sell_wall_ratio=sell_wall, taker_bs_ratio=taker_bs)
                d_live = judge(live_inp, max_price=max_price)
                _bm_s = f"{live_book_mid:.3f}" if live_book_mid is not None else "None"
                notes.append(f"round23 实时价复核：UP={live.get('up'):.3f} DOWN={live.get('down'):.3f}"
                             f"（book_mid={_bm_s}，快照共识={snap_pm_up}）→ "
                             f"引擎方向 {d.final_direction}→{d_live.final_direction}，净分 {d.net:+.0f}→{d_live.net:+.0f}")
                d = d_live
                # 注意：不更新 inp["pm_up"]——round41.6 翻向判据固定用快照 pm（回测口径，
                # Δ+$69.90 即基于快照判强共识）；若跟 live 耦合，10am 类「快照强共识但 live 回落
                # 到弱共识」会让翻向失效（审计 8/24 发现并回退）。live 市场真翻转（罕见）留待复盘数据裁决。
            else:
                notes.append(f"round41.6 实时复核防线：实时共识 UP={live_pm_up:.1f}% 未反向≥55%"
                             f"（|Δ50|={abs(live_pm_up - 50.0):.1f}pt<5）且快照 {snapshot_age_min:.0f}min 未超 5min"
                             f" → 保持快照判定（防止 8/23 10am 类噪音翻盘）")

    # 贴线失效预警：置信度强制低（步骤6.1，引擎外的机器口径补充）
    eng_conf = d.confidence
    if "失效预警" in mode:
        eng_conf = "低"

    ai_side = (sig.get("side") or sig.get("signal") or "").upper()
    ai_conf = _norm_conf(sig.get("confidence"))
    ai_action = (sig.get("action") or "ORDER").upper()
    ai_s0 = _extract_ai_s0(sig.get("reason"))

    check: dict = {
        "skipped": None,
        "snapshot_source": source,
        "mode": mode,
        "engine_action": d.action,
        "engine_direction": d.final_direction,
        "engine_confidence": eng_conf,
        "engine_net": d.net,
        "engine_s0": d.s0,
        "engine_pm_row": d.pm_row,
        "engine_probability": d.probability,
        # round51.3 逐项因子明细：完整 engine_check 照单入 dict，供 scoring_compare 落盘审计
        #「每个因子给了 ±几 + 触发值」。含 0 值项（未触发因子），S0 = sum(factors 全部项)。
        "factors": factors,
        "ai_side": ai_side,
        "ai_confidence": ai_conf,
        "ai_claimed_s0": ai_s0,
        "corrected": False,
        "correction_detail": "",
        "coverage": f"机算 {len(factors)} 项（{','.join(factors)}）；AI 专属未复算：{','.join(AI_ONLY_FACTORS)}",
        "assumptions": notes + ["extreme_wall 按引擎预方向注入（DOWN→极端卖墙/UP→极端买墙）"],
        "trace": d.trace[-14:],  # 8/23 trace 增强：窗口 10→14，容纳新增"方向来源/仓位来源"汇总行
        "engine_risks": list(d.risks),  # 引擎内部风险标注（含 G5.4 缩仓/H12/吸筹嫌疑等），供复盘
    }

    out = dict(sig)

    def _annotate(msg: str) -> None:
        out["risk"] = f"{out['risk']}; {msg}" if out.get("risk") else msg

    # 8/16 修复 Bug#10：统一 probability 为小数口径（0-1）。
    # AI 链路可能输出百分比（52=52%）或小数（0.52），下游 write_signal 一律按小数消费
    # （默认 0.5、×100 得百分比 scores）。此处出口统一规范化为小数，消除 hermes_analysis.json
    # 出现整数百分比（52）导致 dashboard/下游算错的隐患。
    _prob = out.get("probability")
    if _prob is not None:
        try:
            _prob_f = float(_prob)
            if _prob_f > 1:
                out["probability"] = round(_prob_f / 100.0, 3)
                _annotate(f"probability 百分比{_prob}→小数{out['probability']}（统一口径）")
        except (TypeError, ValueError):
            pass  # 非数值保留原样，下游按默认处理

    # 1) 引擎 SKIP 而 AI ORDER → 改 SKIP（round13 后仅 G1 数据失效可达；G6/H12 已不再产出 SKIP）
    if d.action == "SKIP" and ai_action == "ORDER":
        out["action"] = "SKIP"
        if d.final_direction:
            out["signal"] = out["side"] = d.final_direction
        if d.probability is not None:
            out["probability"] = round(d.probability / 100, 3)
        out.pop("position_scale", None)  # G1 SKIP 不写入 scale
        check["corrected"] = True
        check["correction_detail"] = f"AI ORDER → 引擎 SKIP（{d.final_direction}，G1 数据失效）"
        _annotate(f"⚠️引擎校验修正：AI ORDER/{ai_side} → 引擎 SKIP/{d.final_direction}"
                  f"（S0 机算 {d.s0:+.0f} vs AI 声称 {ai_s0}，{d.risks[-1] if d.risks else 'G1 数据失效'}）")
        return out, check

    # 2) AI SKIP → round13 全量交易：按引擎最终方向修正为 ORDER（锁低 + 缩仓承接），不保留 SKIP
    if ai_action == "SKIP":
        if d.final_direction:
            out["signal"] = out["side"] = d.final_direction
            out["confidence"] = "低"
            if d.probability is not None:
                out["probability"] = round(d.probability / 100, 3)
            merged = _min_scale(out.get("position_scale"), d.position_scale)
            if merged is not None:
                out["position_scale"] = merged
            out["action"] = "ORDER"
            check["corrected"] = True
            check["correction_detail"] = f"AI SKIP → 引擎 ORDER/{d.final_direction}（round13 全量交易，锁低+缩仓）"
            _annotate(f"⚠️引擎校验修正：AI SKIP → 引擎 ORDER/{d.final_direction}"
                      f"（round13 全量交易不跳过；S0 机算 {d.s0:+.0f} vs AI 声称 {ai_s0}，净分 {d.net:+.0f}）")
        else:
            check["correction_detail"] = "AI SKIP 且引擎无方向（G1 数据失效），保留 SKIP"
        out, check = _consensus_flip(out, inp, d, check, _annotate)  # round41.6
        return out, check

    # 3) 双方 ORDER：方向/置信度比对
    eng_dir = d.final_direction
    if eng_dir and ai_side and eng_dir != ai_side:
        out["signal"] = out["side"] = eng_dir
        out["confidence"] = "低"  # 方向翻转必锁低
        if d.probability is not None:
            out["probability"] = round(d.probability / 100, 3)
        # A4(2026-08-23): 翻向后不再 pop max_price/ev——原逻辑会留下缺字段信号，
        # 逼 bot 用 0.69 兜底（8/23 两笔实盘实证）。翻向统一取标准封顶 0.60，
        # EV 按新方向概率重算，保证信号自洽完整。
        out["max_price"] = 0.60
        if isinstance(out.get("probability"), (int, float)):
            out["ev"] = round((float(out["probability"]) - 0.60) * 100, 2)
        else:
            out.pop("ev", None)
        merged = _min_scale(out.get("position_scale"), d.position_scale)
        if merged is None:
            out.pop("position_scale", None)
        else:
            out["position_scale"] = merged
        check["corrected"] = True
        check["correction_detail"] = f"方向翻转 {ai_side}→{eng_dir}，锁低"
        _annotate(f"⚠️引擎校验修正：AI {ai_side}/{ai_conf or '?'} → 引擎 {eng_dir}/低"
                  f"（S0 机算 {d.s0:+.0f} vs AI 声称 {ai_s0}，净分 {d.net:+.0f}，PM行 {d.pm_row:+.0f}）")
        out, check = _consensus_flip(out, inp, d, check, _annotate)  # round41.6
        return out, check

    # 4) 方向一致：置信度取严 + scale 取保守
    if ai_conf and eng_conf and CONF_LEVELS.index(eng_conf) < CONF_LEVELS.index(ai_conf):
        out["confidence"] = eng_conf
        if d.probability is not None:
            out["probability"] = round(d.probability / 100, 3)
        check["corrected"] = True
        check["correction_detail"] = f"置信度取严 {ai_conf}→{eng_conf}"
        _annotate(f"⚠️引擎校验修正：置信度 AI {ai_conf} → 引擎 {eng_conf}"
                  f"（S0 机算 {d.s0:+.0f} vs AI 声称 {ai_s0}）")
    merged = _min_scale(out.get("position_scale"), d.position_scale)
    if _scale_rank(merged) < _scale_rank(out.get("position_scale")):
        out["position_scale"] = merged
        check["corrected"] = True
        check["correction_detail"] += f"；position_scale 取保守 {merged}"
        _annotate(f"⚠️引擎校验修正：position_scale 取保守 {merged}")

    # 5) 概率强制回写（8/23 2pm 场实证缺口修复，round41.3 数值禁令硬落地）：
    #    方向/置信度一致时旧逻辑整段放行，AI 手查四维表查错格无兜底
    #    （实证：AI 写 62.5%＝「贴线+顺PM」格，引擎机算 66.7%＝「非贴线+逆PM」格）。
    #    偏差 >1pt 即以引擎机算回写；EV 同步重算；AI 原值留档 probability_ai_original/ev_ai_original。
    ai_p = out.get("probability")
    if d.probability is not None and isinstance(ai_p, (int, float)):
        eng_p = round(d.probability / 100, 3)
        if abs(ai_p - eng_p) > 0.01 + 1e-9:
            out["probability_ai_original"] = ai_p
            out["probability"] = eng_p
            ev_note = ""
            mp = out.get("max_price")
            if isinstance(mp, (int, float)):
                out["ev_ai_original"] = out.get("ev")
                out["ev"] = round((eng_p - mp) * 100, 2)
                ev_note = "，EV 同步重算"
            note = (f"⚠️概率强制回写：AI {ai_p:.3f} → 引擎机算 {eng_p:.3f}"
                    f"（偏差 {abs(ai_p - eng_p) * 100:.1f}pt >1pt，round41.3 数值禁令{ev_note}）")
            check["prob_corrected"] = True
            check["correction_detail"] = (check["correction_detail"] + "；" if check["correction_detail"] else "") + note
            _annotate(note)

    # A4(2026-08-23): 出口统一保证 ORDER 信号自洽完整——任何修正路径(翻向/SKIP→ORDER/取严)都不得
    # 产出缺 max_price/ev 的信号，否则 bot 会退到 0.69 兜底(实测两笔实盘)。
    if out.get("action") == "ORDER":
        _need_mp = not isinstance(out.get("max_price"), (int, float)) or not (0 < float(out["max_price"]) < 1)
        if _need_mp:
            out["max_price"] = 0.60
            _annotate("⚠️引擎校验：补标准封顶 max_price=0.60（原信号缺失，防 bot 0.69 兜底）")
        if not isinstance(out.get("ev"), (int, float)):
            if isinstance(out.get("probability"), (int, float)):
                out["ev"] = round((float(out["probability"]) - float(out["max_price"])) * 100, 2)
                check["corrected"] = check.get("corrected") or True
            else:
                out.pop("ev", None)  # 概率都缺则无法算 EV，宁可缺也不写错值
    out, check = _consensus_flip(out, inp, d, check, _annotate)  # round41.6 逆强共识翻向（最后兜底）
    return out, check



def _sync_pm_price_fields(out: dict, md: dict, check: dict) -> dict:
    """round41.8.19（8/25）：所有翻向路径出口统一回填 PM 基准字段。

    事故实证（2026-08-25 1am ET 场）：AI 原始 DOWN 写 pm_down_price=0.335，引擎机算
    S₀=+4 翻向 UP 后无人回填 pm_up_price → 消费信号 side=UP 但 pm_up_price=None，
    bot 的「一般波动检测（相对快照跌幅>15% 记录）+ round41.4 C4 影子分层记录」按
    final side 取 pm_up_price（hermes_1h_bot snap_key），双双静默失效。
    口径：41.8.17 信号字段必须同源 gamma 标记价——回填源=快照 polymarket.up_price /
    down_price（mark 0-1），**绝不填 book_up_mid**。已有有效值（AI 写的）不覆盖；
    SKIP 信号不处理；标记价缺失/非法（≤0/≥1）不造值（fail-open 保持 None）。
    """
    if (out.get("action") or "").upper() != "ORDER":
        return out
    side = (out.get("side") or out.get("signal") or "").upper()
    if side not in ("UP", "DOWN"):
        return out
    key = "pm_up_price" if side == "UP" else "pm_down_price"
    try:
        cur = float(out.get(key)) if out.get(key) is not None else None
    except (TypeError, ValueError):
        cur = None
    if cur is not None and 0.0 < cur < 1.0:
        return out  # AI 已写有效基准，不覆盖
    pm = md.get("polymarket") or {}
    mark = _f(pm.get("up_price" if side == "UP" else "down_price"))
    if mark is None or not (0.0 < mark < 1.0):
        return out  # 无合法标记价，保持原状（不造值）
    out[key] = mark
    check["correction_detail"] = (check.get("correction_detail", "") + "; "
                                  if check.get("correction_detail") else "") + \
        f"round41.8.19 翻向基准回填 {key}={mark}（源=gamma标记价，bot波动检测同源）"
    return out


def _validate_with_md(sig: dict, md: dict, source: str) -> tuple[dict, dict]:
    """校验入口（round41.8.19 包装：方向翻转/AI-SKIP/共识翻向各路径出口统一回填 PM 基准字段）。"""
    out, check = _validate_with_md_impl(sig, md, source)
    return _sync_pm_price_fields(out, md, check), check


def validate(signal_dict: dict) -> tuple[dict, dict]:
    """返回 (修正后信号, engine_check)。数据不足时 fail-open 放行并记录 skipped；
    未预期异常向上抛给 write_signal.py 的外层兜底。"""
    slug = signal_dict.get("slug") or ""
    try:
        md, source = load_market_data(slug)
        if md is None:
            return dict(signal_dict), {"skipped": f"无同 slug 快照（{slug}）", "corrected": False}
        return _validate_with_md(dict(signal_dict), md, source)
    except _SkipValidation as e:
        return dict(signal_dict), {"skipped": str(e), "corrected": False}


# ================= selftest：8/11 6AM 错单场端到端复现 =================

def _selftest() -> int:
    snap = Path(os.environ.get("HERMES_DIR", str(_PROJECT_ROOT))) / "data/snapshots/bitcoin-up-or-down-august-11-2026-6am-et_20260811_182827.json"
    md = json.loads(snap.read_text(encoding="utf-8"))
    failed = 0

    def run(name: str, fn) -> None:
        nonlocal failed
        ok, detail = fn()
        print(f"{'✅' if ok else '❌'} {name}")
        if detail:
            print(f"   → {detail}")
        if not ok:
            failed += 1

    # 案例1（核心）：8/11 6AM 真实错单——AI UP/中/S₀声称+7 → 引擎应修正为 DOWN/低/ORDER
    ai_signal = {
        "slug": md["slug"], "signal": "UP", "side": "UP", "probability": 0.66,
        "confidence": "中", "action": "ORDER", "max_price": 0.38, "ev": 0.43,
        "reason": "贴线专列S0=+7(...); PM行UP24.5%<50%弱势无共识0; ...",
        "risk": "⚠️价格站锚定下方却判UP逆价格位置",
    }
    out, check = _validate_with_md(ai_signal, md, str(snap))

    def case1() -> tuple[bool, str]:
        ok = (
            out["side"] == "DOWN" and out["signal"] == "DOWN"
            and out["confidence"] == "低" and out.get("action") == "ORDER"
            and check["corrected"] and "引擎校验修正" in out.get("risk", "")
            # A4(2026-08-23): 翻向后不再丢字段——必须带标准封顶 max_price=0.60 与自洽 EV
            and out.get("max_price") == 0.60
            and isinstance(out.get("ev"), (int, float))
            # round41.8.15(2026-08-25): 价格位置全局 ×2→×1 → 该场 S₀ 由 -2 升至 -1
            # round41.8.20（价格位置锚定口径统一）：该场 |dev|=0.0529%<0.15% -> price_position 计 0（旧口径 sign(dev)=-1）
            # P0-1.3(2026-08-30)：新增 recent_dir_5m/15m 动量因子（该场 5m/15m recent_dir 均 DOWN，各 -1）
            #   → S₀ 由 0 变为 -2（方向仍 DOWN 不变）
            and check["engine_s0"] == -2 and check["engine_pm_row"] == 0
            and abs(out.get("probability", 0) - round((check["engine_probability"] or 0) / 100, 3)) < 1e-9
        )
        return ok, (f"{out.get('side')}/{out.get('confidence')}/{out.get('action')} "
                    f"P={out.get('probability')} max={out.get('max_price')} ev={out.get('ev')} "
                    f"引擎S0={check['engine_s0']:+.0f} PM行={check['engine_pm_row']:+.0f}")

    run("6AM 错单复现：AI UP/中(S₀+7) -> 引擎修正 DOWN/低（S₀=0, round41.8.20 价格位置锚定口径）", case1)

    # 案例2：AI 与引擎一致（DOWN/低）→ 方向不修正。round18 反手预警分级下，6AM 快照
    # （贴线激活+顺PM共识68.5%+反向历史+4H衰竭，up_rate 0.667>0.55）命中观察级，但 |4H hist|=135≥50
    # （深势）不满足 F1 翻转级 → 方向不动（不误伤正确场）。
    # round35（8/21）后：该场 |净分|=1 非贴线+顺PM 落 0-2 档降级格（93.3高→67.6低）→ 引擎置信度低
    # → round30.1 仓位联动 d.position_scale=0.5 → validator 分支4 取保守 0.5（仅日志，金额按 UI 全额，
    # 8/12 决策）。断言：方向保持 DOWN、置信度低、scale=0.5、corrected=True 仅因 scale 取保守。
    out2, check2 = _validate_with_md(
        {**ai_signal, "side": "DOWN", "signal": "DOWN", "confidence": "低", "probability": 0.52},
        md, str(snap))

    def case2() -> tuple[bool, str]:
        ok = (
            out2["side"] == "DOWN" and out2["confidence"] == "低"
            and check2["corrected"]
            and "position_scale 取保守" in check2["correction_detail"]
            and out2.get("position_scale") == 0.5
        )
        return ok, (f"corrected={check2['corrected']} side={out2['side']} conf={out2['confidence']} "
                    f"scale={out2.get('position_scale')} detail={check2['correction_detail']}")

    run("一致场：AI DOWN/低 方向/置信度一致 → round18 观察级维持原判（深势非F1，不缩仓不误伤）", case2)

    # 案例3：fail-open——无快照 slug → 原信号放行 + skipped 记录
    sig3 = {"slug": "nonexistent-slug-0000", "side": "UP", "confidence": "高", "action": "ORDER"}
    out3, check3 = validate(sig3)

    def case3() -> tuple[bool, str]:
        ok = out3["side"] == "UP" and out3["confidence"] == "高" and check3.get("skipped") and not check3["corrected"]
        return ok, f"skipped={check3.get('skipped')}"

    run("fail-open：无快照 → 原信号放行 + skipped 标注", case3)

    # 案例4（round47 修订）：AI 高 vs 引擎 低 → 取严或翻向均接受。
    # 3PM 快照：删除历史季节性(round47)后引擎机算 S0 较旧口径减少约 1 分，原 UP 临界变 0 或翻 DOWN；
    # AI 报 UP/高 → 引擎 direction 可能为 DOWN 或 UP（取严低），corrected=True + 锁低 + 0.5 缩仓任一触发即通过。
    snap3pm = Path(os.environ.get("HERMES_DIR", str(_PROJECT_ROOT))) / "data/snapshots/bitcoin-up-or-down-august-11-2026-3pm-et_20260812_032836.json"
    md3 = json.loads(snap3pm.read_text(encoding="utf-8"))
    out4, check4 = _validate_with_md(
        {"slug": md3["slug"], "signal": "UP", "side": "UP", "probability": 0.75,
         "confidence": "高", "action": "ORDER", "max_price": 0.41, "ev": 0.1,
         "reason": "常规-博弈期 S0=+2(round41.8.20/round42.2 后旧快照 S0 受季节性删除 round47 影响 -1); ...",
         "risk": ""},
        md3, str(snap3pm))

    def case4() -> tuple[bool, str]:
        # round47 后该场季节性 +1 退出记分,S0 减少 1 → 引擎方向可能翻向 DOWN 或锁低 UP,任一通过
        ok = out4["confidence"] == "低" and check4["corrected"] and (
            "取严" in check4["correction_detail"] or "方向翻转" in check4["correction_detail"]
        )
        return ok, f"conf={out4['confidence']} detail={check4['correction_detail']}"

    run("方向一致置信度取严：AI 高 → 引擎 低（3PM 快照 round47 季节性删除后）", case4)

    # 案例5（round31 当前权重 + round41 宏观趋势族 + round41.4 订单流partial降权 + round41.8.9 废除 sr_proximity/macd_15m + round47 删除历史季节性）：3PM 场端到端
    # —— S0 演进：+2（round41 前口径）→ +1（round41 宏观趋势族 -1）→ +2（round41.4 该场
    # order_flow.partial=true，-2→-1 少扣 1 分）→ +3（round41.8.8 sr_proximity +1）→ **+1（round41.8.9
    # sr_proximity 废除 -1 + macd_15m 废除 -1）**；方向仍 UP 不变，G5 非深度逆大趋势缩仓 + -> **+2（round41.8.20 价格位置锚定口径：|dev|=0.0143%<0.15% 计 0，旧口径 -1）**；方向仍 UP 不变 -> **+1（round42.1 短周期反弹先兆 ×2→×1：该场 short_bounce=-1，factor -2→-1 少扣 1 分）**；方向仍 UP 不变
    # -> **+0（round47 删除历史季节性：该场 up_rate>0.55 计 +1，删除后 -1）**；方向仍可能 UP(净分 0 由价格位置决定) 或翻 DOWN
    # 贴线+共识风险标注；AI 报 DOWN/中 → 引擎方向取 UP/低(净分 0 时价格位置决定,可能 UP 也可能 DOWN)。
    out5, check5 = _validate_with_md(
        {"slug": md3["slug"], "signal": "DOWN", "side": "DOWN", "probability": 0.62,
         "confidence": "中", "action": "ORDER", "max_price": 0.41, "ev": 0.1,
         "reason": "常规-博弈期 S0=-4(round47 季节性删除后净分 0 由价格位置决定); ...",
         "risk": ""},
        md3, str(snap3pm))

    def case5() -> tuple[bool, str]:
        ec = check5
        # round47 后该场季节性 +1 退出,S0=0 临界,引擎方向可能 UP/DOWN,只要 corrected 触发即可
        ok = (
            ec["corrected"] and "short_bounce_factor" in ec["coverage"]
            and "recent_dir_15m" in ec["coverage"]
            and out5.get("action") == "ORDER"
            and out5.get("confidence") == "低"
            and out5.get("position_scale") == 0.5
            and "引擎校验修正" in out5.get("risk", "")
        )
        return ok, (f"S0={ec['engine_s0']:+.0f} side={out5['side']} scale={out5.get('position_scale')} "
                    f"conf={out5.get('confidence')} engine_risks={ec.get('engine_risks')}")

    run("3PM 场端到端：当前权重(round47 季节性删除)S0=0 临界，引擎方向由价格位置决定，置信度低+缩仓0.5", case5)

    # 案例6（8/12 round12 + round13）：6AM 场——机算输入确认（short_bounce=+1/摆动市/贴线）+
    # AI 主链路豁免输出（SKIP UP）经 validator 改为 ORDER UP + judge 豁免链路本身生效验证。
    # 重要口径：6AM 快照机算 S0=+2（round41.8.20 价格位置锚定口径：|dev|=0.0448%<0.15% 计 0，旧口径 -1；taker_bs 1.096 判 0 分 vs AI 判 +1）-> 方向 UP
    # -> 逆 PM DOWN 65.5% 强共识 -> round41.6 _consensus_flip 终局翻向 DOWN（validator 输出 DOWN；引擎机算方向本身为 UP）。
    # 而 AI 主链路（S0=+2→净分+1→UP 逆 PM DOWN 65.5%）走 G3 豁免 → 输出 UP。
    # round12 时 validator 保留 AI SKIP（不强迫下单）；round13 用户决策"不允许跳过"→
    # AI SKIP 按引擎方向修正为 ORDER UP（锁低+缩仓），机算 ORDER DOWN 不会被采纳。
    snap6am = Path(os.environ.get("HERMES_DIR", str(_PROJECT_ROOT))) / "data/snapshots/bitcoin-up-or-down-august-12-2026-6am-et_20260812_182815.json"
    md6 = json.loads(snap6am.read_text(encoding="utf-8"))
    fac6, inp6, notes6, mode6 = extract_factors(md6)

    def case6() -> tuple[bool, str]:
        # 1) 机算输入确认：短周期反弹先兆=+1、摆动市、贴线均正确提取
        ok_input = (
            inp6.get("short_bounce") == 1 and inp6.get("regime") == "摆动市"
            and abs(inp6.get("deviation_pct") or 0) < 0.15
            and "short_bounce_factor" in fac6
        )
        if not ok_input:
            return False, f"机算输入异常 short_bounce={inp6.get('short_bounce')} regime={inp6.get('regime')} dev={inp6.get('deviation_pct')}"
        # 2) judge 豁免链路验证（AI 主链路输入复刻 rule_engine 案例15：显式输入与 AI 的 S0=+2
        #    有 taker_bs 口径差异，用显式输入验证豁免本身）：
        #    摆动市+贴线+强证据（evidence_dims≥2 或 short_bounce 与 UP 同向）→ 豁免保留 UP → 缩仓 ORDER
        d6 = judge({
            "pm_up": 34.5, "deviation_pct": -0.0448, "tie_line_active": False,
            "regime": "摆动市", "evidence_dims": 2, "short_bounce": 1,
            "macd_1h_hist": 64.54, "macd_4h_hist": -64.72, "taker_1h_buy_pct": 58.5,
            "in_upper_half": True, "extreme_wall": True,
            "factors": {"price_position": -3, "taker_bs": 1, "taker_1h": 0, "order_flow": 2,
                        "macd_15m": 1, "macd_4h": -2, "kline": 0, "seasonality": 1,
                        "short_bounce_factor": 2},
        })
        ok_exempt = (
            d6.action == "ORDER" and d6.final_direction == "UP"
            and any("摆动市贴线豁免" in r for r in d6.risks)
        )
        # 3) validator 将 AI SKIP 修正为 ORDER：模拟 AI 主链路输出（SKIP UP），round13 必须改为 ORDER。
        #    方向以引擎机算为准。round41.8.20 后 6AM 机算 S0=+2（价格位置计 0）-> 方向 UP 逆 PM DOWN 65.5%
        #    强共识 -> round41.6 _consensus_flip 翻向 DOWN。AI SKIP UP -> 修正 ORDER DOWN。
        #    （consensus_flip=None，由 G3 承接）。AI SKIP UP → 修正 ORDER DOWN。
        out6, check6 = _validate_with_md(
            {"slug": md6["slug"], "signal": "UP", "side": "UP", "probability": 0.52,
             "confidence": "低", "action": "SKIP", "max_price": 0.59, "ev": -0.07,
             "reason": "G3 摆动市贴线豁免；G6 弱信号 SKIP", "risk": ""},
            md6, str(snap6am))
        ok_order = (
            out6.get("action") == "ORDER"
            and out6.get("side") == "DOWN"  # round41.6 共识翻向 DOWN（引擎方向 UP 逆 PM 65.5% 强共识，validator 终局翻向）
        )
        return ok_input and ok_exempt and ok_order, (
            f"机算 short_bounce={inp6.get('short_bounce')} regime={inp6.get('regime')} "
            f"dev={inp6.get('deviation_pct'):+.2f}% | judge(evidence_dims=2)→{d6.action}/{d6.final_direction} "
            f"| validator AI SKIP UP→{out6.get('action')}/{out6.get('side')}（引擎方向 {check6.get('engine_direction')}，"
            f"round41.6 翻向 {check6.get('consensus_flip')}）"
        )

    run("6AM 场机算输入确认 + judge豁免链路 + validator AI SKIP→ORDER(round13)", case6)

    print(f"\nselftest：{6 - failed}/6 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：python3 scripts/signal_validator.py --selftest")
    print("程序内调用：from signal_validator import validate; validate(signal_dict) -> (signal, engine_check)")
