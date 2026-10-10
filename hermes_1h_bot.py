#!/usr/bin/env python3
"""
Hermes 1H BTC Bot — 自动发现当前窗口 + 信号下单

流程:
  1. 自动检测当前 ET 小时 → 构造 Polymarket 1H 合约 slug
  2. 检查是否在交易窗口内（开盘 0-55 min）
  3. 读取 Hermes 信号，匹配当前合约
  4. 限价单下单

启动:
  python3 hermes_1h_bot.py           # paper 模拟
  python3 hermes_1h_bot.py --live    # 真实钱包下单
  python3 hermes_1h_bot.py --once    # 只跑一次

依赖: polymarket-client, python-dotenv
"""

import sys
# 排除 ~/.hermes 的 venv(不是当前项目的 .venv)
sys.path = [p for p in sys.path if ".hermes" not in p]

try:
    import fcntl as _fcntl_mod
    _HAS_FCNTL = True
except ImportError:
    _fcntl_mod = None
    _HAS_FCNTL = False
import portalocker
import os, json, time, signal, re, calendar
def _flock_ex(f, nb=False):
    """Cross-platform exclusive lock: fcntl(Mac/Linux) or portalocker(Windows)"""
    if _HAS_FCNTL:
        op = _fcntl_mod.LOCK_EX | (_fcntl_mod.LOCK_NB if nb else 0)
        _fcntl_mod.flock(f.fileno(), op)
    else:
        op = portalocker.LOCK_EX | (portalocker.LOCK_NB if nb else 0)
        portalocker.lock(f, op)
def _flock_un(f):
    """Cross-platform unlock"""
    if _HAS_FCNTL:
        _fcntl_mod.flock(f.fileno(), _fcntl_mod.LOCK_UN)
    else:
        portalocker.unlock(f)
from hermes_common import now_et, build_slug, http_get_json, log, get_proxy, reset_net_cache, load_pm_creds, save_pm_creds, PROXY, GAMMA_API, CLOB_API, ET, fetch_book_momentum
from datetime import datetime, timezone, timedelta
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

QUEUE = DATA_DIR / "claude_signal.jsonl"        # 8/16 v4: claude 任务写信号到 hermes btc/data/claude_signal.jsonl,bot 直接读这个文件(macOS host FS)
# 8/4 BOSS 决策:只认 OpenClaw btc-agent 子代理信号,其他 source(v5/fallback/墨痕/Trae AI 等)全部忽略
# 防止多源抢队列导致方向打架(曾出现同 slug 4 条信号 DOWN/UP 互相覆盖)
ALLOWED_SOURCES = ("btc-agent", "BTC Sentinel", "btc_signal_watcher")


def _source_allowed(sig):
    """只放行 btc-agent 子代理信号(8/4 BOSS 指令:其他源全禁)。

    9/3 P2-13 修复: 原「子串包含」匹配会放行任意含关键字的伪造源(如 x-btc-agent-evil)；
    现改为集合精确匹配(strip+lower 规范化后等值比较)。硬约束口径=仅 btc-agent，
    其余两源为历史遗留兼容保留，当前生产信号源实证均为 btc-agent。
    """
    src = (sig.get("source") or "").strip().lower()
    if not src:
        return False
    return src in {k.strip().lower() for k in ALLOWED_SOURCES}
DONE = DATA_DIR / "hermes_done.jsonl"              # 交易记录持久化
RESULTS = DATA_DIR / "hermes_results.jsonl"        # review 写入的实际盈亏流水
PENDING = DATA_DIR / "hermes_pending.jsonl"        # Bug-2: 下单后立即写入，填补风控盲区
FILLS = DATA_DIR / "hermes_fills.jsonl"            # 8/19: 订单终态记录（成交/未成交），供 review/dashboard 分开统计
SELLS = DATA_DIR / "hermes_sells.jsonl"             # 8/10: 最后5分钟止盈卖出记录（防重+复盘）
ENV = THIS_DIR / ".env"
CONFIG = THIS_DIR / "hermes_config.json"


def _trading_pause_active(cfg_path=None) -> bool:
    """round41.8.7(2026-08-25 用户决策「暴雷止血」): 全局暂停实盘下单开关。
    config hermes_config.json `"trading_paused": true` → ORDER 信号不入市（UI 按钮/dashboard
    /api/pause 或手改 config 均可切换，每次 process_signal 现读、无需重启）。
    语义：config 文件缺失=不暂停（全新环境默认可交易）；**JSON 解析异常=暂停**
    （fail-closed：止损开关宁可误停不可漏停，并会在 bot 日志打 ⚠️）。cfg_path 接受 str 或 Path。"""
    p = Path(cfg_path) if cfg_path else CONFIG
    try:
        if not p.exists():
            return False
        return bool(json.loads(p.read_text(encoding="utf-8")).get("trading_paused"))
    except Exception:
        return True

GAMMA = GAMMA_API
PIDFILE = DATA_DIR / "hermes_bot.pid"              # 进程锁，/tmp 即可
LAST_SIGNAL = DATA_DIR / "hermes_last_signal.json" # UI 展示数据
PEAK_BALANCE = DATA_DIR / "hermes_peak_balance.json"  # Bug-1: 历史峰值余额
SHADOW_EDGE = DATA_DIR / "hermes_shadow_edge.jsonl"    # round36 影子模式：edge 通道 would-be 记录（不改交易行为）
CHASE_ALERTS = DATA_DIR / "hermes_chase_alerts.jsonl"  # 8/21 方案A3: 追价后仍超时的告警记录（复盘用）
# round41.8: 规则A被拦单影子账——「晚间(16-21ET)超低价」拦截的假想交易记录,
# 攒两周按 slug join 结算复核拦错率,拦错率高则 config 置 rule_a_skip_enabled=false 回滚
SHADOW_RULE_A = DATA_DIR / "hermes_shadow_rule_a.jsonl"

# —— 交易窗口配置 ——
WINDOW_OPEN_MIN = 0    # 整点开盘立刻下单
WINDOW_CLOSE_MIN = 55  # 开盘后第几分钟停止交易
ANALYZE_AT_MIN = 40    # 9/3 用户拍板: bot 推迟到整点后 :40 才分析下单(此前收到引擎信号即处理≈:36)
# round55: 分析时刻改为 dashboard UI 可调——config `analyze_min` 热读（TTL 60s 缓存防高频 tick 读盘），
# 无需重启 bot。缺失/解析异常/越界一律回退 40（与历史行为一致）。
_analyze_cache = {"ts": 0.0, "val": 40}

def _analyze_at_min() -> int:
    now = time.time()
    if now - _analyze_cache["ts"] >= 60:
        try:
            v = json.loads(CONFIG.read_text(encoding="utf-8")).get("analyze_min")
            # 排除 bool（bool 是 int 子类，True 会被误取为 1）；非数值一律回退 40
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                _analyze_cache["val"] = 40
            else:
                iv = int(v)
                _analyze_cache["val"] = iv if 0 <= iv <= 55 else 40
        except Exception:
            _analyze_cache["val"] = 40
        _analyze_cache["ts"] = now
    return _analyze_cache["val"]

_pid_lock_fd = None

def acquire_pidfile():
    """fcntl.flock 单实例锁：锁随进程消亡自动释放，不受 PID 复用影响"""
    global _pid_lock_fd
    _pid_lock_fd = open(PIDFILE, "w", encoding="utf-8")
    try:
        _flock_ex(_pid_lock_fd, nb=True)
    except OSError:
        log("⚠️ 另一实例运行中（flock 占用），退出", "E")
        sys.exit(1)
    _pid_lock_fd.write(str(os.getpid()))
    _pid_lock_fd.flush()

def release_pidfile():
    global _pid_lock_fd
    try:
        if _pid_lock_fd:
            _flock_un(_pid_lock_fd)
            _pid_lock_fd.close()
            _pid_lock_fd = None
        PIDFILE.unlink(missing_ok=True)
    except Exception:
        pass

# ==================== 当前窗口检测 ====================

def get_current_et():
    """返回当前 ET 时间的 (hour, minute, et_dt)"""
    et = now_et()
    return et.hour, et.minute, et

def get_current_window():
    """返回当前 1H 窗口信息，如果不在窗口内返回 None"""
    et_hour, et_minute, et_dt = get_current_et()
    slug = build_slug(et_dt)
    in_window = WINDOW_OPEN_MIN <= et_minute <= WINDOW_CLOSE_MIN
    return {
        "slug": slug,
        "et_hour": et_hour,
        "et_minute": et_minute,
        "et_time": et_dt.strftime("%Y-%m-%d %H:%M ET"),
        "in_window": in_window,
        "window_str": f"{WINDOW_OPEN_MIN}-{WINDOW_CLOSE_MIN}min",
    }

# ==================== HTTP 工具 ====================

def http_get(url, timeout=10):
    """使用统一的线程安全 HTTP 工具"""
    return http_get_json(url, timeout=timeout, proxy=PROXY, user_agent="hermes-bot/1.0")

def get_market(slug):
    """获取市场数据，最多重试 3 次"""
    for attempt in range(3):
        try:
            m = http_get(f"{GAMMA}/markets?slug={slug}")
            if m and len(m) > 0:
                return m[0]
            if attempt < 2:
                time.sleep(2)
        except Exception as e:
            log(f"Gamma 重试 {attempt+1}/3: {str(e)[:60]}", "W")
            if attempt < 2:
                time.sleep(3)
    log(f"Gamma 失败: 3次重试后仍无法获取 {slug}", "E")
    return None

# ==================== Bot 主类 ====================

# —— 风控常量 ——
MAX_DAILY_LOSS_USD = 100.0      # 日最大亏损（USD）
MAX_CONSECUTIVE_LOSSES = 10     # 连续亏损熔断（从3放宽到10，避免频繁误触发）
MAX_RETRIES_PER_WINDOW = 3      # 每窗口最多重试次数
MAX_DRAWDOWN_PCT = 100.0        # 余额最大回撤 % (8/19 临时放宽 60→100,覆盖 63% 当前回撤)
MIN_LIQUIDITY_USD = 10.0        # 最低流动性要求（USD）— 放宽以适应低流动性环境
MAX_BID_ASK_SPREAD = 0.25       # 最大买卖价差 — 放宽：Polymarket 1H合约CLOB价差天然偏高
MIN_MEANINGFUL_SIZE = 50        # Bug-B2: CLOB 最小有意义 size，与分析器一致
# 8/21 提到模块级（原为 execute_order 局部变量）：追价重挂逻辑(_poll_pending_orders)也要用。
# 历史: 8/13 统一封顶 0.80→0.60；8/15 round19 放宽 0.69；8/16 round24 回退 0.60（历史最优平衡点）。
MAX_ORDER_PRICE = 0.60

# —— 8/21 方案A3: 追价重挂参数（BOSS 决策，推翻 8/5"不追价"指令）——
# 依据: 超时取消单方向对率 88.4%(99对/13错) vs 成交单胜率 53.2%（data/hermes_fills.jsonl 382单）
# round41.7(2026-08-24) 两段式进场: 本值语义升级为「第一段(maker)静置期」——
# 静置期内绝不追价(等回落成交)，到期后才允许第二段(taker兜底)评估(含实时方向复核)。
CHASE_AFTER_MIN = 2        # 首挂静置分钟数后开始阶梯追价(9/2 优化: 5→2——9/1 实证强趋势日静置 5 分钟内 ask 已从 0.65 冲到 0.90+, 静置期=让利期)
CHASE_MAX_TIMES = 5        # 【已废弃 9/2 round52.2】无封顶一直追后不再限次数，仅留作历史记录
CHASE_MAX_STEP = 0.25      # 【已废弃 9/2 round52.2】累计涨幅放弃线随封顶一起移除，仅留作历史记录
CHASE_CHECK_INTERVAL = 120 # 评估节流（秒, per-oid），tick 3s/次防 API 轰炸；2 分钟/档(9/2: 5min→2min——趋势日 5min 一档太慢, ask 常在档位间隙冲过封顶)
CHASE_TARGETS = [0.65, 0.70, 0.75, 0.80, 0.85]  # roundXX(2026-09-01 用户拍板, 9/2 改 5 档每档+0.05 封顶 0.85) 阶梯追价档位价: 首挂0.6 → 逐档+0.05 封顶 0.85
ORDER_TTL_MIN = 25         # roundXX 最终超时(首挂起)分钟数，保留 round41.7 "26 分钟总窗口"语义；与追价档位分开
# A2(2026-08-23): 紧急关闭时保留「仍在本小时交易窗口内」的挂单不撤，
# 由重启后的 _recover_pending_orders 继续追踪。避免 bot 被杀/重启时当小时敞口静默清零
# （KeepAlive 自动重启 + restart_hermes.sh pkill 均触发旧路径）。50min < 26min超时+余量，
# 超过则窗口临近结算，正常超时路径已能收口，关闭时撤单是安全默认。
PRESERVE_ON_SHUTDOWN_SEC = 50 * 60

# —— 8/21 round37: 条件封顶（高概率通道）——
# 数据依据: 114 笔超时单中 27 笔挂价顶死 0.60，其中 25 笔方向对（92.6%，praw 0.75-0.93）；
# praw≥0.70 信号全量胜率 71.6% → 买 0.70 微正 EV；praw<0.70 仅 61% → 必须继续封 0.60（round24 教训）。
HIGH_PROB_CAP = 0.80   # 高概率通道封顶(9/2 优化: 0.70→0.80——强信号首挂原封顶 0.70 连首档追价空间都没留, 9/1 五单踏空实证)
HIGH_PROB_MIN = 0.70   # probability_raw 门槛

# —— 8/24 round41.7: 两段式进场（用户批准全套，替代 round41.5 单段 taker）——
# 矛盾: 追价吃单价越高利润越薄尾部越重(0.95 需 >95% 胜率)，纯低挂则成交率低。
# 数据(SYNC_MAP 十二): ① 全历史真实成交 ≤0.65 段累计 +$229 是利润引擎、≥0.65 合计净亏；
#   ② 0.88→0.95 仅多覆盖 3% 决策时刻(283 小时中 9 小时)却把平衡线拉到 95%；
#   ③ 未成交单方向正确率 91.5%(n=71) 在 0.88 为薄正 EV、在 0.95 为负 EV(-$0.37/单)。
# 解法(round52.4 废除两段式前的历史口径): 便宜区 taker、贵区 maker 静置等回落。
# 9/2 用户拍板成交率优先: 首挂一律按实时 best_ask 跨价吃（见 execute_order round52.4 块），
# 下列三常量仅剩历史脚本(scripts/test_round4x*)引用，主流程已不再使用。
FAST_TAKE_MAX = 0.65    # [已废弃 round52.4] 原 ≤此价 taker 快道
MAKER_CAP = 0.60        # [已废弃 round52.4] 原 maker 静置挂价封顶
MAKER_DISCOUNT = 0.03   # [已废弃 round52.4] 原 maker 相对 ask 折价
RECHECK_PM_MIN = 0.50   # 第二段复核线: 我方方向 Gamma 实时中间价 <此值=共识已反超→撤单SKIP（9/2 round52.2 收窄: 只剩这一道线）
TAKER_MAX = 0.65        # taker 兜底硬顶(round41.5 设 0.88 → 41.5.3 曾上调 0.95 → round41.7 回滚 0.88 → round43 再收紧到 0.65, 赔率平衡线依据：0.65 吃单盈亏平衡胜率=65%, 落入全历史盈利引擎的 ≤0.65 段+0.55-0.65 段累计+$229 范围)
                        # 数学平衡线=入场价，历史未成交单方向正确率 91.5% 在 0.95 为负 EV)
# 9/2 round52.2 用户拍板: 无封顶持续追价——CHASE_EAT_MAX 以下直接吃价，其余每 2 分钟一档
# 上移挂价排队（CHASE_TARGETS 5 档用尽后 +0.05 延伸至 CLOB 上限 0.99），直到成交/超时
CHASE_EAT_MAX = 0.65        # ① 实时 best_ask ≤ 此值(历史盈利引擎段) → 直接 taker 吃价，确定性成交

def _effective_price_cap(sig):
    """round37 条件封顶: probability_raw≥0.70 → 0.70, 其余 0.60。
    praw 缺失时退回 reported probability（现行信号被 R2 封顶 0.67 → 永不开通道，保守安全）。
    A5(2026-08-23): 信号显式 price_cap 优先——write_signal 在 praw≥0.70 时写入 0.70，
    让「高概率通道提价」在信号层显式可见、可复盘，而不是 bot 静默覆盖。"""
    try:
        _cap = float(sig.get("price_cap"))
        if 0 < _cap <= 1:
            return _cap
    except (TypeError, ValueError):
        pass
    try:
        praw = float(sig.get("probability_raw", sig.get("probability")))
    except (TypeError, ValueError):
        return MAX_ORDER_PRICE
    return HIGH_PROB_CAP if praw >= HIGH_PROB_MIN else MAX_ORDER_PRICE

# ==================== Bug-5: 文件并发写入工具 ====================

def _atomic_rewrite_locked(path: Path, kept_lines: list[str]):
    """带 fcntl 排他锁的原子重写，防止与 review 进程并发写入丢失 entry"""
    if not path.exists():
        return
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lf:
        _flock_ex(lf)
        try:
            content = "\n".join(kept_lines) + ("\n" if kept_lines else "")
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)
        finally:
            _flock_un(lf)

def momentum_conflict(flag, side):
    """盘感覆盖层冲突判定：book_momentum flag 与信号方向冲突 → 返回 True（建议 SKIP）。

    DUMP_TRAP/ASK_SURGE = 盘口看涨动能（诱空拉涨/买UP扫盘），信号却 DOWN → 冲突；
    ASK_FADE = UP 盘口走弱，信号却 UP → 冲突。其余组合不冲突。
    """
    side = (side or "").upper()
    flag = (flag or "").upper()
    return ((flag in ("DUMP_TRAP", "ASK_SURGE") and side == "DOWN")
            or (flag == "ASK_FADE" and side == "UP"))


class HermesBot:
    def __init__(self, live=False):
        self.live = live
        self.client = None
        self._client_proxy = None  # 8/11 自适应: 记录当前 client 使用的代理，网络切换时检测变化重建
        self.last_slug = self._recover_last_slug()
        self._processing_slug = None  # B2: 内存锁，防止同一 slug 重复处理
        self._window_retries = {}     # {slug: retry_count}
        self._pending_orders = {}     # {order_id: {"slug": ..., "side": ..., "ts": ...}}
        self._recover_pending_orders()  # 8/19: 重启后从 PENDING 文件恢复挂单追踪（否则终态永不记录）
        self._risk_stats = self._load_risk_stats()  # B3: 风控统计
        self._peak_balance = self._load_peak_balance()  # Bug-1: 历史峰值余额（只增不减）
        self._shutdown = False  # Bug-9: 紧急停止标志，让重试 sleep 可中断
        self._tp_done = self._load_tp_done()  # 8/10: 已做止盈判定的 slug（防重，从 sells.jsonl 预填）

    # ========== B3: 风控模块 ==========

    def _estimate_pending_pnl(self, slug, side, amount, buy_price):
        """估算 PENDING 订单的当前盈亏（基于 Gamma 实时价格）。

        Bug-2 修复: 用实时价格估算未结算订单盈亏，避免风控滞后 1 小时。
        异常或无法获取价格时降级为 -amount（最坏情况），但不跳过统计。
        """
        try:
            m = get_market(slug)
            if not m:
                return -amount
            raw_prices = m.get("outcomePrices", "[]")
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else (raw_prices or [])
            if len(prices) < 2:
                return -amount
            up_p = float(prices[0])
            dn_p = float(prices[1])
            current_price = up_p if str(side).upper() == "UP" else dn_p
            if buy_price <= 0 or current_price <= 0:
                return -amount
            # pnl = (当前价 - 买入价) * 持仓份额；持仓份额 = amount / buy_price
            shares = amount / buy_price
            pnl = (current_price - buy_price) * shares
            return pnl
        except Exception:
            return -amount

    def _load_risk_stats(self):
        """加载风控统计（从 review 写入的 RESULTS 流水 + PENDING 未结算，只统计今日 ET）"""
        today = now_et().strftime("%Y-%m-%d")
        stats = {"daily_pnl": 0.0, "consecutive_losses": 0, "total_trades": 0,
                 "today_date": today}
        settled_pnls = []  # Bug-B4: 分开追踪已结算和 PENDING，避免 PENDING 影响连续亏损统计

        # 1. 读取已结算的 RESULTS
        if RESULTS.exists():
            try:
                with open(RESULTS, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = entry.get("ts", "")
                        try:
                            entry_date = datetime.fromisoformat(ts).astimezone(ET).strftime("%Y-%m-%d")
                        except Exception:
                            continue
                        if entry_date != today:
                            continue
                        try:
                            settled_pnls.append(float(entry.get("pnl", 0) or 0))
                        except (TypeError, ValueError):
                            continue
            except OSError:
                pass

        # 2. Bug-2 修复: 合并 PENDING 未结算交易，用 Gamma 实时价格估算盈亏
        pending_estimated_pnls = []
        pending_pnl = 0.0
        if PENDING.exists():
            try:
                with open(PENDING, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = entry.get("ts", "")
                        try:
                            entry_date = datetime.fromisoformat(ts).astimezone(ET).strftime("%Y-%m-%d")
                        except Exception:
                            continue
                        if entry_date != today:
                            continue
                        amount = float(entry.get("amount", 0) or 0)
                        slug = entry.get("slug", "")
                        side = entry.get("side", "")
                        buy_price = float(entry.get("price", 0) or 0)
                        est_pnl = self._estimate_pending_pnl(slug, side, amount, buy_price)
                        pending_estimated_pnls.append(est_pnl)
                        pending_pnl += est_pnl
            except OSError:
                pass

        stats["total_trades"] = len(settled_pnls) + len(pending_estimated_pnls)
        stats["daily_pnl"] = sum(settled_pnls) + pending_pnl
        # Bug-2 修复: 合并 settled + pending 估算后统计连续亏损
        all_pnls = settled_pnls + pending_estimated_pnls
        for pnl in reversed(all_pnls):
            if pnl < 0:
                stats["consecutive_losses"] += 1
            else:
                break
        if pending_estimated_pnls:
            log(f"📊 风控: {len(pending_estimated_pnls)} 笔未结算交易实时估算 pnl=${pending_pnl:.1f}", "I")
        return stats

    def _check_risk(self, slug, side, amt):
        """风控检查，返回 (allowed: bool, reason: str, transient: bool)

        9/2 审查修复(B4): 新增第三返回值 transient——瞬时性失败（钱包连接抖动等）
        标记 True，调用方保留信号待重试；策略性拦截（日亏/熔断/回撤）标记 False，
        照旧删信号。此前两类失败共用 (False, reason)，_do_tick 统一清队列导致
        瞬时抖动也会永久删除信号 → 本小时踏空。"""
        # 每次从磁盘刷新：跨日自动重置 + 进程内不滞后（每窗口最多调用一次，IO 可忽略）
        self._risk_stats = self._load_risk_stats()
        # 每日亏损上限
        if self._risk_stats["daily_pnl"] < -MAX_DAILY_LOSS_USD:
            return False, f"日亏损超限 (${-self._risk_stats['daily_pnl']:.0f} > ${MAX_DAILY_LOSS_USD})", False
        # 连续亏损熔断
        if self._risk_stats["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            return False, f"连续亏损熔断 ({self._risk_stats['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES})", False
        # 单窗口重试限制
        retries = self._window_retries.get(slug, 0)
        if retries >= MAX_RETRIES_PER_WINDOW:
            return False, f"窗口重试上限 ({retries}/{MAX_RETRIES_PER_WINDOW})", False
        # 余额回撤检查（live 模式且钱包已连接或可连接）
        if self.live:
            # 8/11 自适应: 用 _ensure_client() 确保 client 与当前网络代理一致（网络切换自动重建）。
            # 首次失败重建连接重查一次，避免单次网络抖动/代理失效直接把整个窗口 SKIP。
            for _attempt in range(2):
                if not self._ensure_client():
                    # client 确保失败（连接/探测异常），重试一次后仍失败才拦截
                    if _attempt == 0:
                        log("⚠️ 钱包连接失败(重试1/2)，重建重查", "W")
                        reset_net_cache()
                        os.environ.pop("http_proxy", None)
                        os.environ.pop("https_proxy", None)
                        self.client = None
                        self._client_proxy = None
                        continue
                    return False, "余额查询失败: 钱包连接异常", True
                try:
                    bal = self.client.get_balance_allowance(asset_type="COLLATERAL")
                    current = float(bal.balance) / 1e6
                    peak = self._update_peak_balance(current)
                    if peak and peak > 0:
                        drawdown_pct = (peak - current) / peak * 100
                        if drawdown_pct >= MAX_DRAWDOWN_PCT:
                            return False, f"余额回撤超{MAX_DRAWDOWN_PCT:.0f}% (${current:.0f} / 峰值${peak:.0f})", False
                    break  # 成功，跳出重试
                except Exception as e:
                    # Bug-8 修复: 余额查询失败时拒绝下单，不静默跳过
                    if _attempt == 0:
                        log(f"⚠️ 余额查询失败(重试1/2): {str(e)[:50]} | 重建连接重查", "W")
                        reset_net_cache()
                        os.environ.pop("http_proxy", None)
                        os.environ.pop("https_proxy", None)
                        self.client = None
                        self._client_proxy = None
                    else:
                        log(f"⚠️ 余额查询失败，拒绝下单(瞬时,保留信号): {str(e)[:50]}", "W")
                        return False, f"余额查询失败: {str(e)[:30]}", True
            else:
                return False, "余额查询失败: 连接异常", True
        return True, "ok", False

    # ========== Bug-1: 历史峰值余额管理 ==========

    def _load_peak_balance(self):
        """从磁盘加载历史峰值余额，失败返回 None"""
        if not PEAK_BALANCE.exists():
            return None
        try:
            data = json.loads(PEAK_BALANCE.read_text(encoding="utf-8"))
            peak = float(data.get("peak_balance", 0) or 0)
            return peak if peak > 0 else None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def _save_peak_balance(self, balance):
        """原子写入峰值余额到磁盘"""
        try:
            tmp = PEAK_BALANCE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"peak_balance": balance, "ts": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
            tmp.replace(PEAK_BALANCE)
        except OSError:
            pass

    def _update_peak_balance(self, current):
        """只增不减地更新历史峰值余额，返回当前峰值"""
        if current <= 0:
            return self._peak_balance
        if self._peak_balance is None or current > self._peak_balance:
            self._peak_balance = current
            self._save_peak_balance(current)
        return self._peak_balance

    # ========== B12: 流动性预检 ==========

    def _check_liquidity(self, token_id, max_price):
        """检查 CLOB 订单簿流动性，返回 (ok, best_ask, reason)

        设计原则：流动性检查是参考，不是硬闸门。
        - CLOB 价差大不代表无法成交（限价单可挂在合理价位等待）
        - 检查不过时仍允许下单（通过 return ok=True 但记录 warning）
        - 只有 CLOB book 完全获取失败才 return False

        Bug-B1 修复: 过滤范围统一为 0.10-0.90（与分析器一致），排除做市商镜像单
        Bug-B2 修复: 加入 MIN_MEANINGFUL_SIZE 过滤填充单
        """
        def _sf(val, default=0):
            try: return float(val)
            except (TypeError, ValueError): return default
        # roundXX(2026-09-01 盘口三级追价): 统一返回 (ok, best_ask, spread, reason)，
        # 追价决策需用 realtime spread 判断"价差大退档"；提前 return 分支给默认 0.10。
        _spread = 0.10
        try:
            book_url = f"{CLOB_API}/book?token_id={token_id}"
            # Bug-CLOB 修复: 代理 SSL 握手 3-5s 波动大, 原 timeout=8 偏紧易超时
            # 1) timeout 提到 15s  2) 失败重试 1 次  3) return_error 拿具体异常记入 reason
            book, err = http_get_json(book_url, timeout=15, proxy=PROXY, return_error=True)
            if not book:
                time.sleep(1)
                book, err = http_get_json(book_url, timeout=15, proxy=PROXY, return_error=True)
            if not book:
                err_info = f"{type(err).__name__}: {str(err)[:80]}" if err else "返回空"
                return False, None, _spread, f"CLOB book 数据获取失败(重试后仍失败): {err_info}"
            # Bug-B1: 过滤范围 0.10-0.90（与分析器一致，排除做市商 0.05/0.95 镜像单）
            raw_ask_prices = [_sf(a.get("price"), 0) for a in book.get("asks", [])]
            bids = [b for b in book.get("bids", []) if 0.10 <= _sf(b.get("price", 0)) <= 0.90]
            asks = [a for a in book.get("asks", []) if 0.10 <= _sf(a.get("price", 1)) <= 0.90]
            if not asks:
                # ===== round41.5.2 方案①(8/24): 篮子被过滤空 ≠ 没有卖家 =====
                # 若原始卖单存在但全部 >0.90（单边行情最常见，如 8/23 9pm DOWN 0.895→0.985），
                # 返回真实最低卖一价，交由下游定价层判定（round41.7: ≤FAST_TAKE_MAX 直接吃 /
                # 贵区 maker 静置；追价兜底上限 TAKER_MAX=0.65, round43 收紧）。
                # 历史 bug: 此处直接误判「CLOB无卖单」走低挂兜底 max_price×0.95=0.57，
                # 在 0.90+ 行情里挂假低价死单 26 分钟超时（日志同签名 8/9 起 25 次；
                # 8/23 5pm/9pm/10pm 三单实证，且骗过 TAKER_MAX 防线）。
                above_band = [p for p in raw_ask_prices if p > 0.90]
                if above_band:
                    real_min = min(above_band)
                    return True, real_min, _spread, f"卖单一均在{real_min:.2f}(>0.90观察带上沿)，返回真实卖一价由taker上限判定"
                # ===== round41.5.2 方案②(8/24): 账本无有效卖单 → SKIP，废除低挂兜底 =====
                # 旧兜底「max_price×0.95 低挂等跌」是 round41.5 已废除逻辑的残留：
                # 低挂只有方向错、价格回落时才成交（结构性逆向选择），方向对永远买不到。
                # 账本真空/仅剩 ≤0.10 镜像单时无合理供给价，按 round41.5 口径直接放弃本单。
                return False, None, _spread, "CLOB账本无有效卖单(空/仅≤0.10镜像单)，round41.5.2 不再低挂兜底→SKIP"
            # Bug-B2: 跳过 size < MIN_MEANINGFUL_SIZE 的填充单
            meaningful_asks = [a for a in asks if _sf(a.get("size", 0)) >= MIN_MEANINGFUL_SIZE]
            meaningful_bids = [b for b in bids if _sf(b.get("size", 0)) >= MIN_MEANINGFUL_SIZE]
            if meaningful_asks:
                best_ask = min(_sf(a["price"], 1) for a in meaningful_asks)
            else:
                best_ask = _sf(asks[0]["price"], 1)  # fallback: 用第一个（即使流动性薄）
            # Bug-B3: bids 为空时的安全处理
            best_bid = max(_sf(b["price"]) for b in meaningful_bids) if meaningful_bids else (0 if not bids else _sf(bids[0]["price"]))
            ask_size = _sf(meaningful_asks[0].get("size", 0)) if meaningful_asks else _sf(asks[0].get("size", 0))
            spread = best_ask - best_bid if best_bid > 0 else 0.10
            _spread = spread

            # 8/9 方案A2: 实时 best_ask 为主，不因价差/流动性/卖价偏高退回旧 max_price（那正是买不到的原因）
            # 实时 best_ask 缺失/异常时才退回 max_price 兜底
            # 卖价偏高不硬拦截，返回实时 best_ask（主流程的 0.69 统一上限会兜底）
            if best_ask > max_price * 1.20:
                return True, best_ask, spread, f"卖价{best_ask:.3f}偏高(信号限价{max_price:.3f})，用实时价格"
            # 价差大/流动性低不硬拦截，仍用实时 best_ask 挂单等待成交
            if spread > MAX_BID_ASK_SPREAD:
                return True, best_ask, spread, f"价差{spread:.3f}偏大，限价挂单等待"
            if ask_size * best_ask < MIN_LIQUIDITY_USD:
                return True, best_ask, spread, f"流动性偏低(${ask_size*best_ask:.0f})，限价挂单等待"
            return True, best_ask, spread, "ok"
        except Exception as e:
            return False, None, 0.10, f"流动性检查异常: {str(e)[:50]}"

    # ========== B4: 订单状态追踪 ==========

    def _check_order_status(self, order_id, token_id=None):
        """轮询订单状态，返回 "FILLED"/"OPEN"/"CANCELLED"/"UNKNOWN"

        Polymarket SDK 返回的 status 值:
        - "live": 订单在簿（未成交）
        - "matched": 已成交
        - "delayed": 延迟中
        9/2 审查修复(异味): 删除过时描述"抛异常 → 视为 FILLED"——9/2 假成交修复后
        查不到一律返回 UNKNOWN，须以 list_account_trades 成交证据判 FILLED（硬约束）。
        """
        if not self.live or order_id.startswith("PAPER_"):
            return "PAPER"
        if not self.client:
            # 8/11 审查修复: 网络故障期间 client=None 时返回 UNKNOWN 而非 PAPER。
            # 原逻辑把 client=None 与 PAPER 混为一谈，_poll_pending_orders 会把真实挂单
            # 当作模拟单清出 _pending_orders，导致失去超时取消追踪 + PENDING 幽灵记录。
            return "UNKNOWN"
        try:
            resp = self.client.get_order(order_id=order_id)
            # OpenOrder 对象: status 是字符串 "live"/"matched"/"delayed"
            status = getattr(resp, 'status', None)
            if status is None and isinstance(resp, dict):
                status = resp.get("status", "UNKNOWN")
            if status is None:
                status = "UNKNOWN"
            status_str = str(status).lower()
            # SDK: "matched" = 已成交, "live" = 在簿, "delayed" = 延迟
            if status_str == "matched":
                return "FILLED"
            elif status_str in ("live", "delayed"):
                return "OPEN"
            else:
                return status_str.upper()
        except Exception as e:
            err_str = str(e).lower()
            # get_order 只返回 open orders，订单已成交/取消后会抛异常
            # 9/2 假成交修复: not found ≠ 已成交——还可能是①下单后 CLOB 索引延迟
            # (实例: 9/2 4pm-et 0x0633d9 下单3s即被误判 FILLED, 实际 LIVE 0股成交,
            #  假 FILLED 污染 fills/复盘) 或②订单被外部撤销。
            # 统一规则: 必须用 trades API 找到真实成交证据才判 FILLED,
            # 无证据 → UNKNOWN(继续轮询, TTL 终态路径用同一证据规则定案)
            if ("not found" in err_str or "does not exist" in err_str or "404" in err_str
                    or "did not match expected shape" in err_str or "shape" in err_str):
                try:
                    _fs = self._fetch_fill_stats(order_id, token_id)
                    if (_fs.get("size_matched") or 0) > 0:
                        return "FILLED"
                except Exception:
                    pass
                return "UNKNOWN"
            log(f"⚠️ 订单状态查询失败 {order_id[:20]}: {str(e)[:50]}", "W")
            return "UNKNOWN"

    def _recover_last_slug(self):
        """从 DONE 文件恢复 last_slug，防止重启后重复处理当前窗口"""
        if not DONE.exists():
            return None
        try:
            # 只读文件末尾 ~8000 字节（约最后 50 行），避免全量加载大数据文件
            file_size = DONE.stat().st_size
            with open(DONE, "rb") as f:
                f.seek(max(0, file_size - 8000))
                tail_bytes = f.read()
                tail_text = tail_bytes.decode(errors="replace")
            lines = [l.strip() for l in tail_text.splitlines() if l.strip()]
            if not lines:
                return None
            # 从后往前找最后一条成功记录的 slug（逐行容错）
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 跳过损坏行，不中断
                result = entry.get("result", {})
                # 只恢复 ORDER 成功的 slug，SKIP 允许被兜底覆盖
                if result.get("ok") and not result.get("skipped"):
                    slug = entry.get("signal", {}).get("slug")
                    if slug:
                        log(f"📦 恢复 last_slug: {slug}", "I")
                        return slug
        except OSError:
            pass
        return None

    def _init_live(self):
        # Bug-3 修复: Polymarket relayer 在国内网络需要代理。
        # 8/10 修复: 不能仅"不清除代理环境变量"——bot 由 launchd 启动环境干净(无代理变量)，
        # SecureClient 的 httpx(trust_env=True) 读不到代理 → 直连 relayer 被墙
        # ([Errno 54] Connection reset / timed out)，导致止盈-持仓查询全程失败漏执行。
        # 与 dashboard api_wallet 修复一致: 先 pop 旧代理，再按网络自适应 get_proxy() 注入。
        from dotenv import load_dotenv
        load_dotenv(ENV)
        from polymarket import SecureClient

        pk = os.getenv("POLYMARKET_PK")
        if not pk:
            raise RuntimeError("POLYMARKET_PK not set in .env")

        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        _live_proxy = get_proxy()
        if _live_proxy:
            os.environ["http_proxy"] = _live_proxy
            os.environ["https_proxy"] = _live_proxy

        for attempt in range(5):
            try:
                # 8/19 根治: 传入缓存凭据跳过 POST /auth/api-key 派生（该端点间歇故障/限流，
                # 且不传 credentials 时每次 create 都派生一次）。验证走 GET /auth/api-keys。
                cached = load_pm_creds()
                if cached:
                    from polymarket.models.clob import ApiKeyCreds
                    self.client = SecureClient.create(
                        private_key=pk, credentials=ApiKeyCreds(**cached))
                else:
                    self.client = SecureClient.create(private_key=pk)
                # create 成功即缓存凭据（幂等）。若旧凭据失效，SDK 内部 fallback 派生
                # 新凭据——client.credentials 是实际持有的最新凭据，此处自动覆盖缓存。
                save_pm_creds(self.client.credentials.model_dump())
                self._client_proxy = _live_proxy  # 8/11 自适应: 记录当前 client 用的代理
                break
            except Exception as e:
                log(f"⚠️ 连接重试 {attempt+1}/5: {str(e)[:60]}", "W")
                if attempt < 4:
                    time.sleep(5)
                else:
                    raise
        wallet = getattr(self.client, "wallet", "?")
        log(f"✅ 钱包连接 | {str(wallet)[:15]}...", "I")

        try:
            bal = self.client.get_balance_allowance(asset_type="COLLATERAL")
            balance_raw = float(bal.balance)
            balance_usd = balance_raw / 1e6
            self._initial_balance = balance_usd
            log(f"💰 余额: ${balance_usd:.2f}", "I")
        except Exception as e:
            # 8/11 修复: 余额查询失败多为代理失效(Connection refused/timeout)。必须重建连接闭环:
            # reset_net_cache() 清缓存 → 清代理环境变量 → client=None 强制下次重建，
            # 否则长驻进程会一直拿着死代理(SecureClient 创建后不重探)静默挂死。
            log(f"⚠️ 余额查询失败: {str(e)[:80]} | 重置网络缓存并清代理，下次重建连接", "W")
            reset_net_cache()
            os.environ.pop("http_proxy", None)
            os.environ.pop("https_proxy", None)
            self.client = None
            self._client_proxy = None  # 8/11 审查修复: 与 client 同步失效，避免 _ensure_client 误认为代理未变
            self._initial_balance = None

    def _ensure_client(self):
        """8/11 网络自适应: 每次交互前确保 client 与当前网络代理一致。

        与 dashboard api_wallet 一致的思路——长驻进程不持固定 client，
        每次使用前通过 get_proxy()(带 TTL 缓存+失败强制重探)拿最新代理，
        与当前 client 使用的代理比对；网络切换(换 VPN/代理失效)导致代理变化
        或连接断开时，置 client=None 强制 _init_live() 重建，真正做到自适应。
        返回 True 表示 client 可用，False 表示失败(调用方自行处理)。
        """
        if not self.live:
            return True  # paper 模式无需钱包
        try:
            # get_proxy() 带 TTL 缓存：每 NET_PROXY_TTL 秒自动重探一次，覆盖换网络/VPN
            latest = get_proxy()
            # 代理变化(切换 VPN/代理失效→直连等) → 旧 client 不再适用，强制重建
            if latest != self._client_proxy:
                if self.client is not None:
                    log(f"🌐 网络切换: 代理 {self._client_proxy or '直连'} → {latest or '直连'}，重建钱包连接", "I")
                self.client = None
                self._client_proxy = None
            if self.client is None:
                self._init_live()  # 内部会更新 self._client_proxy
            return self.client is not None
        except Exception as e:
            log(f"⚠️ 钱包连接确保失败: {str(e)[:60]}", "W")
            return False

    def _find_existing_order(self, token_id):
        """检查是否已存在同 token 的 open order，返回 order_id 或 None

        用 list_open_orders(token_id=) 替代不存在的 get_orders()
        """
        if not self.client:
            return None
        try:
            paginator = self.client.list_open_orders(token_id=token_id)
            for order in paginator:
                o_id = getattr(order, 'id', None) or (order.get("id") if isinstance(order, dict) else None)
                if o_id:
                    return str(o_id)
        except Exception:
            pass
        return None

    def place_order(self, token_id, price, size, side="BUY"):
        # 基础参数校验
        if price <= 0 or price >= 1:
            return {"ok": False, "error": f"价格越界: {price}"}
        if size < 0.1:
            return {"ok": False, "error": f"仓位过小: {size:.2f}sh"}
        # Polymarket 要求 tick size 0.01，最多 2 位小数
        price = round(price, 2)
        if not self.live:
            log(f"📝 [PAPER] {side} {size:.0f}sh @ {price}", "I")
            return {"ok": True, "order_id": f"PAPER_{int(time.time())}"}

        # 延迟连接:第一次 ORDER 时才连；8/11 自适应: 每次下单前确保 client 与当前网络一致
        if not self._ensure_client():
            return {"ok": False, "error": "钱包连接失败(网络/代理异常)"}
        if not self.client:
            return {"ok": False, "error": "client not connected"}

        # 9/2: 交易所限制静默期——"trading is disabled" 维护期间每小时只探测一次，不空烧重试
        if time.time() < getattr(self, "_exchange_disabled_until", 0):
            return {"ok": False, "error": "exchange trading disabled (silent window)"}

        # Polymarket: 做多=买UP token, 做空=买DOWN token → 都是BUY
        api_side = "BUY"  # UP/DOWN 都对应 BUY，token_id 区分方向

        for attempt in range(3):
            try:
                resp = self.client.place_limit_order(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=api_side,
                )
                # SDK 返回 AcceptedOrder(ok=True, order_id, status) 或 RejectedOrder(ok=False, code, message)
                ok = getattr(resp, 'ok', None)
                if ok is True:
                    oid = str(resp.order_id)
                    status = getattr(resp, 'status', 'unknown')
                    log(f"🔥 [LIVE] {side} {size:.0f}sh @ {price} → {oid[:20]} (status={status})", "I")
                    # round41.5.1(8/24): 带出 SDK status（matched=立即成交）--
                    # taker 单下单即成交，调用方据此直接记终态、不再进轮询
                    return {"ok": True, "order_id": oid, "status": str(status).lower()}
                elif ok is False:
                    # RejectedOrder
                    code = getattr(resp, 'code', 'unknown')
                    msg = getattr(resp, 'message', '')
                    log(f"❌ 下单被拒: {code} {msg}", "E")
                    return {"ok": False, "error": f"rejected: {code} {msg}"}
                else:
                    # 兜底：兼容旧版 dict 响应
                    if isinstance(resp, dict):
                        oid = resp.get("order_id") or resp.get("id") or resp.get("orderID")
                        if oid:
                            return {"ok": True, "order_id": str(oid)}
                    return {"ok": False, "error": f"unexpected response: {str(resp)[:100]}"}
            except Exception as e:
                # 8/19: 带 HTTP 状态码（RequestRejectedError.status）——4xx=权限/地域拒绝, 5xx=服务端故障
                log(f"❌ 下单失败 {attempt+1}/3: [HTTP {getattr(e, 'status', '?')}] {type(e).__name__}: {str(e)[:200]}", "W")
                # 9/2: 交易所限制识别——"trading is disabled"=平台维护/限制中，
                # 重试无意义。置10分钟静默期（官方维护通常1.5小时内恢复，
                # 10分钟一探测既不空烧也能尽快恢复）；结构化字段
                # (code/restriction/retry_after)服务端均未提供，已用真实探测单验证
                if "trading is disabled" in str(e).lower():
                    self._exchange_disabled_until = time.time() + 600
                    log("🚫 交易所交易禁用(trading is disabled)——平台维护/限制中，本窗口放弃，10分钟后自动重新探测", "E")
                    return {"ok": False, "error": "trading disabled (exchange restriction)"}
                if attempt < 2:
                    # 防重复下单：重试前先查证是否已存在同 token 订单（网络抖动可能已成交）
                    try:
                        existing = self._find_existing_order(token_id)
                        if existing:
                            log(f"♻️ 已存在同 token 订单，视为成功: {existing[:20]}", "W")
                            return {"ok": True, "order_id": existing}
                    except Exception:
                        pass
                    time.sleep(3)
        # Bug-7 修复: 所有重试失败后，做最终检查确认是否已有同 token 订单
        try:
            existing = self._find_existing_order(token_id)
            if existing:
                log(f"♻️ 最终检查发现已存在订单，视为成功: {existing[:20]}", "W")
                return {"ok": True, "order_id": existing}
        except Exception:
            pass
        return {"ok": False, "error": "3次重试均失败"}

    def find_signal_for_slug(self, slug):
        """返回最新匹配的信号（队列里同 slug 可能有多条，取最后一条）

        8/4 BOSS 指令:只认 btc-agent 子代理信号(source 白名单),
        其他源(v5/fallback/墨痕/Trae AI 等)全部忽略,防止多源抢队列打架。
        """
        if not QUEUE.exists():
            return None
        try:
            with open(QUEUE, encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            found = None
            rejected = 0
            for l in lines:
                try:
                    sig = json.loads(l)
                except json.JSONDecodeError:
                    continue
                if sig.get("slug") != slug:
                    continue
                if not _source_allowed(sig):
                    rejected += 1
                    continue  # 非白名单 source 跳过
                found = sig  # 继续找，取最后一条
            if rejected and not found:
                log(f"🚫 {slug} | 队列有 {rejected} 条非 btc-agent 信号被白名单拦截", "W")
            return found
        except (json.JSONDecodeError, OSError):
            return None

    def clear_queue(self, slug: str | None = None):
        """清空队列。指定 slug 时只删该 slug 的信号，保留其他。"""
        if not QUEUE.exists():
            return
        if slug is None:
            try:
                QUEUE.unlink()
            except OSError:
                pass
            return
        # Bug-6 修复: 带 fcntl 排他锁的 read-modify-write，防止丢失 analyzer 同时追加的新信号
        lock_path = QUEUE.with_suffix(".lock")
        try:
            with open(lock_path, "w", encoding="utf-8") as lf:
                _flock_ex(lf)
                try:
                    lines = [l.strip() for l in QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
                    kept = []
                    for l in lines:
                        try:
                            sig = json.loads(l)
                            if sig.get("slug") != slug:
                                kept.append(l)
                        except json.JSONDecodeError:
                            kept.append(l)  # 保留无法解析的行
                    tmp = QUEUE.with_suffix(".tmp")
                    tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                    tmp.replace(QUEUE)
                finally:
                    _flock_un(lf)
        except OSError:
            pass

    def _cleanup_stale_queue(self):
        """启动时清理队列中非当前小时的过期信号，防止 dashboard 显示旧信号。"""
        if not QUEUE.exists():
            return
        current_slug = build_slug(now_et())
        try:
            lines = [l.strip() for l in QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
            kept = []
            removed = 0
            for l in lines:
                try:
                    sig = json.loads(l)
                    if sig.get("slug") == current_slug:
                        kept.append(l)
                    else:
                        removed += 1
                except json.JSONDecodeError:
                    kept.append(l)
            if removed > 0:
                tmp = QUEUE.with_suffix(".tmp")
                tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                tmp.replace(QUEUE)
                log(f"🧹 启动清理: 移除 {removed} 条过期信号，保留 {len(kept)} 条", "I")
        except OSError:
            pass

    @staticmethod
    def _load_risk_cfg():
        """任务0.1(2026-08-23 升级计划书): 金额风控配置。
        - min_bet_amount: UI bet_amount 合法地板(防 $0.50 抖动事故复发)，低于它视为异常
          → 回退信号自带 amount，不再让异常值进入下单链。默认 1.0(=Polymarket CLOB 最小单)。
        - position_scale_enabled: 缩仓真实生效开关。false(默认)=维持 8/12 用户决策
          「金额按 UI 全额」；true=position_scale 真实乘进金额。开启属规则行为变更，
          须按 SYNC_MAP 同步 prompt_exec/SKILL 后再开。
        - default_bet_amount: 下单金额的最终兜底。A1(2026-08-23): 信号不再携带 amount
          (8/10 起 done.signal.amount 恒 None)，原硬编码 fallback=3.0 会造成 amount 抖动、
          且 review 读不到正确金额把真实成交单 pnl 记成 0。改为 config 兜底，缺省沿用 10.0。"""
        cfg = {"min_bet_amount": 1.0, "position_scale_enabled": False, "default_bet_amount": 10.0,
               "rule_a_skip_enabled": True, "rule_a_price_max": 0.55,
               "rule_a_hour_start": 16, "rule_a_hour_end": 21,
               # 9/4 事故复盘：P(side) 双门控 SKIP（prob_bucket_stats.py 分桶实证）：
               #  1) P < prob_skip_min：P=0.308 档 7 笔 2W/5L 累计 -19.66 负EV；
               #  2) P ∈ [prob_skip_band_lo, prob_skip_band_hi)：0.733 档 24 笔仅 37.5% 胜率
               #     （声称 70-79%），无标签组更差(25%/-52.04)，虚高陷阱整档禁入；
               # 同开关 prob_skip_enabled 控制，回滚=config 置 false。
               "prob_skip_enabled": True, "prob_skip_min": 0.40,
               "prob_skip_band_lo": 0.70, "prob_skip_band_hi": 0.80,
               # 9/4 盘感覆盖层开关：fetch_book_momentum(CLOB 动能旁路)与信号方向冲突时 SKIP。
               # 默认 FALSE（live 安全）；paper 实弹验证命中率后再开闸，回滚=config 置 false。
               "momentum_skip_enabled": False}
        try:
            if CONFIG.exists():
                _c = json.loads(CONFIG.read_text(encoding="utf-8"))
                _mb = _c.get("min_bet_amount")
                if isinstance(_mb, (int, float)) and _mb >= 0.5:
                    cfg["min_bet_amount"] = float(_mb)
                cfg["position_scale_enabled"] = bool(_c.get("position_scale_enabled", False))
                # round41.8 出血点拦截规则A参数(config 可调; 一键回滚 = 置 rule_a_skip_enabled=false)
                cfg["rule_a_skip_enabled"] = bool(_c.get("rule_a_skip_enabled", True))
                _rap = _c.get("rule_a_price_max")
                if isinstance(_rap, (int, float)) and 0 < _rap < 1:
                    cfg["rule_a_price_max"] = float(_rap)
                for _rk in ("rule_a_hour_start", "rule_a_hour_end"):
                    _rv = _c.get(_rk)
                    if isinstance(_rv, (int, float)) and 0 <= _rv <= 23:
                        cfg[_rk] = int(_rv)
                _da = _c.get("default_bet_amount")
                if isinstance(_da, (int, float)) and _da >= cfg["min_bet_amount"]:
                    cfg["default_bet_amount"] = float(_da)
                cfg["prob_skip_enabled"] = bool(_c.get("prob_skip_enabled", True))
                _psm = _c.get("prob_skip_min")
                if isinstance(_psm, (int, float)) and 0 < _psm < 1:
                    cfg["prob_skip_min"] = float(_psm)
                _psl = _c.get("prob_skip_band_lo")
                _psh = _c.get("prob_skip_band_hi")
                if (isinstance(_psl, (int, float)) and isinstance(_psh, (int, float))
                        and 0 < _psl < _psh <= 1):
                    cfg["prob_skip_band_lo"] = float(_psl)
                    cfg["prob_skip_band_hi"] = float(_psh)
                cfg["momentum_skip_enabled"] = bool(_c.get("momentum_skip_enabled", False))
        except Exception:
            pass
        return cfg

    @staticmethod
    def _apply_position_scale(sig, amt, mx, scale_enabled=False):
        """8/12: position_scale 判定保留、金额缩仓废除（用户决策: 金额一律按 UI bet_amount 全额）。
        任务0.1(2026-08-23): 新增 scale_enabled 开关（config position_scale_enabled）——
        false(默认)=维持 8/12 行为只记日志；true=真实缩仓（"min"→max($3.0,5×max_price)，数字→amt×scale）。
        返回 (amt, log_msg_or_None)"""
        _ps = sig.get("position_scale", 1.0)
        if isinstance(_ps, str) and _ps.strip().lower() == "min":
            _min_amt = max(3.0, 5 * mx)
            if scale_enabled and _min_amt < amt:
                return _min_amt, f"position_scale=min 真实缩仓 ${amt:.2f}→${_min_amt:.2f} (max($3.0, 5×{mx:.2f}))"
            return amt, f"position_scale=min 规则判定最小仓 ${_min_amt:.2f} (max($3.0, 5×{mx:.2f}))，但金额按 UI 全额 ${amt:.2f}"
        try:
            _psf = float(_ps)
        except (TypeError, ValueError):
            return amt, None
        if 0 < _psf < 1:
            if scale_enabled:
                _new = round(amt * _psf, 2)
                return _new, f"position_scale={_psf:g} 真实缩仓 ${amt:.2f}→${_new:.2f}"
            return amt, f"position_scale={_psf:g} 规则判定缩仓，但金额按 UI 全额 ${amt:.2f}"
        return amt, None

    def save_last_signal(self, sig, result):
        """保存最后处理的信号供 Dashboard 展示（原子写入）
        8/8 修复: 回写实际下单 amount/max_price 到 sig，防止 last_signal 记录信号原值导致 UI 显示错误"""
        try:
            # 重新计算实际下单用的 amount/max_price（与 process_signal 同逻辑）
            # A1: 金额兜底取 config，与 process_signal 保持一致
            _amt = float(sig.get("amount") or self._load_risk_cfg()["default_bet_amount"])
            _mx = float(sig.get("max_price") or MAX_ORDER_PRICE)
            try:
                _risk = self._load_risk_cfg()
                if CONFIG.exists():
                    _cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
                    _ui_bet = float(_cfg.get("bet_amount", 0))
                    if _ui_bet >= _risk["min_bet_amount"]:
                        _amt = _ui_bet
            except Exception:
                pass
            _scale_on = self._load_risk_cfg()["position_scale_enabled"]
            _amt, _ = self._apply_position_scale(sig, _amt, _mx, scale_enabled=_scale_on)
            # 回写实际下单值到 sig 副本（不修改原始信号）
            sig_display = dict(sig)
            sig_display["amount"] = _amt
            # round41.8.19: max_price 语义拆分——下方把本字段改写为实际挂单价（UI 展示），
            # 分析层 EV 核算基准（封顶 0.60 / 翻向重算值）保留进 max_price_signal，
            # 避免「max_price=0.8 与 ev=(P−0.60)×100 同框」的口径打架（8/25 1am 场实证）。
            if sig.get("max_price") is not None:
                sig_display["max_price_signal"] = sig.get("max_price")
            # 显示实际挂单价（result.order_price），无则回退信号原值，保证 UI 与真实订单一致
            sig_display["max_price"] = result.get("order_price", _mx)
            data = {"signal": sig_display, "result": result, "processed_at": datetime.now(timezone.utc).isoformat()}
            tmp = LAST_SIGNAL.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(LAST_SIGNAL)
        except Exception:
            pass

    def process_signal(self, sig):
        a = sig.get("action", "").upper()
        slug = sig.get("slug", "")
        # 9/3 P1-5 修复: side 键存在但值为 None 时 .get(default) 不生效 → None.upper()
        # 每 tick 崩溃连环重试 60 次后进程退出；用 or 链兜底。
        side = (sig.get("side") or sig.get("signal") or "").strip().upper()
        # 9/3 P1-5 修复: 非法/脏 side 值（None/"UP "带空格/"LONG"等）原会静默走
        # else 分支买 DOWN token（方向反转无告警）。现入口校验，非法直接拒收留日志。
        if side not in ("UP", "DOWN"):
            # skipped 语义: 拒收即终态写 done（防每 tick 重试刷日志），SKIP 可被
            # 同 slug 后续合法修正信号覆盖（_slug_already_done 只认 ORDER 成功）。
            log(f"❌ 非法 side={side!r}，拒收信号防方向反转 | {slug}", "E")
            return {"ok": True, "skipped": True, "skip_reason": f"invalid side: {side!r}"}
        # 9/2 审查修复(B2): 删除 8/17 遗留的硬编码 slug 方向劫持
        # （if slug == "bitcoin-up-or-down-august-17-2026-12am-et": side = "UP"）。
        # 该 hack 无条件改写引擎信号方向，日期已过属休眠死代码，但属于
        # "数据驱动硬编码"危险模式，防止被复制或误触发。
        log(f"DEBUG: full sig content: {sig}", "I")
        if a not in ("ORDER", "SKIP"):  # 修复缩进错误(8/17): action 缺失/非法 → 默认 ORDER(8/5 BOSS:分析好了就下单)
            log(f"⚠️ action 缺失,白名单 source 默认 ORDER | {slug} | 8/10 兜底", "W")
            a = "ORDER"

        # round41.8.7(2026-08-25 用户决策「暴雷止血」): 全局暂停实盘下单开关。
        # config hermes_config.json "trading_paused": true → 信号照收、PM序列照记，
        # 但不下单不写 done（与 SKIP 同路径），复盘按未成交/p_l=0 处理。
        # 读取异常按暂停处理(fail-closed)：止损开关宁可误停不可漏停。恢复=config 置 false 或 UI 按钮。
        if a == "ORDER" and _trading_pause_active():
            log(f"⏸️ 交易暂停(trading_paused) | {slug} | 信号已收但不入市", "W")
            return {"ok": True, "skipped": True, "skip_reason": "trading_paused(config r4187)"}

        # 8/5 BOSS 指令: 分析好了就下单,去掉 EV 否决
        # no_trade 信号不再 SKIP,改走 ORDER 流程(填充默认 amount/max_price)
        # 8/8 BOSS 指令: 下单金额优先读 UI 配置的 bet_amount(用户可调),信号自带 amount 作为 fallback
        try:
            # A1(2026-08-23): 金额兜底取 config default_bet_amount，不再硬编码 3.0
            _default_amt = self._load_risk_cfg()["default_bet_amount"]
            amt = float(sig.get("amount") or _default_amt)
            mx = float(sig.get("max_price") or MAX_ORDER_PRICE)
            # 优先用 UI 配置的 bet_amount 覆盖信号自带 amount
            # round45(2026-08-26 用户决策「UI 小金额=SKIP 开关」): 改原抖动回退逻辑——
            # UI bet_amount < min_bet_amount 视为用户主动 SKIP(用户原意"调到 $0.50 即不交易"),
            # 不再回退到信号默认金额。原 8/23 任务0.1 防抖动事故机制作废,
            # 若需回归改回回退模式,把下面 elif 分支去掉 + 加 if _ui_bet>=min: amt=_ui_bet 即可。
            try:
                _risk = self._load_risk_cfg()
                if CONFIG.exists():
                    _cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
                    _ui_bet = float(_cfg.get("bet_amount", 0))
                    if _ui_bet > 0 and _ui_bet < _risk["min_bet_amount"]:
                        log(f"⏸️ UI bet_amount=${_ui_bet:.2f} 低于地板 ${_risk['min_bet_amount']:.2f}"
                            f" → 用户主动 SKIP 本窗口 | {slug}", "W")
                        return {"ok": True, "skipped": True, "skip_reason": f"ui_bet_below_threshold(${_ui_bet:.2f}<${_risk['min_bet_amount']:.2f})"}
                    if _ui_bet >= _risk["min_bet_amount"]:
                        amt = _ui_bet
            except Exception:
                pass
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid amount/max_price"}

        # 8/10: position_scale 结构化缩仓（prompt.md 5.6 / 步骤9.7；在风控检查前应用，风控按缩仓后金额统计）
        # 任务0.1(2026-08-23): 缩仓是否真实生效由 config position_scale_enabled 控制（默认 false=8/12 行为）
        _scale_on = self._load_risk_cfg()["position_scale_enabled"]
        amt, _ps_msg = self._apply_position_scale(sig, amt, mx, scale_enabled=_scale_on)
        if _ps_msg:
            log(f"📉 {_ps_msg} | {slug}", "I")

        if mx <= 0 or mx >= 1:
            log(f"❌ 非法限价 {mx}", "E")
            return {"ok": False, "error": f"invalid max_price: {mx}"}

        # 8/4 EV 合理性校验:防止错误 EV 公式(如 payout=1/max_price)产生荒谬值触发下单
        try:
            ev_raw = sig.get("ev")
            if ev_raw is not None:
                ev_val = float(ev_raw)
                if abs(ev_val) > 400:  # 单位=每$100美元口径(round41.8.2);|ev|>400 即期望偏离 ±400%/单 必定是公式错误
                    log(f"🚫 EV 异常 {ev_val:.2f} (阈值±400, 每$100美元口径) | {slug} | 拒收,防止错误公式触发下单", "E")
                    return {"ok": False, "error": f"invalid EV: {ev_val:.2f} (likely wrong formula)"}
        except (TypeError, ValueError):
            pass  # EV 字段不存在或非数字,跳过校验

        # 9/4 事故复盘概率门控(双区间): P(side) 低于 prob_skip_min 或落入
        # [prob_skip_band_lo, prob_skip_band_hi) 虚高档 → SKIP 不追。
        # 实证(prob_bucket_stats.py, n=175): P=0.308 档 7 笔 2W/5L 累计 -19.66(均 -2.81)
        # 28.6% 胜率负EV; P∈[0.70,0.80) 档 24 笔仅 37.5% 胜率(声称70-79%), 无标签组 25%/-52.04
        # 比带收割标签组(43.8%/-30.59)更差 → 问题在档位本身, 整档禁入。同时兜住
        # 「方向与概率错配」(3am 单 side=UP 但 P=0.308)。
        # 8/5 "去掉 EV 否决" 后引擎负EV信号无拦截是 3am -$6 亏损根因之一, 此处恢复只留概率线。
        # 开关: hermes_config.json prob_skip_enabled(默认 true), 回滚=置 false。
        _rcfg_prob = self._load_risk_cfg()
        if _rcfg_prob.get("prob_skip_enabled", True):
            _ps_min = float(_rcfg_prob.get("prob_skip_min", 0.40))
            _ps_lo = float(_rcfg_prob.get("prob_skip_band_lo", 0.70))
            _ps_hi = float(_rcfg_prob.get("prob_skip_band_hi", 0.80))
            _p_val = sig.get("probability_raw")
            if _p_val is None:
                _p_val = sig.get("probability")
            try:
                _p_skip = float(_p_val)
                if _p_skip > 1:          # 兼容百分比口径(100→1.0)
                    _p_skip /= 100.0
            except (TypeError, ValueError):
                _p_skip = 0.5            # 概率缺失时放行(保持原行为不变)
            if _p_skip < _ps_min:
                log(f"🚫 P(side)={_p_skip:.3f} < {_ps_min:.2f} → SKIP（9/4 概率门控·低概率档）| {slug}", "W")
                return {"ok": True, "skipped": True,
                        "skip_reason": f"prob_below_min({_p_skip:.3f}<{_ps_min:.2f})"}
            if _ps_lo <= _p_skip < _ps_hi:
                log(f"🚫 P(side)={_p_skip:.3f} ∈ [{_ps_lo:.2f},{_ps_hi:.2f}) 虚高档 → SKIP（9/4 概率门控）| {slug}", "W")
                return {"ok": True, "skipped": True,
                        "skip_reason": f"prob_band_skip({_p_skip:.3f} in [{_ps_lo:.2f},{_ps_hi:.2f}))"}

        # 9/4 盘感覆盖层（旁路 CLOB 动能门）：book_momentum flag 与信号方向冲突 → SKIP。
        # 实证（代理回测 666 场）：盘口标记价演变方向命中 77.6-84.1%（|Δ| 越大越强），
        # 引擎盘口类因子 75-88%；DUMP_TRAP/ASK_SURGE 正是"诱空拉涨/买UP扫盘"剧本信号。
        # 只做单向否决（SKIP），不做方向翻转——欺诈止损优先，翻转属激进策略另议。
        # 开关 momentum_skip_enabled 默认 false（live 安全），paper 验证后开闸，回滚=config 置 false。
        if _rcfg_prob.get("momentum_skip_enabled", False):
            try:
                _mom = fetch_book_momentum(slug, 40)  # bot :40 决策点窗口
            except Exception as _e:
                _mom = None
                log(f"[!] 盘感层取数失败(放行): {str(_e)[:80]} | {slug}", "W")
            if _mom and _mom.get("samples", 0) >= 2:
                _flag = _mom.get("flag")
                _conflict = momentum_conflict(_flag, side)
                if _conflict:
                    _rev = "看涨动能" if _flag in ("DUMP_TRAP", "ASK_SURGE") else "UP走弱"
                    log(f"🚫 盘感覆盖层 {_flag}<{_rev}> vs 信号{side} → SKIP（CLOB动能与方向冲突）"
                        f" | {slug} | Δask_up={_mom.get('up_ask_delta')} 大单={_mom.get('max_down_bid1_sz')}", "W")
                    return {"ok": True, "skipped": True,
                            "skip_reason": f"momentum_conflict({_flag} vs {side})"}
                log(f"👁️ 盘感覆盖层 {_flag} 与信号{side}同向/持平，放行 | {slug}", "I")

        reason = sig.get("reason", "")

        # 8/5 BOSS 指令: no_trade 信号强制走 ORDER,不再 SKIP
        if sig.get("no_trade"):
            log(f"⚡ no_trade → ORDER | {slug} | {sig.get('no_trade_reason', 'EV 不达标')[:50]} | 强制下单(BOSS 8/5 指令)", "I")
            a = "ORDER"

        if a == "SKIP" and not sig.get("no_trade"):
            log(f"⏭️ SKIP | {slug} | {reason}", "I")
            return {"ok": True, "skipped": True}

        if a != "ORDER":
            log(f"❓ 未知动作: {a}", "W")
            return {"ok": False, "error": f"unknown action: {a}"}

        # B3: 风控检查（B4: transient=True 表示瞬时性失败，调用方保留信号重试）
        risk_ok, risk_reason, risk_transient = self._check_risk(slug, side, amt)
        if not risk_ok:
            log(f"🛑 风控拦截{'(瞬时,保留信号)' if risk_transient else ''} | {slug} | {risk_reason}", "W")
            return {"ok": True, "skipped": True, "risk_reason": risk_reason, "transient": risk_transient}

        log(f"🎯 {side} | {slug} | ${amt} max${mx} | {reason}", "I")

        m = get_market(slug)
        if not m:
            return {"ok": False, "error": "market not found"}

        # 安全解析 tokens 和 prices
        raw_tokens = m.get("clobTokenIds", "[]")
        raw_prices = m.get("outcomePrices", "[]")
        try:
            tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else (raw_tokens or [])
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else (raw_prices or [])
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "error": "invalid token/price format"}

        if len(tokens) < 2 or len(prices) < 2:
            return {"ok": False, "error": "insufficient tokens/prices"}

        up_id, dn_id = tokens[0], tokens[1]
        try:
            up_p = float(prices[0])
            dn_p = float(prices[1])
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid outcome prices"}

        tok = up_id if side == "UP" else dn_id

        # 8/18 实时PM极端反转拦截（用户决策）：信号在 :15 采集，bot 在 :23 下单，存在约 8 分钟时差。
        # 本次事故（02:15→02:23）PM DOWN 从 0.755 崩到 0.155（bot 下单时实时 0.385、成交 0.18），
        # 信号沿旧共识做空 DOWN，bot 却成交在 DOWN 崩盘区全亏 $5.04。
        # 两级处理（8/18 交叉复查 + 用户决策）：
        #  ① **极端反转硬拦截（无条件）**：实时方向价 <0.25 = 市场已 ≥75% 定价对家胜出，属极端反转，
        #     风险不可控，**SKIP 不成交**。此判据不依赖快照字段（bot 实时 get_market 必有），无条件生效。
        #  ② **一般波动记录不拦截**：实时≥0.25 但相对快照 PM 跌幅>15% → 写日志 + result 带 pm_volatile
        #     标记供复盘，**继续正常下单**（历史回测显示 1H 合约 PM 小时内跌 15% 是常态，拦截会误杀
        #     大量正常盈利单，故一般波动不拦）。
        target_live = dn_p if side == "DOWN" else up_p  # 我们要买方向的实时价
        if target_live < 0.25:
            log(f"🛑 实时PM极端反转拦截 | {slug} | 信号 {side} 但实时{side}价={target_live:.3f}<0.25（市场已≥75%定价对家胜出），SKIP 不成交", "W")
            return {"ok": True, "skipped": True,
                    "skip_reason": f"实时PM极端反转: {side}实时价{target_live:.3f}<0.25"}
        pm_volatile = None
        snap_key = "pm_down_price" if side == "DOWN" else "pm_up_price"
        try:
            snap_pm = float(sig.get(snap_key)) if sig.get(snap_key) is not None else None
        except (TypeError, ValueError):
            snap_pm = None
        if snap_pm is not None and snap_pm > 0:
            drop = (snap_pm - target_live) / snap_pm
            if target_live < snap_pm and drop > 0.15:
                pm_volatile = (f"实时{side}价{target_live:.3f}相对快照{snap_pm:.3f}跌幅{drop*100:.0f}%>15%")
        if pm_volatile:
            log(f"⚠️ PM大幅波动(记录不拦截) | {slug} | 信号 {side} 但{pm_volatile}，继续下单（复盘标记）", "W")

        # round41.4 C4 影子模式：分层记录 PM 不利移动，**不拦截下单**。
        # 目的：用真实分布校准斜率守卫阈值（历史教训：PM 小时内跌15%是常态，激进拦截会误杀盈利单）。
        # 候选阈值 = 绝对概率点 pp：t05/t06/t08/t10/t12；legacy_rel15 为旧相对口径对照。
        # 攒 ≥2 周数据后由用户拍板是否转正式拦截及采用哪档阈值（8/23 3AM 场挂单时跌 7.5pp、成交时 17.5pp）。
        if snap_pm is not None and snap_pm > 0 and target_live < snap_pm:
            _drop_pp = round(snap_pm - target_live, 4)
            _drop_rel = round((snap_pm - target_live) / snap_pm, 4)
            _shadow = {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "slug": slug, "side": side,
                "snap_pm": snap_pm, "live": target_live,
                "drop_pp": _drop_pp, "drop_rel": _drop_rel,
                "shadow_t05": _drop_pp >= 0.05, "shadow_t06": _drop_pp >= 0.06,
                "shadow_t08": _drop_pp >= 0.08, "shadow_t10": _drop_pp >= 0.10,
                "shadow_t12": _drop_pp >= 0.12,
                "legacy_rel15": _drop_rel > 0.15,
                "max_price": mx,
            }
            try:
                with open(DATA_DIR / "pm_guard_shadow.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(_shadow, ensure_ascii=False) + "\n")
                if _shadow["shadow_t05"]:
                    _tier = "取消级" if _shadow["shadow_t12"] else ("减半级" if _shadow["shadow_t08"] else "观察级")
                    log(f"🧪 PM斜率守卫[影子·不拦截] | {slug} | 快照{snap_pm:.3f}→实时{target_live:.3f}"
                        f" 跌{_drop_pp*100:.1f}pp → 影子判定:{_tier}", "I")
            except Exception as e:
                log(f"⚠️ PM守卫影子记录写入失败 | {str(e)[:50]}", "W")

        # B12: 流动性预检
        liq_ok, best_ask, _liq_spread, liq_reason = self._check_liquidity(tok, mx)
        if not liq_ok:
            log(f"📉 流动性不足 | {slug} | {liq_reason}", "W")
            return {"ok": True, "skipped": True, "liquidity_reason": liq_reason}

        # ===== round41.8(2026-08-24 用户批准) 出血点拦截·规则A / round41.8.4 暂停观察模式 =====
        # 回测依据(SYNC_MAP 十三): 「入场<0.55 × 16-21ET」37笔仅35%胜率、净-$105 最大失血点;
        # 反事实改善 +$105.34。样本内优化风险(Bonferroni 后 p≈0.09) → 用户 8/24 决定**暂停拦截、
        # 只留观察账**: 条件命中时无论开关状态都写 hermes_shadow_rule_a.jsonl（blocked 字段区分），
        # 开关=false 时正常放行下单——攒够样本后按真实结算分桶对比决定是否恢复拦截。
        try:
            _rcfg = self._load_risk_cfg()
            _ra_on = bool(_rcfg.get("rule_a_skip_enabled"))
            if best_ask:
                _hs = slug.split('-')[-2]
                _hr = int(_hs[:-2])
                if _hs.endswith('pm') and _hr != 12:
                    _hr += 12
                if _hs.endswith('am') and _hr == 12:
                    _hr = 0
                if (best_ask < float(_rcfg.get("rule_a_price_max", 0.55))
                        and int(_rcfg.get("rule_a_hour_start", 16)) <= _hr < int(_rcfg.get("rule_a_hour_end", 21))):
                    try:
                        with open(SHADOW_RULE_A, "a", encoding="utf-8") as _raf:
                            _raf.write(json.dumps({
                                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "slug": slug, "side": side, "would_be_ask": best_ask,
                                "praw": sig.get("probability_raw", sig.get("probability")),
                                "pm_up": sig.get("pm_up_price"), "pm_dn": sig.get("pm_down_price"),
                                "hour_et": _hr,
                                "mode": "r418-rule-a-skip" if _ra_on else "r418-observe-paused",
                                "blocked": bool(_ra_on),
                            }, ensure_ascii=False) + "\n")
                    except OSError:
                        pass
                    if _ra_on:
                        log(f"🛑 规则A出血点拦截 | {slug} | ask={best_ask:.2f}<0.55 且 {_hr}:00ET∈[16,21) "
                            f"晚间超低价历史胜率仅35%(已记影子账,r418)，SKIP", "W")
                        return {"ok": True, "skipped": True,
                                "skip_reason": f"规则A: 晚间({_hr}ET)超低价{best_ask:.2f} 出血点拦截(r418)"}
                    log(f"🟡 规则A暂停观察中 | {slug} | ask={best_ask:.2f}<0.55 且 {_hr}:00ET∈[16,21) "
                        f"命中出血点画像，照常下单仅记观察账(r418.4)", "I")
        except Exception as _e:  # noqa: BLE001 - 解析失败 fail-open 不影响正常下单
            log(f"⚠️ 规则A判定异常(不影响下单) | {_e}", "W")

        order_price = best_ask if best_ask else mx
        gamma_price = up_p if side == "UP" else dn_p

        # 8/21 方案A3 (BOSS 决策): taker 首挂 — 直接吃实时 best_ask 立即成交。
        # 依据: 超时取消单方向对率 88.4%(99对/13错) vs 成交单胜率 53.2%（fills.jsonl 382单），
        # 挂低价排队 = 结构性逆向选择（错单跌回挂价成交、对单价格走掉永远追不上）。
        # CLOB 限价买单挂 best_ask 即与最优卖单撮合，按 ask 实际价成交，不会多付。
        # book 缺失时兜底用信号旧价与 gamma 中较低者。
        # 历史: 原 8/5 gamma×0.90/0.95/0.98 打折 → 买不到；8/9 方案A2 实时价为主但保留
        #       gamma*0.92 低价分支 → 仍部分买不到；本次彻底删除打折分支。
        # round41.5 (8/24): A3 taker 首挂落地(用户决策 1A)，effective_cap「等跌」压价废除；
        # round41.7 (8/24): 两段式进场取代单段 taker；
        # round52.4 (9/2): 两段式废除，首挂一律跨价吃实时 ask（成交率优先，用户拍板）。
        conf = str(sig.get("confidence", "")).strip()
        conf_lower = conf.lower()
        if "高" in conf_lower or "high" in conf_lower:
            conf_label = "高"
        elif "中" in conf_lower or "med" in conf_lower or "middle" in conf_lower:
            conf_label = "中"
        else:
            conf_label = "低"

        # ===== round52.4(2026-09-02 用户拍板): 首挂直接按实时卖一价吃，成交率优先 =====
        # 原 round41.7 两段式（便宜区 taker 快道 / 贵区低价挂单静置等回落）已废除：
        # 低价静置在单边行情里挂价永远落后 ask，是"方向对但买不到"的根源
        # （9/2 6am-et 实证：阶梯追价 7 档到 0.95 仍踏空，而信号时刻 ask 仅 0.85≈praw 0.846）。
        # CLOB 跨价单：限价买挂 best_ask 立即与最优卖单撮合，按 ask 实际价成交，不会多付。
        # 已知代价(用户知情): 成交价=实时 ask，可能高于引擎 praw，成交率优先于单笔 EV。
        if not best_ask:
            # book 缺失：沿袭 round37/41.5 兜底口径（信号价封顶）
            order_price = min(mx, gamma_price)
            effective_cap = _effective_price_cap(sig)
            if order_price > effective_cap:
                _lane = "高概率通道" if effective_cap > MAX_ORDER_PRICE else "统一上限"
                log(f"💡 挂单价 {order_price:.3f} > {_lane} {effective_cap}（book 缺失兜底）| {slug}", "I")
                order_price = effective_cap
            _mode = "book缺失兜底"
        elif best_ask <= 0.99:
            order_price = round(best_ask, 2)
            effective_cap = max(TAKER_MAX, order_price)  # 恢复单兼容: price_cap 仅透传不封顶
            _mode = "taker吃价(成交率优先,round52.4)"
        else:
            # best_ask > 0.99 不可能(CLOB tick 上限 0.99)，防御性兜底按 book 缺失口径
            order_price = min(mx, gamma_price)
            effective_cap = _effective_price_cap(sig)
            if order_price > effective_cap:
                order_price = effective_cap
            _mode = "ask异常兜底"
        log(f"    🎯 置信度={conf_label} | Gamma={gamma_price:.3f} | 信号max_price={mx:.3f} | "
            f"实时best_ask={best_ask if best_ask else 'N/A'} → 首段挂单价={order_price:.3f} ({_mode})", "I")
        order_price = max(order_price, 0.10)
        order_price = round(order_price, 2)

        # ===== round41.5.3(8/24 用户指令): 最终成交价 0.25 硬底线——含翻向信号全路径 =====
        # 8/18 的 <0.25 拦截(L1029)查的是下单时刻实时中间价 target_live，8/23 7pm 实证被绕过：
        # 中间价 0.510 放行后，taker 吃到簿内惊慌/stale 卖单 ask=0.20 → 成交 -$10。
        # 历史 <0.25 成交 8 笔仅 2 胜（净 +$1.98 全靠 8/5 单笔 +$31.93 彩票；剔除后 7 笔净 -$29.95）。
        # 本检查位于 order_price 定稿后的公共必经点——AI 直出/引擎翻向/G3/G4/round41.6 翻向
        # 等一切路径统一适用：最终要吃的价格 <0.25 一律不买。
        if order_price < 0.25:
            log(f"🛑 成交价低于底线 | {slug} | 最终挂单价 {order_price:.2f} < 0.25"
                f"（低价卖单绕过中间价拦截，含翻向信号一律不买, round41.5.3），SKIP", "W")
            return {"ok": True, "skipped": True,
                    "skip_reason": f"成交价{order_price:.2f}<0.25 底线(round41.5.3)"}

        # round36 影子模式（8/21）：不改交易行为，仅追加记录 edge 通道 would-be 数据，
        # 供影子回测裁决 R1（晋级/否决条件见 data/hermes_round36_review_20260821.md）。
        # edge = probability_raw(模型原始概率) − pm 快照价；R1 规则：8-20pt→cap 0.70、
        # ≥20pt→cap 0.52、其余 0.60。would_be_price = min(参考价, cap_r1)。
        # 任何异常仅告警不拦截（与校验层同纪律）。
        try:
            _p_raw = sig.get("probability_raw", sig.get("probability"))
            try:
                _p_f = float(_p_raw)
            except (TypeError, ValueError):
                _p_f = None
            _edge = None
            if _p_f is not None and snap_pm is not None and snap_pm > 0:
                _edge = round(_p_f - snap_pm, 4)
            if _edge is not None and 0.08 <= _edge < 0.20:
                _cap_r1 = 0.70
            elif _edge is not None and _edge >= 0.20:
                _cap_r1 = 0.52
            else:
                _cap_r1 = 0.60
            _wb_ref = best_ask if best_ask else target_live
            _wb_price = round(max(min(_wb_ref, _cap_r1), 0.10), 2) if _wb_ref else None
            _shadow = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "slug": slug, "side": side,
                "probability_raw": _p_f, "pm_snap": snap_pm, "pm_live": round(target_live, 4),
                "best_ask": best_ask, "edge": _edge,
                "cap_current": effective_cap, "cap_r1": _cap_r1,
                "would_be_price": _wb_price, "actual_price": order_price,
                "position_scale": sig.get("position_scale"),
                "mode": "round36-shadow-v1",
            }
            with SHADOW_EDGE.open("a", encoding="utf-8") as _sf:
                _sf.write(json.dumps(_shadow, ensure_ascii=False) + "\n")
            log(f"🪞 影子记录 edge={_edge if _edge is None else round(_edge, 3)} cap_r1={_cap_r1} "
                f"would_be={_wb_price} actual={order_price} | {slug}", "I")
        except Exception as _e:  # noqa: BLE001 - 影子记录失败绝不影响下单
            log(f"⚠️ 影子记录失败(不影响交易) | {_e}", "W")
        # Bug-4 修复: MAX_AMOUNT 从 config 读取，与分析器保持一致
        MAX_AMOUNT = 20.0
        try:
            if CONFIG.exists():
                cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
                MAX_AMOUNT = float(cfg.get("max_amount", 20.0))
        except Exception:
            pass
        amt = min(amt, MAX_AMOUNT)
        # Polymarket CLOB 最小订单金额 $1，低于直接跳过（信号过弱不值得下注）
        MIN_ORDER_AMOUNT = 1.0
        if amt < MIN_ORDER_AMOUNT:
            log(f"📉 金额过小 | {slug} | ${amt:.2f} < ${MIN_ORDER_AMOUNT} 最小限额，跳过", "W")
            return {"ok": True, "skipped": True, "skip_reason": f"amount ${amt:.2f} < min ${MIN_ORDER_AMOUNT}"}
        # Bug-5 修复: 增加 shares 上限，防止 order_price 极小时生成异常大仓位
        MAX_SHARES = 200
        MIN_SHARES = 5  # Polymarket CLOB 最小下单量
        sz = amt / max(order_price, 0.05)
        # 最小 shares 检查：连续亏损缩仓后金额可能不足以买 5 shares
        if sz < MIN_SHARES:
            min_amt = MIN_SHARES * order_price
            if min_amt <= amt * 3 and min_amt <= MAX_AMOUNT:
                amt = min_amt
                sz = MIN_SHARES
                log(f"    ⚠️ shares 不足 5，金额上调至 ${amt:.2f} ({sz:.0f}shares)", "W")
            else:
                log(f"    📉 金额不足以满足最小 shares | ${amt:.2f} @ {order_price:.2f} → {sz:.1f}sh < {MIN_SHARES}sh | 跳过", "W")
                return {"ok": True, "skipped": True, "skip_reason": f"shares {sz:.1f} < min {MIN_SHARES}"}
        sz = min(sz, MAX_SHARES)
        log(f"    📊 Gamma: UP={up_p:.3f} DOWN={dn_p:.3f} | 限价={order_price:.3f} | ${amt:.1f} → {sz:.0f}shares", "I")

        # Bug-3 修复: 下单前检查同 token 是否已有 open order，防止崩溃重启后重复下单
        if self.live and self.client is not None:
            try:
                existing = self._find_existing_order(tok)
                if existing:
                    log(f"♻️ 同 token 已有订单，跳过下单 | {slug} | {existing[:20]}", "I")
                    return {"ok": True, "order_id": existing, "skipped": True, "skip_reason": "duplicate_order_prevented"}
            except Exception:
                pass

        result = self.place_order(tok, order_price, sz, side)
        # 记录实际挂单价到 result，供 _mark_done 持久化到 done 文件（执行价偏离监控用）
        if result.get("ok") and not result.get("skipped"):
            result["order_price"] = order_price
            # 8/23 修复(实盘记账): result 带上真实下单金额——复盘任务 hermes_review 从信号层
            # 读 amount 永远是 None(AI 信号不含金额)，导致 LIVE 单 pnl 记成 ±0。回退链见 review。
            result["amount"] = amt

        # B4: 记录待追踪订单
        if result.get("ok") and not result.get("skipped"):
            oid = result.get("order_id", "")
            if oid:
                # 8/19: PAPER 模拟单视作成交（下单即记终态；_poll 对 PAPER 单只清理不检测）
                if oid.startswith("PAPER_"):
                    self._record_fill(oid, slug, side, True, "PAPER")
                    # 9/2 B7: 显式标记模拟口径——paper 下单即记 FILLED 是理想化模拟
                    # （真实限价单可能不成交/部分成交），done/fills 双端带标记供复盘过滤，
                    # paper 胜率/成交率不可线性外推 live
                    result["paper_instant"] = True
                # round41.5.1(8/24): taker 单 SDK 返回 matched=立即成交 -> 直接记 FILLED 终态，不进轮询
                # (修复: 已成交单 get_order 响应不再匹配 OpenOrder 形状 -> 每 3s 刷「订单状态查询失败」26 分钟)
                elif str(result.get("status", "")).lower() == "matched":
                    _fs = self._fetch_fill_stats(oid, tok)
                    self._record_fill(oid, slug, side, True, "FILLED",
                                      size_matched=_fs.get("size_matched"),
                                      avg_price=_fs.get("avg_price"))
                    # 9/3: result 补 fill_status——UI 决策链/review 判成交用
                    # (此前 LIVE taker 单 result 只有 status=matched，UI 误显示「追价中」)
                    result["fill_status"] = "FILLED"
                    log(f"✅ taker 立即成交(matched) | {slug} {side} | {oid[:16]} | "
                        f"size_matched={_fs.get('size_matched')} avg={_fs.get('avg_price')} -> 记FILLED不进轮询", "I")
                    return result
                # 9/2: 随单存 probability_raw 供复盘/策略分析（追价档位已统一，
                # 此字段不再参与档位判定，保留以便回测按信号强度分桶）
                try:
                    _sig_praw = float(sig.get("probability_raw") or 0)
                except (TypeError, ValueError):
                    _sig_praw = 0.0
                self._pending_orders[oid] = {"slug": slug, "side": side, "ts": time.time(),
                                               "amount": amt, "token_id": tok, "retry": 0,
                                               "max_price": mx,  # Bug-A4: 存储原始限价用于reprice上限
                                               "price": order_price, "orig_price": order_price,  # 8/21 方案A3: 追价基准价
                                               "price_cap": effective_cap,  # round41.5: 追价继承上限(=TAKER_MAX 或 book缺失兜底)
                                               "probability_raw": _sig_praw,  # 9/2: 追价档位动态封顶依据
                                               "chase_count": 0, "last_chase_check": 0.0}
                # Bug-2 修复: 写入 PENDING 文件，填补风控盲区
                # 9/2 BUG-1/BUG-4 修复: 补 token_id(恢复单追价必需,缺了 _check_liquidity 拿空 ID
                # 拉簿必败→重启后挂单永远无法追价) + chase_count/matched_size/matched_notional
                # (恢复时还原追价档位与链上部分成交,防止 5 档上限被重启绕过)
                try:
                    with open(PENDING, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "order_id": oid, "slug": slug, "side": side,
                            "amount": amt, "price": order_price,
                            "orig_price": order_price, "price_cap": effective_cap,  # A2: 追价恢复用
                            "max_price": mx,  # 9/2 ⚪3: 信号原始限价持久化，恢复单流动性检查用
                            "token_id": tok,
                            "probability_raw": _sig_praw,  # 9/2: 追价档位动态封顶恢复用
                            "chase_count": 0,
                            "matched_size": 0, "matched_notional": 0,  # 追价链部分成交继承(9/2 BUG-2)
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }) + "\n")
                except OSError:
                    pass
        # 8/18 记录不拦截：PM 大幅波动标记附加到 result，供 _mark_done 持久化到 done 文件复盘
        if result.get("ok") and not result.get("skipped") and pm_volatile:
            result["pm_volatile"] = pm_volatile
        return result

    def _slug_already_done(self, slug):
        """检查该 slug 是否已成功处理（只认 ORDER 成功，SKIP 可被兜底覆盖）"""
        if not DONE.exists():
            return False
        try:
            with open(DONE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("signal", {}).get("slug") == slug:
                        result = entry.get("result", {})
                        # 只认 ORDER 成功（ok=True 且非 skipped）；SKIP 允许被兜底覆盖
                        if result.get("ok") and not result.get("skipped"):
                            return True
            return False
        except (json.JSONDecodeError, OSError):
            return False

    def _mark_done(self, sig, result):
        """记录已处理（仅成功或跳过时调用）"""
        with open(DONE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"signal": sig, "result": result}) + "\n")

    # ========== 8/10: 结算前最后5分钟止盈卖出 ==========

    def _load_tp_done(self):
        """启动时扫描 sells.jsonl，预填已卖出 slug（幂等防重，防止重启后重复卖出）"""
        done = set()
        if not SELLS.exists():
            return done
        try:
            for line in SELLS.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    s = entry.get("slug")
                    if s:
                        done.add(s)
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        if done:
            log(f"📦 恢复止盈防重: {len(done)} 个已卖出 slug", "I")
        return done

    def _parse_settlement(self, slug):
        """从 slug 解析该小时合约的结算时间（下一整点 ET）。解析失败返回 None（安全跳过判定）。"""
        try:
            # slug 格式: bitcoin-up-or-down-august-9-2026-10am-et
            m = re.search(r"-([a-z]+)-(\d{1,2})-(\d{4})-(\d{1,2})(am|pm)-et$", slug)
            if not m:
                return None
            month_name, day, year, h12, ampm = m.groups()
            month = next((i for i, name in enumerate(calendar.month_name)
                          if name and name.lower() == month_name), None)
            if not month:
                return None
            h = int(h12) % 12
            if ampm == "pm":
                h += 12
            start = datetime(int(year), month, int(day), h, tzinfo=ET)
            return start + timedelta(hours=1)  # 下一整点结算，timedelta 自动处理跨天
        except (ValueError, TypeError):
            return None

    def _sell_market(self, token_id, shares, min_price=0.01, avg_price=0.0, cur_price=0.0):
        """市价卖出持仓。复用 place_order 的 3 次重试模式，side=SELL。

        min_price 由调用方保本设置（max(avg_price, tp_min_price)）。
        PAPER 模式仅记录日志不真实下单。返回含真实/估算成交价。
        """
        if not self.live:
            log(f"📝 [PAPER] SELL {shares:.0f}sh @ 市价(~{cur_price:.4f})", "I")
            return {"ok": True, "order_id": f"PAPER_SELL_{int(time.time())}",
                    "shares": shares, "sell_price": cur_price}
        # 8/11 自适应: 卖出前确保 client 与当前网络一致（网络切换自动重建）
        if not self._ensure_client():
            return {"ok": False, "error": "client not connected"}
        for attempt in range(3):
            try:
                resp = self.client.place_market_order(
                    token_id=token_id,
                    side="SELL",
                    shares=shares,
                    min_price=min_price,
                    order_type="FAK",
                )
                ok = getattr(resp, 'ok', None)
                if ok is True:
                    oid = str(resp.order_id)
                    status = getattr(resp, 'status', 'unknown')
                    # 9/2 审查修复(B5): 卖出成交价复核——旧代码 get_order 查了 size_matched
                    # 却不用（real_price = cur_price 恒成立），止盈 pnl 永远按判定现价估算。
                    # 现按排查纪律改用 list_account_trades 证据（_fetch_fill_stats），
                    # FAK 市价单为 taker，按 taker_order_id 匹配成交均价；查不到回退判定现价。
                    real_price = cur_price
                    estimated = True
                    try:
                        time.sleep(2)  # 等 trades API 索引（与撤单复核同节奏）
                        fs = self._fetch_fill_stats(oid, token_id)
                        _ap = fs.get("avg_price")
                        if _ap is not None and float(_ap) > 0:
                            real_price = float(_ap)
                            estimated = False
                    except Exception:
                        pass
                    tag = "估算" if estimated else "真实"
                    log(f"🔥 [LIVE-SELL] {shares:.0f}sh @ 市价({tag}{real_price:.4f}) → {oid[:20]} (status={status})", "I")
                    return {"ok": True, "order_id": oid, "shares": shares,
                            "sell_price": real_price, "estimated": estimated}
                elif ok is False:
                    code = getattr(resp, 'code', 'unknown')
                    msg = getattr(resp, 'message', '')
                    log(f"❌ 卖出被拒: {code} {msg}", "E")
                    return {"ok": False, "error": f"rejected: {code} {msg}"}
                else:
                    return {"ok": False, "error": f"unexpected response: {str(resp)[:100]}"}
            except Exception as e:
                log(f"❌ 卖出失败 {attempt+1}/3: {type(e).__name__}: {str(e)[:200]}", "W")
                if attempt < 2:
                    time.sleep(3)
        return {"ok": False, "error": "3次卖出重试均失败"}

    def _paper_position(self, slug):
        """PAPER 模式模拟持仓（Bug3 修复）：从 PENDING 文件按 slug 构造伪持仓。

        用 PENDING 的 price 当成本、amount/price 当 shares，Gamma 实时价当现价。
        返回 dict {size, avg_price, cur_price, token_id, outcome} 或 None。
        """
        if not PENDING.exists():
            return None
        try:
            for line in reversed(PENDING.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("slug") != slug:
                    continue
                price = float(entry.get("price", 0) or 0)
                amount = float(entry.get("amount", 0) or 0)
                if price <= 0 or amount <= 0:
                    return None
                cur = self._gamma_price(slug, str(entry.get("side", "")).upper())
                return {"size": amount / price, "avg_price": price,
                        "cur_price": cur, "token_id": None,
                        "outcome": str(entry.get("side", "")).upper()}
        except OSError:
            pass
        return None

    def _gamma_price(self, slug, side):
        """Gamma 实时价兜底（Bug1 修复）：cur_price 异常时用 get_market 的 outcomePrices。"""
        try:
            m = get_market(slug)
            if not m:
                return 0.0
            raw = m.get("outcomePrices", "[]")
            prices = json.loads(raw) if isinstance(raw, str) else (raw or [])
            if len(prices) < 2:
                return 0.0
            return float(prices[0]) if side == "UP" else float(prices[1])
        except Exception:
            return 0.0

    def _remaining_size(self, slug):
        """问题5: 复查该 slug 剩余持仓（FAK 部分成交残留）。返回剩余 shares。"""
        try:
            paginator = self.client.list_positions(size_threshold=0.0001)
            items = list(paginator.iter_items()) if hasattr(paginator, "iter_items") else list(paginator)
            pos = next((p for p in items if (getattr(p, "slug", None) or "") == slug), None)
            return float(getattr(pos, "size", 0) or 0) if pos else 0.0
        except Exception:
            return -1.0  # -1 表示查询失败（视为仍有残留，继续重试）

    def _take_profit_check(self, win=None):
        """结算前最后 tp_final_minutes 分钟止盈检查（tick() 开头调用，必须在窗口判断之前）。

        流程: 配置读取 → 时间窗口判定 → _tp_done 防重 → 查持仓(LIVE真实/PAPER模拟) →
        cur_price>avg_price 且保本 min_price 卖出 → 部分成交复查 → 写 sells.jsonl → 标记防重。
        数据异常(cur<=0)/查询失败不标记可重试; 不盈利/无持仓标记死等结算。
        """
        # 配置读取（失败兜底默认值）
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
        except Exception:
            cfg = {}
        try:
            tp_enabled = bool(cfg.get("tp_enabled", True))
            tp_min = int(cfg.get("tp_final_minutes", 5))
            tp_min_price = float(cfg.get("tp_min_price", 0.01))
        except (ValueError, TypeError):
            tp_enabled, tp_min, tp_min_price = True, 5, 0.01
        if not tp_enabled:
            return

        slug = (win or get_current_window())["slug"]
        settlement = self._parse_settlement(slug)
        if settlement is None:
            return  # 解析失败安全跳过（不标记防重）
        now = now_et()
        if not (settlement - timedelta(minutes=tp_min) <= now < settlement):
            return  # 不在止盈窗口，零开销
        if slug in self._tp_done:
            return  # 已判定过

        # Bug3: LIVE 查真实持仓，PAPER 模拟持仓
        if self.live:
            # 8/11 自适应: 止盈前确保 client 与当前网络一致（网络切换自动重建，防漏执行）
            if not self._ensure_client():
                log(f"⚠️ 止盈-钱包连接失败将重试 | {slug}", "W")
                return  # 不标记，下个 tick 重试
            # 查询钱包真实持仓（按 slug 过滤）
            # 注意: list_positions 返回 Paginator，需 iter_items() 取 Position 对象
            # 8/10 修正: size_threshold 对"已结算但 size 未清零"的持仓无效（dry-run 实测），
            #            改为 slug 匹配后再过滤 size<=0，真正排除已结算残留
            try:
                paginator = self.client.list_positions()
                if hasattr(paginator, "iter_items"):
                    positions = list(paginator.iter_items())
                else:
                    positions = list(paginator)
            except Exception as e:
                log(f"⚠️ 止盈-持仓查询失败 | {slug} | {str(e)[:60]}", "W")
                return  # 不标记，下个 tick 重试
            pos = next((p for p in positions
                        if (getattr(p, "slug", None) or "") == slug
                        and float(getattr(p, "size", 0) or 0) > 0), None)
            if pos is None:
                log(f"⏳ 止盈-无持仓 | {slug} | 持有单未成交或已平仓", "I")
                self._tp_done.add(slug)
                return
            size = float(getattr(pos, "size", 0) or 0)
            avg_price = float(getattr(pos, "avg_price", 0) or 0)
            cur_price = float(getattr(pos, "cur_price", 0) or 0)
            token_id = getattr(pos, "token_id", None)
            side = str(getattr(pos, "outcome", "") or "").upper() or "?"
        else:
            p = self._paper_position(slug)
            if p is None:
                log(f"⏳ 止盈-无持仓(PAPER) | {slug} | PENDING 无该 slug 记录", "I")
                self._tp_done.add(slug)
                return
            size, avg_price = p["size"], p["avg_price"]
            cur_price, token_id, side = p["cur_price"], p["token_id"], p["outcome"]

        if size <= 0 or avg_price <= 0:
            log(f"⏳ 止盈-持仓数据异常 | {slug} | size={size} avg={avg_price}", "W")
            self._tp_done.add(slug)
            return
        # Bug1: cur_price 异常(=0)与真实不盈利区分 → Gamma 兜底，仍异常则重试不标记
        if cur_price <= 0:
            cur_price = self._gamma_price(slug, side)
            if cur_price <= 0:
                log(f"⚠️ 止盈-现价数据异常将重试 | {slug} | cur_price=0 且 Gamma 兜底失败", "W")
                return  # 不标记防重，下个 tick 重试
        if self.live and not token_id:
            log(f"⏳ 止盈-持仓数据异常 | {slug} | token_id 缺失", "W")
            self._tp_done.add(slug)
            return

        if cur_price > avg_price:
            # 盈利 → 保本市价卖出全部持仓（Bug2: min_price 不低于成本）
            min_price = max(avg_price, tp_min_price)
            result = self._sell_market(token_id, size, min_price, avg_price, cur_price)
            if not result.get("ok"):
                log(f"⚠️ 止盈-卖出失败将重试 | {slug} | {result.get('error','')[:60]}", "W")
                return  # 不标记，下个 tick 重试
            # 问题5: 部分成交复查，残留则不标记继续卖
            if self.live:
                remaining = self._remaining_size(slug)
                if remaining != 0:
                    log(f"⚠️ 止盈-部分成交 | {slug} | 剩余 {remaining:.2f}sh 下个 tick 继续卖", "W")
                    return  # 不标记，继续卖剩余
            sell_price = result.get("sell_price", cur_price)
            pnl_usd = (sell_price - avg_price) * size
            # 问题9: 先写文件成功再标记防重（保证幂等，重启不重复卖）
            wrote = False
            try:
                with open(SELLS, "a", encoding="utf-8") as f:
                    rec = {
                        "slug": slug, "side": side, "token_id": token_id,
                        "shares": round(size, 2),
                        "avg_price": round(avg_price, 4),
                        "sell_price": round(sell_price, 4),
                        "pnl_usd": round(pnl_usd, 4),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    if not self.live:
                        rec["paper"] = True
                    if result.get("estimated"):
                        rec["estimated"] = True
                    f.write(json.dumps(rec) + "\n")
                wrote = True
            except OSError as e:
                log(f"⚠️ 止盈-卖出记录写入失败 | {slug} | {str(e)[:40]}", "W")
            log(f"💰 止盈卖出 | {slug} {side} | {size:.0f}sh @ ~{sell_price:.4f} (均价{avg_price:.4f}) | 浮盈≈${pnl_usd:.2f}", "I")
            if not wrote:
                log(f"⚠️ 止盈-记录未落盘仍标记防重 | {slug} | 重启靠持仓清空兜底", "W")
            self._tp_done.add(slug)
        else:
            pct = (cur_price / avg_price - 1) * 100 if avg_price > 0 else 0.0
            mode = "" if self.live else "(PAPER)"
            log(f"📉 止盈-不盈利持有到结算{mode} | {slug} | 均价{avg_price:.4f} 现价{cur_price:.4f} ({pct:+.2f}%)", "I")
            self._tp_done.add(slug)

    # ========== round41.4 C5：PM 价格序列记录器（纯影子数据层，不影响任何交易逻辑） ==========
    def _record_pm_prices(self):
        """每分钟边界（ET :00/:01/:02/.../:59）调度一次 PM 价格记录。
        9/3 用户拍板新增 1 分钟档（pm_1m_*.jsonl）：回测显示 5min 粒度下信号时落后
        于最终获胜方的场次里 68.9% 的翻转发生在信号后——5min 快照看不见「砸完就拉」
        的 V 型反转，1min 档为动量翻转因子研究/定标攒数据。
        5min 档（pm_YYYYMM.jsonl）口径与触发完全不变，旧消费方（引擎/回测/dashboard）
        不受影响；1min 档静默写盘不打日志（log 是 dashboard FEED 数据源，防刷屏）。
        审计修复（8/23）保留：HTTP 请求在后台线程，主线程只做槽位去重和线程派发。"""
        try:
            _, et_min, et_dt = get_current_et()
            slot = et_dt.strftime("%Y%m%d%H%M")
            if getattr(self, "_pm_rec_slot", None) == slot:
                return  # 同一分钟槽只派发一次（tick 约 5 秒一次）
            self._pm_rec_slot = slot  # 主线程先占坑，防止重复派发
            import threading
            threading.Thread(target=self._pm_rec_worker, args=(et_dt,), daemon=True,
                             name=f"pm-recorder-{slot}").start()
        except Exception as e:
            log(f"⚠️ PM价格记录调度失败(不影响交易) | {str(e)[:60]}", "W")

    def _pm_rec_worker(self, et_dt):
        """工作线程：拉当前+下一小时合约价并追加落盘（网络/文件异常全吞）。
        双档落盘：5min 边界写 pm_YYYYMM.jsonl（历史口径+日志）；每分钟写 pm_1m_YYYYMM.jsonl（静默）。"""
        try:
            out = {"ts_utc": datetime.now(timezone.utc).isoformat(),
                   "et": et_dt.strftime("%Y-%m-%d %H:%M"),
                   "slot_minute": et_dt.minute}
            _cur_token, _cur_enable = None, False
            for tag, dt in (("cur", et_dt), ("next", et_dt + timedelta(hours=1))):
                slug_n = build_slug(dt)
                up = dn = None
                try:
                    m = get_market(slug_n)
                    if m:
                        _cids = m.get("clobTokenIds")
                        _cids = json.loads(_cids) if isinstance(_cids, str) else _cids
                        if tag == "cur":
                            # 记录当前合约 clob token，供下方 book_imbalance 现算
                            _cur_token = _cids[0] if _cids else None
                            _cur_enable = bool(m.get("enableOrderBook"))
                    prices = json.loads(m.get("outcomePrices", "[]")) if m else []
                    if isinstance(prices, list) and len(prices) >= 2:
                        up, dn = float(prices[0]), float(prices[1])
                except Exception:
                    pass
                out[f"slug_{tag}"] = slug_n
                out[f"up_{tag}"] = up
                out[f"dn_{tag}"] = dn
            # 9/3 用户拍板: 1min 档补 book_imbalance。盘口失衡是分钟级动态信号，后续供 LightGBM
            # 做「慢 consensus 稳在那、快失衡悄悄堆/剧变」的背离特征。
            # 口径与 signal_validator._fetch_live_pm_book 完全一致: 0.3-0.7 区间 bid/ask 量差比。
            # 纯影子数据层, 任何异常/fail-open 记 None, 不影响交易与其余字段。
            book_imb = None
            if _cur_enable and _cur_token:
                try:
                    _bk = http_get_json(f"{CLOB_API}/book?token_id={_cur_token}",
                                        timeout=8, proxy=PROXY)
                    if isinstance(_bk, dict):
                        _bids = [(float(b["price"]), float(b["size"])) for b in _bk.get("bids", [])]
                        _asks = [(float(a["price"]), float(a["size"])) for a in _bk.get("asks", [])]
                        _bid_mid = sum(s for p, s in _bids if 0.3 <= p <= 0.7)
                        _ask_mid = sum(s for p, s in _asks if 0.3 <= p <= 0.7)
                        if _bid_mid + _ask_mid > 0:
                            book_imb = round((_bid_mid - _ask_mid) / (_bid_mid + _ask_mid), 4)
                except Exception:
                    book_imb = None
            out["book_imb"] = book_imb
            pdir = DATA_DIR / "pm_prices"
            pdir.mkdir(parents=True, exist_ok=True)
            if et_dt.minute % 5 == 0:  # 5min 档：历史口径不变
                with open(pdir / f"pm_{et_dt.strftime('%Y%m')}.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(out, ensure_ascii=False) + "\n")
                log(f"📊 PM价格序列已记录 | {out['et']} ET | cur={out.get('up_cur')}/{out.get('dn_cur')} "
                    f"next={out.get('up_next')}/{out.get('dn_next')}")
            # 1min 档：每分钟落盘，静默（9/3 用户拍板）
            with open(pdir / f"pm_1m_{et_dt.strftime('%Y%m')}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
        except Exception as e:
            log(f"⚠️ PM价格记录失败(不影响交易) | {str(e)[:60]}", "W")

    def tick(self):
        # round41.4 C5：PM 价格序列记录（9/3 起每分钟边界双档：5min 历史口径 + 1min 新档，静默容错）
        self._record_pm_prices()
        # B4: 轮询订单状态
        self._poll_pending_orders()

        win = get_current_window()
        slug = win["slug"]

        # 8/10: 最后5分钟止盈检查 —— 必须在窗口判断之前（55-60min 在 0-55min 窗口外）
        # 问题8: 传入 win 复用，避免 _take_profit_check 内部重复调用 get_current_window
        try:
            self._take_profit_check(win)
        except Exception as e:
            log(f"⚠️ 止盈检查异常: {str(e)[:80]}", "W")

        if not win["in_window"]:
            # 窗口外：清除处理中标记
            if self._processing_slug and self._processing_slug != slug:
                self._processing_slug = None
            return

        # B2: 内存锁 — 同一 slug 正在处理中，跳过
        if self._processing_slug == slug:
            return

        if slug == self.last_slug:
            return

        if self._slug_already_done(slug):
            self.last_slug = slug
            self.clear_queue(slug)  # Bug修复: 清理队列中残留的旧信号，防止dashboard显示过期信号
            log(f"⏭️ 已处理 | {slug} | 跳过(防重复)", "I")
            return

        sig = self.find_signal_for_slug(slug)
        if not sig:
            if slug not in getattr(self, "_waited", set()):
                if not hasattr(self, "_waited"):
                    self._waited = set()
                self._waited.add(slug)
                log(f"📭 无信号 | {slug} | 等待分析...", "I")
            return

        # 9/3 用户拍板: 推迟到整点后 :40 才分析下单(此前收到引擎信号即处理≈:36)。
        # round55: 改为 config analyze_min 热读（UI 可调），未到点前只确认信号在、不触发首挂;
        # 到点后下一轮 tick(几秒内)再处理。
        _am = _analyze_at_min()
        if win["et_minute"] < _am:
            if (getattr(self, "_defer_slug", None) != slug
                    or getattr(self, "_defer_min", None) != win["et_minute"]):
                self._defer_slug = slug
                self._defer_min = win["et_minute"]
                log(f"🕓 延后至 :{_am} 分析 | {slug} | 当前 :{win['et_minute']:02d}", "I")
            return

        # B2: 获取内存锁
        self._processing_slug = slug
        try:
            self._do_tick(win, slug, sig)
        finally:
            self._processing_slug = None

    def _do_tick(self, win, slug, sig):
        """实际处理逻辑（持有内存锁）"""
        if hasattr(self, "_waited"):
            self._waited.discard(slug)
        et_time = win["et_time"]; w_str = win["window_str"]
        log(f"🪟 新窗口 | {slug} | ET {et_time} | 窗口 {w_str}", "I")

        # B3: 记录重试次数
        retries = self._window_retries.get(slug, 0)
        self._window_retries[slug] = retries + 1

        # 先下单，成功后再清队列（防止中途被杀导致信号丢失）
        result = self.process_signal(sig)

        if result.get("ok"):
            if result.get("skipped"):
                if result.get("transient"):
                    # 9/2 审查修复(B4): 瞬时性失败（钱包连接抖动/余额查询异常）保留信号，
                    # 下轮 tick 重试；_window_retries 持续累加，达到 MAX_RETRIES_PER_WINDOW
                    # 上限后由 _check_risk 熔断——不会无限空转。
                    log(f"⏳ 瞬时失败保留信号 | {slug} | {result.get('risk_reason','')[:40]} | 下轮重试", "W")
                else:
                    # SKIP：清队列（防 bot 空转），但不设 last_slug 和 done（允许兜底 :12 覆盖）
                    self.clear_queue(slug)
                    self._window_retries.pop(slug, None)
                    log(f"⏭️ SKIP 不锁定 | {slug} | 等待兜底覆盖", "I")
            else:
                # ORDER 成功：写 done + 清队列 + 设 last_slug
                self.last_slug = slug
                self.save_last_signal(sig, result)
                self._mark_done(sig, result)
                self.clear_queue(slug)
                self._window_retries.pop(slug, None)
        else:
            log(f"⚠️ 失败保留信号 | {slug} | 下次重试 | {result.get('error','')[:40]}", "W")
            self.last_slug = None
            self._retry_backoff = getattr(self, "_retry_backoff", 0) + 1
            backoff = min(self._retry_backoff * 5, 30)
            # Bug-9 修复: 可中断的 sleep 循环，收到 SIGTERM 时立即退出
            for _ in range(backoff):
                if getattr(self, "_shutdown", False):
                    return
                time.sleep(1)

    def _remove_pending_record(self, order_id):
        """从 PENDING 文件中删除已结算的订单记录，防止风控误算。

        Bug-5 修复: 使用 fcntl 排他锁的原子重写，防止与 review 进程并发写入丢失 entry。
        """
        if not PENDING.exists():
            return
        try:
            lines = [l.strip() for l in PENDING.read_text(encoding="utf-8").splitlines() if l.strip()]
            kept = []
            removed = False
            for l in lines:
                try:
                    entry = json.loads(l)
                    if entry.get("order_id") == order_id:
                        removed = True
                        continue
                except json.JSONDecodeError:
                    pass
                kept.append(l)
            if removed:
                _atomic_rewrite_locked(PENDING, kept)
        except OSError:
            pass

    def _recover_pending_orders(self):
        """重启后从 PENDING 文件恢复挂单追踪（8/19 修复：重启丢追踪导致终态永不记录）。
        恢复的单由 _poll_pending_orders 正常处理：先查状态（可能已成交），
        超时则取消并记终态。
        9/2 BUG-1 修复: 落盘端现补写 token_id（此前 PENDING 落盘缺该键，恢复拿到
        空串 → _check_liquidity 拉簿必败，恢复单追价被静默跳过只能死等超时）。
        """
        if not PENDING.exists():
            return
        n = 0
        try:
            for line in PENDING.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                oid = e.get("order_id", "")
                if oid and oid not in self._pending_orders:
                    ts = time.time()
                    if e.get("ts"):
                        try:
                            ts = datetime.fromisoformat(e["ts"]).timestamp()
                        except (ValueError, TypeError):
                            pass
                    _price = e.get("price", 0)
                    _orig = e.get("orig_price", _price) or _price
                    _cap = e.get("price_cap")
                    self._pending_orders[oid] = {"slug": e.get("slug", ""), "side": e.get("side", ""),
                                                  "ts": ts, "amount": e.get("amount", 0),
                                                  "token_id": e.get("token_id", ""),
                                                  # 9/2 ⚪3: max_price 恢复口径修正——原写 e.get("price")
                                                  # (当前挂单价)，旧 PENDING 行无 max_price 时回退
                                                  # orig_price(首挂基准价≈信号限价)，新行直接读持久化值。
                                                  # 口径影响: max_price 用于流动性检查上限，用当前挂价
                                                  # 会把上限漂移到追价后的高价档
                                                  "retry": 0,
                                                  "max_price": e.get("max_price") or e.get("orig_price") or e.get("price", 0),
                                                  # 9/2 交叉验证修复: 补 "price" 键——追价分支直接读
                                                  # info["price"]（L2218+），恢复单缺此键会 KeyError 被
                                                  # except 吞掉 → 重启恢复的挂单永远无法追价、只能等超时
                                                  "price": _price,
                                                  # A2(2026-08-23): 恢复时带上追价基准价与条件封顶，
                                                  # 避免高概率通道/追价单重启后封顶退回 0.60
                                                  "orig_price": _orig,
                                                  "price_cap": _cap if _cap is not None else MAX_ORDER_PRICE,
                                                  # C2: 恢复追价链头，终态回写 CHASED_ 镜像不断链
                                                  "orig_order_id": e.get("orig_order_id", ""),
                                                  # 9/2: 随单恢复 probability_raw 供复盘分桶（档位已统一，不参与判定）
                                                  "probability_raw": e.get("probability_raw", 0),
                                                  # 9/2 BUG-4 修复: 恢复追价档位——此前硬编码 0，重启一次
                                                  # 档位从头爬，CHASE_MAX_TIMES=5 上限被绕过（N 次重启=N×5 档）
                                                  "chase_count": int(e.get("chase_count", 0) or 0),
                                                  "last_chase_check": 0.0,
                                                  # 9/2 BUG-2 修复: 恢复追价链上已并入的部分成交份额
                                                  "matched_size": float(e.get("matched_size", 0) or 0),
                                                  "matched_notional": float(e.get("matched_notional", 0) or 0)}
                    n += 1
        except OSError:
            pass
        if n:
            log(f"📦 恢复 {n} 笔挂单追踪（PENDING 文件）", "I")

    def _get_fills_seen(self):
        """9/2 审查修复(⚪2): fills.jsonl 已记录 order_id 的内存集合缓存。
        进程生命周期内首次调用全量扫描一次，此后 O(1) 判重——替换原「每次落档
        全文件扫描」的 O(n) 模式（634 行时每次追价终态都全扫，随历史增长线性变慢）。
        注意: 外部脚本(backfill)追加 fills 后需重启 bot 才能感知，与原逻辑等价
        （原逻辑每次扫描能感知，但代价是 O(n)；bot 重启是常态操作，可接受）。"""
        seen = getattr(self, "_fills_seen", None)
        if seen is None:
            seen = set()
            if FILLS.exists():
                try:
                    for line in FILLS.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            _oid = json.loads(line).get("order_id")
                            if _oid:
                                seen.add(_oid)
                        except json.JSONDecodeError:
                            pass
                except OSError:
                    pass
            self._fills_seen = seen
        return seen

    def _record_fill(self, order_id, slug, side, filled, status, size_matched=None, price=None,
                     avg_price=None, info=None):
        """订单终态持久化到 fills.jsonl（幂等，order_id 已存在则跳过）。
        filled: True=已成交 / False=未成交 / None=未知。8/19: 供 review/dashboard 成交口径分开统计。
        A3(2026-08-23): 可携带 size_matched / price，把「部分成交后被取消」从『未成交』中区分出来，
        不污染成交口径和胜率统计（历史 118 笔 TIMEOUT_CANCELLED 可能含真实部分持仓）。
        C2(2026-08-23): 可携带 avg_price（来自 trades API 的真实成交加权均价，非挂单价），
        复盘用真实均价算 pnl，替代「限价≈成交价」的近似。
        C2 追价回写: info 带 orig_order_id（追价链首挂单）时，终态同步镜像一条 CHASED_ 记录到
        首挂单 order_id——done.jsonl/信号只认首挂 id，不回写则复盘永远查不到追价后的真实成交。
        9/2 BUG-2 修复: info 带 matched_size/matched_notional（追价链中间单被撤时已成交的
        部分份额）时，并入本终态记录——保证链上部分成交份额不消失、总仓位/均价口径完整。
        """
        try:
            # 9/2 BUG-2: 追价链部分成交继承合并（放在判重前，合并后再落档）
            _inh_sz = 0.0
            _inh_nt = 0.0
            if info:
                try:
                    _inh_sz = float(info.get("matched_size") or 0)
                    _inh_nt = float(info.get("matched_notional") or 0)
                except (TypeError, ValueError):
                    _inh_sz, _inh_nt = 0.0, 0.0
            if _inh_sz > 0:
                _cur_sz = float(size_matched or 0)
                _cur_nt = 0.0
                if _cur_sz > 0:
                    if avg_price is not None and float(avg_price) > 0:
                        _cur_nt = float(avg_price) * _cur_sz
                    elif price is not None and float(price) > 0:
                        _cur_nt = float(price) * _cur_sz
                _tot_sz = _inh_sz + _cur_sz
                if _tot_sz > 0:
                    size_matched = _tot_sz
                    avg_price = (_inh_nt + _cur_nt) / _tot_sz
                    if filled is not True:
                        filled = True  # 链上已有真实持仓（含 UNKNOWN），终态不再是纯未成交
        except (TypeError, ValueError):
            pass
        try:
            # 9/2 ⚪2: 内存集合判重替换全文件扫描（O(n)→O(1)）
            if order_id in self._get_fills_seen():
                return  # 已记录
            rec = {"order_id": order_id, "slug": slug, "side": side,
                   "filled": filled, "status": status,
                   "ts": datetime.now(timezone.utc).isoformat()}
            # 9/2 B7: PAPER 模拟单显式标记——paper 下单即记 FILLED 是模拟口径，
            # 限价单在真实环境可能不成交/部分成交，复盘/统计可按此过滤
            if status == "PAPER":
                rec["paper_instant"] = True
            if size_matched is not None:
                rec["size_matched"] = round(float(size_matched), 4)
            if price is not None:
                rec["price"] = round(float(price), 4)
            # C2: 真实成交均价（trades API 加权），与 price（限价/挂单价）语义区分
            if avg_price is not None and float(avg_price) > 0:
                rec["avg_price"] = round(float(avg_price), 4)
            with open(FILLS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            self._get_fills_seen().add(order_id)
        except OSError as e:
            # C2审计修复(🟡7): 终态写入失败不能静默——这是永久统计口径数据
            log(f"❌ fills 终态写入失败(数据丢失风险!) {order_id[:20]} status={status}: {e}", "E")
            return
        # C2 追价回写: 镜像终态到追价链首挂单（幂等独立判重）
        if info:
            _orig_oid = info.get("orig_order_id") or ""
            if _orig_oid and _orig_oid != order_id:
                self._writeback_chase_fill(
                    _orig_oid, chased_oid=order_id, slug=slug, side=side,
                    filled=filled, status=status,
                    size_matched=size_matched, avg_price=avg_price)

    def _writeback_chase_fill(self, orig_order_id, chased_oid, slug, side, filled, status,
                              size_matched=None, avg_price=None):
        """C2(2026-08-23): 追价回写——把追价新单的终态镜像写到首挂单 order_id 下。

        背景: 追价重挂后旧单被撤不写 fills（口径保护），新单以新 order_id 记终态；
        而 done.jsonl/result.order_id 永远是信号时的首挂 id → hermes_review 用首挂 id
        查 fills_map 落空，把「追价后成交」误按未知处理、pnl 按首挂限价估算。
        本函数在 fills.jsonl 给首挂 id 补一条 CHASED_<status> 镜像记录（含 repriced_to
        溯源新单 id），review 端即可命中真实成交状态与均价。"""
        try:
            # 9/2 ⚪2: 内存集合判重替换全文件扫描（O(n)→O(1)）
            if orig_order_id in self._get_fills_seen():
                # C2审计修复(🟡3): 首挂已有终态(如紧急关闭先落档)→镜像阻断。
                # 静默 return 会让子单真实成交证据永久丢失，必须留痕
                log(f"⚠️ 追价回写跳过: 首挂{orig_order_id[:16]}已有终态，"
                    f"子单{chased_oid[:16]}的{status}证据未镜像(需人工对账 trades API)", "W")
                return
            rec = {"order_id": orig_order_id, "slug": slug, "side": side,
                   "filled": filled, "status": f"CHASED_{status}",
                   "repriced_to": chased_oid,
                   "ts": datetime.now(timezone.utc).isoformat()}
            if size_matched is not None:
                rec["size_matched"] = round(float(size_matched), 4)
            if avg_price is not None and float(avg_price) > 0:
                rec["avg_price"] = round(float(avg_price), 4)
            with open(FILLS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            self._get_fills_seen().add(orig_order_id)
            log(f"🔗 追价回写 | 首挂{orig_order_id[:16]} ← 新单{chased_oid[:16]} {status}"
                f"{f' @avg={float(avg_price):.3f}' if avg_price else ''}", "I")
        except OSError:
            pass

    def _fetch_fill_stats(self, oid, token_id=None):
        """C2(2026-08-23): 从 trades API 取订单的真实成交统计。

        返回 {"avg_price": float|None, "size_matched": float|None}；查不到/异常返回 {}（调用方回退旧逻辑）。
        原理: list_account_trades 按 token 过滤本账户成交，逐笔匹配
        - taker 单: trade.taker_order_id == oid，贡献 (trade.price, trade.size)
        - maker 单: trade.maker_orders[].order_id == oid，贡献 (mo.price, mo.matched_amount)
        avg_price = Σ(price×量)/Σ量。只读不抛，页数封顶防 API 轰炸。"""
        if not self.live or not self.client or oid.startswith("PAPER_"):
            return {}
        try:
            if token_id:
                paginator = self.client.list_account_trades(token_id=str(token_id))
            else:
                paginator = self.client.list_account_trades()
            total_sz = 0.0
            total_notional = 0.0
            pages = 0
            for page in paginator:
                pages += 1
                if pages > 5:  # 封顶 5 页(约500笔): 本账户近期成交足够覆盖当前订单
                    break
                for tr in getattr(page, "items", []) or []:
                    try:
                        # C2审计修复(🟡1): 链上 FAILED/RETRYING 的成交不是真实持仓，计入会虚增份额
                        _tst = str(getattr(tr, "status", "") or "").upper()
                        if "FAIL" in _tst or "RETRY" in _tst:
                            continue
                        if str(getattr(tr, "taker_order_id", "")) == str(oid):
                            p = float(tr.price)
                            s = float(tr.size)
                            if s > 0 and p > 0:
                                total_sz += s
                                total_notional += p * s
                        for mo in (getattr(tr, "maker_orders", ()) or ()):
                            if str(getattr(mo, "order_id", "")) == str(oid):
                                p = float(mo.price)
                                s = float(mo.matched_amount)
                                if s > 0 and p > 0:
                                    total_sz += s
                                    total_notional += p * s
                    except (TypeError, ValueError, ArithmeticError):
                        continue
                # 早停: 当前页已匹配到该订单成交量且无更多页 → 不必翻页
                if total_sz > 0 and not getattr(page, "has_more", False):
                    break
            if total_sz <= 0:
                return {}
            return {"avg_price": total_notional / total_sz, "size_matched": total_sz}
        except Exception as e:
            log(f"⚠️ 真实成交均价查询失败 {oid[:20]}: {str(e)[:60]}", "W")
            return {}

    def _read_matched(self, oid):
        """A3(2026-08-23): 读取订单已成交份额 size_matched（get_order 仅对 open orders 有效）。
        返回 float，读不到返回 None。C2 后作为 trades API 失败时的回退路径保留。
        兼容 SDK 对象与 dict 两种返回形态，只读不抛。"""
        try:
            o = self.client.get_order(order_id=oid)
        except Exception:
            return None
        try:
            if isinstance(o, dict):
                m = o.get("size_matched") or o.get("matched") or o.get("mySizeMatched") or 0
            else:
                m = getattr(o, "size_matched", None)
                if m is None:
                    m = o.get("size_matched") if isinstance(o, dict) else 0
            return float(m or 0)
        except Exception:
            return None

    def _poll_pending_orders(self):
        """B4: 轮询已挂单的状态，log 成交/取消事件。
        8/21 方案A3: OPEN 且超 8 分钟未成交时评估追价重挂（ask 上移则撤旧挂新，最多 2 次）。
        超时（首挂起 ORDER_TTL_MIN=25 分钟）取消后不重新下单。
        """
        now_ts = time.time()
        expired = []
        for oid, info in list(self._pending_orders.items()):
            age_min = (now_ts - info["ts"]) / 60
            # roundXX(2026-09-01 修复): 最终超时用 ORDER_TTL_MIN（round46 曾误用 CHASE_AFTER_MIN=10 做无条件
            # 取消，导致追价分支 age>10 永远进不去、阶梯追价完全失效——"一直买不到"根因之一）。
            if age_min > ORDER_TTL_MIN and not oid.startswith("PAPER_"):
                # 8/19: 超时取消前先查状态，防止把刚好成交的单误记为未成交（终态记录永久影响统计）
                pre = self._check_order_status(oid, info.get("token_id"))
                if pre == "FILLED":
                    # C2: 终态取 trades API 真实成交均价/份额，不再只按挂单价口径
                    _fs = self._fetch_fill_stats(oid, info.get("token_id"))
                    self._record_fill(oid, info["slug"], info["side"], True, "FILLED",
                                      size_matched=_fs.get("size_matched"),
                                      avg_price=_fs.get("avg_price"), info=info)
                else:
                    # 9/3 BUG-3 修复: client 不可用(网络故障窗口重建失败置 None)时订单恰逢
                    # 超时——原实现跳过整个撤单+终态块后无条件删 PENDING 追踪 → 挂单留簿
                    # 到结算(方向错全亏)、fills 无终态、风控漏计敞口，双重不可见。
                    # 现保留追踪等下轮(client 恢复后走正常撤单/成交判定收口)，落告警留证据。
                    if self.client is None or not hasattr(self.client, "cancel_order"):
                        log(f"⚠️ 超时处理挂起: client 不可用，保留追踪待恢复 | "
                            f"{info['slug']} {info['side']} | {oid[:20]} | {age_min:.0f}min", "W")
                        continue
                    try:
                        if hasattr(self.client, "cancel_order"):
                            # C2审计修复(🟡2): 先查成交再撤单——get_order 回退只对 open 单有效，
                            # 撤后查询会漏掉"撤单瞬间刚成交"的份额，误记纯未成交
                            _fs = self._fetch_fill_stats(oid, info.get("token_id"))
                            self.client.cancel_order(order_id=oid)
                            # A3: 取消前可能已部分成交——区分「部分持仓」与「纯未成交」。
                            # C2: 优先 trades API 真实成交份额，get_order(open-only) 作回退
                            _matched = _fs.get("size_matched")
                            if _matched is None:
                                _matched = self._read_matched(oid)
                            # 9/2 BUG-2: 分支判定用「本单成交 + 链上继承份额」——链上已有
                            # 部分成交时即使本单零成交也不是纯未成交（状态名要对）
                            _m_total = (_matched or 0) + float(info.get("matched_size") or 0)
                            if _m_total > 0:
                                self._record_fill(oid, info["slug"], info["side"], True,
                                                  "TIMEOUT_PARTIAL", size_matched=_matched,
                                                  avg_price=_fs.get("avg_price"),
                                                  price=info.get("price", 0), info=info)
                                log(f"⏰ 订单超时取消(部分成交{_m_total:.4g}) | {info['slug']} {info['side']} | {oid[:20]}", "W")
                            else:
                                self._record_fill(oid, info["slug"], info["side"], False, "TIMEOUT_CANCELLED", info=info)
                                log(f"⏰ 订单超时取消 | {info['slug']} {info['side']} | {oid[:20]} | {age_min:.0f}min", "W")
                            # 8/21 方案A3: 追过价仍超时 → 写告警记录（复盘"市场状态是否变化"用）
                            if info.get("chase_count", 0) > 0:
                                try:
                                    with open(CHASE_ALERTS, "a", encoding="utf-8") as _af:
                                        _af.write(json.dumps({
                                            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                            "slug": info["slug"], "side": info["side"],
                                            "chase_count": info.get("chase_count", 0),
                                            "orig_price": info.get("orig_price"),
                                            "last_price": info.get("price"),
                                            "age_min": round(age_min, 1),
                                        }, ensure_ascii=False) + "\n")
                                except OSError:
                                    pass
                    except Exception:
                        # cancel 失败：单可能已成交或早已取消。诚实记录而非保守 FILLED——
                        # 此处是永久终态记录，误报会永久污染成交口径统计。
                        # C2: get_order 查不到时用 trades API 真实成交证据定终态，
                        # 有成交→FILLED/TIMEOUT_PARTIAL(含真实均价)，无证据→保持 UNKNOWN
                        post = self._check_order_status(oid, info.get("token_id"))
                        _fs = self._fetch_fill_stats(oid, info.get("token_id"))
                        _m = _fs.get("size_matched")
                        # 9/2 BUG-2: 判定与分类都用「本单 + 链上继承」总份额
                        _m_total = (_m or 0) + float(info.get("matched_size") or 0)
                        if post == "FILLED" or _m_total > 0:
                            _est_shares = (info.get("amount", 0) or 0) / max(info.get("price", 0.5) or 0.5, 0.05)
                            if post != "FILLED" and _m_total < _est_shares * 0.98:
                                self._record_fill(oid, info["slug"], info["side"], True,
                                                  "TIMEOUT_PARTIAL", size_matched=_m,
                                                  avg_price=_fs.get("avg_price"), info=info)
                            else:
                                self._record_fill(oid, info["slug"], info["side"], True, "FILLED",
                                                  size_matched=_m, avg_price=_fs.get("avg_price"), info=info)
                        else:
                            self._record_fill(oid, info["slug"], info["side"], None, "UNKNOWN_NOT_FOUND", info=info)
                self._remove_pending_record(oid)
                expired.append(oid)
                # 8/5"不追价"指令已被 8/21 方案A3（BOSS 决策）取代：窗口内追价见下方 OPEN 分支；
                # 超时取消后仍不重新下单（总窗口 ORDER_TTL_MIN=25 分钟语义保持）
                continue
            if age_min > 60:  # 1 小时后清理
                self._remove_pending_record(oid)
                expired.append(oid)
                continue
            status = self._check_order_status(oid, info.get("token_id"))
            if status in ("FILLED", "CANCELLED"):
                log(f"📋 订单结算 | {info['slug']} {info['side']} | {oid[:20]} → {status}", "I")
                # C2: FILLED 终态带真实成交均价/份额；CANCELLED 也回写追价链(镜像未成交终态)
                _fs = self._fetch_fill_stats(oid, info.get("token_id")) if status == "FILLED" else {}
                # 9/2 修复: 成交瞬间 trades API 可能有延迟查不到成交记录（如 9/1 5am-et
                # CHASED_FILLED 落了 size_matched=None/avg_price=None），pnl 只能按挂单价估算。
                # 延迟 2s 重查一次，把真实均价/份额带回终态记录。
                if status == "FILLED" and not _fs:
                    time.sleep(2)
                    _fs = self._fetch_fill_stats(oid, info.get("token_id")) or {}
                self._record_fill(oid, info["slug"], info["side"], status == "FILLED", status,
                                  size_matched=_fs.get("size_matched"),
                                  avg_price=_fs.get("avg_price"), info=info)
                self._remove_pending_record(oid)
                expired.append(oid)
            elif status == "PAPER":
                expired.append(oid)  # 模拟单直接清理（成交状态已在下单时记录，见 execute_order）
            elif status == "UNKNOWN":
                # 9/2 round52.3 幽灵单修复: 重启打断「撤旧挂新」时（撤单请求已发出、新单未及
                # 落盘），CLOB 旧单已不存在 → get_order 抛 not found/shape 异常 + trades 零成交
                # → UNKNOWN。旧逻辑对 UNKNOWN 静默跳过 → 幽灵单卡死：不追价、超时 cancel 也无效
                # （单已不在簿），本小时仓位悬空（9/2 5am-et 0x187f3af262 实证）。
                # 处理: 连续 UNKNOWN≥5 次（15s，排除 CLOB 索引延迟）+ 追价间隔到期 → 按
                # 「撤单已生效」处理，直接按当前档位重挂接续追价链（无旧单可撤，跳过 cancel）。
                info["unknown_count"] = info.get("unknown_count", 0) + 1
                if (info["unknown_count"] >= 5
                        and age_min > CHASE_AFTER_MIN
                        and now_ts - info.get("last_chase_check", 0.0) > CHASE_CHECK_INTERVAL):
                    info["last_chase_check"] = now_ts
                    # 9/2 审查修复(B3): 幽灵判定复核——get_order 的 not found/shape 异常文本
                    # 匹配过宽，网络/代理异常串若含这些词会被误判幽灵 → 不撤旧单直接重挂 →
                    # 旧单仍在簿 + 新单 = 双仓。重挂前用 list_open_orders 正向确认旧单确实
                    # 不在簿（与「成交结论须 trades 佐证」同款证据纪律）。
                    try:
                        _ghost_alive = self._find_existing_order(info.get("token_id"))
                    except Exception:
                        _ghost_alive = None  # 复核通道本身故障: 保持原幽灵判定流程（不扩大拦截面）
                    if _ghost_alive == oid:
                        # 单其实还在簿上（此前 not found 判定为误报）→ 恢复正常轮询
                        info["unknown_count"] = 0
                        log(f"🕵️ 幽灵判定复核: 旧单 {oid[:20]} 实际仍在簿(not found 系误报)，"
                            f"恢复 OPEN 轮询 | {info['slug']}", "I")
                    elif _ghost_alive:
                        # 同 token 已有另一张挂单 → 不得重挂，防止双仓
                        log(f"🛑 幽灵重挂取消: 同token已有另一挂单 {_ghost_alive[:20]}，"
                            f"跳过重挂防双仓 | {info['slug']}", "W")
                    else:
                        # 确认幽灵: 簿上无此 token 的挂单，继续原重挂流程
                        liq_ok3, ghost_ask, _sp4, _r4 = self._check_liquidity(
                            info.get("token_id"), info.get("max_price", MAX_ORDER_PRICE))
                        if not (liq_ok3 and ghost_ask):
                            # 9/2 补日志盲区: 幽灵单重挂前簿检查失败原为静默跳过，留证据便于归因
                            log(f"🕵️ 幽灵单重挂跳过: 簿检查失败 | {info['slug']} {info['side']} | "
                                f"原因: {_r4}", "W")
                        if liq_ok3 and ghost_ask:
                            _gmid = self._gamma_price(info["slug"], str(info.get("side", "")).upper())
                            if 0 < _gmid < RECHECK_PM_MIN:
                                log(f"🛑 幽灵单重挂前守门: {_gmid:.2f}<{RECHECK_PM_MIN} 共识已反转，"
                                    f"放弃重挂等超时收口 | {info['slug']}", "W")
                            else:
                                _g_count = info.get("chase_count", 0) + 1
                                if _g_count <= len(CHASE_TARGETS):
                                    _g_tier = CHASE_TARGETS[_g_count - 1]
                                else:
                                    _g_tier = round(CHASE_TARGETS[-1] + (_g_count - len(CHASE_TARGETS)) * 0.05, 2)
                                # 9/3 BUG-2 修复(round52.4 适配): 重挂价取「现挂价与档位价的较大者」
                                # 再被 ask 封顶——原实现按旧档位从 0.65 起算且无降档保护，首挂吃价单
                                # (0.93)幽灵重挂直接降 0.28 挂 0.65(9/3 7am-et 实证)，逐档爬回期间
                                # 市场已走远最终超时踏空。现保证重挂价≥现挂价不降档；ask 更低时
                                # min 封顶仍吃到更优价。
                                _g_price = round(min(ghost_ask, max(info["price"], min(_g_tier, 0.99))), 2)
                                _g_sz = info["amount"] / max(_g_price, 0.05)
                                if _g_sz < 5:
                                    _g_min = 5 * _g_price
                                    _g_sz = 5.0 if (_g_min <= (info.get("amount", 0) or 0) * 3 and _g_min <= 20.0) else 0
                                    if _g_sz:
                                        info["amount"] = _g_min
                                if _g_sz:
                                    rg = self.place_order(info["token_id"], _g_price, _g_sz, info["side"])
                                    if rg.get("ok") and not rg.get("skipped") and rg.get("order_id"):
                                        g_oid = rg["order_id"]
                                        log(f"👻→🔄 幽灵单重挂成功 | {info['slug']} {info['side']} | "
                                            f"{_g_price:.2f}×{_g_sz:.0f}sh 新单{g_oid[:20]} "
                                            f"(旧幽灵{oid[:20]}已核实不在簿)", "I")
                                        self._pending_orders[g_oid] = {
                                            "slug": info["slug"], "side": info["side"],
                                            "ts": info["ts"],  # 继承首挂 ts: 总超时窗口不变
                                            "amount": info["amount"], "token_id": info.get("token_id", ""),
                                            "retry": 0, "max_price": info["max_price"],
                                            "price": _g_price,
                                            "orig_price": info.get("orig_price", info["price"]),
                                            "price_cap": info.get("price_cap", MAX_ORDER_PRICE),
                                            "chase_count": _g_count,
                                            "last_chase_check": now_ts,
                                            "repriced_from": oid[:20],
                                            "orig_order_id": info.get("orig_order_id") or oid,
                                            "probability_raw": info.get("probability_raw", 0),
                                            "matched_size": float(info.get("matched_size") or 0),
                                            "matched_notional": float(info.get("matched_notional") or 0),
                                        }
                                        try:
                                            with open(PENDING, "a", encoding="utf-8") as f:
                                                f.write(json.dumps({
                                                    "order_id": g_oid, "slug": info["slug"],
                                                    "side": info["side"], "amount": info["amount"],
                                                    "price": _g_price, "repriced_from": oid[:20],
                                                    "orig_order_id": info.get("orig_order_id") or oid,
                                                    "orig_price": info.get("orig_price", info["price"]),
                                                    "price_cap": info.get("price_cap", MAX_ORDER_PRICE),
                                                    "max_price": info.get("max_price"),  # 9/2 ⚪3
                                                    "probability_raw": info.get("probability_raw", 0),
                                                    "token_id": info.get("token_id", ""),
                                                    "chase_count": _g_count,
                                                    "matched_size": float(info.get("matched_size") or 0),
                                                    "matched_notional": float(info.get("matched_notional") or 0),
                                                    "ts": datetime.fromtimestamp(info["ts"], timezone.utc).isoformat(),
                                                }) + "\n")
                                        except OSError:
                                            pass
                                        self._remove_pending_record(oid)
                                        expired.append(oid)
                                    else:
                                        log(f"⚠️ 幽灵单重挂失败 | {info['slug']} | "
                                            f"{str(rg.get('error'))[:80]} | 保留追踪下轮再试", "W")
                                else:
                                    log(f"⚠️ 幽灵单重挂放弃: shares不足5 | {info['slug']}", "W")
            elif status == "OPEN":
                # ===== 8/21 方案A3: 追价重挂（BOSS 决策，推翻 8/5"不追价"指令）=====
                # roundXX(2026-09-01 用户拍板, 9/2 调优): 阶梯追价——首挂 0.6 静置 CHASE_AFTER_MIN=2 分钟未成交，
                # 每 2 分钟一档按 chase_count 推进追价档位 [0.65→0.70→0.75→0.80→0.85]（9/2: 3档→5档每档+0.05）。
                # 依据: 超时取消单 88.4% 方向对（99对/13错），价格朝预测方向走时挂单价追不上。
                # 只追涨不追跌: 价格跌回挂价时 CLOB 自动撮合原单（限价买单在簿，ask 跌穿即成交），
                # 未成交的定义就是 ask 始终高于挂价 → 唯一需要的动作是向上追。
                # 口径保护: 被撤的旧单不写 fills.jsonl（非终态），新单终态是该信号唯一记录。
                try:
                    if (age_min > CHASE_AFTER_MIN
                            and now_ts - info.get("last_chase_check", 0.0) > CHASE_CHECK_INTERVAL):
                        info["last_chase_check"] = now_ts
                        liq_ok2, new_ask, _spread3, _reason = self._check_liquidity(
                            info["token_id"], info.get("max_price", MAX_ORDER_PRICE))
                        if not (liq_ok2 and new_ask):
                            # 9/2 补日志盲区: 簿检查失败原为静默跳过，导致"为什么不追了"无法事后
                            # 归因(9/2 5am-et 17:57 评估点无任何日志实证)。现落 WARNING 留证据。
                            log(f"🕵️ 追价评估跳过: 簿检查失败 | {info['slug']} {info['side']} | "
                                f"挂价{info['price']:.2f} | 原因: {_reason}", "W")
                        if liq_ok2 and new_ask:
                            # ===== 9/2 用户拍板(round52.2): 取消追价封顶，买不到就一直追 =====
                            # tier③ 死区根因(修复): 原 ask>封顶时挂价原地不动，强趋势日(ask 0.9+)
                            # 挂 0.60 永远吃不到 → 方向对也踏空(9/1 教训重演)。现改为: 每 2 分钟
                            # 一档上移挂价排队(0.65→0.70→...→0.85 后继续 +0.05 无封顶，CLOB 上限
                            # 0.99)，ask 回落到档位即成交，档位覆盖 ask 后=直接 taker 吃价。
                            # 已知代价(用户知情确认): 成交价>引擎概率(praw)的部分是负 EV
                            # (盈亏平衡胜率=成交价)，用户选择成交率优先于单笔 EV。
                            # 保留底线: ①ask≤0.65 直接吃价(历史盈利引擎段)；②第二段守门只留
                            # RECHECK_PM_MIN 共识反转线(mid<0.50=方向反了才撤单)，不按拟付价抬线。
                            orig_price = info.get("orig_price", info["price"])
                            _tier_count = info.get("chase_count", 0) + 1
                            if _tier_count <= len(CHASE_TARGETS):
                                _tier_price = CHASE_TARGETS[_tier_count - 1]
                            else:
                                # 5 档用尽后继续 +0.05 延伸(0.90/0.95/0.99...)，无封顶
                                _tier_price = round(CHASE_TARGETS[-1] + (_tier_count - len(CHASE_TARGETS)) * 0.05, 2)
                            # 9/3 BUG-2 修复(round52.4 适配): 档位以「现挂价」为基线——首挂吃价单
                            # (如 0.93 taker)现挂价远高于 round52.2 时代档位(0.65~0.85)，按旧档位
                            # 索引起爬则前数档 new_price<=price 空转约 12 分钟(9/3 7am-et 实证: 方向对
                            # 整小时踏空)。max 保证每次评估至少比现挂价高 0.05，不被旧口径拖回。
                            _tier_price = min(max(_tier_price, round(info["price"] + 0.05, 2)), 0.99)  # CLOB 限价上限
                            if new_ask <= info["price"]:
                                # 保护: 实时 ask 未高于现挂价(含已回落) → 维持原单等成交(CLOB 自动撮合)，
                                # 不撤不重挂(避免向下吃价 / 无意义重挂)。
                                new_price = round(info["price"], 2)
                            elif new_ask <= CHASE_EAT_MAX:
                                # ① 盈利引擎段: 直接 taker 吃 best_ask，确定性成交不损失机会。
                                new_price = round(new_ask, 2)
                            else:
                                # ②③ 合并: 挂 min(best_ask, 当前档位价) 排队等回落——挂价每 2 分钟
                                # 上移一档，永不因「ask 太高」停摆；档位覆盖 ask 后即 taker 成交。
                                new_price = round(min(new_ask, _tier_price), 2)
                            # ===== 第二段守门(9/2 收窄): 只保共识反转线 =====
                            # 我方方向 Gamma 实时中间价 < RECHECK_PM_MIN(0.50) → 方向共识已反转，
                            # 此时追价=买必输边，撤单 SKIP；查询失败/缺失 → 保守放弃本次追价
                            # （不撤不加价，维持原单等回落）。
                            # 原「拟付价抬线」(BUG-6 _raising_tier2)与无封顶一直追冲突，按 9/2
                            # 拍板移除——mid 只要 ≥0.50 就放行追价，成交率优先。
                            _live_pm = self._gamma_price(info["slug"], str(info.get("side", "")).upper())
                            _gate = RECHECK_PM_MIN
                            if _live_pm <= 0:
                                log(f"🕵️ 第二段复核数据缺失，保守放弃本次追价 | {info['slug']} {info['side']}", "W")
                                new_price = round(info["price"], 2)  # 强制走下方「同价无意义」分支=维持原单
                            elif _live_pm < _gate:
                                log(f"🛑 第二段复核失败: 我方{info['side']}实时Gamma={_live_pm:.2f}<守门线{_gate:.2f} "
                                    f"共识已反超/拟付价高于共识 → 撤单SKIP | {info['slug']}", "W")
                                self.client.cancel_order(order_id=oid)
                                # 9/2 BUG-3: 竞态窗口 0.5→2s（CLOB 索引延迟 9/1 实证 3s+）+
                                # 非 FILLED 时用 trades 成交证据复核，部分成交也如实落档
                                time.sleep(2)
                                _post = self._check_order_status(oid, info.get("token_id"))
                                _fs_rc = self._fetch_fill_stats(oid, info.get("token_id"))
                                _m_rc = _fs_rc.get("size_matched")
                                if _post == "FILLED" or (_m_rc or 0) > 0:
                                    _st = "FILLED" if _post == "FILLED" else "RECHECK_PARTIAL"
                                    log(f"✅ 撤单瞬间已成交(竞态/成交证据复核) | {info['slug']} | 记{_st}", "I")
                                    self._record_fill(oid, info["slug"], info["side"], True, _st,
                                                      size_matched=_m_rc,
                                                      avg_price=_fs_rc.get("avg_price"), info=info)
                                else:
                                    self._record_fill(oid, info["slug"], info["side"], False,
                                                      "RECHECK_CANCELLED", info=info)
                                self._remove_pending_record(oid)
                                expired.append(oid)
                                continue
                            # 9/2 审查(异味): 删除重复赋值 orig_price（本函数前段 L2402 已取值）
                            if new_price <= info["price"]:
                                # 边界: 重挂同价无意义（ask 已回落/维持判定），保持等撮合模式
                                pass
                            else:
                                log(f"🔄 追价评估 | {info['slug']} {info['side']} | "
                                    f"挂价{info['price']:.2f} ask已上移至{new_ask:.2f} → 撤单重挂{new_price:.2f} "
                                    f"(第{info.get('chase_count', 0) + 1}次)", "I")
                                self.client.cancel_order(order_id=oid)
                                # 防竞态: cancel 异步，撤单瞬间可能已成交（吃 ask 价位的限价单被对方吃到）。
                                # 9/2 BUG-3: sleep 0.5→2s——CLOB 索引延迟 9/1 实证 3s+，0.5s 查不到≠没成交；
                                # 且非 FILLED 时必须再用 trades 成交证据复核，防「已成交仍重挂」双仓
                                time.sleep(2)
                                post = self._check_order_status(oid, info.get("token_id"))
                                _fs_old = self._fetch_fill_stats(oid, info.get("token_id"))
                                _m_old = _fs_old.get("size_matched")
                                _avg_old = _fs_old.get("avg_price")
                                # 本单预期份额（近似），区分「整单成交」与「部分成交」
                                _est_old_sz = (info.get("amount", 0) or 0) / max(info.get("price", 0.5) or 0.5, 0.05)
                                _full_fill = post == "FILLED" or (
                                    _m_old is not None and _est_old_sz > 0 and _m_old >= _est_old_sz * 0.98)
                                if _full_fill:
                                    log(f"✅ 撤单瞬间已成交(竞态/成交证据复核) | {info['slug']} | 记FILLED不重挂", "I")
                                    self._record_fill(oid, info["slug"], info["side"], True, "FILLED",
                                                      size_matched=_m_old,
                                                      avg_price=_avg_old, info=info)
                                    self._remove_pending_record(oid)
                                    expired.append(oid)
                                else:
                                    # 9/2 BUG-2: 撤单前部分成交——份额并入追价链(matched_size/notional)，
                                    # 新单金额扣减已成交部分，总敞口不超预算；部分成交不单独落终态，
                                    # 由链尾终态统一携带（防镜像撞车丢失最终成交证据）
                                    if _m_old:
                                        _nt_old = _m_old * (_avg_old if _avg_old else info["price"])
                                        info["matched_size"] = float(info.get("matched_size") or 0) + _m_old
                                        info["matched_notional"] = float(info.get("matched_notional") or 0) + _nt_old
                                        info["amount"] = round(max(info["amount"] - _nt_old, 0.0), 2)
                                        log(f"⚠️ 撤单前部分成交 {_m_old:.4g}sh 已并入追价链 | "
                                            f"剩余预算${info['amount']:.2f} | {info['slug']}", "W")
                                        if info["amount"] < 1.0:
                                            # 剩余预算不足 CLOB 最小单$1 → 链终态: 已成交部分即全部仓位
                                            self._record_fill(oid, info["slug"], info["side"], True,
                                                              "CHASE_PARTIAL", info=info)
                                            self._remove_pending_record(oid)
                                            expired.append(oid)
                                            continue
                                    new_sz = info["amount"] / max(new_price, 0.05)
                                    _chase_sz_ok = True
                                    if new_sz < 5:  # MIN_SHARES 与 execute_order 一致
                                        # roundXX(2026-09-01): 沿用 execute_order 的"上调到最小 5 shares"逻辑，
                                        # 避免小金额(bet_amount≈$2.5)在追价档位因 shares<5 而静默失效。
                                        # 交叉验证修正: 原改动把 place_order 留在 else 分支——上调成功后
                                        # 不会下单（旧单已撤、新单没挂，仓位静默丢失）。现改为: 上调失败
                                        # 置 _chase_sz_ok=False 走既有 REPRICE_FAILED 终态路径。
                                        _min_amt = 5 * new_price
                                        if _min_amt <= (info.get("amount", 0) or 0) * 3 and _min_amt <= 20.0:
                                            new_sz = 5.0
                                            # 9/2 交叉审核修复: 记账金额必须同步上调——否则实际花费
                                            # 5×new_price 而 pending/done 记的还是旧 amount，成交后
                                            # pnl 按旧 amount 计算，与真实盈亏脱节。
                                            info["amount"] = _min_amt
                                            log(f"⚠️ 追价金额上调至 ${_min_amt:.2f} ({new_sz:.0f}sh) | {info['slug']}", "W")
                                        else:
                                            log(f"⚠️ 追价放弃: shares不足5 ({new_sz:.1f}sh) | {info['slug']}", "W")
                                            _chase_sz_ok = False
                                    else:
                                        new_sz = min(new_sz, 200)  # MAX_SHARES 与 execute_order 一致
                                    r2 = (self.place_order(info["token_id"], new_price, new_sz, info["side"])
                                          if _chase_sz_ok
                                          else {"ok": False, "error": "chase shares<5"})
                                    if r2.get("ok") and not r2.get("skipped") and r2.get("order_id"):
                                        new_oid = r2["order_id"]
                                        self._pending_orders[new_oid] = {
                                            "slug": info["slug"], "side": info["side"],
                                            "ts": info["ts"],  # 继承首挂 ts: 总超时窗口 ORDER_TTL_MIN 不变
                                            "amount": info["amount"], "token_id": info["token_id"],
                                            "retry": 0, "max_price": info["max_price"],
                                            "price": new_price, "orig_price": orig_price,
                                            # 9/2 交叉审核修复: _eff_cap 从未赋值(阶梯追价重构漏改)，
                                            # 每次追价重挂必抛 NameError——且异常点在「旧单已撤、
                                            # 新单已下单成功」之后，新单无追踪/无PENDING记录，
                                            # 成交后成未知成交黑洞。改为继承旧单 price_cap。
                                            "price_cap": info.get("price_cap", MAX_ORDER_PRICE),
                                            "chase_count": info.get("chase_count", 0) + 1,
                                            "last_chase_check": now_ts,
                                            "repriced_from": oid[:20],  # 复盘溯源: 追价链路
                                            # C2: 追价链头(首挂完整 order_id)——多级追价塌缩到链头，
                                            # 终态回写 CHASED_ 镜像时按链头 id 落档，复盘按信号原始 id 命中
                                            "orig_order_id": info.get("orig_order_id") or oid,
                                            # 9/2: 追价链继承 probability_raw 供复盘分桶（档位已统一，不参与判定）
                                            "probability_raw": info.get("probability_raw", 0),
                                            # 9/2 BUG-2: 继承链上已并入的部分成交份额（链尾终态统一携带）
                                            "matched_size": float(info.get("matched_size") or 0),
                                            "matched_notional": float(info.get("matched_notional") or 0),
                                        }
                                        try:
                                            with open(PENDING, "a", encoding="utf-8") as f:
                                                f.write(json.dumps({
                                                    "order_id": new_oid, "slug": info["slug"],
                                                    "side": info["side"], "amount": info["amount"],
                                                    "price": new_price, "repriced_from": oid[:20],
                                                    "orig_order_id": info.get("orig_order_id") or oid,
                                                    "orig_price": orig_price,
                                                    "price_cap": info.get("price_cap", MAX_ORDER_PRICE),  # 9/2 修复: 原 _eff_cap 未定义
                                                    "max_price": info.get("max_price"),  # 9/2 ⚪3
                                                    "probability_raw": info.get("probability_raw", 0),  # 9/2: 追价链动态封顶恢复用
                                                    # 9/2 BUG-1/BUG-4 修复: 补 token_id/chase_count（恢复单追价+档位还原）
                                                    "token_id": info.get("token_id", ""),
                                                    "chase_count": info.get("chase_count", 0) + 1,
                                                    # 9/2 BUG-2: 链上部分成交份额随单持久化
                                                    "matched_size": float(info.get("matched_size") or 0),
                                                    "matched_notional": float(info.get("matched_notional") or 0),
                                                    "ts": datetime.fromtimestamp(info["ts"], timezone.utc).isoformat(),
                                                }) + "\n")
                                        except OSError:
                                            pass
                                        self._remove_pending_record(oid)
                                        expired.append(oid)
                                        log(f"🔄 追价重挂成功 | {info['slug']} {info['side']} | "
                                            f"{new_price:.2f}×{new_sz:.0f}sh | 旧单{oid[:12]}→新单{new_oid[:12]}", "I")
                                    else:
                                        # 重挂失败(网络/钱包异常): 旧单已撤，诚实记终态防口径黑洞
                                        # C2: info 带追价链 → 同步镜像 CHASED_* 到首挂 id
                                        # 9/2 BUG-2: 链上已并入部分成交时不是纯未成交，状态名如实区分
                                        _inh_sz_now = float(info.get("matched_size") or 0)
                                        _st = "REPRICE_PARTIAL" if _inh_sz_now > 0 else "REPRICE_FAILED"
                                        self._record_fill(oid, info["slug"], info["side"],
                                                          _inh_sz_now > 0, _st, info=info)
                                        self._remove_pending_record(oid)
                                        expired.append(oid)
                                        log(f"❌ 追价重挂失败 | {info['slug']} | {str(r2.get('error', ''))[:60]} | 记{_st}", "E")
                except Exception as e:
                    log(f"⚠️ 追价评估异常(不影响挂单) | {oid[:20]}: {str(e)[:60]}", "W")
        for oid in expired:
            self._pending_orders.pop(oid, None)

    def _cancel_all_pending_orders(self):
        """Bug-4 修复: 紧急停止时取消所有已挂订单，清理 PENDING 文件。
        A2(2026-08-23): 仍在本小时交易窗口内(放置 <50min)的 LIVE 挂单**不撤**，
        保留 PENDING 记录由重启后的 _recover_pending_orders 继续追踪，避免敞口静默丢失。
        仅取消已过期/临近结算的旧单，并清理其余 PENDING 记录。"""
        cancelled_count = 0
        preserved_count = 0
        if not self.live or not self.client:
            # paper 模式或未连接钱包：只清理本地记录
            try:
                PENDING.unlink()
            except OSError:
                pass
            self._pending_orders.clear()
            return 0
        now = time.time()
        # 1. 分类：窗口内未过期 → 保留；其余 → 取消并记 EMERGENCY_CANCELLED
        preserved = []  # (oid, info)
        cancel_list = []
        for oid, info in list(self._pending_orders.items()):
            if oid.startswith("PAPER_"):
                continue
            age = now - info.get("ts", 0)
            if 0 <= age < PRESERVE_ON_SHUTDOWN_SEC:
                preserved.append((oid, info))
            else:
                cancel_list.append((oid, info))
        # 2. 取消过期旧单（保留期间窗口内的不取消）
        for oid, info in cancel_list:
            try:
                # C2审计修复(🟡2): 同超时分支——先查成交再撤单，防撤单瞬间成交漏检
                _fs = self._fetch_fill_stats(oid, info.get("token_id"))
                self.client.cancel_order(order_id=oid)
                cancelled_count += 1
                # C2: 优先 trades API 真实成交统计，get_order 作回退；终态回写追价链
                _m = _fs.get("size_matched")
                if _m is None:
                    _m = self._read_matched(oid)  # A3: 区分部分持仓(trades API 失败回退)
                if _m is not None and _m > 0:
                    self._record_fill(oid, info.get("slug", ""), info.get("side", ""), True,
                                      "EMERGENCY_PARTIAL", size_matched=_m,
                                      avg_price=_fs.get("avg_price"),
                                      price=info.get("price", 0), info=info)
                else:
                    self._record_fill(oid, info.get("slug", ""), info.get("side", ""), False,
                                      "EMERGENCY_CANCELLED", info=info)
            except Exception:
                # 9/1 修复: 原来直接 pass——撤单失败（含"撤单瞬间已成交"被拒）时既不记终态
                # 又在下方清空 PENDING，终态永久丢失 → review 按 filled=None 伪造 pnl，
                # UI 成交/未成交显示错乱（8/23-8/27 有 4 单因此丢终态）。
                # 与超时路径同口径: 查成交证据定终态，无证据记 UNKNOWN_NOT_FOUND。
                try:
                    post = self._check_order_status(oid, info.get("token_id"))
                    _fs2 = self._fetch_fill_stats(oid, info.get("token_id"))
                    _m2 = _fs2.get("size_matched")
                    if post == "FILLED" or (_m2 is not None and _m2 > 0):
                        self._record_fill(oid, info.get("slug", ""), info.get("side", ""), True,
                                          "EMERGENCY_PARTIAL" if (_m2 is not None and 0 < _m2 <
                                           (info.get("amount", 0) or 0) / max(info.get("price", 0.5) or 0.5, 0.05) * 0.98)
                                          else "FILLED",
                                          size_matched=_m2, avg_price=_fs2.get("avg_price"), info=info)
                    else:
                        self._record_fill(oid, info.get("slug", ""), info.get("side", ""), None,
                                          "UNKNOWN_NOT_FOUND", info=info)
                except Exception:
                    log(f"⚠️ 紧急撤单终态记录失败 {oid[:20]}", "W")
        # 3. 兜底: 遍历 list_open_orders 取消所有非 PAPER、非保留订单
        preserved_oids = {oid for oid, _ in preserved}
        try:
            paginator = self.client.list_open_orders()
            for order in paginator:
                o_id = getattr(order, 'id', None) or (order.get("id") if isinstance(order, dict) else None)
                if o_id and not str(o_id).startswith("PAPER_") and o_id not in preserved_oids:
                    try:
                        self.client.cancel_order(order_id=str(o_id))
                        cancelled_count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        # 4. 清理 PENDING：重写为仅保留的窗口内记录；无保留则删除
        if preserved:
            preserved_count = len(preserved)
            try:
                # C2审计修复(🟡4): 改用带锁原子重写，防与 review 进程并发丢记录
                _kept = []
                for oid, info in preserved:
                    _kept.append(json.dumps({
                        "order_id": oid, "slug": info.get("slug", ""),
                        "side": info.get("side", ""), "amount": info.get("amount", 0),
                        "price": info.get("price", 0),
                        "orig_price": info.get("orig_price", info.get("price", 0)),
                        "price_cap": info.get("price_cap", MAX_ORDER_PRICE),
                        "token_id": info.get("token_id", ""),
                        # C2: 保留追价链头，重启后终态回写不断链
                        "orig_order_id": info.get("orig_order_id", ""),
                        # 9/3 P1-4 修复: 补齐恢复挂单必备键——原实现缺 5 键，重启恢复后
                        # chase_count 归 0 档位从头爬(硬约束违例)、部分成交份额蒸发。
                        "chase_count": info.get("chase_count", 0),
                        "max_price": info.get("max_price", MAX_ORDER_PRICE),
                        "probability_raw": info.get("probability_raw", 0),
                        "matched_size": info.get("matched_size", 0),
                        "matched_notional": info.get("matched_notional", 0),
                        "ts": datetime.fromtimestamp(info.get("ts", time.time()), timezone.utc).isoformat(),
                    }, ensure_ascii=False))
                if _kept:
                    _atomic_rewrite_locked(PENDING, _kept)
                else:
                    PENDING.unlink()
            except OSError:
                pass
            log(f"🛑 紧急关闭保留 {preserved_count} 笔本窗口挂单（重启后继续追踪，防敞口丢失）", "W")
        else:
            try:
                PENDING.unlink()
            except OSError:
                pass
        self._pending_orders.clear()
        if cancelled_count > 0:
            log(f"🛑 紧急取消 {cancelled_count} 笔过期挂单", "I")
        return cancelled_count

    def run(self, once=False):
        log(f"🚀 Bot | {'LIVE' if self.live else 'PAPER'} | 窗口 {WINDOW_OPEN_MIN}-{WINDOW_CLOSE_MIN}min | {QUEUE}", "I")
        # 启动时清理队列中的过期信号，防止 dashboard 显示旧信号
        self._cleanup_stale_queue()
        consecutive_errors = 0
        while True:
            try:
                self.tick()
                consecutive_errors = 0  # 成功则重置
            except Exception as e:
                consecutive_errors += 1
                # Bug-10 修复: 连续异常超限则退出，避免无限错误日志
                log(f"❌ tick 异常 ({consecutive_errors}/60)（已兜底）: {str(e)[:100]}", "E")
                if consecutive_errors >= 60:
                    log("💀 连续异常超 60 次（5分钟），退出", "E")
                    break
                time.sleep(5)
            if once:
                break
            time.sleep(3)
        log("🛑 退出", "I")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    # 从配置文件读取模式（命令行参数优先）
    if not args.live:
        try:
            if CONFIG.exists():
                cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
                if cfg.get("mode") == "live":
                    args.live = True
        except Exception:
            pass

    acquire_pidfile()
    bot = HermesBot(live=args.live)

    def shutdown(sig, frame):
        log("🛑 收到退出信号", "I")
        # Bug-9: 设置 shutdown 标志，让重试 sleep 循环可中断
        try:
            bot._shutdown = True
        except Exception:
            pass
        # Bug-4 修复: 先取消所有已挂订单，再释放锁
        try:
            bot._cancel_all_pending_orders()
        except Exception as e:
            log(f"⚠️ 取消挂单异常: {str(e)[:50]}", "W")
        release_pidfile()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        bot.run(once=args.once)
    finally:
        release_pidfile()