#!/usr/bin/env python3
"""
Hermes BTC 交易仪表盘 — Flask 后端
"""

import sys, os, json, re, subprocess, time, threading, calendar
from hermes_common import now_et, build_slug, http_get_json, get_proxy, load_pm_creds, save_pm_creds, PROXY, GAMMA_API, BINANCE_API, ET, log
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import wraps
from collections import OrderedDict

sys.path = [p for p in sys.path if ".hermes" not in p]

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 8/9: 强制清理残留代理环境变量（如 http://127.0.0.1:10808 无监听），防止 httpx/urllib 读到后所有外部请求失败。
# 需要代理时通过 HERMES_PROXY 显式指定。
for _k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)

ENV_FILE = THIS_DIR / ".env"
SIGNAL_FILE = DATA_DIR / "claude_signal.jsonl"       # 与 bot 共用队列（8/18 修复：bot 8/16 v4 已从 /tmp/hermes_signal.jsonl 迁到 data/claude_signal.jsonl）
DONE_FILE = DATA_DIR / "hermes_done.jsonl"             # 交易记录持久化
ANALYSIS_FILE = DATA_DIR / "hermes_analysis.json"
MARKET_FILE = DATA_DIR / "hermes_market_data.json"   # 当前市场实时快照（含原文盘口读数值）
LAST_SIGNAL_FILE = DATA_DIR / "hermes_last_signal.json"
# bot 日志文件：Windows 下 start_bot.bat 将 bot stdout/stderr 重定向到 hermes_bot.log
# (原 launchd 时代用 hermes_bot_launchd.log；Windows 迁移后统一收敛至 hermes_bot.log)
LOG_FILE = DATA_DIR / "hermes_bot.log"
CONFIG_FILE = THIS_DIR / "hermes_config.json"
PIDFILE = DATA_DIR / "hermes_bot.pid"

GAMMA = GAMMA_API
BINANCE_KLINES = f"{BINANCE_API}/api/v3/klines"
BINANCE_TICKER = f"{BINANCE_API}/api/v3/ticker/price?symbol=BTCUSDT"

# Gamma API 缓存：LRU 策略，限制大小避免内存泄漏
_market_cache = OrderedDict()
MAX_CACHE_SIZE = 500

# 8/10: BTC 价格进程内缓存 + 锁（threaded=True 后多线程访问需线程安全）。
# 外部请求失败时用最近一次成功值降级返回，避免前端 2s 轮询反复触发 8s 超时请求。
_ticker_cache: dict = {"price": None, "ts": 0.0}
_ticker_lock = threading.Lock()
# api_status 外部请求超时（秒）：比全局 8s 收紧，降低单个慢请求拖垮服务的概率
STATUS_HTTP_TIMEOUT = 4

def get_market_cached(slug):
    """获取市场数据，已结算的永久缓存，未结算的缓存 20 秒，失败也缓存 20 秒防止重复打爆 API"""
    now_ts = time.time()
    cached = _market_cache.get(slug)
    if cached:
        if cached["closed"] or (now_ts - cached["ts"]) < 20:
            _market_cache.move_to_end(slug)  # LRU: 移到末尾
            return None if cached.get("fail") else cached
    # 8/17 修复: GAMMA 路径形式 `markets/slug/{slug}` 返回 dict（有 outcomePrices），
    # query 形式 `markets?slug={slug}` 返回 list（老 BTC 1H slug 已被清理，长度=0）。
    # 8/15 8pm 起的 PENDING 13 笔都因此失败。先试 path，回退 query。
    m = http_get(f"{GAMMA}/markets/slug/{slug}")
    market = m if isinstance(m, dict) else None
    if not market:
        m2 = http_get(f"{GAMMA}/markets?slug={slug}")
        if m2 and isinstance(m2, list) and len(m2) > 0:
            market = m2[0]
    if market:
        closed = market.get("closed", False)
        try:
            prices = market.get("outcomePrices", [])
            if isinstance(prices, str): prices = json.loads(prices)
            up_p = float(prices[0]) if prices and len(prices) > 0 else 0
        except (json.JSONDecodeError, TypeError, ValueError, IndexError):
            return None  # 坏数据不缓存、不中断调用方
        entry = {"closed": closed, "up_p": up_p, "ts": now_ts}
        _market_cache[slug] = entry
        if len(_market_cache) > MAX_CACHE_SIZE:
            _market_cache.popitem(last=False)  # 淘汰最老
        return entry
    # 失败结果缓存 20s，平衡「避免打爆 GAMMA」与「修复后 review 补数据能及时生效」
    # 8/17 调优：原 60s 过长，导致 review 补 hermes_results.jsonl 后 dashboard 兜底路径需要
    # 60s 后才能重试；现在 20s + side 缺失保护+GAMMA fail 404 仍是 no-op，不浪费 API 配额
    _market_cache[slug] = {"closed": False, "up_p": None, "ts": now_ts, "fail": True}
    if len(_market_cache) > MAX_CACHE_SIZE:
        _market_cache.popitem(last=False)
    return None

_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
           "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}


def _slug_end_utc(slug: str) -> int | None:
    """从 slug 解析该小时合约结算时间的 UTC 时间戳（结束 = ET 整点后 1h）。

    8/14 修复: dashboard /api/history 兜底结算缺时间守卫——盘中实时价>0.9 会被误判已结算。
    slug 形如 bitcoin-up-or-down-august-14-2026-5am-et → ET 05:00 开始，06:00 结束。
    """
    m = re.search(r"-(january|february|march|april|may|june|july|august|september|october|november|december)-(\d+)-(\d+)-(\d+)(am|pm)-et", slug)
    if not m:
        return None
    month_str, day, year, hour, ap = m.groups()
    month = _MONTHS.get(month_str)
    if not month:
        return None
    hour = int(hour)
    if ap == "pm" and hour != 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    try:
        et_start = datetime(int(year), month, int(day), hour, 0, tzinfo=ET)
        return int((et_start + timedelta(hours=1)).timestamp())
    except Exception:
        return None


def http_get(url, timeout=8):
    """使用统一的线程安全 HTTP 工具"""
    return http_get_json(url, timeout=timeout, proxy=PROXY, user_agent="hermes-dash/1.0")


def get_btc_price_cached(timeout=STATUS_HTTP_TIMEOUT):
    """获取 BTC 价格，带进程内降级缓存。

    8/10: 外部请求失败时返回最近一次成功值（stale 数据），而非每次轮询都等满超时返回空，
    避免前端 2s 轮询在单线程/网络抖动下页面数据全 "--"。
    9/3: 缓存 <1.5s 新鲜时直接返回（配合后台线程 1s 刷新），避免代理下每个 Binance
    请求 ~2s 造成的并发堆积（实测代理 7890 单请求 ~2s，500ms 高频同步拉取会拖垮全部外呼）。
    返回 (price, stale_flag)：price 为 float 或 None。
    """
    with _ticker_lock:
        _p, _ts = _ticker_cache.get("price"), _ticker_cache.get("ts", 0)
    if _p is not None and time.time() - _ts < 1.5:
        return _p, False
    try:
        ticker = http_get(BINANCE_TICKER, timeout=timeout)
        if ticker and "price" in ticker:
            price = float(ticker["price"])
            with _ticker_lock:
                _ticker_cache["price"] = price
                _ticker_cache["ts"] = time.time()
            return price, False
    except Exception:
        pass
    # 失败：用最近一次成功缓存降级（stale=True）
    with _ticker_lock:
        return _ticker_cache.get("price"), True

def get_current_et():
    et = now_et()
    return et.hour, et.minute, et

# 9/2 审查修复(B6): 节流日志——dashboard 被 UI 每 2s 轮询，坏行若每轮都打日志会刷屏。
# 同一 key 间隔 ≥300s 才重复打点，首次必打。
_log_throttle = {}

def _log_throttled(key, msg, level="W", interval=300):
    now = time.time()
    if now - _log_throttle.get(key, 0) >= interval:
        _log_throttle[key] = now
        log(msg, level)

# 9/2 审查修复(A2): 配置防抹除守卫——原链条「读取失败→返回3键defaults→UI改一项→
# save_config 全量写回」会把 mode/trading_paused/rule_a_* 等整份配置静默抹掉。
# 修复: 文件存在但读取/解析失败时置标志，save_config 检测到即拒绝写回(fail-closed)。
# 注: 全局标志在并发下可能「误拒」(别请求读失败拦住本请求保存)——误拒方向安全，
# 用户重试即可；误写方向(抹配置)不可接受。
_cfg_load_failed = {"flag": False}

def load_config():
    defaults = {"bet_amount": 2.0, "mode": "paper", "tp_enabled": True, "analyze_min": 40}
    _cfg_load_failed["flag"] = False
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in defaults.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
        except Exception as e:
            _cfg_load_failed["flag"] = True
            log(f"⚠️ config 读取失败: {str(e)[:60]} | 返回默认值展示，本次禁止写回", "W")
    return defaults

def save_config(cfg):
    """返回 True=已写回；False=读取失败守卫拦截（调用方应返回错误，不得静默）"""
    if _cfg_load_failed["flag"]:
        log("🛑 config 写回被拦截: 上次 load_config 读取失败，写回会抹掉整份配置", "E")
        return False
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return True

# round55.1: UI 调 analyze_min 时联动修改 HermesBTC1HEngine 信号任务触发时刻——
# 调几点，引擎就几点出信号、bot 几点下单（信号数据时刻 = 下单时刻，完全对齐，真正生效）。
ENGINE_TASK_NAME = r"\HermesBTC1HEngine"

def _sync_engine_schedule(analyze_min: int) -> tuple[bool, str]:
    """schtasks /change 把引擎信号任务开始时间改为 00:{analyze_min:02d}（每小时重复保留，已实测）。
    返回 (ok, err 或空)。stdin=DEVNULL 防 run-as 密码提示挂起（任务 Run As Administrator 空密码）。"""
    st = f"00:{analyze_min:02d}"
    try:
        r = subprocess.run(
            ["schtasks", "/change", "/tn", ENGINE_TASK_NAME, "/st", st],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        return False, f"schtasks 执行异常: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or f"rc={r.returncode}").strip()[:120]
    return True, ""

def _verify_engine_schedule(analyze_min: int) -> tuple[bool, str]:
    """复核：查任务 Next Run Time，分钟位必须 == analyze_min，防改失败静默（/change 返回 0 但未生效）。
    编码：dashboard 以 pythonw（无控制台）运行，schtasks 输出为系统 ANSI（中文系统=GBK）且标签为中文
    （"下次运行时间:"），与 PowerShell 交互环境（英文标签）不同——实测 9/5，须 BOM 引导解码 + 中英日标签兼容。"""
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", ENGINE_TASK_NAME, "/fo", "LIST", "/v"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=30,
        )
    except Exception as e:
        return False, f"schtasks query 异常: {e}"
    raw = r.stdout
    if raw[:2] == b"\xff\xfe":
        text = raw.decode("utf-16-le", errors="replace")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
    m = re.search(r"(?:Next Run Time|下次运行时间|次回の実行時刻):\s*(\d{4}/\d{1,2}/\d{1,2})\s+(\d{1,2}):(\d{2}):\d{2}", text)
    if not m:
        return False, "未能解析 Next Run Time（输出标签/编码不兼容）"
    minute = int(m.group(3))
    if minute != analyze_min:
        return False, f"Next Run 分钟位 {minute} 与目标 :{analyze_min} 不符"
    return True, ""

from flask import Flask, jsonify, request, render_template_string, make_response
from flask_cors import CORS

app = Flask(__name__)
# round55.2: 修复 CORS 裸主机名永不匹配——浏览器 Origin 头总是 scheme://host:port 形态，
# 裸 "127.0.0.1" 一个都匹配不上：经 http://localhost:5050 打开的页面所有 /api/* 跨域请求
# 被浏览器静默拦截（轮询拿不到 config，ANALYZE 下拉永远停在模板默认 :40；SET 的 POST 也发不出）。
# 9/6 实证：服务端 analyze_min=45、127.0.0.1 会话正常显示 :45，localhost 会话 stuck :40。
CORS(app, resources={r"/api/*": {"origins": ["http://127.0.0.1:5050", "http://localhost:5050"]}})

# round55.2: API 响应一律 no-store——页面 HTML 9/3 已 no-store 但 /api/* 一直裸奔，
# 长驻标签页可能命中浏览器/代理启发式缓存拿到旧 config（UI 轮询显示旧值）。
@app.after_request
def _api_no_store(resp):
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp

# —— 认证（与 .env 中 FLASK_USER / FLASK_PASS 对齐）——
DASH_USER = os.getenv("FLASK_USER", "admin")
DASH_PASS = os.getenv("FLASK_PASS", "")

def require_auth(f):
    """写操作端点需要认证（仅在设置了 DASH_PASS 时生效）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if DASH_PASS:
            auth = request.authorization
            if not auth or auth.username != DASH_USER or auth.password != DASH_PASS:
                return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    # 9/3: 加 no-store，防止浏览器缓存旧版页面 JS（用户强刷后才见新功能的教训）
    resp = make_response(render_template_string(HTML_TEMPLATE))
    resp.headers["Cache-Control"] = "no-store"
    return resp

# bot 存活检测结果缓存（powershell 进程扫描开销大，30s 内复用）
_bot_alive_cache = {"ts": 0.0, "alive": False}

def _bot_alive_cached():
    """真实进程存活检测（按命令行匹配），带 30s 缓存。
    修复：原来只判 PIDFILE.exists()，bot 崩溃/被杀后残留 pid 文件会持续误报在线"""
    now = time.time()
    if now - _bot_alive_cache["ts"] > 30:
        _bot_alive_cache["alive"] = bool(_bot_running_win())
        _bot_alive_cache["ts"] = now
    return _bot_alive_cache["alive"]

@app.route("/api/health")
def api_health():
    """健康检查端点"""
    bot_running = _bot_alive_cached()
    signal_exists = SIGNAL_FILE.exists()
    done_exists = DONE_FILE.exists()
    try:
        with open(DONE_FILE, encoding="utf-8") as f:
            done_lines = sum(1 for _ in f)
    except Exception:
        done_lines = 0
    return jsonify({
        "ok": True,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "bot_running": bot_running,
        "signal_queued": signal_exists,
        "trades_total": done_lines,
    })

@app.route("/api/status")
def api_status():
    result = {
        "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "time_et": "", "btc_price": None, "anchor_price": None,
        "delta": None, "delta_pct": None, "contract_slug": "",
        "contract_tokens": {}, "wallet_balance": None,
        "wallet_address": "", "in_window": False, "window_minute": -1,
    }
    et_h, et_m, et_dt = get_current_et()
    result["time_et"] = et_dt.strftime("%Y-%m-%d %H:%M ET")
    result["window_minute"] = et_m
    result["in_window"] = 0 <= et_m <= 55

    slug = build_slug(et_dt)
    result["contract_slug"] = slug

    try:
        price, _stale = get_btc_price_cached()
        if price is not None:
            result["btc_price"] = price
    except Exception: pass

    try:
        # 8/17 修复: GAMMA 路径形式 `markets/slug/{slug}` 返回 dict（含 outcomePrices+events+eventMetadata），
        # query 形式 `markets?slug={slug}` 返回 list（老 BTC 1H slug 已被清理）。
        market = http_get(f"{GAMMA}/markets/slug/{slug}", timeout=STATUS_HTTP_TIMEOUT)
        if isinstance(market, dict):
            m = market
        else:
            market2 = http_get(f"{GAMMA}/markets?slug={slug}", timeout=STATUS_HTTP_TIMEOUT)
            if market2 and len(market2) > 0:
                m = market2[0]
            else:
                m = None
        if m:
            # eventMetadata 在 events[0] 下（与 collect/analyze_bucket 一致），兼容顶层直挂
            meta = m.get("eventMetadata")
            if not meta:
                events = m.get("events") or []
                if events:
                    meta = events[0].get("eventMetadata")
            if isinstance(meta, str): meta = json.loads(meta)
            ptb = meta.get("priceToBeat") if meta else None
            if ptb:
                result["anchor_price"] = float(ptb)
            else:
                end_date = m.get("endDate", "")
                if end_date:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    start_ms = int((end_dt - timedelta(hours=1)).timestamp() * 1000)
                    klines = http_get(f"{BINANCE_KLINES}?symbol=BTCUSDT&interval=1h&startTime={start_ms}&limit=1", timeout=STATUS_HTTP_TIMEOUT)
                    if klines and len(klines) > 0:
                        result["anchor_price"] = float(klines[0][1])

            tokens = m.get("clobTokenIds", [])
            prices = m.get("outcomePrices", [])
            if isinstance(tokens, str): tokens = json.loads(tokens)
            if isinstance(prices, str): prices = json.loads(prices)
            result["contract_tokens"] = {
                "up_token": tokens[0] if len(tokens) > 0 else "",
                "down_token": tokens[1] if len(tokens) > 1 else "",
                "up_price": float(prices[0]) if len(prices) > 0 else 0,
                "down_price": float(prices[1]) if len(prices) > 1 else 0,
            }
    except Exception: pass

    if result["btc_price"] and result["anchor_price"]:
        result["delta"] = round(result["btc_price"] - result["anchor_price"], 2)
        result["delta_pct"] = round(result["delta"] / result["anchor_price"] * 100, 4)

    return jsonify(result)

# —— /api/price_history：BTC 图表整小时数据（9/3 UI改版：原来只有近4分钟2s轮询点，
#    用户看不到锚定线穿越过程。改为 1m klines 回填整小时 + pm_prices 5min 概率 + 锚定线）——
_anchor_cache = {"slug": "", "price": None}

def _get_anchor_cached(slug):
    """当前小时锚定价：gamma priceToBeat 优先，binance 1h 开盘价兜底。每 slug 只拉一次"""
    if _anchor_cache["slug"] == slug and _anchor_cache["price"] is not None:
        return _anchor_cache["price"]
    try:
        market = http_get(f"{GAMMA}/markets/slug/{slug}", timeout=STATUS_HTTP_TIMEOUT)
        m = market if isinstance(market, dict) else None
        if not m:
            ms = http_get(f"{GAMMA}/markets?slug={slug}", timeout=STATUS_HTTP_TIMEOUT)
            m = ms[0] if ms and len(ms) > 0 else None
        if m:
            meta = m.get("eventMetadata")
            if not meta:
                events = m.get("events") or []
                meta = events[0].get("eventMetadata") if events else None
            if isinstance(meta, str):
                meta = json.loads(meta)
            ptb = meta.get("priceToBeat") if meta else None
            if ptb:
                _anchor_cache.update(slug=slug, price=float(ptb))
                return _anchor_cache["price"]
            end_date = m.get("endDate", "")
            if end_date:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                start_ms = int((end_dt - timedelta(hours=1)).timestamp() * 1000)
                klines = http_get(f"{BINANCE_KLINES}?symbol=BTCUSDT&interval=1h&startTime={start_ms}&limit=1",
                                  timeout=STATUS_HTTP_TIMEOUT)
                if klines and len(klines) > 0:
                    _anchor_cache.update(slug=slug, price=float(klines[0][1]))
                    return _anchor_cache["price"]
    except Exception:
        pass
    return None

_price_hist_cache = {"minute": "", "points": [], "pm": []}

@app.route("/api/price_history")
def api_price_history():
    """整小时图表数据：points=1m收盘价(缓存到自然分钟,多标签页共享) + pm=5min概率采样 + anchor"""
    out = {"points": [], "pm": [], "anchor": None}
    try:
        et_dt = now_et()
        slug = build_slug(et_dt)
        out["anchor"] = _get_anchor_cached(slug)
        mkey = et_dt.strftime("%Y%m%d%H%M")
        if _price_hist_cache["minute"] != mkey:
            hour_start = et_dt.replace(minute=0, second=0, microsecond=0)
            start_ms = int(hour_start.timestamp() * 1000)
            limit = max(1, min(60, et_dt.minute + 1))
            kl = http_get(f"{BINANCE_KLINES}?symbol=BTCUSDT&interval=1m&startTime={start_ms}&limit={limit}",
                          timeout=8) or []
            end_s = start_ms // 1000 + 3600
            pts = [{"t": int(k[0]) // 1000, "btc": float(k[4])}
                   for k in kl if int(k[0]) // 1000 < end_s]
            # bot 每 5min 记录的当前小时 PM 概率（data/pm_prices/pm_YYYYMM.jsonl）
            pm = []
            pf = DATA_DIR / "pm_prices" / ("pm_" + et_dt.strftime("%Y%m") + ".jsonl")
            if pf.exists():
                hour_key = et_dt.strftime("%Y-%m-%d %H")
                with open(pf, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("slug_cur") == slug and str(r.get("et", "")).startswith(hour_key):
                            try:
                                minute = int(str(r["et"]).split(":")[1])
                                pm.append({"t": int(hour_start.timestamp()) + minute * 60,
                                           "up": round(float(r.get("up_cur") or 0) * 100, 1),
                                           "dn": round(float(r.get("dn_cur") or 0) * 100, 1)})
                            except Exception:
                                continue
                pm.sort(key=lambda x: x["t"])
            _price_hist_cache.update(minute=mkey, points=pts, pm=pm)
        out["points"] = _price_hist_cache["points"]
        out["pm"] = _price_hist_cache["pm"]
    except Exception as e:
        out["error"] = str(e)[:120]
    return jsonify(out)


@app.route("/api/ticker")
def api_ticker():
    """9/3 轻量行情：专供图表 500ms 高频采样。
    后台线程 500ms 刷 Binance 价格缓存，本接口读缓存秒回（不碰 gamma）。
    不这样做的后果：每请求同步等 ~600ms 代理往返，把 Flask backlog 挤爆，
    其他 API 出现 CONNECTION_RESET/REFUSED（实测 /api/status 10 次 1 败）。
    9/3: 增加 up/down——从市场缓存(20s)读 Polymarket UP 共识价，让 500ms ticker
    也能带概率，概率线像 BTC 线一样连贯，不再只靠 2s refresh 慢链（其含超时的 wallet）。"""
    _start_ticker_bg()
    out = {"btc": None, "anchor": None, "up": None, "down": None}
    with _ticker_lock:
        out["btc"] = _ticker_cache.get("price")
        ts = _ticker_cache.get("ts", 0)
    if out["btc"] is None or time.time() - ts > 5:
        # 兜底：后台线程未跑或缓存过期，同步拉一次
        try:
            price, _stale = get_btc_price_cached()
            out["btc"] = price
        except Exception:
            pass
    try:
        out["anchor"] = _get_anchor_cached(build_slug(now_et()))
    except Exception:
        pass
    try:
        mk = get_market_cached(build_slug(now_et()))
        if mk and mk.get("up_p") is not None:
            up_p = float(mk["up_p"])
            out["up"] = round(up_p * 100, 1)
            out["down"] = round((1.0 - up_p) * 100, 1)
    except Exception:
        pass
    return jsonify(out)


_ticker_bg_started = False

def _start_ticker_bg():
    """后台 500ms 刷新 Binance 价格（daemon 线程，幂等启动）"""
    global _ticker_bg_started
    if _ticker_bg_started:
        return
    _ticker_bg_started = True
    def _loop():
        while True:
            try:
                get_btc_price_cached()
            except Exception:
                pass
            time.sleep(1.0)   # 9/3: 代理下单请求 ~2s，1s 间隔是安全上限（500ms 会并发堆积拖垮代理）
    threading.Thread(target=_loop, daemon=True, name="ticker-bg").start()


@app.route("/api/wallet")
def api_wallet():
    result = {"wallet_balance": None, "wallet_address": ""}
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
        from polymarket import SecureClient
        pk = os.getenv("POLYMARKET_PK")
        if pk:
            # 8/10 修复: Polymarket SecureClient 的 httpx(trust_env=True) 依赖环境代理访问
            # relayer（国内被墙）。dashboard 启动时 L21-22 清空了代理变量 → 直连 ConnectTimeout，
            # 表现为钱包余额不显示。这里按网络自适应结果(get_proxy)临时注入代理，自适应切换。
            # http_get_json 用 ProxyHandler({}) 强制直连，不受此注入影响。
            os.environ.pop("http_proxy", None)
            os.environ.pop("https_proxy", None)
            _wallet_proxy = get_proxy()
            if _wallet_proxy:
                os.environ["http_proxy"] = _wallet_proxy
                os.environ["https_proxy"] = _wallet_proxy
            # 8/19 根治: 传入缓存凭据跳过 POST /auth/api-key 派生。本接口 10s 轮询一次，
            # 不缓存时每次 create 都派生 ≈ 360 次/小时轰炸认证端点（间歇故障/限流诱因）。
            _cached = load_pm_creds()
            if _cached:
                from polymarket.models.clob import ApiKeyCreds
                client = SecureClient.create(private_key=pk, credentials=ApiKeyCreds(**_cached))
            else:
                client = SecureClient.create(private_key=pk)
            save_pm_creds(client.credentials.model_dump())  # 幂等；fallback 派生后自动更新
            bal = client.get_balance_allowance(asset_type="COLLATERAL")
            result["wallet_balance"] = round(float(bal.balance) / 1e6, 2)
            result["wallet_address"] = client.wallet
        else:
            print("api_wallet: POLYMARKET_PK 未加载", flush=True)
    except Exception as e:
        # 8/9: 不静默吞异常，记录真实错误便于排障
        import traceback
        print(f"api_wallet 异常: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
    return jsonify(result)

@app.route("/api/signal")
def api_signal():
    # 优先读队列（仅返回"当前小时 slug"的有效信号，忽略残留的过期信号）
    # 根因修复：bot 长驻只在启动时清理过期队列，dashboard 读队列需按当前小时口径过滤，
    # 否则会一直显示残留的旧 slug 信号（如 8/13 8am 的 UP $0），造成"不更新"假象。
    current_slug = build_slug(now_et())  # 与 bot 的 _cleanup_stale_queue/find_signal_for_slug 口径一致
    if SIGNAL_FILE.exists():
        try:
            with open(SIGNAL_FILE, encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            matched = None
            for l in lines:
                try:
                    sig = json.loads(l)
                except json.JSONDecodeError:
                    continue
                if sig.get("slug") == current_slug:
                    matched = sig  # 取最后一条匹配当前小时的信号
            if matched is not None:
                out = {"has_signal": True, "signal": matched}
                # 决策链修复：bot 处理完当前小时信号后 last_signal 会更新为同 slug，
                # 附带其 result 供前端点亮 ORDER/FILL 节点（否则队列信号无 result，决策链全小时不亮）
                try:
                    if LAST_SIGNAL_FILE.exists():
                        last = json.loads(LAST_SIGNAL_FILE.read_text(encoding="utf-8"))
                        if (last.get("signal") or {}).get("slug") == current_slug:
                            out["result"] = last.get("result")
                            out["processed_at"] = last.get("processed_at")
                except Exception:
                    pass
                return jsonify(out)
        except Exception:
            pass
    # 队列无当前小时信号，读最后处理的信号（供 UI 持续展示最新已处理状态）
    if LAST_SIGNAL_FILE.exists():
        try:
            data = json.loads(LAST_SIGNAL_FILE.read_text(encoding="utf-8"))
            return jsonify({"has_signal": True, "signal": data.get("signal"), "result": data.get("result"), "processed_at": data.get("processed_at")})
        except Exception: pass
    return jsonify({"has_signal": False, "signal": None})

@app.route("/api/analysis")
def api_analysis():
    if not ANALYSIS_FILE.exists():
        return jsonify({"has_analysis": False, "analysis": None})
    try:
        txt = ANALYSIS_FILE.read_text(encoding="utf-8").strip()
        if not txt:
            return jsonify({"has_analysis": False, "analysis": None})
        try:
            analysis = json.loads(txt)  # 单 JSON（旧格式）
        except json.JSONDecodeError:
            analysis = json.loads(txt.splitlines()[-1])  # JSONL 追加格式：取最后一条
        # 盘口陷阱预警·常亮口径：附原始盘口读数（只读 market_data，供前端比方向，不碰引擎/不下单）
        try:
            _md = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
            _pm = _md.get("polymarket") or {}
            _sm = _pm.get("smart_money_consensus_1h") or {}
            analysis["raw_book_imb"] = _pm.get("book_imbalance")      # 盘口失衡（快）
            analysis["raw_book_up_mid"] = _pm.get("book_up_mid")      # 盘口绝对价共识（兜底慢）
            analysis["raw_smart_imb"] = _sm.get("net_up_imbalance")   # 聪明钱净失衡（真·慢）
            analysis["raw_source"] = str(_md.get("slug", ""))
        except Exception:
            pass
        return jsonify({"has_analysis": True, "analysis": analysis})
    except Exception:
        return jsonify({"has_analysis": False, "analysis": None})

@app.route("/api/logs")
def api_logs():
    if not LOG_FILE.exists():
        return jsonify({"logs": []})
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        recent = all_lines[-30:]
        parsed = []
        for line in recent:
            line = line.strip()
            if not line: continue
            match = re.match(r'\[(.*?)\]\s*\[(.*?)\]\s*(.*)', line)
            if match:
                parsed.append({"time": match.group(1), "level": match.group(2), "msg": match.group(3)})
            else:
                parsed.append({"time": "", "level": "", "msg": line})
        return jsonify({"logs": parsed})
    except Exception:
        return jsonify({"logs": []})

@app.route("/api/history")
def api_history():
    if not DONE_FILE.exists():
        return jsonify({"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0, "skipped": 0, "win_rate": 0}})
    # 9/3 瘦身: 数据指纹未变时直接短路——UI 每 2s 轮询全量 640 行(~200KB)纯属浪费，
    # 客户端带 ?ver=指纹，未变则只回指纹不回数据(省 CPU 解析 + 99% 带宽)
    def _mtime(p):
        try:
            return str(int(p.stat().st_mtime_ns))
        except OSError:
            return "0"
    _ver = _mtime(DONE_FILE) + "-" + _mtime(DATA_DIR / "hermes_results.jsonl") + "-" + _mtime(DATA_DIR / "hermes_fills.jsonl")
    if request.args.get("ver") == _ver:
        return jsonify({"data_version": _ver, "trades": [], "short": True})
    try:
        trades = []
        with open(DONE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    entry = json.loads(line)
                    sig = entry.get("signal", {})
                    # 8/17 修复: side 缺失 fallback 到 signal/direction 字段
                    # 8/16 5pm-10pm + 8/17 12am-3am 历史 done.jsonl 行缺 side
                    # （bot 当时只写了 signal/direction 字段），避免空字符串与 "UP"/"DOWN"
                    # 比对 False 导致错判 LOSS
                    side = sig.get("side") or sig.get("signal") or sig.get("direction") or ""
                    res = entry.get("result", {})
                    trade = {
                        "action": sig.get("action", "?"),
                        "side": side,
                        "slug": sig.get("slug", ""),
                        # 9/2 修复: 金额列显示真实下单额度——result.amount 是 bot 实际下单金额
                        # （signal.amount 有 432/622 条为 None，原写法导致历史表大量显示 $0）
                        "amount": res.get("amount") or sig.get("amount", 0) or 0,
                        "reason": sig.get("reason", "")[:60],
                        "ok": res.get("ok", False),
                        "skipped": res.get("skipped", False),
                        "order_id": res.get("order_id", ""),
                        # 9/1: paper 单标记——模拟单不是真实成交，UI 单独显示 📝、不进成交口径
                        "paper": str(res.get("order_id", "")).startswith("PAPER_"),
                        "result": "PENDING",
                    }
                    if trade["skipped"]:
                        trade["result"] = "SKIP"
                    elif not trade["ok"]:
                        trade["result"] = "ERROR"
                    trades.append(trade)
                except Exception as e:  # B6: 坏行留痕(节流)，不再静默——此前 11 行历史数据因此丢失
                    _log_throttled("done_badline", f"⚠️ done.jsonl 坏行跳过: {str(e)[:50]} | {line[:80]}")

        # 8/9 方案B: 优先读 hermes_results.jsonl（review 结算写入的实际结果），
        # 避免依赖 GAMMA（已清理旧 BTC 1H slug）。未在 results 里的才走 GAMMA/CLOB 兜底。
        results_map = {}
        results_file = DATA_DIR / "hermes_results.jsonl"
        if results_file.exists():
            try:
                with open(results_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            r = json.loads(line)
                            if r.get("slug") and r.get("actual"):
                                results_map[r["slug"]] = r
                        except Exception as e:  # B6: 坏行留痕
                            _log_throttled("results_badline", f"⚠️ results.jsonl 坏行跳过: {str(e)[:50]}")
            except Exception as e:  # B6: 文件级失败留痕
                _log_throttled("results_readfail", f"⚠️ results.jsonl 读取失败: {str(e)[:60]}", "E")

        # 8/19: 成交状态映射（bot 终态记录 + 回填脚本补的历史）。
        # order_id → filled: True/False/None(未知)。用于成交/未成交分开统计。
        fills_map = {}
        fills_file = DATA_DIR / "hermes_fills.jsonl"
        if fills_file.exists():
            try:
                with open(fills_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            r = json.loads(line)
                            if r.get("order_id"):
                                fills_map[r["order_id"]] = r.get("filled")
                        except Exception as e:  # B6: 坏行留痕
                            _log_throttled("fills_badline", f"⚠️ fills.jsonl 坏行跳过: {str(e)[:50]}")
            except Exception as e:  # B6: 文件级失败留痕
                _log_throttled("fills_readfail", f"⚠️ fills.jsonl 读取失败: {str(e)[:60]}", "E")

        wins = losses = skipped = 0
        f_wins = f_losses = 0          # 成交口径
        f_pnl = 0.0
        u_wins = u_losses = 0          # 未成交口径（分析对错）
        fetches = 0
        MAX_FETCHES_PER_REQ = 8  # 8/17 调优：3→8 让单次请求能消化更多 PENDING（review 补数据后 dashboard
                                  # 仍需兜底 GAMMA 拉已结算的旧 slug）。GAMMA 8s 超时，8 个串行最坏 64s，
                                  # 仍小于前端 2s 轮询 → 兜底慢不阻塞主响应（threaded=True 隔离）
        for t in trades:
            if t["result"] == "SKIP":
                skipped += 1
                continue
            # 成交状态: results 记录自带 filled 优先，否则查 fills_map（历史记录兜底）
            t["filled"] = None
            if t["slug"]:
                _rr_pre = results_map.get(t["slug"])
                if _rr_pre and _rr_pre.get("filled") is not None:
                    t["filled"] = _rr_pre.get("filled")
                elif t.get("order_id"):
                    t["filled"] = fills_map.get(t["order_id"])
            # 优先用 results 结算（review 已确认 actual + pnl）
            rr = results_map.get(t["slug"]) if t["slug"] else None
            if rr:
                actual = rr.get("actual")
                t["actual"] = actual
                t["pnl"] = rr.get("pnl")
                t["buy_price"] = rr.get("buy_price")
                # 9/2: review 记账金额（signal→result→pending→config 回退链）比 done 单点更完整
                if rr.get("amount"):
                    t["amount"] = rr.get("amount")
                if t["side"] == actual:
                    t["result"] = "WIN"; wins += 1
                else:
                    t["result"] = "LOSS"; losses += 1
                # 8/19: 未成交单 pnl=0（review 已置 0；老记录兜底强制清零），不进成交口径盈亏
                # 9/1 修复: 只有 filled=True（真实成交）才进成交口径。原来 else 分支把
                # filled=None（终态丢失❔）和 PAPER 模拟单的 pnl 也计进去，导致
                # 「实际成交战绩」卡片盈亏与 W/L 对不上（UI 成交/未成交混乱的主因之一）
                if t["filled"] is False:
                    t["pnl"] = 0.0
                elif t["filled"] is True and not t.get("paper"):
                    f_pnl += (t["pnl"] or 0)
                if t["filled"] is True and not t.get("paper"):
                    if t["side"] == actual: f_wins += 1
                    else: f_losses += 1
                elif t["filled"] is False:
                    if t["side"] == actual: u_wins += 1
                    else: u_losses += 1
                continue
            if t["slug"]:
                # 8/17 修复: 兜底 GAMMA 路径前必须确认 side 非空——空 side 与 "UP"/"DOWN" 比对 False
                # 会被错判 LOSS（即使实际方向对）。side 已由读取 done.jsonl 时的 fallback 尽量补全；
                # 仍为空说明历史 done.jsonl 缺 direction/signal/side 全部字段，保守保持 PENDING。
                if not t.get("side"):
                    continue
                in_cache = t["slug"] in _market_cache
                if not in_cache and fetches >= MAX_FETCHES_PER_REQ:
                    continue  # 本轮不再发起新请求，留下次刷新结算
                cached = get_market_cached(t["slug"])
                if not in_cache:
                    fetches += 1
                if cached:
                    # 8/14 修复: 时间守卫——未到该小时结算时间，即使盘中实时价>0.9 也不判已结算
                    # （与 hermes_review.get_actual_result 的 endDate 守卫同口径，修复 dashboard
                    # 兜底路径把进行中场误显为 WIN/LOSS 的 bug）。
                    end_utc = _slug_end_utc(t["slug"])
                    if end_utc is not None and time.time() < end_utc:
                        continue  # 未到结算时间，保持 PENDING，留下次刷新结算
                    # 8/9 修复: Polymarket BTC 1H 合约不显式标记 closed(winner 永久 None)，
                    # 实际结算看 outcomePrices 归 1: UP 侧 >0.9 或 <0.1 即已分出胜负。
                    # 9/5 7am 错账同源修复: 未 closed 的收盘前残价可冲 0.9+（7am 场
                    # UP=0.945 最后一分钟反转 DOWN 赢，被误显 WIN）——未 closed 市场
                    # 只有价格严格归 1(>=0.999/<=0.001) 才算分出胜负，其余保持
                    # PENDING 留下次刷新（与 hermes_review.get_actual_result 同口径）。
                    up_p = cached.get("up_p")
                    if cached.get("closed") or (up_p is not None and (up_p >= 0.999 or up_p <= 0.001)):
                        actual = "UP" if (cached.get("closed") and up_p > 0.5) or (up_p is not None and up_p >= 0.999) else "DOWN"
                        t["actual"] = actual
                        # 8/17 修复: 兜底 GAMMA 路径没有 results_map 不会带 pnl/buy_price，
                        # 用 done.jsonl 里的 result.order_price + signal.amount/scale 推算 pnl，
                        # 让前端「盈亏」列能显示金额（不再显示 "−"）
                        order_price = entry.get("result", {}).get("order_price")
                        sig_inner = entry.get("signal", {})
                        # 9/2: 真实下单额度优先 result.amount（signal.amount 大量为 None）
                        amt = entry.get("result", {}).get("amount") or sig_inner.get("amount", 0) or 0
                        scale = sig_inner.get("position_scale", 1.0) or 1.0
                        if order_price is not None and t["side"] == actual:
                            t["result"] = "WIN"; wins += 1
                            t["buy_price"] = order_price
                            t["pnl"] = round((1.0 - order_price) * amt * scale, 4)
                        else:
                            t["result"] = "LOSS"; losses += 1
                            t["buy_price"] = order_price
                            t["pnl"] = round(-order_price * amt * scale, 4) if order_price is not None else None
                        # 8/19: 兜底路径同样处理成交口径——未成交单 pnl=0、不进成交口径盈亏
                        # 9/1 修复: 与主路径同口径，只有 filled=True 且非 paper 才进成交口径
                        if t["filled"] is False:
                            t["pnl"] = 0.0
                        if t["filled"] is True and not t.get("paper"):
                            f_pnl += (t["pnl"] or 0)
                            if t["result"] == "WIN": f_wins += 1
                            else: f_losses += 1
                        elif t["filled"] is False:
                            if t["result"] == "WIN": u_wins += 1
                            else: u_losses += 1

        resolved = [t for t in trades if t["result"] in ("WIN", "LOSS")]
        total = len(resolved)
        f_total = f_wins + f_losses
        u_total = u_wins + u_losses

        # 9/2 UI审查修复(P1-4): 数据版本指纹——done/results/fills 的 mtime 组合。
        # UI 每 2s 轮询，前端据此前端「数据没变就跳过 638 行表格重建」。
        data_version = _ver

        return jsonify({
            # 9/2 审查修复(⚪1): 去掉 [-50:] 截断——此前 UI 表格只显示最近 50 笔，
            # 与顶部统计卡(623 笔)对不上，用户误以为数据丢失。634 行全量渲染无压力。
            "trades": trades,
            "data_version": data_version,
            "stats": {
                "total": total, "wins": wins, "losses": losses,
                "skipped": skipped,
                "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
            },
            # 8/19 双口径: 实际成交战绩（只算真实成交单）/ 未成交单分析对错
            "stats_filled": {
                "total": f_total, "wins": f_wins, "losses": f_losses,
                "win_rate": round(f_wins / f_total * 100, 1) if f_total > 0 else 0,
                "pnl": round(f_pnl, 2),
            },
            "stats_unfilled": {
                "total": u_total, "wins": u_wins, "losses": u_losses,
                "win_rate": round(u_wins / u_total * 100, 1) if u_total > 0 else 0,
            }
        })
    except Exception as e:
        # B6: 顶层失败留痕(节流)——此前 UI 显示空统计时无从归因
        _log_throttled("api_trades_fail", f"❌ api_trades 整体失败(返回空统计): {str(e)[:80]}", "E")
        return jsonify({"trades": [], "stats": {"total": 0, "wins": 0, "losses": 0, "skipped": 0, "win_rate": 0}})

@app.route("/api/config", methods=["GET", "POST"])
@require_auth
def api_config():
    if request.method == "POST":
        data = request.get_json() or {}
        cfg = load_config()
        updated = {}
        if "bet_amount" in data:
            try:
                bet = float(data["bet_amount"])
                if bet < 0.5 or bet > 50:
                    return jsonify({"ok": False, "error": "bet_amount out of range [0.5, 50]"}), 400
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid bet_amount"}), 400
            cfg["bet_amount"] = bet
            updated["bet_amount"] = bet
        if "tp_enabled" in data:
            tp = data["tp_enabled"]
            if not isinstance(tp, bool):
                return jsonify({"ok": False, "error": "tp_enabled must be boolean"}), 400
            cfg["tp_enabled"] = tp
            updated["tp_enabled"] = tp
        if "analyze_min" in data:
            # round55: BOT 分析下单时刻（ET 整点后第 N 分钟），bot 热读无需重启；写 config + 触发 bot 缓存刷新
            # round55.1: 信号时刻联动——同步改 HermesBTC1HEngine 任务触发分钟，调几点即几点出信号下单
            am_raw = data["analyze_min"]
            if isinstance(am_raw, bool):  # bool 是 int 子类，排除避免 true→1 静默写入（与 _analyze_at_min 同口径）
                return jsonify({"ok": False, "error": "analyze_min must be an integer, not boolean"}), 400
            try:
                am = int(am_raw)
                if am < 0 or am > 55:
                    return jsonify({"ok": False, "error": "analyze_min out of range [0, 55]"}), 400
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid analyze_min"}), 400
            ok_sync, err_sync = _sync_engine_schedule(am)
            if not ok_sync:
                # 联动失败则不写 config，保持 config 与任务时刻一致（避免 UI 显示 :25 而任务仍 :40 的假生效）
                return jsonify({"ok": False,
                                "error": f"引擎信号任务联动失败，config 未写入: {err_sync}"}), 500
            ok_ver, err_ver = _verify_engine_schedule(am)
            if not ok_ver:
                # 复核不过（Next Run 分钟位与目标不符，改任务静默失效）同样不写 config
                return jsonify({"ok": False,
                                "error": f"引擎信号任务复核未通过，config 未写入: {err_ver}"}), 500
            cfg["analyze_min"] = am
            updated["analyze_min"] = am
        if not updated:
            return jsonify({"ok": False, "error": "no supported fields"}), 400
        if not save_config(cfg):  # 9/2 A2: 读取失败守卫拦截，防止 defaults 抹掉整份配置
            return jsonify({"ok": False, "error": "config 读取异常，写回已拦截（防抹除），请检查 hermes_config.json"}), 500
        updated["ok"] = True
        return jsonify(updated)
    return jsonify(load_config())

@app.route("/api/mode", methods=["GET", "POST"])
@require_auth
def api_mode():
    if request.method == "POST":
        data = request.get_json()
        if data and "mode" in data and data["mode"] in ("paper", "live"):
            cfg = load_config()
            cfg["mode"] = data["mode"]
            if not save_config(cfg):  # 9/2 A2: 读取失败守卫拦截，防止 defaults 抹掉整份配置
                return jsonify({"ok": False, "error": "config 读取异常，写回已拦截（防抹除），请检查 hermes_config.json"}), 500
            # 8/19 重构: bot 启动时自动读 hermes_config.json 的 mode 决定 live/paper，
            # 因此只需写 config + 重启即可生效。9/2: 重启统一走 _restart_bot()（Windows 版）。
            ok, _method, err = _restart_bot()
            if not ok:
                return jsonify({"ok": False,
                                "error": f"config 已写入但重启失败: {err}"}), 500
            return jsonify({"ok": True, "mode": data["mode"]})
        return jsonify({"ok": False, "error": "invalid mode"}), 400
    return jsonify({"mode": load_config().get("mode", "paper")})

@app.route("/api/pause", methods=["GET", "POST"])
@require_auth
def api_pause():
    """round41.8.7(2026-08-25 用户决策「暴雷止血」): 全局暂停实盘下单开关（UI 按钮）。
    POST {"paused": bool} → 写 hermes_config.json 的 trading_paused。
    bot 每次 process_signal 现读该值（hermes_1h_bot._trading_pause_active），无需重启即时生效。
    暂停期间：分析照跑、PM 序列照记、影子账照记；仅 ORDER 不入市、不写 done。"""
    if request.method == "POST":
        data = request.get_json() or {}
        if not isinstance(data.get("paused"), bool):
            return jsonify({"ok": False, "error": "paused must be boolean"}), 400
        cfg = load_config()
        cfg["trading_paused"] = data["paused"]
        if not save_config(cfg):  # 9/2 A2: 读取失败守卫拦截，防止 defaults 抹掉整份配置
            return jsonify({"ok": False, "error": "config 读取异常，写回已拦截（防抹除），请检查 hermes_config.json"}), 500
        return jsonify({"ok": True, "trading_paused": data["paused"]})
    return jsonify({"trading_paused": bool(load_config().get("trading_paused"))})

@app.route("/api/restart", methods=["POST"])
@require_auth
def api_restart():
    """重启 bot 进程（8/19 UI 重启按钮；9/2 迁移 Windows 版）。
    旧 Mac 版用 launchctl/pgrep/pkill——Windows 上全部不存在，按钮已失效。
    Windows 标准方式: 计划任务 HermesBTC1HBot /End 结束 + /Run 拉起
    （任务动作 = scripts/migration/bat/start_bot.bat，bot 启动自读 config mode）。
    PID 锁为 portalocker flock（随进程消亡自动释放），强杀后重启无双实例风险。
    """
    ok, method, err = _restart_bot()
    if ok:
        return jsonify({"ok": True, "method": method})
    return jsonify({"ok": False, "error": err}), 500


BOT_TASK_NAME = "HermesBTC1HBot"


def _bot_running_win():
    """检测 hermes_1h_bot.py 进程是否存活（按命令行匹配，返回 PID 列表）"""
    import subprocess
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
              "Where-Object {$_.CommandLine -match 'hermes_1h_bot'} | "
              "Select-Object -ExpandProperty ProcessId")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=20)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _restart_bot():
    """Windows 版 bot 重启，返回 (ok, method, error)。
    1) schtasks /End 结束任务实例  2) 兜底 taskkill 命令行匹配进程
    3) schtasks /Run 经 start_bot.bat 拉起  4) 轮询验证进程存活"""
    import subprocess
    # 1. 结束计划任务实例
    try:
        subprocess.run(["schtasks", "/End", "/TN", BOT_TASK_NAME],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    time.sleep(2)
    # 2. 兜底: /End 可能漏杀（如非任务启动的实例），按命令行补杀
    #    （dashboard 自身命令行是 dashboard.py，不会被误杀）
    for pid in _bot_running_win():
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    time.sleep(1)
    # 3. 经计划任务拉起（不传参，bot 自读 hermes_config.json 的 mode）
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", BOT_TASK_NAME],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return False, None, f"schtasks /Run 失败: {(r.stderr or r.stdout or '').strip()[:120]}"
    except Exception as e:
        return False, None, f"schtasks /Run 异常: {e}"
    # 4. 验证（bot 冷启动握手可能较慢，最多等 16s）
    for _ in range(8):
        time.sleep(2)
        if _bot_running_win():
            return True, "schtasks", None
    return False, None, "已执行重启命令但 16s 内未检测到 bot 进程，请查 data/hermes_bot.log"

@app.route("/api/position")
def api_position():
    """返回当前持仓：最新一笔订单"""
    result = {"has_position": False, "position": None}
    if not DONE_FILE.exists():
        return jsonify(result)
    try:
        with open(DONE_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return jsonify(result)
        # 9/3 修复：只显示「当前小时」的持仓。旧小时残留订单(lines[-1])会被误判为当前持仓，
        # 与本小时尚未下单冲突。从后往前找 slug==当前小时的第一笔，找不到则视为无持仓。
        current_slug = build_slug(now_et())
        entry = None
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("signal", {}).get("slug") == current_slug:
                entry = e
                break
        if entry is None:
            return jsonify(result)
        sig = entry.get("signal", {})
        res = entry.get("result", {})
        slug = sig.get("slug", "")
        side = sig.get("side", "")
        # 金额/价格回退：signal.amount 可能为空，实际下单金额在 result.amount；
        # 持仓面板显示的 @价格用真实下单价 result.order_price 优先（如追价后 0.87 > 信号封顶 0.60）
        amount = round(float(sig.get("amount") or res.get("amount") or 0), 2)
        max_price = round(float(res.get("order_price") or sig.get("max_price") or 0), 2)
        reason = sig.get("reason", "")
        order_id = res.get("order_id", "")
        skipped = res.get("skipped", False)

        if skipped:
            return jsonify(result)

        settled = False
        actual = None
        if slug:
            cached = get_market_cached(slug)
            if cached and cached["closed"]:
                actual = "UP" if cached["up_p"] > 0.5 else "DOWN"
                settled = True

        result = {
            "has_position": True,
            "position": {
                "side": side,
                "amount": amount,
                "max_price": max_price,
                "slug": slug,
                "reason": reason[:80],
                "order_id": order_id,
                "settled": settled,
                "actual": actual,
                "is_paper": order_id.startswith("PAPER_") if order_id else True,
                "win": (side == actual) if settled else None,
            }
        }
    except Exception: pass
    return jsonify(result)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes BTC Quant Terminal</title>
<style>
  :root{
    --bg:#05050e; --panel:#0a0a1a; --line:#141432;
    --cyan:#00e5ff; --green:#16c784; --red:#ff3b5c; --yellow:#f0b90b;
    --orange:#ff9f43; --text:#c8d0e0; --dim:#555a78;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:'SF Mono','Cascadia Code',Consolas,monospace;overflow-x:hidden}
  ::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-thumb{background:var(--line)}

  /* BOOT 开机序列（sessionStorage 每会话只播一次） */
  #fx-boot{position:fixed;inset:0;z-index:10000;background:var(--bg);display:flex;flex-direction:column;
    justify-content:center;align-items:flex-start;padding:0 18vw;font-size:13px;color:var(--cyan);
    text-shadow:0 0 8px rgba(0,229,255,.7)}
  #fx-boot.done{display:none}
  #fx-boot .ok::before{content:'[ ';color:var(--dim)}#fx-boot .ok::after{content:' ]';color:var(--dim)}

  #fx-particles{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.5}
  #fx-crt{position:fixed;inset:0;z-index:9999;pointer-events:none;
    background:repeating-linear-gradient(0deg,rgba(0,0,0,.10) 0 1px,transparent 1px 3px);animation:flick 5s infinite}
  @keyframes flick{0%,100%{opacity:1}93%{opacity:.86}94%{opacity:.97}}
  #fx-crt::after{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at center,transparent 58%,rgba(0,0,5,.42))}

  .wrap{position:relative;z-index:1;max-width:1600px;margin:0 auto;padding:10px}

  /* 顶栏 */
  .topbar{display:flex;align-items:center;gap:12px;padding:8px 12px;border:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
  .brand{color:var(--cyan);font-weight:800;letter-spacing:1px;font-size:14px;text-shadow:0 0 10px rgba(0,229,255,.8)}
  .brand small{color:var(--dim);font-weight:400}
  .params{display:flex;gap:6px;flex-wrap:wrap;flex:1}
  .param{font-size:9px;color:var(--dim);border:1px solid var(--line);padding:2px 7px;white-space:nowrap}
  .param b{color:var(--cyan);font-weight:400}
  .clock{color:var(--red);font-size:14px;font-weight:700;text-shadow:0 0 8px rgba(255,59,92,.6)}
  .px{color:var(--green);font-size:13px;font-weight:700;text-shadow:0 0 8px rgba(22,199,132,.5)}
  .status-dot{display:inline-block;width:8px;height:8px;border-radius:50%}
  .status-dot.online{background:var(--green);box-shadow:0 0 8px rgba(22,199,132,.8)}
  .status-dot.offline{background:var(--red);box-shadow:0 0 8px rgba(255,59,92,.8)}
  .pulse{animation:blinkdot 2s infinite}
  @keyframes blinkdot{50%{opacity:.45}}

  .feed{overflow:hidden;border:1px solid var(--line);border-top:none;background:#030309;padding:4px 0;white-space:nowrap}
  .feed-inner{display:inline-block;animation:marq 30s linear infinite;font-size:10px}
  .feed-inner b{color:var(--cyan);font-weight:400}
  .feed-inner .g{color:var(--green)}.feed-inner .r{color:var(--red)}.feed-inner .y{color:var(--yellow)}
  @keyframes marq{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}

  /* 纯 grid 行对齐: 同行面板自动等高 */
  .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:8px;margin-top:8px;align-items:stretch}
  .c2{grid-column:span 2}.c3{grid-column:span 3}.c4{grid-column:span 4}.c5{grid-column:span 5}.c7{grid-column:span 7}.c12{grid-column:span 12}
  @media (max-width:1200px){.c2,.c3,.c4,.c5,.c7{grid-column:span 12}}
  .panel{border:1px solid var(--line);background:rgba(10,10,26,.9);display:flex;flex-direction:column;min-width:0}
  .pt{font-size:9px;letter-spacing:1px;color:var(--cyan);padding:6px 9px;border-bottom:1px solid var(--line);
    text-shadow:0 0 8px rgba(0,229,255,.5);display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
  .pt::before{content:'● ';font-size:7px}
  .pt .tag{color:var(--bg);background:var(--cyan);padding:0 5px;border-radius:2px;font-weight:700}
  .pt .warn{color:var(--yellow);background:none;border:1px solid var(--yellow)}
  .pt .btns{display:flex;gap:4px}
  .pb{padding:9px;flex:1;min-height:0}

  /* PnL */
  .pnl-big{font-size:40px;font-weight:800;color:var(--red);text-shadow:0 0 18px rgba(255,59,92,.6);font-variant-numeric:tabular-nums;letter-spacing:-1px}
  .pnl-big.pos{color:var(--green);text-shadow:0 0 18px rgba(22,199,132,.6)}
  .pnl-sub{display:flex;justify-content:space-between;font-size:9px;color:var(--dim);margin-top:6px;flex-wrap:wrap;gap:4px}
  .pnl-sub b{color:var(--text);font-weight:400}
  .kpi{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
  .kpi>div{border:1px solid var(--line);padding:5px 7px;font-size:9px;color:var(--dim)}
  .kpi b{display:block;font-size:14px;color:var(--cyan);text-shadow:0 0 8px rgba(0,229,255,.55);margin-top:2px;font-weight:700}
  .blink{animation:fup .5s}
  @keyframes fup{0%{color:#fff}}

  /* 控制 */
  .ctl-row{display:flex;gap:6px;align-items:center;margin-top:6px;font-size:10px}
  .ctl-row label{color:var(--dim);font-size:9px;min-width:52px}
  .btn{border:1px solid var(--line);background:#060614;color:var(--cyan);padding:5px 10px;font-size:9px;cursor:pointer;
    font-family:inherit;letter-spacing:1px;transition:all .2s}
  .btn:hover{border-color:var(--cyan);box-shadow:0 0 10px rgba(0,229,255,.35)}
  .btn.warn{color:var(--yellow);border-color:rgba(240,185,11,.4)}
  .btn.danger{color:var(--red);border-color:rgba(255,59,92,.4)}
  .btn.danger:hover{box-shadow:0 0 10px rgba(255,59,92,.4)}
  .btn.mode-active{border-color:var(--cyan);color:var(--cyan);box-shadow:0 0 8px rgba(0,229,255,.3)}
  .bet-input{width:74px;border:1px solid var(--line);background:#060614;color:var(--green);padding:5px 7px;
    font-family:inherit;font-size:11px;font-weight:700}
  .bet-warn{font-size:9px;color:var(--red);margin-top:4px;display:none;text-shadow:0 0 6px rgba(255,59,92,.5)}
  .mode-chip{border:1px solid var(--green);color:var(--green);padding:2px 8px;font-size:9px;letter-spacing:1px;
    text-shadow:0 0 6px rgba(22,199,132,.5);animation:mpulse 2.4s infinite;white-space:nowrap}
  .mode-chip.live{border-color:var(--red);color:var(--red);text-shadow:0 0 6px rgba(255,59,92,.5)}
  .mode-chip.paused{border-color:var(--yellow);color:var(--yellow);text-shadow:0 0 6px rgba(240,185,11,.5)}
  @keyframes mpulse{50%{box-shadow:0 0 10px rgba(22,199,132,.4)}}
  #save-status{font-size:9px;color:var(--dim);min-width:60px}

  /* 12 因子 */
  .vs-head{display:flex;justify-content:space-between;font-size:12px;font-weight:700;margin:2px 0 5px}
  .vs-bar{display:flex;height:15px;border:1px solid var(--line);background:#060614;overflow:hidden}
  .vs-bar .up{background:linear-gradient(90deg,rgba(22,199,132,.25),var(--green));box-shadow:0 0 12px rgba(22,199,132,.5);transition:width 1.2s cubic-bezier(.2,.8,.2,1)}
  .vs-bar .dn{flex:1;background:linear-gradient(90deg,var(--red),rgba(255,59,92,.25))}
  .meta{display:flex;gap:6px;flex-wrap:wrap;margin:7px 0 2px}
  .meta span{font-size:8px;color:var(--dim);border:1px solid var(--line);padding:1px 5px}
  .meta span b{color:var(--cyan);font-weight:400}
  .frow{display:grid;grid-template-columns:118px 1fr 34px;gap:7px;align-items:center;margin-top:6px;font-size:9px;
    opacity:0;transform:translateX(-8px);transition:all .4s ease-out}
  .frow.show{opacity:1;transform:none}
  .frow .fn{color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .frow .fn i{font-style:normal;color:var(--text)}
  .fbar2{position:relative;height:9px;background:#060614;border:1px solid var(--line)}
  .fbar2 em{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--dim);opacity:.6}
  .fbar2 .pos,.fbar2 .neg{position:absolute;top:0;bottom:0;width:0;transition:width .9s cubic-bezier(.2,.8,.2,1)}
  .fbar2 .pos{left:50%;background:linear-gradient(90deg,rgba(22,199,132,.3),var(--green));box-shadow:0 0 7px rgba(22,199,132,.5)}
  .fbar2 .neg{right:50%;background:linear-gradient(270deg,rgba(255,59,92,.3),var(--red));box-shadow:0 0 7px rgba(255,59,92,.45)}
  .frow .fv{text-align:right;font-variant-numeric:tabular-nums}
  .fv.pos{color:var(--green);text-shadow:0 0 6px rgba(22,199,132,.5)}
  .fv.neg{color:var(--red);text-shadow:0 0 6px rgba(255,59,92,.5)}
  .fv.zero{color:var(--dim)}
  .verdict{margin-top:9px;padding:6px 8px;border:1px dashed var(--line);font-size:10px;color:var(--dim);line-height:1.6}
  .verdict b{color:var(--yellow);font-weight:400}
  .verdict .g{color:var(--green)}

  .risk{font-size:9px;color:var(--yellow);line-height:1.7;border-left:2px solid var(--yellow);padding:2px 0 2px 8px;margin-top:5px;
    text-shadow:0 0 6px rgba(240,185,11,.3);animation:rwarn 2.6s infinite}
  @keyframes rwarn{50%{opacity:.65}}

  /* 信号卡 */
  .sig-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
  .sig-cell{border:1px solid var(--line);padding:6px 8px;font-size:9px;color:var(--dim)}
  .sig-cell b{display:block;font-size:15px;color:var(--cyan);text-shadow:0 0 8px rgba(0,229,255,.5);margin-top:2px;font-weight:700}
  .sig-cell b.g{color:var(--green);text-shadow:0 0 8px rgba(22,199,132,.5)}
  .sig-cell b.r{color:var(--red);text-shadow:0 0 8px rgba(255,59,92,.5)}
  .sig-cell b.y{color:var(--yellow);text-shadow:0 0 8px rgba(240,185,11,.5)}
  .sig-reason{margin-top:7px;font-size:9px;color:var(--dim);line-height:1.6;border-top:1px dashed var(--line);padding-top:6px;
    overflow:hidden;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical}

  /* 市场脉搏/情景 */
  .pulse-row{display:flex;justify-content:space-between;font-size:10px;padding:5px 2px;border-bottom:1px dashed rgba(20,20,50,.8);gap:8px}
  .pulse-row:last-child{border:none}
  .pulse-row .k{color:var(--dim)}
  .pulse-row .k small{opacity:.6}
  .pulse-row .v{color:var(--cyan);text-shadow:0 0 6px rgba(0,229,255,.4);text-align:right}
  .pulse-row .v.g{color:var(--green)}.pulse-row .v.r{color:var(--red)}.pulse-row .v.y{color:var(--yellow)}

  /* 球/图/频谱 */
  #sphere-box{position:relative;height:330px;background:radial-gradient(circle at 50% 45%,rgba(0,229,255,.05),transparent 60%)}
  #sphere{width:100%;height:100%;display:block}
  .cap{position:absolute;bottom:5px;left:0;right:0;text-align:center;font-size:8px;color:var(--dim);letter-spacing:1px}
  #chart{width:100%;height:150px;display:block}
  #chartProb{width:100%;height:64px;display:block}
  #spec{width:100%;height:54px;display:block}

  /* 钱包/持仓 */
  .wal{display:flex;justify-content:space-between;font-size:10px;padding:5px 2px;border-bottom:1px dashed rgba(20,20,50,.8)}
  .wal:last-child{border:none}
  .wal .k{color:var(--dim)}.wal .v{color:var(--green);text-shadow:0 0 6px rgba(22,199,132,.4)}
  .wal .v.c{color:var(--cyan)}

  /* 热力 */
  .heat{display:grid;grid-template-columns:repeat(9,1fr);gap:3px}
  .hcell{aspect-ratio:1;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:8px;border-radius:2px;
    color:#02020a;font-weight:700;animation:pop .4s ease-out backwards}
  .hcell small{font-size:6.5px;opacity:.75;font-weight:400}
  @keyframes pop{0%{transform:scale(.3);opacity:0}100%{}}
  .hw{background:var(--green);box-shadow:0 0 6px rgba(22,199,132,.45)}
  .hl{background:var(--red);box-shadow:0 0 6px rgba(255,59,92,.4)}
  .hp{background:var(--yellow);box-shadow:0 0 6px rgba(240,185,11,.4)}
  .hleg{display:flex;gap:10px;font-size:8px;color:var(--dim);margin-top:7px;flex-wrap:wrap}
  .hleg i{display:inline-block;width:7px;height:7px;border-radius:1px;margin-right:3px}

  /* 决策链 */
  .chain{display:flex;align-items:center;padding:12px 6px 6px}
  .node{text-align:center;position:relative;z-index:1;flex:1}
  .nc{width:42px;height:42px;border-radius:50%;border:2px solid var(--line);display:flex;align-items:center;justify-content:center;
    font-size:8px;color:var(--dim);background:#060614;margin:0 auto 4px;transition:all .5s}
  .node.on .nc{border-color:var(--green);color:var(--green);box-shadow:0 0 14px rgba(22,199,132,.55)}
  .node.cur .nc{border-color:var(--yellow);color:var(--yellow);box-shadow:0 0 18px rgba(240,185,11,.7);animation:npulse 1.1s infinite}
  @keyframes npulse{50%{transform:scale(1.1)}}
  .nl{width:12px;height:2px;background:var(--line);position:relative;overflow:hidden;margin-bottom:20px;flex-shrink:0}
  .nl i{position:absolute;inset:0;background:var(--green);transform:scaleX(0);transform-origin:left;transition:transform .7s}
  .nl.on i{transform:scaleX(1)}

  /* 历史表 */
  .tbl{width:100%;border-collapse:collapse;font-size:9px}
  .tbl th{color:var(--dim);text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);font-weight:400;letter-spacing:1px;position:sticky;top:0;background:var(--panel)}
  .tbl td{padding:4px 6px;border-bottom:1px solid rgba(20,20,50,.55);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px}
  .tbl .win{color:var(--green)}.tbl .loss{color:var(--red)}.tbl .pend{color:var(--yellow)}.tbl .skip{color:var(--dim)}
  .tbl-wrap{overflow-y:auto;max-height:262px}

  /* 日志 */
  .log{font-size:9px;line-height:1.75;overflow-y:auto;max-height:262px}
  .log .lw{color:var(--yellow)}.log .le{color:var(--red)}.log .li{color:var(--dim)}
  .log .lt{color:#3a3f60;margin-right:6px}

  .foot{margin-top:8px;border:1px solid var(--line);background:var(--panel);padding:5px 12px;font-size:9px;color:var(--dim);
    display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px}
  .foot b{color:var(--cyan);font-weight:400}
  @media (prefers-reduced-motion:reduce){*,#fx-crt{animation:none!important}#fx-particles{display:none}}
</style>
</head>
<body>
<div id="fx-boot"></div>
<canvas id="fx-particles"></canvas>
<div id="fx-crt"></div>

<div class="wrap">
  <!-- 顶栏: 品牌 + 引擎参数 + 控制 -->
  <div class="topbar">
    <div class="brand">◉ HERMES <small>// POLYMARKET BTC 1H</small></div>
    <div class="params">
      <span class="param">MODE <b id="p-mode">--</b></span>
      <span class="param">ENGINE <b id="p-eng">--</b></span>
      <span class="param">ANCHOR <b id="p-anchor">--</b></span>
      <span class="param">P(RAW) <b id="p-praw">--</b></span>
      <span class="param">EV <b id="p-ev">--</b></span>
      <span class="param">TIER <b id="p-tier">--</b></span>
    </div>
    <span class="status-dot online pulse" id="conn-dot" title="后端连接正常"></span>
    <span class="mode-chip" id="mode-chip">--</span>
    <button class="btn warn" id="btn-pause" onclick="togglePause()" title="全局暂停实盘下单：分析照跑、影子照记，仅停止入市">⏸ PAUSE</button>
    <button class="btn warn" id="btn-tp" onclick="toggleTP()" title="止盈：结算前≤5min盈利时自动市价卖出">💸 TP</button>
    <button class="btn danger" id="btn-live" onclick="switchMode('live')">🔥 LIVE</button>
    <button class="btn" id="btn-paper" onclick="switchMode('paper')">📝 PAPER</button>
    <button class="btn danger" onclick="restartBot()">⟳ RESTART</button>
    <span id="save-status"></span>
    <div class="px" id="btc">--</div>
    <div class="clock" id="clock">--:--:-- ET</div>
  </div>

  <div class="feed"><div class="feed-inner" id="feed"></div></div>

  <!-- Row1: PNL+控制 | 12因子 | 球 -->
  <div class="grid">
    <div class="panel c3">
      <div class="pt">REAL PNL — 成交口径 <span class="tag">LIVE</span></div>
      <div class="pb">
        <div class="pnl-big" id="pnl">--</div>
        <div class="pnl-sub"><span>FILLS <b id="pnl-fills">-</b></span><span>WIN RATE <b id="pnl-wr">-</b></span><span>AVG <b id="pnl-avg">-</b></span></div>
        <div class="kpi">
          <div>WINDOWS<b id="k-win">-</b></div>
          <div>TRADES 今日(ET)<b id="k-24h">-</b></div>
          <div>FILL RATE<b id="k-fill">-</b></div>
          <div>UNFILLED<b id="k-unfilled">-</b></div>
        </div>
      </div>
    </div>

    <div class="panel c5">
      <div class="pt">ENGINE SCORE — <span id="fcount">--</span> 项机算因子 <span class="tag" id="fside">--</span></div>
      <div class="pb">
        <div class="vs-head">
          <span style="color:var(--green)">🔼 UP <b id="sup">0</b></span>
          <span style="font-size:9px;color:var(--dim)">conf <b style="color:var(--yellow)" id="econf">-</b> · 净分 <b style="color:var(--cyan)" id="enet">-</b></span>
          <span style="color:var(--red)">DOWN <b id="sdn">0</b> 🔽</span>
        </div>
        <div class="vs-bar"><div class="up" id="bup" style="width:0%"></div><div class="dn"></div></div>
        <div class="meta">
          <span>S0 <b id="m-s0">-</b></span><span>PM行 <b id="m-pm">-</b></span><span>AI对照 <b id="m-ai">-</b></span>
          <span>概率 <b id="m-prob">-</b></span><span>EV <b id="m-ev">-</b></span><span>首挂 <b id="m-mp">-</b></span>
        </div>
        <div id="frows"></div>
        <div class="verdict" id="fverdict">⟶ 等待引擎信号 ...</div>
        <div id="bookwarn" style="display:none"></div>
      </div>
    </div>

    <div class="panel c4">
      <div class="pt">NEURAL SHELL — 历史窗口空间 <span class="tag" id="sphere-tag">32W</span></div>
      <div id="sphere-box"><canvas id="sphere"></canvas>
        <div class="cap">NODE = 1H WINDOW · GREEN WIN / RED LOSS / YELLOW PENDING</div></div>
    </div>

    <!-- Row2: 钱包持仓 | 市场脉搏 | 信号卡+决策链 -->
    <div class="panel c3">
      <div class="pt">WALLET — 钱包 · 持仓 <span class="tag" id="wal-mode">--</span></div>
      <div class="pb">
        <div class="wal"><span class="k">USDC 余额</span><span class="v" id="wal-balance">--</span></div>
        <div class="wal"><span class="k">钱包地址</span><span class="v c" id="wal-addr">--</span></div>
        <div class="wal"><span class="k">当前持仓</span><span class="v c" id="wal-pos">--</span></div>
        <div class="wal"><span class="k">持仓状态</span><span class="v" id="wal-pos-state">--</span></div>
        <div class="ctl-row"><label>BET $</label>
          <button class="btn" onclick="adjustBet(-0.5)">−</button>
          <input class="bet-input" id="bet" value="2.00">
          <button class="btn" onclick="adjustBet(0.5)">+</button>
          <button class="btn" onclick="setBet()">SET</button></div>
        <div class="bet-warn" id="bet-warn">⚠ 低于 $1.00 硬约束下限，bot 不会执行交易</div>
        <div class="ctl-row" title="BOT 分析下单时刻（ET 整点后第 N 分钟）。SET 时同步修改引擎信号任务 HermesBTC1HEngine 触发时刻——调几点，引擎就几点出信号、bot 几点下单，信号数据与下单时刻完全对齐，约 1 分钟内生效，无需重启 bot。示例：调 :25 → 引擎每整点 :25 判定信号、bot 立即按此信号处理。">
          <label>ANALYZE</label>
          <select class="bet-input" id="analyze-min">
            <option value="5">:05</option><option value="10">:10</option><option value="15">:15</option>
            <option value="20">:20</option><option value="25">:25</option><option value="30">:30</option>
            <option value="35">:35</option><option value="40" selected>:40</option><option value="45">:45</option>
            <option value="50">:50</option><option value="55">:55</option>
          </select>
          <button class="btn" onclick="setAnalyzeMin()">SET</button></div>
      </div>
    </div>

    <div class="panel c5">
      <div class="pt">MARKET READ — 市场脉搏 · 情景 <span class="tag" id="mp-slug">--</span></div>
      <div class="pb">
        <div id="pulse"></div>
      </div>
    </div>

    <div class="panel c4">
      <div class="pt">SIGNAL — 当前信号 · 决策流</div>
      <div class="pb">
        <div class="sig-grid">
          <div class="sig-cell">方向 / 概率<b id="sg-side">--</b></div>
          <div class="sig-cell">首挂上限<b class="y" id="sg-cap">--</b></div>
          <div class="sig-cell">EV (校准)<b class="g" id="sg-ev">--</b></div>
          <div class="sig-cell">下单金额<b id="sg-amt">--</b></div>
        </div>
        <div class="chain" id="chain">
          <div class="node"><div class="nc">SIGNAL</div>信号</div>
          <div class="nl"><i></i></div>
          <div class="node"><div class="nc">RISK</div>风控</div>
          <div class="nl"><i></i></div>
          <div class="node"><div class="nc">ORDER</div>下单</div>
          <div class="nl"><i></i></div>
          <div class="node"><div class="nc">CHASE</div>追价</div>
          <div class="nl"><i></i></div>
          <div class="node"><div class="nc">FILL</div>成交</div>
        </div>
        <div class="sig-reason" id="sg-reason"></div>
      </div>
    </div>

    <!-- Row3: 风险 | BTC图表 | 热力 -->
    <div class="panel c3">
      <div class="pt">ENGINE RISKS — 风控提示 <span class="tag warn" id="risk-n">0</span></div>
      <div class="pb" id="risks"></div>
    </div>

    <div class="panel c5">
      <div class="pt">BTC/USD 实时走势 · 锚定线 <span class="tag" id="chart-slug">--</span></div>
      <div class="pb" style="padding:6px">
        <canvas id="chart"></canvas>
        <canvas id="chartProb" style="margin-top:4px"></canvas>
        <div style="font-size:8px;color:var(--dim);letter-spacing:1px;margin:4px 0 2px">
          <span style="color:var(--cyan)">■ BTC</span> · <span style="color:var(--yellow)">ANCHOR</span>（价格，上）
          · <span style="color:var(--green)">■ UP%</span> / <span style="color:var(--red)">■ DOWN%</span>（概率，下）
          — 500ms/点 · 滚动4分钟窗口</div>
        <canvas id="spec"></canvas>
      </div>
    </div>

    <div class="panel c4">
      <div class="pt">RESOLUTION GRID — 近 36 期结算</div>
      <div class="pb">
        <div class="heat" id="heat"></div>
        <div class="hleg">
          <span><i class="hw"></i>WIN</span><span><i class="hl"></i>LOSS</span>
          <span>¢=买价</span><span style="margin-left:auto;opacity:.7">淡格=未成交(方向对)</span>
        </div>
      </div>
    </div>

    <!-- Row4: 历史表 | 日志 -->
    <div class="panel c7">
      <div class="pt">TRADES — 历史交易 <span class="tag" id="tbl-n">0 行</span></div>
      <div class="pb" style="padding:0"><div class="tbl-wrap"><table class="tbl" id="tbl"></table></div></div>
    </div>

    <div class="panel c5">
      <div class="pt">LOGS — 引擎日志
        <span class="btns"><button class="btn" id="log-filter-all" onclick="setLogFilter('')">全部</button>
        <button class="btn warn" id="log-filter-we" onclick="setLogFilter('WE')">⚠W/E</button></span></div>
      <div class="pb" style="padding:6px 9px"><div class="log" id="log"></div></div>
    </div>
  </div>

  <div class="foot">
    <span><b>HERMES QUANT</b> · 真实数据源 /api/signal · /api/analysis · /api/history · /api/status</span>
    <span>策略 <b id="foot-strategy">--</b> · 数据驱动 · 粒子/CRT/BOOT 纯前端效果</span>
  </div>
</div>

<script>
/* ================= 工具 ================= */
// 9/2 UI审查修复(P2-9): HTML 转义——所有来自文件/API 的插值一律过 esc() 防注入
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmtSign(v,unit){return (v>=0?'+':'−')+Math.abs(v).toFixed(2)+(unit||'')}
function rollNum(el,from,to,ms,fmt){
  const t0=performance.now();
  (function f(t){const p=Math.min(1,(t-t0)/ms),e=1-Math.pow(1-p,3);
    el.textContent=fmt(Math.round(from+(to-from)*e));
    if(p<1)requestAnimationFrame(f)})(t0);
}

/* ================= 全局状态 ================= */
const S={status:null,signal:null,analysis:null,position:null,config:null,history:null};
let fetchFailCount=0;
function updateStatusDot(ok){
  const dot=document.getElementById('conn-dot');
  if(!dot) return;
  if(ok){dot.className='status-dot online pulse';dot.title='后端连接正常';}
  else{dot.className='status-dot offline pulse';dot.title='后端连接失败(连续'+fetchFailCount+'次)';}
}
async function fetchAPI(p){
  try{
    const r=await fetch(p,{cache:'no-store'}); // round55.2: 绕过 HTTP 缓存，轮询永远拿实时数据
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j=await r.json();
    fetchFailCount=0;updateStatusDot(true);
    return j;
  }catch(e){
    fetchFailCount++;
    if(fetchFailCount>=3){updateStatusDot(false);console.error('fetchAPI',p,e);}
    return null;
  }
}

/* ================= BOOT 开机序列（sessionStorage 每会话一次） ================= */
(function(){
  const el=document.getElementById('fx-boot');
  if(!el) return;
  if(sessionStorage.getItem('hermes_boot_done')){el.remove();return;}
  sessionStorage.setItem('hermes_boot_done','1');
  const L=[
    'HERMES BIOS v52.4 ...',
    'MEM CHECK 640K OK',
    'LOADING  engine.btc-1h .......... <span class="ok">OK</span>',
    'LOADING  signal.whitelist ....... <span class="ok">OK</span>',
    'LOADING  chase.no-cap ........... <span class="ok">OK</span>',
    'CONNECT clob.polymarket ......... <span class="ok">OK</span>',
    'MODE    reading hermes_config ...',
    'BOOT COMPLETE',
  ];
  let i=0;
  const t=setInterval(()=>{
    if(i<L.length){const d=document.createElement('div');d.innerHTML=L[i++];el.appendChild(d);}
    else{clearInterval(t);setTimeout(()=>{el.classList.add('done');el.remove();},420);}
  },170);
})();

/* ================= 12 因子引擎面板 ================= */
const F_LABELS={price_position:'价格位置',taker_bs:'Taker买卖',recent_dir_5m:'5m方向',recent_dir_15m:'15m方向',
  kline_15m:'15m K线',kline_1h:'1h K线',momentum_burst:'动量爆发',pm_imbalance:'PM挂单失衡',data_completeness:'聪明订单',
   resonance:'强共振', pm_pair:'强配对',
  pm_momentum:'PM价动量',pm_consensus:'PM共识',fake_breakout:'假突破',divergence:'背离',
  reversal_intent:'深跌反转',short_bounce_factor:'短反弹先兆',taker_1m:'贴线1m Taker',wall_divergence:'墙背离'};
let lastEngineVer='';
function renderEngine(x){
  if(!x){  // 空值守卫：无信号时重置面板，禁止触碰 x.*（原来直接 TypeError 崩掉整条渲染链）
    ['fside','fcount','sup','sdn','econf','enet','m-s0','m-pm','m-ai','m-prob','m-ev','m-mp'].forEach(id=>{
      const el=document.getElementById(id); if(el)el.textContent='--';});
    document.getElementById('bup').style.width='0%';
    document.getElementById('frows').innerHTML='';
    document.getElementById('fverdict').textContent='⟶ 等待引擎信号...';
    return;
  }
  const sc=x.scoring_compare||{}; const eng=sc.engine_score||{};
  const ver=sc.timestamp||x.slug||'';
  if(ver===lastEngineVer) return;   // 引擎快照没变不重放动画
  lastEngineVer=ver;
  const up=eng.up||0,down=eng.down||0;
  document.getElementById('fside').textContent=eng.side||(x&&x.side)||'--';
  document.getElementById('bup').style.width=(up*100)+'%';
  rollNum(document.getElementById('sup'),0,Math.round(up*100),900,v=>v+'%');
  rollNum(document.getElementById('sdn'),0,Math.round(down*100),900,v=>v+'%');
  document.getElementById('econf').textContent=eng.confidence!=null?eng.confidence:'-';
  document.getElementById('enet').textContent=(eng.net>0?'+':'')+(eng.net??0);
  document.getElementById('m-s0').textContent=(eng.s0>0?'+':'')+(eng.s0??0);
  document.getElementById('m-pm').textContent=eng.pm_row??0;
  const d=sc.diff||{};
  document.getElementById('m-ai').textContent=sc.ai_score?((d.agree)?'一致 ✓':'分歧 ⚠'):'-';
  document.getElementById('m-prob').textContent=(x.probability??'-')+' (校准)';
  document.getElementById('m-ev').textContent=(x.ev>0?'+':'')+(x.ev??'-');
  document.getElementById('m-mp').textContent='≤ '+(x.max_price??'-');
  const box=document.getElementById('frows'); box.innerHTML='';
  const fc=document.getElementById('fcount'); if(fc)fc.textContent=Object.keys(eng.factors||{}).length||'--';
  // 因子动态渲染：F_LABELS 为中文映射表，引擎新增因子自动追加显示
  const fkeys=Object.keys(F_LABELS);
  Object.keys(eng.factors||{}).forEach(k=>{if(!F_LABELS[k])fkeys.push(k);});
  fkeys.forEach((key,i)=>{
    const val=(eng.factors&&eng.factors[key]!==undefined)?eng.factors[key]:0;
    const label=F_LABELS[key]||key;
    const r=document.createElement('div');r.className='frow';r.title=key+'  值='+val;
    const pw=Math.abs(val)/2*50;
    r.innerHTML='<span class="fn">'+esc(label)+' <i>'+key+'</i></span>'+
      '<span class="fbar2"><em></em>'+(val>0?'<i class="pos"></i>':val<0?'<i class="neg"></i>':'')+'</span>'+
      '<span class="fv '+(val>0?'pos':val<0?'neg':'zero')+'">'+(val>0?'+':'')+val.toFixed(0)+'</span>';
    box.appendChild(r);
    setTimeout(()=>{r.classList.add('show');
      const bar=r.querySelector(val>0?'.pos':'.neg');
      if(bar)setTimeout(()=>bar.style.width=pw+'%',60);
    },80+i*70);
  });
  const confTxt={高:'(高)',中:'(中)',低:'(低)'}[x.confidence]||'';
  document.getElementById('fverdict').innerHTML=
    '⟶ 引擎判定: <b>'+esc(x.side||'--')+' · p='+(x.probability_raw??'-')+'</b> · conf '+esc(x.confidence||'-')+confTxt+
    ' · tier '+esc((d.tier)||'-')+' · 引擎AI'+((d.agree)?'一致✓':'分歧⚠')+'<br>'+
    '⟶ EV <span class="g">'+(x.ev>0?'+':'')+(x.ev??'-')+'/百$</span> · 下单 $'+(x.amount??'-')+
    ' (scale '+(x.position_scale??1)+') · 首挂 taker@ask 封顶 '+(x.max_price??'-')+
    ' · '+esc((x.reason||'').split(' | ')[0].replace(/^\[engine\]\s*/,''));
}

/* ================= 盘口陷阱预警（常亮口径：原始盘口读数比方向，纯展示，不碰交易） ================= */
/* 9/3 用户拍板放宽为常亮：不再依赖引擎阈值因子(±1)，直接用 market_data 的原始盘口——
   快=book_imbalance(盘口失衡)，慢=smart_money net_up_imbalance(聪明钱，缺则 book_up_mid-0.5 兜底)，
   引擎方向=scoring_compare.engine_score.side。 任一背离即亮。belang 不参与决策。 */
function renderBookWarn(x, an){
  const el=document.getElementById('bookwarn');
  if(!el)return;
  const eng=(x&&x.scoring_compare&&x.scoring_compare.engine_score)||{};
  const side=eng.side||(x&&x.side)||'';
  const a=((an||{}).has_analysis&&(an||{}).analysis)||{};
  const fast=a['raw_book_imb'];
  const smart=a['raw_smart_imb'];
  const upmid=a['raw_book_up_mid'];
  const sgn=v=> v>0?1:(v<0?-1:0);
  const dir=(v)=> v>0?'偏UP':(v<0?'偏DOWN':'中立');
  // 慢方向：优先聪明钱净失衡；缺失时用盘口绝对价共识 book_up_mid-0.5 兜底
  let slow=null;
  if(typeof smart==='number'){ slow=smart; }
  else if(typeof upmid==='number'){ slow=upmid-0.5; }
  const sF=sgn(fast);
  const slowSgn=(typeof slow==='number')?sgn(slow):0;
  const sideSgn=(side==='UP'?1:side==='DOWN'?-1:0);
  const items=[];
  // 预警1 快慢打架：盘口失衡(快) 方向 ≠ 聪明钱共识(慢) 方向
  if(sF!==0&&slowSgn!==0&&sF!==slowSgn){
    items.push('⚡ 快慢打架：盘口失衡 <b>'+dir(fast)+'</b>('+fast+
      ')，聪明钱共识 <b>'+dir((typeof slow==='number')?slow:0)+
      '</b>('+((typeof slow==='number')?slow:'—')+
      ')——庄家可能正在盘口反向埋伏');
  }
  // 预警2 盘口反着引擎：盘口失衡(快) 方向 ≠ 引擎方向
  if(sF!==0&&sideSgn!==0&&sF!==sideSgn){
    items.push('⚠ 盘口反着引擎：盘口失衡 <b>'+dir(fast)+'</b>('+fast+
      ')，但引擎判 <b>'+side+'</b>——你的单方向和盘口的力相反，留意反杀/回补');
  }
  if(items.length){
    el.style.display='block';
    el.style.border='1px solid #EFAA17';
    el.style.borderLeft='4px solid #EFAA17';
    el.style.background='rgba(239,170,23,.08)';
    el.style.borderRadius='8px';
    el.style.padding='8px 10px';
    el.style.fontSize='13px';
    el.style.lineHeight='20px';
    el.style.marginTop='8px';
    el.innerHTML='<div style="margin-bottom:4px;font-weight:600">🥷 盘口陷阱预警</div>'+
      items.map(t=>'<div style="margin-bottom:4px">'+t+'</div>').join('')+
      '<div style="font-size:12px;opacity:.7">仅提示 · 不参与决策</div>';
  }else{
    el.style.display='none';
  }
}

/* ================= 市场脉搏（从信号 assumptions 解析） ================= */
function renderPulse(x,an){
  const el=document.getElementById('pulse');
  if(!x){el.innerHTML='<div class="pulse-row"><span class="k">等待信号...</span></div>';return;}
  const asm=(x.scoring_compare&&x.scoring_compare.assumptions)||[];
  const rows=[];
  const find=re=>{const m=asm.find(s=>re.test(s));return m||''};
  const push=(k,v,cls,note)=>rows.push('<div class="pulse-row"><span class="k">'+k+(note?' <small>· '+note+'</small>':'')+
    '</span><span class="v '+(cls||'')+'">'+v+'</span></div>');
  const mm=(x.reason||'').match(/mode=([^\s|]+)/);
  if(mm)push('引擎模式',esc(mm[1]),'y','');
  const t=find(/买占比/);const tv=t.match(/买占比=([\d.]+%?)/);
  if(tv)push('1H Taker B/S 买占比',esc(tv[1]),'y','仅参考不计分');
  const mc=find(/MACD hist/);const mv=mc.match(/: ([+-]?[\d.]+)/);
  if(mv)push('15m MACD hist',esc(mv[1]),parseFloat(mv[1])>=0?'g':'r','仅参考不计分');
  const im=find(/imb=/);const iv=im.match(/imb=([+-]?[\d.]+)/);
  if(iv)push('PM 挂单失衡 imb',esc(iv[1]),parseFloat(iv[1])>=0?'g':'r',esc((im.match(/->\s*(.+)$/)||[])[1]||''));
  const mo=find(/Δ=/);const mov=mo.match(/Δ=([+-]?[\d.]+)/);
  if(mov)push('PM 价动量 Δ',esc(mov[1]),parseFloat(mov[1])>=0?'g':'r',
    asm.some(s=>s.includes('动量掉头'))?'⚠动量掉头':'触发 → ±2');
  if(x.pm_up_price!=null)push('pm_up_price (gamma)','$'+x.pm_up_price,'','翻向基准回填');
  const vo=find(/波动率/);const vv=vo.match(/波动率([\d.]+)/);
  if(vv)push('波动率',esc(vv[1])+'%',vo.includes('贴线禁用')?'y':'g',vo.includes('贴线禁用')?'>0.3% → 贴线禁用':'');
  if(an&&an.timeframe_consensus)push('共振状态',esc(an.timeframe_consensus),'','');
  if(!rows.length){asm.slice(0,8).forEach(s=>push('',esc(s),'',''));}
  el.innerHTML=rows.join('');
}

/* ================= 信号卡 + 决策链 ================= */
function renderSignal(x,res,pos){
  if(!x){
    ['sg-side','sg-cap','sg-ev','sg-amt'].forEach(id=>document.getElementById(id).textContent='--');
    document.getElementById('sg-reason').textContent='';
    document.querySelectorAll('#chain .node,#chain .nl').forEach(n=>n.classList.remove('on','cur'));
    return;
  }
  const sideEl=document.getElementById('sg-side');
  sideEl.textContent=x.side+' · '+x.probability;
  sideEl.className=x.side==='UP'?'g':'r';
  document.getElementById('sg-cap').textContent='≤ '+x.max_price;
  document.getElementById('sg-ev').textContent=(x.ev>0?'+':'')+(x.ev??'-')+'/百$';
  document.getElementById('sg-amt').textContent='$'+(x.amount??'-')+' · scale '+(x.position_scale??1);
  document.getElementById('sg-reason').textContent='📝 '+((x.reason||'').slice(0,220));
  const nodes=document.querySelectorAll('#chain .node'),links=document.querySelectorAll('#chain .nl');
  const hasOrder=res&&res.order_id;
  // LIVE taker 路径的 result 只有 status:'matched'（无 fill_status/paper_instant），
  // 不纳入判定会导致已成交单一直显示"追价中"（9/3 12pm/1pm LIVE 单实测复现）
  const filled=hasOrder&&(res.paper_instant===true||res.fill_status==='FILLED'||res.status==='matched');
  const settled=pos&&pos.has_position&&pos.position&&pos.position.settled;
  const st=[!!x,!!(x.scoring_compare&&x.scoring_compare.engine_risks&&x.scoring_compare.engine_risks.length),
    !!hasOrder,!!hasOrder&&!filled&&!settled,!!(filled||settled)];
  st.forEach((on,i)=>{if(nodes[i]){nodes[i].classList.toggle('on',on);nodes[i].classList.toggle('cur',i===3&&on);}});
  links.forEach((l,i)=>{if(l)l.classList.toggle('on',i<4&&st[i]);});
}

/* ================= 风控提示 ================= */
function renderRisks(x,an){
  const rs=(x&&x.scoring_compare&&x.scoring_compare.engine_risks)||(an&&an.risks)||[];
  document.getElementById('risk-n').textContent=rs.length;
  document.getElementById('risks').innerHTML=rs.map((r,i)=>
    '<div class="risk" style="animation-delay:'+(i*.4)+'s">'+esc(r)+'</div>').join('')||
    '<div class="pulse-row"><span class="k">无风险提示</span></div>';
}

/* ================= PnL 面板 ================= */
let lastPnl=null;
function renderPnl(h){
  if(!h)return;
  const sf=h.stats_filled||{total:0,wins:0,losses:0,win_rate:0,pnl:0};
  const pnl=sf.pnl||0;
  const el=document.getElementById('pnl');
  el.textContent=fmtSign(pnl).replace('+','+$');
  el.className='pnl-big'+(pnl>=0?' pos':'');
  if(lastPnl!==null&&pnl!==lastPnl){el.classList.remove('blink');void el.offsetWidth;el.classList.add('blink');}
  lastPnl=pnl;
  document.getElementById('pnl-fills').textContent=sf.total;
  document.getElementById('pnl-wr').textContent=sf.win_rate+'%';
  document.getElementById('pnl-avg').textContent=sf.total?fmtSign(pnl/sf.total):'-';
  const trades=h.trades||[];
  document.getElementById('k-win').textContent=trades.length;
  // 今日(ET)交易数: slug 含 "month-day-year"
  let etDate='';
  try{etDate=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'long',day:'numeric',year:'numeric'})
    .format(new Date()).toLowerCase().replace(/, /,'-').replace(/ /,'-');}catch(e){}
  document.getElementById('k-24h').textContent=trades.filter(t=>(t.slug||'').includes(etDate)).length;
  const unfilled=(h.stats_unfilled&&h.stats_unfilled.total)||0;
  const denom=sf.total+unfilled;
  document.getElementById('k-fill').textContent=denom?Math.round(sf.total/denom*100)+'%':'-';
  document.getElementById('k-unfilled').textContent=unfilled+' PEND';
}

/* ================= 钱包/持仓 ================= */
function renderWallet(){
  const w=S.wallet;
  document.getElementById('wal-balance').textContent=w&&w.wallet_balance!=null?'$'+w.wallet_balance.toFixed(2):'--';
  document.getElementById('wal-addr').textContent=w&&w.wallet_address?w.wallet_address.slice(0,10)+'...':'--';
  const p=S.position&&S.position.has_position&&S.position.position;
  document.getElementById('wal-pos').textContent=p?(p.side+' $'+Number(p.amount||0).toFixed(2)+' @'+Number(p.max_price||0).toFixed(2)+(p.is_paper?' 📝':' 🔴')):'无';
  const st=document.getElementById('wal-pos-state');
  if(p&&p.settled){st.textContent=p.win?'WIN ✅':'LOSS ❌';st.className='v';st.style.color=p.win?'var(--green)':'var(--red)';}
  else if(p){st.textContent=p.is_paper?'持有中 (模拟)':'持有中 (实盘)';st.className='v c';st.style.color='';}  // 清掉上一次 WIN/LOSS 残留色
  else{st.textContent='--';st.className='v';}
  const c=S.config||{};
  document.getElementById('wal-mode').textContent=(c.mode||'paper').toUpperCase();
  document.getElementById('p-mode').textContent=(c.mode||'paper').toUpperCase();
}

/* ================= 顶栏 / 时钟 / BTC ================= */
function renderTopbar(s,x,c){
  if(s){
    document.getElementById('p-anchor').textContent=s.anchor_price?'$'+Number(s.anchor_price).toLocaleString('en-US'):'--';
    const el=document.getElementById('btc');
    if(s.btc_price!=null){
      const prev=el.dataset.v?parseFloat(el.dataset.v):null;
      el.textContent='$'+s.btc_price.toLocaleString('en-US',{maximumFractionDigits:1});
      if(prev!==null&&s.btc_price!==prev){el.classList.remove('blink');void el.offsetWidth;el.classList.add('blink');}
      el.style.color=(prev===null||s.btc_price>=prev)?'var(--green)':'var(--red)';
      el.dataset.v=s.btc_price;
    }
    document.getElementById('chart-slug').textContent=(s.contract_slug||'').replace('bitcoin-up-or-down-','').replace('-et','').toUpperCase();
  }
  if(x){
    document.getElementById('p-eng').textContent=x.strategy_version||'--';
    document.getElementById('p-praw').textContent=x.probability_raw??'--';
    document.getElementById('p-ev').textContent=(x.ev>0?'+':'')+(x.ev??'--');
    document.getElementById('p-tier').textContent=(x.scoring_compare&&x.scoring_compare.diff&&x.scoring_compare.diff.tier)||'-';
    document.getElementById('mp-slug').textContent=(x.slug||'').replace('bitcoin-up-or-down-','').replace('-et','').toUpperCase();
    document.getElementById('foot-strategy').textContent=x.strategy_version||'--';
  }
  if(c){
    const chip=document.getElementById('mode-chip');
    const paused=c.trading_paused===true, mode=c.mode||'paper';
    chip.classList.remove('live','paused');
    if(paused){chip.classList.add('paused');chip.textContent='⏸ PAUSED';}
    else if(mode==='live'){chip.classList.add('live');chip.textContent='🔴 LIVE';}
    else{chip.textContent='📝 PAPER';}
    document.getElementById('btn-paper').classList.toggle('mode-active',mode==='paper'&&!paused);
    document.getElementById('btn-live').classList.toggle('mode-active',mode==='live');
  }
}
setInterval(()=>{const el=document.getElementById('clock');
  try{el.textContent=new Date().toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'})+' ET';}
  catch(e){el.textContent=new Date().toLocaleTimeString('en-US',{hour12:false});}
},1000);

/* ================= 滚动 FEED（来自引擎日志） ================= */
let lastFeedSig='';
function renderFeed(){
  const sig=logsCache.map(x=>x.time+x.level+x.msg).join('|');
  if(sig===lastFeedSig)return;   // 内容没变不重建（重建会重启 CSS 跑马灯动画）
  lastFeedSig=sig;
  const items=logsCache.slice(-9).reverse().map(x=>{
    const lv=(x.level||'I').toUpperCase();
    const cls=lv==='W'?'y':lv==='E'?'r':'g';
    return '<b>['+lv+']</b> <span class="'+cls+'">'+esc(x.msg)+'</span>';
  });
  if(!items.length)return;
  const half=items.map(s=>'<span style="margin-right:34px">'+s+'</span>').join('');
  document.getElementById('feed').innerHTML=half+half;
}

/* ================= BTC 图表（滚动窗口：2s 实时采样 + 锚定线）
   9/3 二次修复：整小时 X 轴下 2s 只推进 ~0.24px，肉眼视为静止（用户两次反馈"不走"）。
   改回原版滚动窗口节奏：X 轴映射「最近 4 分钟」，2s 推进 ~3.5px，曲线持续左移延伸 */
const MAX_POINTS=1800;
let priceHistory=[];
let seedTs=0;
async function seedChart(){
  const h=await fetchAPI('/api/price_history');
  if(!h||!h.points||!h.points.length)return;
  const rows=h.points.map(p=>({
    t:p.t,btc:p.btc,anchor:h.anchor||null,
    time:new Date(p.t*1000).toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'})
  }));  // 9/3: 概率线改纯实时 2s 采样，回填只铺 BTC 价格（不再合并 5min 概率快照，避免与价格线数据密度不对称）
  // 保留比回填更新的 live 采样点
  const live=priceHistory.filter(p=>p.t&&p.t>seedTs);
  priceHistory=rows.concat(live);
  seedTs=h.points[h.points.length-1].t;
  drawChart();
}
function initChart(){
  const dpr=window.devicePixelRatio||1;
  // 9/3: 拆成上下两图——价格图(chart) + 概率图(chartProb)，共享 X 轴各用各的刻度
  [['chart',150],['chartProb',64]].forEach(([id,H])=>{
    const canvas=document.getElementById(id);
    if(!canvas)return;
    const rect=canvas.getBoundingClientRect();
    if(rect.width===0)return;
    canvas.width=rect.width*dpr;canvas.height=H*dpr;
    const ctx=canvas.getContext('2d');
    ctx.scale(dpr,dpr);
    canvas.ctx=ctx;canvas.w=rect.width;canvas.h=H;
  });
}
/* 9/3: 滚动窗口 X 轴 —— 两图共享同一时间轴 + 最近4分钟窗口 */
function chartWindow(){
  const nowS=Date.now()/1000;
  const SPAN=240;
  return {nowS,SPAN,hist:priceHistory.filter(p=>p.t>=nowS-SPAN-2)};
}

function drawChart(){ drawPriceChart(); drawProbChart(); }

/* 9/3: X 轴时间刻度（两图共享同一个滚动窗口，画首/中/尾 3 个时刻） */
function timeLabel(ts){
  const d=new Date(ts*1000);
  const p=n=>String(n).padStart(2,'0');
  return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
}
function drawTimeAxis(ctx,nowS,SPAN,pad,pw,h){
  ctx.fillStyle='#5a6078';ctx.font='8px monospace';ctx.textAlign='center';
  [0,0.5,1].forEach(f=>{
    const ts=nowS-SPAN*(1-f);
    const x=pad.left+Math.min(1,Math.max(0,f))*pw;
    ctx.fillText(timeLabel(ts),x,h-3);
  });
  ctx.textAlign='left';
}

/* 上图：BTC 价格 + ANCHOR（左轴价格，锚定价居中） */
function drawPriceChart(){
  const canvas=document.getElementById('chart');
  if(!canvas.ctx||canvas.w<=0)return;
  const {nowS,SPAN,hist}=chartWindow();
  if(hist.length<2)return;
  const ctx=canvas.ctx,w=canvas.w,h=canvas.h;
  const pad={top:12,right:44,bottom:14,left:56};
  const pw=w-pad.left-pad.right,ph=h-pad.top-pad.bottom;
  const last=hist[hist.length-1];
  let allVals=[];
  hist.forEach(p=>{if(p.btc)allVals.push(p.btc);if(p.anchor)allVals.push(p.anchor);});
  if(!allVals.length)return;
  let yMin,yMax;
  if(last.anchor){
    let maxDev=0;
    hist.forEach(p=>{if(p.btc)maxDev=Math.max(maxDev,Math.abs(p.btc-last.anchor));});
    const target=Math.max(maxDev*1.25,last.anchor*.0005);
    if(drawChart._anchor!==last.anchor){drawChart._anchor=last.anchor;drawChart._half=0;}  // 跨小时锚点变更，量程重算
    if(!drawChart._half||target>drawChart._half)drawChart._half=target;
    else drawChart._half=Math.max(target,drawChart._half*0.99);
    yMin=last.anchor-drawChart._half;yMax=last.anchor+drawChart._half;
  }else{
    yMin=Math.min(...allVals);yMax=Math.max(...allVals);
    const range=yMax-yMin||100;
    yMin-=range*.1;yMax+=range*.1;
  }
  const toX=t=>pad.left+Math.max(0,Math.min(1,(t-(nowS-SPAN))/SPAN))*pw;
  const toY=v=>pad.top+ph-((v-yMin)/(yMax-yMin))*ph;
  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle='rgba(20,20,50,.9)';ctx.lineWidth=1;
  for(let g=1;g<4;g++){ctx.beginPath();ctx.moveTo(pad.left,pad.top+ph*g/4);ctx.lineTo(pad.left+pw,pad.top+ph*g/4);ctx.stroke()}
  ctx.fillStyle='#555a78';ctx.font='9px monospace';ctx.textAlign='right';
  for(let i=0;i<=4;i++){const y=pad.top+(ph/4)*i;
    ctx.fillText('$'+(yMax-((yMax-yMin)/4)*i).toFixed(0),pad.left-5,y+3)}
  ctx.textAlign='left';
  if(last.anchor){
    const ay=toY(last.anchor);
    ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(pad.left,ay);ctx.lineTo(pad.left+pw,ay);
    ctx.strokeStyle='rgba(240,185,11,.7)';ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle='rgba(240,185,11,.9)';ctx.fillText('ANCHOR $'+last.anchor.toFixed(0),pad.left+4,ay-4);
  }
  const pts=[];
  hist.forEach((p)=>{if(p.btc)pts.push({x:toX(p.t),y:toY(p.btc)})});
  if(pts.length>=2){
    ctx.strokeStyle='rgba(0,229,255,.9)';ctx.lineWidth=1.6;
    ctx.shadowColor='rgba(0,229,255,.8)';ctx.shadowBlur=8;
    ctx.beginPath();ctx.moveTo(pts[0].x,pts[0].y);
    for(let i=1;i<pts.length;i++)ctx.lineTo(pts[i].x,pts[i].y);
    ctx.stroke();ctx.shadowBlur=0;
    const lp=pts[pts.length-1];
    ctx.beginPath();ctx.arc(lp.x,lp.y,3,0,7);
    ctx.fillStyle='#00e5ff';ctx.shadowColor='#00e5ff';ctx.shadowBlur=10;ctx.fill();ctx.shadowBlur=0;
    ctx.fillStyle='#00e5ff';ctx.font='bold 10px monospace';ctx.textAlign='right';
    ctx.fillText('BTC $'+Math.round(last.btc).toLocaleString('en-US'),Math.min(lp.x,pad.left+pw)-6,lp.y-8);
  }
  drawTimeAxis(ctx,nowS,SPAN,pad,pw,h);
}

/* 下图：UP%/DOWN% 概率（右轴 0-100%，纯实时 2s 采样） */
function drawProbChart(){
  const canvas=document.getElementById('chartProb');
  if(!canvas.ctx||canvas.w<=0)return;
  const {nowS,SPAN,hist}=chartWindow();
  if(hist.length<2)return;
  const ctx=canvas.ctx,w=canvas.w,h=canvas.h;
  const pad={top:10,right:44,bottom:12,left:56};
  const pw=w-pad.left-pad.right,ph=h-pad.top-pad.bottom;
  const toX=t=>pad.left+Math.max(0,Math.min(1,(t-(nowS-SPAN))/SPAN))*pw;
  const toY=v=>pad.top+ph-((v-0)/100)*ph;
  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle='rgba(20,20,50,.9)';ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(pad.left,pad.top+ph/2);ctx.lineTo(pad.left+pw,pad.top+ph/2);ctx.stroke();
  ctx.fillStyle='#555a78';ctx.font='9px monospace';ctx.textAlign='left';
  for(let i=0;i<=2;i++){const y=pad.top+(ph/2)*i;ctx.fillText((100-i*50)+'%',pad.left+pw+5,y+3)}
  const midY=toY(50);
  ctx.strokeStyle='rgba(85,90,120,.25)';ctx.lineWidth=.6;ctx.setLineDash([3,5]);
  ctx.beginPath();ctx.moveTo(pad.left,midY);ctx.lineTo(pad.left+pw,midY);ctx.stroke();ctx.setLineDash([]);
  function drawLine(key,color,labelTxt,dy){
    const pts=[];
    hist.forEach((p)=>{if(p[key]!=null)pts.push({x:toX(p.t),y:toY(p[key]),v:p[key]})});
    if(pts.length<2)return;
    ctx.strokeStyle=color;ctx.lineWidth=1.4;
    ctx.beginPath();ctx.moveTo(pts[0].x,pts[0].y);
    for(let i=1;i<pts.length;i++)ctx.lineTo(pts[i].x,pts[i].y);
    ctx.stroke();
    const lp=pts[pts.length-1];
    ctx.fillStyle=color;ctx.font='bold 10px monospace';ctx.textAlign='right';
    ctx.fillText(labelTxt(lp.v),Math.min(lp.x,pad.left+pw)-4,lp.y+dy);
  }
  drawLine('up','rgba(22,199,132,.9)',v=>'UP '+v.toFixed(1)+'%',-4);
  drawLine('down','rgba(255,59,92,.9)',v=>'DOWN '+v.toFixed(1)+'%',12);
  drawTimeAxis(ctx,nowS,SPAN,pad,pw,h);
}

/* ================= 点阵球（历史窗口空间，真实结算数据） ================= */
const SPHERE={nodes:[],raf:null,W:0,H:0};
function sphereRebuild(trades){
  const resolved=trades.filter(t=>t.result==='WIN'||t.result==='LOSS'||t.result==='PENDING').slice(-32);
  SPHERE.nodes=resolved.map(t=>({
    x:0,y:0,z:0,
    hue:t.result==='WIN'?'g':t.result==='LOSS'?'r':'y',
    glow:Math.random(),
    price:t.buy_price!=null?Math.round(t.buy_price*100):null}));
  const N=SPHERE.nodes.length||1;
  SPHERE.nodes.forEach((n,i)=>{const y=1-(i/(N-1||1))*2,r=Math.sqrt(Math.max(0,1-y*y)),th=i*2.39996;
    n.x=Math.cos(th)*r;n.y=y;n.z=Math.sin(th)*r;});
  document.getElementById('sphere-tag').textContent=resolved.length+'W';
}
(function(){
  const cv=document.getElementById('sphere');if(!cv)return;
  const ctx=cv.getContext('2d');
  function size(){const b=cv.parentElement.getBoundingClientRect();
    SPHERE.W=cv.width=b.width*(window.devicePixelRatio||1);SPHERE.H=cv.height=b.height*(window.devicePixelRatio||1)}
  function frame(t){
    if(!SPHERE.nodes.length){  // 数据未到先铺随机点
      SPHERE.nodes=Array.from({length:32},(_,i)=>{const y=1-(i/31)*2,r=Math.sqrt(Math.max(0,1-y*y)),th=i*2.39996;
        return {x:Math.cos(th)*r,y,z:Math.sin(th)*r,hue:['g','r','y'][i%3],glow:Math.random(),price:null}});
    }
    ctx.clearRect(0,0,SPHERE.W,SPHERE.H);
    const cx=SPHERE.W/2,cy=SPHERE.H/2,R=Math.min(SPHERE.W,SPHERE.H)*.36,rot=t*.00028,dpr=window.devicePixelRatio||1;
    const proj=SPHERE.nodes.map(n=>{
      const x=n.x*Math.cos(rot)-n.z*Math.sin(rot),z=n.x*Math.sin(rot)+n.z*Math.cos(rot);
      const s=1/(2.2-z*.9);
      return{sx:cx+x*R*s*2.2,sy:cy+n.y*R*s*2.2,s,z,n};
    }).sort((a,b)=>a.z-b.z);
    for(let i=0;i<proj.length;i++)for(let j=i+1;j<proj.length;j++){
      const a=proj[i],b=proj[j],d=Math.hypot(a.sx-b.sx,a.sy-b.sy);
      if(d<R*.75){ctx.beginPath();ctx.moveTo(a.sx,a.sy);ctx.lineTo(b.sx,b.sy);
        ctx.strokeStyle='rgba(0,229,255,'+((1-d/(R*.75))*.14)+')';ctx.lineWidth=dpr*.7;ctx.stroke()}
    }
    for(const p of proj){
      const col=p.n.hue==='g'?'22,199,132':p.n.hue==='r'?'255,59,92':'240,185,11';
      const rr=(5+p.n.glow*4)*p.s*dpr;
      ctx.beginPath();ctx.arc(p.sx,p.sy,rr*2.1,0,7);ctx.fillStyle='rgba('+col+',.08)';ctx.fill();
      ctx.beginPath();ctx.arc(p.sx,p.sy,rr,0,7);ctx.fillStyle='rgba('+col+','+(.25+.5*p.s)+')';
      ctx.shadowColor='rgba('+col+',.9)';ctx.shadowBlur=10*p.s*dpr;ctx.fill();
      ctx.beginPath();ctx.arc(p.sx,p.sy,rr*.45,0,7);ctx.fillStyle='rgba('+col+',.95)';ctx.fill();ctx.shadowBlur=0;
    }
    SPHERE.raf=requestAnimationFrame(frame);
  }
  size();frame();
  addEventListener('resize',()=>{cancelAnimationFrame(SPHERE.raf);size();frame()});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)cancelAnimationFrame(SPHERE.raf);else frame()});
})();

/* ================= 频谱（氛围动效） ================= */
(function(){
  const cv=document.getElementById('spec');if(!cv)return;
  const ctx=cv.getContext('2d');
  const bars=72;let t=0;
  function f(){
    if(!document.hidden){
      const b=cv.parentElement.getBoundingClientRect();
      if(cv.width!==Math.round(b.width*(window.devicePixelRatio||1))){
        cv.width=b.width*(window.devicePixelRatio||1);cv.height=54*(window.devicePixelRatio||1)}
      ctx.clearRect(0,0,cv.width,cv.height);
      const bw=cv.width/bars;
      for(let i=0;i<bars;i++){
        const v=Math.abs(Math.sin(i*.23+t*.05)+Math.sin(i*.07-t*.02))*.5+Math.random()*.25;
        const h=v*cv.height*.85,col=v>.66?'22,199,132':v>.4?'240,185,11':'255,59,92';
        ctx.fillStyle='rgba('+col+',.75)';ctx.fillRect(i*bw+1,cv.height-h,bw-2,h);
      }
      t++;
    }
    requestAnimationFrame(f);
  }
  f();
})();

/* ================= 粒子星座背景 ================= */
(function(){
  const cv=document.getElementById('fx-particles');if(!cv)return;
  const ctx=cv.getContext('2d');
  let W,H,pts=[],raf;const N=55,dpr=window.devicePixelRatio||1;
  function size(){W=cv.width=innerWidth*dpr;H=cv.height=innerHeight*dpr;
    cv.style.width=innerWidth+'px';cv.style.height=innerHeight+'px'}
  function init(){size();pts=Array.from({length:N},()=>({x:Math.random()*W,y:Math.random()*H,
    vx:(Math.random()-.5)*.2*dpr,vy:(Math.random()-.5)*.2*dpr,r:Math.random()*1.5*dpr+.5}))}
  function frame(){ctx.clearRect(0,0,W,H);
    for(const p of pts){p.x+=p.vx;p.y+=p.vy;
      if(p.x<0||p.x>W)p.vx*=-1;if(p.y<0||p.y>H)p.vy*=-1;
      ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,7);ctx.fillStyle='rgba(0,229,255,.5)';ctx.fill()}
    for(let i=0;i<pts.length;i++)for(let j=i+1;j<pts.length;j++){
      const a=pts[i],b=pts[j],d=Math.hypot(a.x-b.x,a.y-b.y);
      if(d<120*dpr){ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);
        ctx.strokeStyle='rgba(0,229,255,'+((1-d/(120*dpr))*.12)+')';ctx.stroke()}}
    raf=requestAnimationFrame(frame)}
  init();frame();
  addEventListener('resize',()=>{cancelAnimationFrame(raf);init();frame()});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)cancelAnimationFrame(raf);else frame()});
})();

/* ================= 日志 + W/E 过滤 ================= */
let logsCache=[];
let logsWarnOnly=false;
function setLogFilter(mode){
  logsWarnOnly=(mode==='WE');
  const bAll=document.getElementById('log-filter-all'),bWe=document.getElementById('log-filter-we');
  if(bAll)bAll.classList.toggle('mode-active',!logsWarnOnly);
  if(bWe)bWe.classList.toggle('mode-active',logsWarnOnly);
  renderLogs();
}
function renderLogs(){
  const box=document.getElementById('log');
  const shown=logsWarnOnly?logsCache.filter(x=>{const lv=(x.level||'').toUpperCase();return lv==='W'||lv==='E'}):logsCache;
  box.innerHTML=shown.map(x=>{const lv=(x.level||'I').toUpperCase();
    return '<div class="l-'+lv+'"><span class="lt">'+esc(x.time)+'</span>['+lv+'] '+esc(x.msg)+'</div>'}).join('')||
    '<span style="color:var(--dim)">'+(logsWarnOnly?'无 W/E 级日志':'加载中...')+'</span>';
}

/* ================= 热力 + 历史表 ================= */
function slugTime(slug){
  try{const m=slug.match(/(\w+)-(\d+)-(\d+)/);
    if(m){const hm=slug.match(/(\d+)(am|pm)/);
      return m[1].slice(0,3)+'/'+m[2]+' '+(hm?hm[0]:'');}}catch(e){}
  return '';
}
function renderHistory(h){
  if(!h)return;
  const trades=h.trades||[];
  // 热力: 已结算的近36期，新→旧
  const resolved=trades.filter(t=>t.result==='WIN'||t.result==='LOSS').slice(-36).reverse();
  const heat=document.getElementById('heat');
  heat.innerHTML=resolved.map((t,i)=>{
    const cls=t.result==='WIN'?'hw':'hl';
    const dim=t.filled===false;
    const price=t.buy_price!=null?Math.round(t.buy_price*100)+'¢':'--';
    return '<div class="hcell '+cls+'" style="animation-delay:'+(i*40)+'ms;'+(dim?'opacity:.38':'')+'" '+
      'title="'+esc(t.side)+' '+t.result+' · '+(t.buy_price!=null?'买价 '+Math.round(t.buy_price*100)+'¢':'未成交')+' · '+esc(slugTime(t.slug))+'">'+
      price+'<small>'+esc(t.side||'')+'</small></div>';
  }).join('');
  // 表格（全量，新→旧）
  document.getElementById('tbl-n').textContent=trades.length+' 行';
  const tb=document.getElementById('tbl');
  tb.innerHTML='<thead><tr><th>窗口</th><th>方向</th><th>金额</th><th>成交</th><th>结果</th><th>PnL</th></tr></thead><tbody>'+
    (trades.length?[...trades].reverse().map(t=>{
      let resultCell='<span class="pend">PENDING</span>';
      if(t.result==='WIN')resultCell='<span class="win">WIN</span>';
      else if(t.result==='LOSS')resultCell='<span class="loss">LOSS</span>';
      else if(t.result==='SKIP')resultCell='<span class="skip">SKIP</span>';
      else if(t.result==='ERROR')resultCell='<span class="loss">ERROR</span>';
      let fillCell='<span class="skip">--</span>';
      if(t.result==='WIN'||t.result==='LOSS'||t.result==='PENDING'){
        if(t.paper)fillCell='<span class="skip">📝模拟</span>';
        else if(t.filled===true)fillCell='<span class="win">✅</span>';
        else if(t.filled===false)fillCell='<span style="color:var(--yellow)">❌未成交</span>';
        else fillCell='<span class="skip">❔</span>';
      }
      let pnl='<span class="skip">--</span>';
      if(t.filled===false&&t.result!=='PENDING')pnl='<span class="skip">—</span>';
      else if((t.result==='WIN'||t.result==='LOSS')&&t.filled==null&&!t.paper)pnl='<span class="skip">❔</span>';
      else if(t.pnl!==undefined&&t.pnl!==null)pnl='<span class="'+(t.pnl>=0?'win':'loss')+'">'+(t.pnl>=0?'+':'−')+'$'+Math.abs(t.pnl).toFixed(2)+'</span>';
      else if(t.result==='PENDING')pnl='<span class="skip">…</span>';
      const amtCell=(t.result==='SKIP'||t.result==='ERROR')?'<span class="skip">—</span>':'$'+(t.amount||0);
      return '<tr><td class="skip">'+esc(slugTime(t.slug))+'</td>'+
        '<td class="'+(t.side==='UP'?'win':'loss')+'">'+esc(t.side||'--')+'</td>'+
        '<td>'+amtCell+'</td><td>'+fillCell+'</td><td>'+resultCell+'</td><td>'+pnl+'</td></tr>';
    }).join(''):'<tr><td colspan="6" class="skip" style="text-align:center;padding:16px">暂无交易记录</td></tr>')+'</tbody>';
}

/* ================= 主刷新链 ================= */
let refreshInFlight=false;
let lastHistoryVer=null;
let amLastSynced=null; // round55.2: ANALYZE 下拉上次已同步的 config 值——只在 config 变化时回写，防轮询 stomp 用户未提交的选择
async function refresh(){
  if(refreshInFlight)return;
  refreshInFlight=true;
  try{
    const [s,sg,an,pos,l,h,c]=await Promise.all([
      fetchAPI('/api/status'),fetchAPI('/api/signal'),fetchAPI('/api/analysis'),
      fetchAPI('/api/position'),fetchAPI('/api/logs'),
      fetchAPI('/api/history'+(lastHistoryVer?'?ver='+encodeURIComponent(lastHistoryVer):'')),
      fetchAPI('/api/config')
    ]);
    if(s)S.status=s;
    let sigResult=null;
    if(sg&&sg.has_signal&&sg.signal){S.signal=sg.signal;sigResult=sg.result||null;}
    if(an&&an.has_analysis&&an.analysis)S.analysis=an.analysis;
    if(pos)S.position=pos;
    if(l&&l.logs){logsCache=l.logs;renderLogs();renderFeed();}
    if(c){
      S.config=c;
      // BET 输入框同步：原来输入框写死 2.00 且从不同步 config，用户看到的值和真实下注额脱节
      if(c.bet_amount!=null&&Math.abs(Number(c.bet_amount)-betAmount)>1e-9){
        betAmount=Number(c.bet_amount);
        document.getElementById('bet').value=betAmount.toFixed(2);
      }
      const bw=document.getElementById('bet-warn');
      if(bw)bw.style.display=(c.bet_amount!=null&&c.bet_amount<1.0)?'block':'none';
      // TP 按钮状态同步：config.tp_enabled 为唯一事实源（原来按钮视觉与真实状态脱节）
      const tpOn=c.tp_enabled!==false;
      if(tpOn!==tpEnabled){tpEnabled=tpOn;document.getElementById('btn-tp').style.opacity=tpOn?1:.45;}
      // ANALYZE 下拉同步（round55.2 修复 stomp bug）：config.analyze_min 为唯一事实源，但只在
      // config 值发生变化时才回写下拉框——此前每 2s 无条件回写，用户选中新值尚未点 SET 就被轮询
      // 强行改回旧 config 值，SET 把被改回的旧值发了出去（"改不动、一直退回 :40"根因；9/6 浏览器
      // 实证：选 :50 点 SET 实发 45）。变化时同步覆盖三场景：加载首帧对齐 / SET 成功后对齐新值 /
      // 其他标签页改动后联动；未提交的用户选择不再被打断。
      const amSel=document.getElementById('analyze-min');
      if(amSel&&c.analyze_min!=null&&c.analyze_min!==amLastSynced){
        amLastSynced=c.analyze_min;
        if(String(c.analyze_min)!==amSel.value)amSel.value=String(c.analyze_min);
      }
    }
    if(h){
      // 9/3 瘦身: 带 ver 请求——服务端指纹未变时只回 short 响应（无 stats 字段），
      // 跳过重建；renderPnl 也必须跳过，否则用 undefined 覆盖 REAL PNL 面板
      if(h.short){/* 指纹未变，沿用现有 S.history */}
      else if(!h.data_version||h.data_version!==lastHistoryVer){
        lastHistoryVer=h.data_version;
        S.history=h;
        renderPnl(h);
        renderHistory(h);
        sphereRebuild(h.trades||[]);
      }
    }
    // 渲染（用最新快照）
    const x=S.signal;
    renderTopbar(S.status,x,S.config);
    renderEngine(x);
    renderBookWarn(x, S.analysis);
    renderPulse(x,S.analysis);
    renderSignal(x,sigResult,S.position);
    renderRisks(x,S.analysis);
    renderWallet();
    if(S.status&&S.status.btc_price){
      const ct=S.status.contract_tokens||{};
      const now=Date.now();
      // 9/3 恢复原设计：每 2s 采样一点，曲线连续延伸（1m klines 只做整小时回填，两者按时间戳对齐）
      priceHistory.push({t:Math.floor(now/1000),
        time:new Date(now).toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'}),
        btc:S.status.btc_price,anchor:S.status.anchor_price,
        up:ct.up_price!=null?ct.up_price*100:null,down:ct.down_price!=null?ct.down_price*100:null});
      if(priceHistory.length>MAX_POINTS)priceHistory=priceHistory.slice(-MAX_POINTS);
      drawChart();
    }
  } finally { refreshInFlight=false; }
}

/* ================= 控制操作 ================= */
function _se(){return document.getElementById('save-status')}
let betAmount=2.0, tpEnabled=true;
async function saveBet(){
  const se=_se();
  try{
    const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bet_amount:betAmount})});
    const d=await r.json();
    if(d.ok){se.textContent='已保存';setTimeout(()=>se.textContent='',2000)}
    else se.textContent=d.error||'保存失败';
  }catch(e){se.textContent='保存失败';console.error(e)}
}
function clampBet(v){return Math.max(1.0,Math.min(50,v))}
async function adjustBet(d){
  betAmount=clampBet(parseFloat(document.getElementById('bet').value||2)+d);
  document.getElementById('bet').value=betAmount.toFixed(2);
  await saveBet();
}
async function setBet(){
  betAmount=clampBet(parseFloat(document.getElementById('bet').value)||2.0);
  document.getElementById('bet').value=betAmount.toFixed(2);
  await saveBet();
}
async function setAnalyzeMin(){
  const se=_se(), sel=document.getElementById('analyze-min');
  const v=parseInt(sel.value,10);
  try{
    const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({analyze_min:v})});
    const d=await r.json();
    if(d.ok){se.textContent='分析时刻 :'+String(v).padStart(2,'0')+'（引擎已联动）';setTimeout(()=>se.textContent='',2500)}
    else{se.textContent=d.error||'保存失败';}
  }catch(e){se.textContent='保存失败';console.error(e)}
}
async function toggleTP(){
  tpEnabled=!tpEnabled;
  const se=_se();
  document.getElementById('btn-tp').style.opacity=tpEnabled?1:.45;
  try{
    const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tp_enabled:tpEnabled})});
    const d=await r.json();
    if(d.ok){se.textContent='止盈'+(tpEnabled?'开':'关');setTimeout(()=>se.textContent='',2000)}
    else{se.textContent=d.error||'保存失败';tpEnabled=!tpEnabled;document.getElementById('btn-tp').style.opacity=tpEnabled?1:.45;}
  }catch(e){se.textContent='保存失败';tpEnabled=!tpEnabled;console.error(e)}
}
async function togglePause(){
  const cur=(S.config&&S.config.trading_paused===true);
  const se=_se();
  try{
    const r=await fetch('/api/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:!cur})});
    const d=await r.json();
    if(d.ok){S.config=Object.assign({},S.config,{trading_paused:d.trading_paused});
      renderTopbar(null,null,S.config);
      se.textContent=d.trading_paused?'已暂停下单(信号照跑)':'已恢复下单';setTimeout(()=>se.textContent='',2500)}
    else se.textContent=d.error||'操作失败';
  }catch(e){se.textContent='操作失败';console.error(e)}
}
async function switchMode(m){
  if(m==='live'&&!confirm('⚠️ 即将切换到 LIVE 实盘模式\n\n将用真实资金下单！\n\n请确认：\n1. 钱包/代理/市场状态就绪\n2. 当前策略已在 paper 验证\n\n确定切到实盘？'))return;
  const se=_se();
  try{
    const r=await fetch('/api/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})});
    const d=await r.json();
    if(d.ok){S.config=Object.assign({},S.config,{mode:m});renderTopbar(null,null,S.config);
      se.textContent='已切到 '+m.toUpperCase();setTimeout(()=>se.textContent='',2500);}
    else se.textContent='❌ '+(d.error||'切换失败');
  }catch(e){se.textContent='❌ 切换失败(网络)';console.error(e)}
  setTimeout(refresh,3000);
}
async function restartBot(){
  const se=_se();
  if(!confirm('确定重启 Bot？若正在下单可能被中断'))return;
  se.textContent='⏳ Bot 重启中...';
  try{
    const r=await fetch('/api/restart',{method:'POST'});
    const d=await r.json();
    if(d.ok){se.textContent='✅ Bot 已重启';setTimeout(()=>{se.textContent='';refresh()},3000)}
    else se.textContent='❌ '+(d.error||'重启失败');
  }catch(e){se.textContent='❌ 重启失败';console.error(e)}
}

/* ================= 钱包轮询 ================= */
async function refreshWallet(){
  const w=await fetchAPI('/api/wallet');
  if(w){S.wallet=w;renderWallet();}
}

/* ================= 启动 ================= */
window.addEventListener('resize',()=>{clearTimeout(window._rszT);window._rszT=setTimeout(initChart,200)});
/* 9/3 图表高频采样：500ms 一次轻量行情（/api/ticker 不碰 gamma），BTC + 概率曲线连贯走动 */
async function tickChart(){
  try{
    const s=await fetchAPI('/api/ticker');
    if(s&&s.btc){
      // 9/3: up/down 改由 ticker 带出（市场缓存 20s），概率线每 500ms 一个点、与 BTC 线同步连贯
      priceHistory.push({t:Math.floor(Date.now()/1000),btc:s.btc,anchor:s.anchor||null,
        up:s.up!=null?s.up:null,down:s.down!=null?s.down:null});
      if(priceHistory.length>MAX_POINTS)priceHistory=priceHistory.slice(-MAX_POINTS);
      drawChart();
    }
  }catch(e){}
}
initChart();
seedChart();
refresh();
refreshWallet();
setInterval(tickChart,500);      // 图表 500ms 高频采样
setInterval(seedChart,60000);   // 每分钟回填最新 1m kline
setInterval(refresh,2000);
setInterval(refreshWallet,10000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    if not DASH_PASS:
        print("⚠️ 警告: FLASK_PASS 未设置（.env），Dashboard 写操作端点无认证保护")
    print("🚀 Hermes 仪表盘启动: http://127.0.0.1:5050 (0.0.0.0 支持局域网访问)")
    # 8/9: 换网络后 127.0.0.1 可能连不上，改为 0.0.0.0 自动适配所有网卡（本机/局域网均可访问）
    # 8/10: threaded=True 避免单个慢的外部请求占满单线程导致所有 API 排队超时（页面数据全 "--"）
    # 9/3: 默认 TCP backlog=5，页面加载突发 8+ 请求 + 500ms ticker 会挤爆队列出现
    #      CONNECTION_RESET/REFUSED，调大到 128（socketserver.TCPServer 类属性，Werkzeug 继承）
    import socketserver
    socketserver.TCPServer.request_queue_size = 128
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
