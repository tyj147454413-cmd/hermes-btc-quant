"""复盘反思模块 — 逐小时结算已成交交易、更新统计与教训库。

8/9 修复: 文件头(imports + 常量 + 辅助函数)曾被误删导致 IndentationError 崩溃。
已按 data/*.json 结构与调用点重建。classify_regime/_make_pairs 复用 archive/backtest 实现，
force_refresh 复用 hermes_review_feedback。
"""
import json
import math
import os
import re
import sys
import time
try:
    import fcntl as _flock_mod
    _HAS_FCNTL = True
except ImportError:
    _flock_mod = None
    _HAS_FCNTL = False
# 9/2 修复: fcntl 与 portalocker 必须分开 try——合并时 Windows 上 fcntl 先崩,
# portalocker 不会被导入, _pocker=None 导致 _atomic_write 全部 AttributeError
try:
    import portalocker as _pocker
except ImportError:
    _pocker = None
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hermes_common as hc
from hermes_review_feedback import force_refresh  # 权重刷新（regime + source 隔离）

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR / "data"
DONE = DATA_DIR / "hermes_done.jsonl"
BOT_CONFIG = THIS_DIR / "hermes_config.json"  # A1(2026-08-23): 金额最终兜底来源
RESULTS = DATA_DIR / "hermes_results.jsonl"
SELLS = DATA_DIR / "hermes_sells.jsonl"
REVIEW_LOG = DATA_DIR / "review-log.md"
LESSONS_FILE = DATA_DIR / "review-lessons.json"
STATS_FILE = DATA_DIR / "review-stats.json"
REVIEWED_FILE = DATA_DIR / "hermes_reviewed.json"

# 会话时区划分（美东）: 亚洲盘 21-05, 欧盘 05-12, 美盘 12-21
SESSION_BANDS = [(21, "亚洲盘"), (12, "美盘"), (5, "欧盘")]  # 降序判定：>=21 亚洲盘, >=12 美盘, >=5 欧盘, else 亚洲盘


def now_et() -> datetime:
    return hc.now_et()


def log(msg: str, level: str = "I") -> None:
    hc.log(msg, level)


def _load_sells():
    """读取 sells.jsonl 建 slug→卖出记录映射（问题6: 止盈卖出感知，覆盖结算输赢判定）"""
    if not SELLS.exists():
        return {}
    m = {}
    try:
        for line in SELLS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = rec.get("slug")
            if s:
                m[s] = rec
    except OSError:
        pass
    return m


def _atomic_write(path: Path, content: str) -> None:
    """原子写入（排他锁），防止与 hermes_1h_bot.py 并发写丢数据。fcntl 可用走 fcntl,否则回退 portalocker。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(str(path) + ".lock", "w") as lf:
            if _HAS_FCNTL:
                _flock_mod.flock(lf.fileno(), _flock_mod.LOCK_EX)
            else:
                _pocker.lock(lf, _pocker.LOCK_EX)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
    except OSError:
        # 兜底：直接写（无锁环境）
        path.write_text(content, encoding="utf-8")


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return default


def load_stats() -> dict:
    stats = _load_json(STATS_FILE, {"total": 0, "wins": 0, "losses": 0, "skips": 0,
                                    "by_side": {}, "by_session": {}, "by_confidence": {},
                                    "by_ev": {}, "profit_loss": 0.0, "streak": {"current": 0, "max_win": 0, "max_loss": 0}})
    # 8/19: 成交口径统计（旧 stats 文件无此字段，向后兼容）
    stats.setdefault("filled_wins", 0)
    stats.setdefault("filled_losses", 0)
    stats.setdefault("filled_profit_loss", 0.0)
    return stats


def save_stats(stats: dict) -> None:
    _atomic_write(STATS_FILE, json.dumps(stats, ensure_ascii=False, indent=2))


def load_lessons() -> dict:
    return _load_json(LESSONS_FILE, {"lessons": [], "patterns": {}, "regime_patterns": {}, "pair_patterns": {}, "_processed_slugs": {}})


def save_lessons(lessons: dict) -> None:
    _atomic_write(LESSONS_FILE, json.dumps(lessons, ensure_ascii=False, indent=2))


def load_reviewed() -> set:
    return set(_load_json(REVIEWED_FILE, []))


def save_reviewed(reviewed: set) -> None:
    _atomic_write(REVIEWED_FILE, json.dumps(sorted(reviewed), ensure_ascii=False, indent=2))


def classify_regime(adx) -> str:
    """复用 backtest 实现。ADX>25 trending, <20 ranging, 其余 normal。"""
    if adx is None:
        return "normal"
    try:
        adx = float(adx)
    except (TypeError, ValueError):
        return "normal"
    if adx > 25:
        return "trending"
    elif adx < 20:
        return "ranging"
    return "normal"


def _make_pairs(indicators: list[str]) -> list[str]:
    if len(indicators) < 2:
        return []
    sorted_inds = sorted(set(indicators))
    return [f"{sorted_inds[i]}&{sorted_inds[j]}" for i in range(len(sorted_inds)) for j in range(i + 1, len(sorted_inds))]


def get_session_name(hour: int) -> str:
    """按美东小时返回会话名: 亚洲盘 21-05, 欧盘 05-12, 美盘 12-21。"""
    for band_hour, name in SESSION_BANDS:
        if hour >= band_hour:
            return name
    return "亚洲盘"


def get_confidence_band(prob: float) -> str:
    """置信度分带: >=70 high, >=55 mid, else low。

    入口归一化防御：历史 review-lessons 存在 0-1 小数与百分数混用
    （156 条小数 + 42 条百分数 + 2 条字符串），若不归一全部落入 low 档
    导致 by_confidence 统计失真（8/11 报告 D2、8/13 诊断确认）。
    <=1 视为小数，统一 ×100 转为百分数；百分数输入恒等不变。
    """
    try:
        prob = float(prob)
    except (TypeError, ValueError):
        return "low"
    if prob <= 1:
        prob *= 100
    if prob >= 70:
        return "high"
    elif prob >= 55:
        return "mid"
    return "low"


def notify(title: str, body: str) -> None:
    """系统通知（macOS osascript），失败静默。"""
    try:
        os.system(f'osascript -e \'display notification "{body}" with title "{title}"\'')
    except OSError:
        pass


def _slug_to_end_utc(slug: str) -> int | None:
    """从 slug 解析时段结束的 UTC 时间戳。

    slug 形如 bitcoin-up-or-down-august-8-2026-7am-et → 7am ET → UTC 11:00 时段结束(12:00 UTC)。
    失败返回 None。
    """
    m = re.search(r"-(august|january|february|march|april|may|june|july|september|october|november|december)-(\d+)-(\d+)-(\d+)(am|pm)-et", slug)
    if not m:
        return None
    month_str, day, year, hour, ap = m.groups()
    months = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
              "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
    month = months.get(month_str)
    if not month:
        return None
    hour = int(hour)
    if ap == "pm" and hour != 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    try:
        from datetime import datetime as _dt
        et_dt = _dt(int(year), month, int(day), hour, 0).astimezone()  # 本地时区兜底
        # 直接用 pytz 风格: 美东是 UTC-4/-5。简化用 UTC-4（夏令时 8 月为 EDT）
        import zoneinfo
        et_tz = zoneinfo.ZoneInfo("America/New_York")
        et_dt = _dt(int(year), month, int(day), hour, 0, tzinfo=et_tz)
        return int(et_dt.timestamp() // 3600 * 3600) + 3600  # 时段结束 = ET 整点后 1h
    except Exception:
        return None


def _parse_end_utc(end_date: str | None, slug: str = "", sig_ts: str = "") -> int | None:
    """解析市场结算时间的 UTC 时间戳，按优先级: endDate(ISO) → slug → sig_ts。

    - endDate: GAMMA 返回的 ISO 格式（如 2026-08-14T08:00:00Z），最权威。
    - slug:    形如 ...-3am-et → 时段结束 = ET 整点后 1h。
    - sig_ts:  信号 UTC 时间戳 → 时段结束 = 下个整点 UTC。
    全部解析失败返回 None。
    """
    if end_date:
        try:
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            pass
    if slug:
        end = _slug_to_end_utc(slug)
        if end:
            return end
    if sig_ts:
        try:
            dt = datetime.fromisoformat(sig_ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() // 3600 * 3600) + 3600  # 时段结束 = 下个整点 UTC
        except Exception:
            return None
    return None


def _binance_settle(anchor: float, sig_ts: str, slug: str = "") -> tuple[str | None, str]:
    """币安回溯兜底结算：按 PM 官方规则判定涨跌。

    PM 官方规则（市场 description 原文）: Binance BTC/USDT 该小时 1H K线
    close >= open → "Up"，否则 "Down"。9/5 口径升级：直接拉该 1H K线对比 O/C，
    与 PM 官方结算完全同口径；anchor 参数仅作 1H K线拉取失败时的降级依据
    （anchor vs 1m 末根收盘，近似口径）。

    时段由 sig_ts（UTC ISO）或 slug（如 7am-et → ET 08:00 结束）解析。
    返回 (actual_side, desc)。GAMMA 查不到旧 slug 或 GAMMA 未关闭残价时使用。

    8/14 修复: 增加时间守卫——未到结算时间不判，避免盘中实时价被当结算价。
    """
    end_utc = _parse_end_utc(None, slug, sig_ts)
    if end_utc is None:
        return None, "无法解析时段（缺 timestamp 且 slug 格式不识别）"
    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts < end_utc:
        return None, f"未到结算时间 end_utc={end_utc}"
    start_utc = end_utc - 3600
    # 主口径: 该小时 1H K线 O/C（PM 官方结算同口径，close >= open → UP）
    try:
        url_1h = (f"{hc.BINANCE_API}/api/v3/klines?symbol=BTCUSDT&interval=1h"
                  f"&startTime={start_utc * 1000}&limit=1")
        d = hc.http_get_json(url_1h, timeout=10, proxy=hc.PROXY)
        if d and isinstance(d, list) and len(d) >= 1:
            o, c = float(d[0][1]), float(d[0][4])
            if c >= o:
                return "UP", f"币安1H O/C {c:.2f}>={o:.2f}（PM 同口径）"
            return "DOWN", f"币安1H O/C {c:.2f}<{o:.2f}（PM 同口径）"
    except Exception:
        pass  # 落入下方 anchor 降级口径
    if not anchor:
        return None, "缺 anchor_price，无法回溯"
    try:
        url = f"{hc.BINANCE_API}/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={start_utc*1000}&endTime={end_utc*1000}&limit=60"
        d = hc.http_get_json(url, timeout=10, proxy=hc.PROXY)
        if not d or len(d) == 0:
            return None, "币安回溯无数据"
        close = float(d[-1][4])
        if close > anchor * 1.00001:
            return "UP", f"币安回溯收盘 {close:.2f} > 锚定 {anchor:.2f}"
        elif close < anchor * 0.99999:
            return "DOWN", f"币安回溯收盘 {close:.2f} < 锚定 {anchor:.2f}"
        return None, f"币安回溯收盘 {close:.2f} ≈ 锚定 {anchor:.2f}（持平）"
    except Exception as e:
        return None, f"币安回溯异常: {e}"


def get_actual_result(slug: str, anchor: float | None, sig_ts: str) -> tuple[str | None, str]:
    """判断实际涨跌结果。

    8/14 修复（结算误判 bug）: 不再用"outcomePrices>0.9 纯价格阈值"当结算依据——
    盘中一侧领先至 90%+ 会被误当已结算（如 8/14 3am 场 UP 实时价 0.925 被误判已赢，
    实际 DOWN 赢）。改为:
    1. 时间守卫: 解析市场 endDate（缺失回退 slug/sig_ts），未到结算时间直接返回未结算，
       即使 outcomePrices>0.9 也不判。
    2. 已过结算时间后，outcomePrices 为最终结算价（归 0/1），>0.5 判对应侧赢。
    3. 无法解析 endDate 或 GAMMA 无数据时，币安回溯兜底（同样受时间守卫约束）。
    返回 (actual_side, desc): UP/DOWN/None(未结算)/PARTIAL/SKIP_REVIEW
    """
    if not slug:
        return None, "无 slug"
    # 8/19 修复: 老 BTC 1H slug 被 GAMMA 清理后 query 形式返回空 list（8/18 全天卡结算根因），
    # path 形式 markets/slug/{slug} 仍可查到（同 dashboard.py get_market_cached 的 8/17 修复）。
    # 注意返回类型不同: path → dict, query → list。
    market = None
    try:
        m = hc.http_get_json(f"{hc.GAMMA_API}/markets/slug/{slug}", timeout=8, proxy=hc.PROXY)
        if isinstance(m, dict):
            market = m
    except Exception:
        market = None
    if not market:
        try:
            m2 = hc.http_get_json(f"{hc.GAMMA_API}/markets?slug={slug}", timeout=8, proxy=hc.PROXY)
            if m2 and isinstance(m2, list) and len(m2) > 0:
                market = m2[0]
        except Exception:
            market = None
    if market:
        # 时间守卫: 未过 endDate → 未结算（即使价格>0.9 也不判）
        end_utc = _parse_end_utc(market.get("endDate"), slug, sig_ts)
        now_ts = datetime.now(timezone.utc).timestamp()
        if end_utc and now_ts < end_utc:
            return None, f"未到结算时间 endDate={market.get('endDate')}"
        prices_raw = market.get("outcomePrices", "[]")
        try:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
        except (json.JSONDecodeError, TypeError):
            prices = []
        if len(prices) >= 2:
            try:
                up_p = float(prices[0])
                down_p = float(prices[1])
            except (TypeError, ValueError):
                up_p = down_p = None
            if up_p is not None and down_p is not None:
                closed = bool(market.get("closed"))
                # 9/3 错账根因: 收盘后 2 分钟复盘读到收盘前残价(DOWN=0.545>0.5)
                # 被误判 DOWN 赢，实际几分钟后才归 1(UP 赢)。必须等官方 closed=True
                # 或一侧已归 1(>0.9) 才判结算，裸 >0.5 残价阈值不再作数。
                # 9/5 7am-ET 错账根因(二次): 收盘前残价可冲到 0.9+(UP=0.945)，
                # >0.9 依然被误判已结算——实际最后一分钟反转 DOWN 赢，官方几分钟后才归 1。
                # 裸价格阈值(无论 0.5/0.9)对"未 closed 市场"都不可靠：
                # 只有 closed=True 或价格严格归 1(>=0.999) 才是结算证据；
                # 其余一律走 Binance 1H K线 O/C 兜底(PM 官方结算同口径)。
                settled = closed or up_p >= 0.999 or down_p >= 0.999
                if not settled:
                    b_side, b_desc = _binance_settle(anchor, sig_ts, slug)
                    if b_side:
                        return b_side, (f"{b_desc} | GAMMA 未关闭残价 "
                                        f"(UP={up_p:.3f}/DOWN={down_p:.3f}) 不作结算依据")
                    return None, (f"市场未关闭且残价未归1 (UP={up_p:.3f}/DOWN={down_p:.3f})，"
                                  f"Binance 兜底失败: {b_desc}")
                if up_p > 0.9:
                    return "UP", f"GAMMA 结算 UP 价格{up_p:.4f}"
                if down_p > 0.9:
                    return "DOWN", f"GAMMA 结算 DOWN 价格{down_p:.4f}"
                # closed=True 但两侧仍居中（罕见）: 按优势侧兜底，持平则保守不判
                if up_p > down_p:
                    return "UP", f"GAMMA 结算 UP 价格{up_p:.4f}"
                if down_p > up_p:
                    return "DOWN", f"GAMMA 结算 DOWN 价格{down_p:.4f}"
                return None, f"GAMMA 结算持平 (UP={up_p:.3f}/DOWN={down_p:.3f})"
    # GAMMA 查不到 → 币安回溯兜底（8/9 方案A，含时间守卫）
    return _binance_settle(anchor, sig_ts, slug)


def backfill_validate(slug: str, actual_side: str) -> None:
    """回溯填充 :40 验证记录。结算后标记 settled 和 correct（若存在 validate 文件则更新，否则静默）。"""
    try:
        vfile = DATA_DIR / "hermes_validate.jsonl"
        if not vfile.exists():
            return
        lines = []
        with open(vfile, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("slug") == slug:
                    e["settled"] = True
                    e["correct"] = (e.get("side") == actual_side)
                lines.append(json.dumps(e, ensure_ascii=False))
        _atomic_write(vfile, "\n".join(lines) + ("\n" if lines else ""))
    except OSError:
        pass


def analyze_indicators(signal: dict, actual_side: str) -> tuple[list[str], list[str], list[str]]:
    """对比信号 reason 中的指标与方向，归因正确/错误贡献的指标。

    简单启发式: 若方向对，reason 中提到的指标记入 correct；方向错记入 wrong。
    """
    reason = signal.get("reason", "") or ""
    side = signal.get("side", "")
    # 指标白名单（reason 里出现的指标词）
    INDICATORS = ["MACD", "RSI", "量价", "订单流", "Taker", "K线", "BB", "VWAP", "趋势", "支撑", "阻力", "庄家", "PM", "订单簿", "贴线", "偏差", "波动率", "恐贪", "季节性", "V反", "诱多", "诱空"]
    mentioned = []
    for ind in INDICATORS:
        if ind.lower() in reason.lower():
            mentioned.append(ind)
    correct = list(mentioned) if side == actual_side else []
    wrong = list(mentioned) if side != actual_side else []
    analysis = []
    if side == actual_side:
        analysis.append(f"方向判断正确，reason 提及指标 {len(mentioned)} 个")
    else:
        analysis.append(f"方向判断错误（预测{side}实际{actual_side}），reason 提及指标 {len(mentioned)} 个")
    if signal.get("probability", 0):
        analysis.append(f"置信度 {signal['probability']}%，EV {signal.get('ev', 0)}")
    return correct, wrong, analysis


def _get_source_bucket(regime_bucket, regime, source):
    """8/9 修复: 文件头被误删的 def 行恢复。按 regime + source 隔离的 bucket 获取，含 legacy 迁移。

    注意: 调用处为 _get_source_bucket(bucket_dict, regime, source) 3 参数（regime 用于 legacy 迁移判定）。
    """
    if "legacy" not in regime_bucket and source != "legacy":
        old_c = regime_bucket.pop("correct", 0)
        old_w = regime_bucket.pop("wrong", 0)
        regime_bucket["legacy"] = {"correct": old_c, "wrong": old_w}
    source_bucket = regime_bucket.setdefault(source, {"correct": 0, "wrong": 0})
    return source_bucket


def update_lessons(lessons, signal, actual_side, correct, wrong):
    """更新教训规则库（v6 — source 隔离 + 对单写lesson + 蒙对识别）

    v6 改动（2026-08-02）:
    1. patterns/regime_patterns/pair_patterns 按 source 隔离（v5/AI/fallback/legacy）
       解决: AI 时代对的 RSI + v5 时代错的 RSI 混在一起，Wilson 下界被陈旧 correct 稀释
    2. 对单也写 lesson（含 correct_indicators + source + weak_correct）
       解决: 只记错不记对，无法强化正确路径
    3. 蒙对识别：方向对但决策层有错时，correct 不累计，只记 wrong
       解决: 3am 那种"L2 返回 None 却顶着高置信度蒙对"被当成 correct 强化错误路径

    历史:
    - 漏洞6: _processed_slugs 跟踪 correct/wrong，重复复盘时先回滚
    - v4: regime_patterns 按 ADX 分桶
    - v5: pair_patterns 组合效应追踪
    """
    side = signal.get("side", "")
    slug = signal.get("slug", "")
    source = signal.get("source", "legacy") or "legacy"
    prediction_correct = (side == actual_side)

    patterns = lessons.get("patterns", {})
    regime_patterns = lessons.get("regime_patterns", {})
    pair_patterns = lessons.get("pair_patterns", {})
    lessons_list = lessons.get("lessons", [])

    # 当前 regime
    adx = signal.get("v5_adx")
    regime = classify_regime(adx)

    # 蒙对识别：方向对但 correct 被清空（analyze_indicators 中 weak_correct 逻辑）
    weak_correct = prediction_correct and not correct and bool(wrong)

    # 生成指标对
    correct_pairs = _make_pairs(correct)
    wrong_pairs = _make_pairs(wrong)

    # 漏洞6: 用 _processed_slugs 跟踪每个 slug 上次贡献的 correct/wrong，重复复盘时先回滚
    processed = lessons.setdefault("_processed_slugs", {})
    if not isinstance(processed, dict):
        processed = {}
        lessons["_processed_slugs"] = processed

    if slug in processed:
        prev = processed[slug]
        prev_source = prev.get("source", "legacy") or "legacy"
        # 回滚上次的 correct 计数（patterns 全局汇总）
        for ind in prev.get("correct", []):
            if ind in patterns and patterns[ind].get("correct", 0) > 0:
                patterns[ind]["correct"] -= 1
        for ind in prev.get("wrong", []):
            if ind in patterns and patterns[ind].get("wrong", 0) > 0:
                patterns[ind]["wrong"] -= 1
        # 回滚 regime_patterns（按 source 桶回滚）
        prev_regime = prev.get("regime", "")
        if prev_regime:
            for ind in prev.get("correct", []):
                bucket = _get_source_bucket(regime_patterns.setdefault(ind, {}), prev_regime, prev_source)
                if bucket.get("correct", 0) > 0:
                    bucket["correct"] -= 1
            for ind in prev.get("wrong", []):
                bucket = _get_source_bucket(regime_patterns.setdefault(ind, {}), prev_regime, prev_source)
                if bucket.get("wrong", 0) > 0:
                    bucket["wrong"] -= 1
            # Phase 3: 回滚 pair_patterns（按 source 桶）
            prev_correct_pairs = _make_pairs(prev.get("correct", []))
            prev_wrong_pairs = _make_pairs(prev.get("wrong", []))
            for pair in prev_correct_pairs:
                bucket = _get_source_bucket(pair_patterns.setdefault(pair, {}), prev_regime, prev_source)
                if bucket.get("correct", 0) > 0:
                    bucket["correct"] -= 1
            for pair in prev_wrong_pairs:
                bucket = _get_source_bucket(pair_patterns.setdefault(pair, {}), prev_regime, prev_source)
                if bucket.get("wrong", 0) > 0:
                    bucket["wrong"] -= 1
        # 从 lessons_list 中移除该 slug 的旧 lesson
        lessons_list = [l for l in lessons_list if l.get("slug") != slug]

    # 重新统计当前交易的 patterns（全局汇总，不分 source）
    for ind in correct:
        patterns.setdefault(ind, {"correct": 0, "wrong": 0})["correct"] += 1
    for ind in wrong:
        patterns.setdefault(ind, {"correct": 0, "wrong": 0})["wrong"] += 1

    # v6: regime_patterns 按 regime + source 分桶统计
    for ind in correct:
        bucket = _get_source_bucket(regime_patterns.setdefault(ind, {}), regime, source)
        bucket["correct"] += 1
    for ind in wrong:
        bucket = _get_source_bucket(regime_patterns.setdefault(ind, {}), regime, source)
        bucket["wrong"] += 1

    # Phase 3: pair_patterns 组合效应统计（regime + source 分桶）
    for pair in correct_pairs:
        bucket = _get_source_bucket(pair_patterns.setdefault(pair, {}), regime, source)
        bucket["correct"] += 1
    for pair in wrong_pairs:
        bucket = _get_source_bucket(pair_patterns.setdefault(pair, {}), regime, source)
        bucket["wrong"] += 1

    # 记录本次贡献，供下次回滚使用（含 regime + source 信息）
    processed[slug] = {
        "correct": list(correct), "wrong": list(wrong),
        "regime": regime, "source": source,
    }

    # v6: 对单和错单都写 lesson（含 correct_indicators + source + weak_correct）
    lesson = {
        "date": signal.get("timestamp", ""),
        "slug": slug,
        "side": side,
        "actual": actual_side,
        "confidence": signal.get("probability", 0),
        "ev": signal.get("ev", 0),
        "correct_indicators": list(correct),  # v6: 对单也记录正确归因
        "wrong_indicators": list(wrong),
        "source": source,                    # v6: 区分 v5/AI/fallback
        "weak_correct": weak_correct,        # v6: 蒙对标记
        "reason": signal.get("reason", "")[:200],
        "regime": regime,
    }
    lessons_list.append(lesson)
    if len(lessons_list) > 200:
        lessons_list = lessons_list[-200:]

    lessons["lessons"] = lessons_list
    lessons["patterns"] = patterns
    lessons["regime_patterns"] = regime_patterns
    lessons["pair_patterns"] = pair_patterns
    lessons["_processed_slugs"] = processed


def _safe_update_stat_bucket(bucket, side, key, won):
    """安全更新统计分桶，防止 KeyError"""
    if side not in bucket:
        bucket[side] = {"wins": 0, "losses": 0}
    if won:
        bucket[side]["wins"] += 1
    else:
        bucket[side]["losses"] += 1


def update_stats(stats, signal, actual_side, prediction_correct, pnl, filled=None):
    """更新统计数据。
    8/19: 现有 total/wins/losses/profit_loss/by_* 保持全量分析口径（历史兼容）；
    新增 filled_wins/filled_losses/filled_profit_loss 只累计真实成交单（filled=True）。
    filled: True=已成交 / False=未成交 / None=未知（未知不计入成交口径）。
    """
    stats["total"] += 1
    side = signal.get("side", "UP")
    if side not in ("UP", "DOWN"):
        side = "UP"  # 容错：异常 side 归入 UP
    session = signal.get("session", "")
    if session not in ("亚洲盘", "欧盘", "美盘"):
        session = get_session_name(now_et().hour)
    prob = signal.get("probability", 0) or 0
    try:
        prob = float(prob)
    except (TypeError, ValueError):
        prob = 0

    if prediction_correct:
        stats["wins"] += 1
        _safe_update_stat_bucket(stats["by_side"], side, "wins", True)
        _safe_update_stat_bucket(stats["by_session"], session, "wins", True)
        _safe_update_stat_bucket(stats["by_confidence"], get_confidence_band(prob), "wins", True)
        stats["profit_loss"] += pnl
        stats["streak"]["current"] = max(1, stats["streak"]["current"] + 1) if stats["streak"]["current"] >= 0 else 1
        stats["streak"]["max_win"] = max(stats["streak"]["max_win"], stats["streak"]["current"])
        if filled is True:
            stats["filled_wins"] += 1
            stats["filled_profit_loss"] += pnl
    else:
        stats["losses"] += 1
        _safe_update_stat_bucket(stats["by_side"], side, "losses", False)
        _safe_update_stat_bucket(stats["by_session"], session, "losses", False)
        _safe_update_stat_bucket(stats["by_confidence"], get_confidence_band(prob), "losses", False)
        stats["profit_loss"] += pnl  # pnl 已经是负数
        stats["streak"]["current"] = min(-1, stats["streak"]["current"] - 1) if stats["streak"]["current"] <= 0 else -1
        stats["streak"]["max_loss"] = max(stats["streak"]["max_loss"], abs(stats["streak"]["current"]))
        if filled is True:
            stats["filled_losses"] += 1
            stats["filled_profit_loss"] += pnl


def write_review_log(signal, actual_side, actual_desc, correct, wrong, analysis, prediction_correct):
    """写入人类可读的复盘日志"""
    slug = signal.get("slug", "")
    side = signal.get("side", "")
    ts = signal.get("timestamp", "")
    prob = signal.get("probability", 0) or 0
    ev = signal.get("ev", 0) or 0
    amount = signal.get("amount", 0) or 0
    price = signal.get("max_price", 0) or 0
    reason = signal.get("reason", "")

    result_icon = "✅" if prediction_correct else "❌"
    result_text = "预测正确" if prediction_correct else "预测错误"

    entry = f"""
## {ts} | {slug}

**预测**: {side} | 置信度={prob}% | EV=${ev} | 金额=${amount} | 限价={price}
**实际**: {actual_side} | {actual_desc}
**结果**: {result_icon} {result_text}

### 信号原因
{reason}

### 指标分析
- 正确贡献: {", ".join(correct) if correct else "无"}
- 错误贡献: {", ".join(wrong) if wrong else "无"}
"""
    # ---- 复盘 5W（btc-trading-mindset 单次复盘模板，8/17 加入；只改复盘输出，不动风控）----
    entry += "### 复盘反思（5W）\n"
    # 1. What 实际发生
    entry += f"**What（实际发生）**: {actual_side} | {actual_desc}\n"
    # 2. Whether 对错
    entry += f"**Whether（对错）**: 预测 {side}，实际 {actual_side} → {result_icon} {result_text}\n"
    # 3. Why 为什么（最重要）：归因 + 置信度/EV 复盘
    why_parts = []
    if prediction_correct and correct:
        why_parts.append(f"方向对，靠 {', '.join(correct)} 命中")
    elif not prediction_correct and wrong:
        why_parts.append(f"方向错，{', '.join(wrong)} 误导")
    else:
        why_parts.append("方向与归因待结合即时盘面核对")
    if not prediction_correct and prob > 75:
        why_parts.append(f"过度自信（置信度{prob}%）→ 需降低高置信度阈值或增加矛盾信号权重")
    if not prediction_correct and ev > 0.10:
        why_parts.append(f"EV 高估（${ev}）但实际亏损 → 概率估计需校准")
    entry += f"**Why（为什么，最重要）**: {'；'.join(why_parts)}\n"
    for a in analysis:
        entry += f"  - {a}\n"
    # 4. What-if 重来怎么做（针对性，避免空话）
    if not prediction_correct:
        if prob > 70:
            whatif = "若重来：降置信度（不追高概率做单向），对矛盾信号权重加严，宁可 SKIP"
        else:
            whatif = "若重来：保留方向但缩仓/锁低，等待价格位置与共识方向二次确认"
    else:
        whatif = "若重来：保持方向，但警惕同形态已获利后的边际衰减，不追加"
    entry += f"**What-if（重来怎么做）**: {whatif}\n"
    # 5. What's-next 写成可执行规则
    if not prediction_correct:
        cond = f"当置信度>{prob}%" if prob > 70 else "当盘面信号与本场相似"
        entry += f"**What's-next（写成可执行规则）**: 规则={cond} 时，{whatif}；并作为新反例入 selftest 锁定。\n"
    else:
        cond = ", ".join(correct) if correct else "本场形态"
        entry += f"**What's-next（写成可执行规则）**: 规则=当{cond}同向成立时保持方向；已 2 次+同错反例则收紧。\n"
    entry += "---\n"

    with open(REVIEW_LOG, "a", encoding="utf-8") as f:
        f.write(entry)


def get_unreviewed_trades():
    """获取已成交但未复盘的交易（逐行容错，损坏行不影响后续）

    漏洞1 修复: 同 slug 可能有多笔（PAPER_ 单 + 真实单），需 slug 级去重，
    否则 stats.total/wins/losses/profit_loss 会被加 2 次。
    规则: 同 slug 已有记录时，若新记录是 PAPER_ 单则跳过；
          若新记录是真实单，则覆盖之前的 PAPER_ 单（移除 paper 单并把其 order_id 加入 reviewed）。
    """
    reviewed = load_reviewed()
    if not DONE.exists():
        return []

    trades = []
    seen_slugs = set()
    with open(DONE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # 跳过损坏行，不中断
            sig = entry.get("signal", {})
            res = entry.get("result", {})
            oid = res.get("order_id", "")
            slug = sig.get("slug", "")
            if oid and oid not in reviewed and res.get("ok") and not res.get("skipped"):
                if slug in seen_slugs:
                    # 同 slug 已有记录
                    if oid.startswith("PAPER_"):
                        # 新记录是 PAPER_ 单 → 跳过（保留已记录的真实单）
                        reviewed.add(oid)
                        continue
                    # 新记录是真实单 → 覆盖之前的 PAPER_ 单
                    for t in trades[:]:
                        if t[0].get("slug") == slug and t[2].startswith("PAPER_"):
                            reviewed.add(t[2])
                            trades.remove(t)
                            break
                seen_slugs.add(slug)
                trades.append((sig, res, oid))
    return trades


def print_summary():
    """打印累计统计"""
    stats = load_stats()
    lessons = load_lessons()

    total = stats.get("total", 0)
    if total == 0:
        log("暂无复盘数据")
        return

    win_rate = stats["wins"] / total * 100 if total > 0 else 0
    log(f"\n📊 累计统计（全量分析口径，含未成交）:")
    log(f"  总交易: {total} | 胜: {stats['wins']} | 负: {stats['losses']} | 胜率: {win_rate:.1f}%")
    log(f"  盈亏: ${stats['profit_loss']:.1f}")
    # 8/19: 实际成交口径（只算真实成交单，未成交 pnl=0 不入盈亏）
    f_total = stats.get("filled_wins", 0) + stats.get("filled_losses", 0)
    if f_total > 0:
        f_wr = stats.get("filled_wins", 0) / f_total * 100
        log(f"\n💰 实际成交战绩（仅成交单）:")
        log(f"  成交: {f_total} | 胜: {stats.get('filled_wins', 0)} | 负: {stats.get('filled_losses', 0)} | 胜率: {f_wr:.1f}%")
        log(f"  盈亏: ${stats.get('filled_profit_loss', 0.0):.1f}")
        log(f"  未成交: {total - f_total} 笔（分析口径计入，无真实盈亏）")
    streak = stats.get("streak", {})
    log(f"  连胜/连负: {streak.get('current', 0)} (最大连胜{streak.get('max_win', 0)}/最大连负{streak.get('max_loss', 0)})")

    for side in ["UP", "DOWN"]:
        s = stats["by_side"].get(side, {"wins": 0, "losses": 0})
        t = s["wins"] + s["losses"]
        if t > 0:
            log(f"  {side}: {s['wins']}/{t} = {s['wins']/t*100:.0f}%")

    for session in ["亚洲盘", "欧盘", "美盘"]:
        s = stats["by_session"].get(session, {"wins": 0, "losses": 0})
        t = s["wins"] + s["losses"]
        if t > 0:
            log(f"  {session}: {s['wins']}/{t} = {s['wins']/t*100:.0f}%")

    patterns = lessons.get("patterns", {})
    if patterns:
        log(f"\n📋 指标准确率:")
        for ind, p in sorted(patterns.items(), key=lambda x: x[1].get("correct", 0) + x[1].get("wrong", 0), reverse=True):
            total_ind = p.get("correct", 0) + p.get("wrong", 0)
            if total_ind > 0:
                acc = p["correct"] / total_ind * 100
                log(f"  {ind}: {p['correct']}/{total_ind} = {acc:.0f}%")

    recent_lessons = lessons.get("lessons", [])[-5:]
    if recent_lessons:
        log(f"\n⚠️ 最近 {len(recent_lessons)} 条教训:")
        for l in recent_lessons:
            log(f"  {l.get('date', '')[:16]} | {l.get('side', '')}→{l.get('actual', '')} | 置信度{l.get('confidence', 0)}% | 错误指标: {', '.join(l.get('wrong_indicators', []))}")


def _wilson_lower(w: int, n: int, z: float = 1.96) -> float:
    """Wilson score 95% 下界（%）。n=0 返回 0。小样本下比 Wald 正态近似可靠。"""
    if n <= 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, (c - h) * 100)


def check_round37_fallback():
    """8/21 A2: round37 条件封顶回退线 Wilson 化。

    子组 = filled=True 且 buy_price>=0.65 且 probability_raw>=0.70（bot 0.70 高概率通道实际成交单）。
    触发条件（原为点估计跌破 65%，小样本易乱触发）: n>=20 且 Wilson95 下界 < 65%。
    触发后追加告警到 data/drift_alerts.jsonl（type=round37_fallback），由周度监控消费。
    注: praw 仅 round36(8/21) 后的信号才有值，早期样本自动落不进子组——正是按版本分段的意图。
    """
    sub = []
    try:
        if RESULTS.exists():
            with open(RESULTS, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("filled") is not True:
                        continue
                    try:
                        praw = float(e.get("probability_raw") or 0)
                        bp = float(e.get("buy_price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if praw >= 0.70 and bp >= 0.65:
                        sub.append(e.get("side") == e.get("actual"))
    except OSError:
        return

    n = len(sub)
    if n == 0:
        return
    w = sum(1 for x in sub if x)
    wr = w / n * 100
    lo = _wilson_lower(w, n)
    log(f"\n🎯 round37 高概率通道监控: praw>=0.70 ∩ 成交价>=0.65 | n={n} 胜率={wr:.1f}% Wilson95下界={lo:.1f}%")
    if n >= 20 and lo < 65.0:
        alert = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "type": "round37_fallback",
            "cell": "praw>=0.70 × buy_price>=0.65 × filled",
            "n": n, "win_rate": round(wr, 1),
            "wilson95_lower": round(lo, 1),
            "verdict": "回退线触发：Wilson下界<65%，建议 HIGH_PROB_CAP 0.70→0.60（BOSS 决策）",
        }
        try:
            with open(DATA_DIR / "drift_alerts.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(alert, ensure_ascii=False) + "\n")
            log(f"  🚨 回退线触发，告警已写 drift_alerts.jsonl：{alert['verdict']}")
        except OSError:
            pass
    elif n < 20:
        log(f"  （n<20 未达触发门槛，持续观察——Wilson 区间尚宽）")


def main():
    current_et = now_et()
    log(f"=== 复盘反思启动 | ET {current_et.strftime('%H:%M')} ===")

    trades = get_unreviewed_trades()
    if not trades:
        log("无新交易需要复盘")
        print_summary()
        check_round37_fallback()
        return

    log(f"待复盘交易: {len(trades)} 笔")

    # 批量加载 stats 和 lessons（避免每笔交易都读写文件）
    stats = load_stats()
    lessons = load_lessons()

    reviewed = load_reviewed()
    # 8/19 修复: results.jsonl 与 reviewed.json 可能不同步（如手动补结算只写 results）。
    # 未同步时 main 会重复结算 → results 重复追加 + stats 重复累计 pnl 污染统计。
    # 启动时把 results 已有 order_id 合并进 reviewed，保证幂等。
    try:
        if RESULTS.exists():
            with open(RESULTS, encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _oid = json.loads(_line).get("order_id")
                        if _oid:
                            reviewed.add(_oid)
                    except Exception:
                        pass
    except Exception:
        pass
    sells_map = _load_sells()  # 问题6: 提前卖出记录（覆盖结算输赢 pnl）
    # 8/19: 成交状态映射（bot 写入的订单终态，回填脚本补了历史）。
    # filled=False 的单方向对错照常结算（分析口径），但 pnl=0（无真实盈亏）。
    fills_map = {}
    fills_file = Path(__file__).resolve().parent / "data" / "hermes_fills.jsonl"
    if fills_file.exists():
        try:
            with open(fills_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("order_id"):
                            # A3: 存整条 fills 记录（含 size_matched/price），支撑部分成交 PnL
                            fills_map[r["order_id"]] = r
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
    # 8/23 修复(实盘记账): PENDING 文件带真实下单金额(bot 下单时写入)。
    # AI 信号层无 amount 字段，历史 LIVE 单金额只能从这里回补。
    pending_amount_map = {}
    pending_file = Path(__file__).resolve().parent / "data" / "hermes_pending.jsonl"
    if pending_file.exists():
        try:
            with open(pending_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("order_id") and r.get("amount"):
                            pending_amount_map[r["order_id"]] = float(r["amount"])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
        except OSError:
            pass
    reviewed_count = 0
    for signal, result, order_id in trades:
        slug = signal.get("slug", "")
        side = signal.get("side", "")
        anchor = signal.get("anchor_price")
        sig_ts = signal.get("timestamp", "")

        # 8/14 修复: 时段结束守卫（双保险）——未到该场结束时间直接跳过，避免场内提前结算。
        # get_actual_result 内部已有 endDate 守卫，这里是提前拦截，省一次 GAMMA 查询。
        _end_utc = _parse_end_utc(None, slug, sig_ts)
        if _end_utc and datetime.now(timezone.utc).timestamp() < _end_utc:
            log(f"  ⏳ {slug} 未到结算时间 end_utc={_end_utc}，跳过")
            continue

        actual_side, actual_desc = get_actual_result(slug, anchor, sig_ts)
        if not actual_side:
            log(f"  ⏳ {slug} 尚未结算 ({actual_desc})")
            continue

        # 重复结算防护（v6 修复）:
        # get_unreviewed_trades 已做 reviewed 去重，但在并发/重启场景下仍可能漏过
        # （如 07:22 复盘 1am，save_reviewed 未完成时 07:49 又跑一次）
        # 这里做二次检查：如果 order_id 已在 reviewed 中，跳过不写 results
        if order_id in reviewed:
            log(f"  ⏭️ {slug} 已复盘（order_id 在 reviewed 中），跳过")
            continue

        # N1: 部分结算或连续失败 — 标记为已复盘但不计入统计
        if actual_side in ("PARTIAL", "SKIP_REVIEW"):
            log(f"  ⏭️ {slug} {actual_desc}，标记已复盘跳过")
            reviewed.add(order_id)
            reviewed_count += 1
            continue

        prediction_correct = (side == actual_side)

        # 回溯填充 :40 验证记录（合约结算后，标记 settled 和 correct）
        backfill_validate(slug, actual_side)

        correct, wrong, analysis = analyze_indicators(signal, actual_side)

        # 8/23 修复(实盘记账): 金额回退链 signal(AI 不写) → bot result(8/23 起携带) → PENDING 文件(历史单)
        # A1(2026-08-23): 链尾加 bot config 兜底。原链全空时落 0，把真实成交单 pnl 记成 ±0
        # (8/10 起曾有 149 笔)。空值读 config，保证「已成交单金额>0」。
        amount = signal.get("amount", 0) or 0
        if not amount:
            amount = (result or {}).get("amount") or 0
        if not amount:
            amount = pending_amount_map.get(order_id, 0)
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            _cfg_amt = 0.0
            try:
                if BOT_CONFIG.exists():
                    _c = json.loads(BOT_CONFIG.read_text(encoding="utf-8"))
                    _cfg_amt = float(_c.get("bet_amount") or _c.get("default_bet_amount") or 0)
            except Exception:
                _cfg_amt = 0.0
            if _cfg_amt > 0:
                if (fills_map.get(order_id) or {}).get("filled") is True:
                    log(f"⚠️ A1 兜底: {slug} 已成交但金额缺失，用 config 单额 ${_cfg_amt:.2f} 补记账", "W")
                amount = _cfg_amt

        # 8/13 修复: PnL 优先用真实成交价 result.order_price（bot 实际挂单价，见 hermes_1h_bot.py place_order 后 result["order_price"]=order_price）
        # 缺省才回退 market_price（分析时刻价）/ max_price（信号上限）
        buy_price = float(result.get("order_price") or signal.get("market_price", 0) or signal.get("max_price", 0) or 0)
        if buy_price <= 0:
            buy_price = 0.5  # fallback
        # 8/19: 成交状态。filled=False（挂单未成交）→ pnl=0（无真实盈亏），方向对错照常记录。
        # sell_rec（止盈卖出）必然是成交单：与 fills 冲突时以 sell_rec 为准。
        fill_rec = fills_map.get(order_id) or {}
        filled = fill_rec.get("filled")
        sell_rec = sells_map.get(slug)
        if sell_rec and filled is False:
            filled = True
        # C2(2026-08-23): 真实成交均价优先——fills.avg_price 来自 trades API 加权均价，
        # 比挂单价(result.order_price=限价)更真实：限价单可能吃在更优价位、
        # 追价重挂单实际成交在更高价。仅当该单确有成交且未提前止盈时覆盖。
        _fill_avg = None
        try:
            _fav = fill_rec.get("avg_price")
            if _fav is not None and float(_fav) > 0:
                _fill_avg = float(_fav)
        except (TypeError, ValueError):
            _fill_avg = None
        if _fill_avg and filled is not False and not sell_rec:
            log(f"  🎯 {slug} 真实成交均价 {_fill_avg:.4f} 覆盖挂单价口径 (fills.status={fill_rec.get('status')})")
            buy_price = _fill_avg
        # C2: 追价链成交标记（bot 把追价新单终态镜像回写到首挂 order_id，status 前缀 CHASED_）
        if str(fill_rec.get("status", "") or "").startswith("CHASED"):
            actual_desc = f"{actual_desc} | 追价链终态{fill_rec.get('status')}"
        # A3(2026-08-23): 部分成交（TIMEOUT_PARTIAL/EMERGENCY_PARTIAL）。若 fills 记录带 size_matched，
        # 用真实已成交份额算 PnL，不用整单 amount（避免把部分持仓当成全额）。
        _partial_matched = None
        if filled is True and not sell_rec:
            _sm = fill_rec.get("size_matched")
            if _sm is not None:
                try:
                    _pm = float(_sm)
                    if 0 < _pm < (float(amount or 0) / max(buy_price, 1e-6)) - 1e-9:
                        _partial_matched = _pm
                except (TypeError, ValueError):
                    _partial_matched = None
        # 问题6: 提前止盈卖出感知——命中 sells.jsonl 用实际卖出 pnl，不按结算输赢
        if sell_rec:
            pnl = round(float(sell_rec.get("pnl_usd", 0) or 0), 2)
            actual_desc = f"{actual_desc} | 已提前止盈卖出@{sell_rec.get('sell_price')}"
            log(f"  💰 {slug} 提前卖出 | 实际 pnl=${pnl:.2f}（覆盖结算判定）")
        elif filled is False:
            pnl = 0.0
            actual_desc = f"{actual_desc} | 挂单未成交"
            log(f"  ⏸️ {slug} 挂单未成交 | 方向{'正确' if prediction_correct else '错误'}（分析口径照常记录，pnl=0）")
        elif filled is None:
            # 9/1 修复: 终态未知（fills 记录缺失，如 bot 宕机/紧急撤单丢终态）。
            # 原逻辑落到下面「按成交」分支伪造 pnl（如未知单显示 WIN +7.54），
            # 是 UI 成交/未成交混乱的数据源。results 落 pnl=None（UI 显示 ❔ 不显金额）；
            # 分析口径 stats 仍用估算 pnl（update_stats 需数值且 None 不进成交口径）。
            pnl = None
            _est_pnl = round(amount * (1.0 / buy_price - 1.0), 2) if prediction_correct else round(-amount, 2)
            actual_desc = f"{actual_desc} | 成交状态未知(终态缺失)"
            log(f"  ❔ {slug} 成交状态未知(无fills终态) | 方向{'正确' if prediction_correct else '错误'}（按成交估算 {_est_pnl:+.2f} 仅入分析口径）")
        elif _partial_matched is not None:
            _cost = _partial_matched * buy_price
            pnl = round(_partial_matched * 1.0 - _cost, 2) if prediction_correct else round(-_cost, 2)
            actual_desc = f"{actual_desc} | 部分成交{_partial_matched:.4g}sh@{buy_price:.2f}（非整单）"
            log(f"  🔸 {slug} 部分成交 {_partial_matched:.4g}sh | pnl=${pnl:.2f}（方向{'正确' if prediction_correct else '错误'}）")
        elif prediction_correct:
            pnl = round(amount * (1.0 / buy_price - 1.0), 2)
        else:
            pnl = round(-amount, 2)

        write_review_log(signal, actual_side, actual_desc, correct, wrong, analysis, prediction_correct)

        # 批量更新内存中的 stats 和 lessons（传入正确的 pnl）
        # 9/1: filled=None 时 pnl=None（results 照实落未知），stats 用成交估算值（分析口径需数值）
        update_stats(stats, signal, actual_side, prediction_correct,
                     pnl if pnl is not None else (_est_pnl if filled is None else 0.0), filled)
        update_lessons(lessons, signal, actual_side, correct, wrong)
        try:
            with open(RESULTS, "a", encoding="utf-8") as rf:
                # 8/21 A3: 落档 probability_raw/strategy_version/model_version（来自信号，缺省 None）
                # 供 fill_bias 回测与按版本分段统计——不再依赖事后 join done.jsonl。
                rf.write(json.dumps({
                    "order_id": order_id, "slug": slug, "side": side,
                    "actual": actual_side, "amount": amount, "pnl": pnl,
                    "buy_price": buy_price, "filled": filled,
                    # C2: 落档真实成交均价与 fills 终态，供后续 fill_bias/追价成交率分析
                    "avg_price": fill_rec.get("avg_price"),
                    "fill_status": fill_rec.get("status"),
                    "probability_raw": signal.get("probability_raw"),
                    "strategy_version": signal.get("strategy_version"),
                    "model_version": signal.get("model_version"),
                    "ts": datetime.now(timezone.utc).isoformat(),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

        # Bug-2 修复: 结算后清理对应的 PENDING 记录（原子写入防并发）
        # 漏洞7 修复: 写入改用 _atomic_write（已内置 fcntl 排他锁），防止与 hermes_1h_bot.py 并发写时丢数据
        try:
            pending_file = Path(__file__).resolve().parent / "data" / "hermes_pending.jsonl"
            if pending_file.exists():
                kept = []
                with open(pending_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            # Bug-R1 修复: 只用 order_id 匹配，同一 slug 可能有多个订单
                            if entry.get("order_id") != order_id:
                                kept.append(line)
                        except json.JSONDecodeError:
                            continue
                content = "\n".join(kept) + ("\n" if kept else "")
                _atomic_write(pending_file, content)
        except OSError:
            pass

        if prediction_correct:
            notify("✅ 交易盈利", f"{side} {slug[:25]} 正确 +${pnl:.1f}")
            log(f"  ✅ {slug} | {side} 预测正确 | {actual_desc}")
        else:
            notify("❌ 交易亏损", f"{side} {slug[:25]} 错误 -${amount:.1f} 实际{actual_side}")
            log(f"  ❌ {slug} | {side} 预测错误 | 实际{actual_side} | {actual_desc}")

        reviewed.add(order_id)
        reviewed_count += 1

        # 漏洞3 修复: 顺序必须是 reviewed(幂等标记) → stats → lessons
        # 崩溃在中间时: reviewed 已写 → 重启跳过(stats 漏 1 笔, 可接受);
        #              反之先写 stats 再写 reviewed, 崩溃在中间 → 重启会重复复盘(不可接受)
        save_reviewed(reviewed)   # 先幂等标记
        save_stats(stats)         # 再写统计
        save_lessons(lessons)

        # 逐笔即时反馈: 每笔交易结算后立即刷新指标权重
        # v6: 按当前交易的 source 刷新（v5/AI/fallback），避免不同 source 数据互相稀释
        try:
            review_source = signal.get("source", "v5") or "v5"
            review_regime = classify_regime(signal.get("v5_adx"))
            new_weights = force_refresh(review_regime, review_source)
            if new_weights:
                changed = {k: round(v, 3) for k, v in new_weights.items() if v != 1.0}
                if changed:
                    log(f"  🔄 权重已刷新[{review_source}/{review_regime}]: {changed}")
        except Exception as e:
            log(f"  ⚠️ 权重刷新失败: {e}", "W")

    # 最终保存（兜底）+ 强制刷新权重
    save_reviewed(reviewed)
    save_stats(stats)
    save_lessons(lessons)
    try:
        force_refresh()
    except Exception:
        pass

    log(f"\n复盘完成: {reviewed_count}/{len(trades)} 笔已结算")
    print_summary()
    check_round37_fallback()
    log(f"=== 复盘结束 ===\n")


if __name__ == "__main__":
    main()
