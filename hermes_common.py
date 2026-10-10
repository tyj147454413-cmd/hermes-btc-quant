"""
Hermes 共享工具模块 — 时区、slug 构造、HTTP 请求、日志

所有模块统一使用此模块的 http_get_json / log / tz_offset_str，
避免代理环境变量竞态、日志格式不一致、时区硬编码等问题。
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

import calendar
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR / "data"
HOURLY_PREDICTION = DATA_DIR / "hermes_hourly_prediction.json"

# —— 统一配置常量 ——
# 8/9 起默认直连。此前硬编码 http://127.0.0.1:10808 在无代理环境下导致全部外部请求失败。
# 8/9 晚: 网络自适应——HERMES_PROXY 显式设置最高优先，否则自动探测当前网络可用代理，都不通则直连。
# 例: HERMES_PROXY=http://127.0.0.1:3067
PROXY = os.getenv("HERMES_PROXY", "") or None
GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_API = "https://api.binance.com"
BINANCE_FUTURES_API = "https://fapi.binance.com"
CLOB_API = "https://clob.polymarket.com"

# —— 线程安全 SSL 上下文（探测函数与 http_get_json 共用）——
try:
    import certifi
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _ssl_ctx = ssl.create_default_context()


# —— 网络自适应探测 ——
# 常见本地代理端口（clash/v2ray/轻量 VPN 等）。仅探测端口是否有服务在监听，不假设必可用。
_COMMON_PROXY_PORTS = (10808, 7890, 7891, 7897, 1080, 3067, 2080, 10809, 1087, 8888, 8118)
# 探测用探针：选轻量、可达性即代表"能访问加密金融数据源"的端点。
_PROBE_URLS = (
    f"{BINANCE_API}/api/v3/ping",
    f"{GAMMA_API}/markets?slug=__probe__",
    f"{CLOB_API}/",
)
# 进程级缓存，避免每次 http_get_json 重复探测
# 8/10: 长驻进程（dashboard/bot 由 launchd 管理，可能跑数小时）网络切换后须能自愈，
# 故引入 TTL 定时重探 + 连续失败强制重探，不再"首次探测后永久缓存"。
_net_cache: dict[str, Any] = {"proxy": None, "done": False, "ts": 0.0, "fail": 0}
_net_lock = threading.Lock()
# 网络探测结果有效期（秒）。长驻进程每 5 分钟重新识别一次，覆盖换网络/VPN 开关场景。
NET_PROXY_TTL = float(os.getenv("HERMES_NET_PROXY_TTL", "300"))
# 连接层异常连续失败达到该次数时强制重置网络缓存重探（防止"代理刚失效"长期不可用）。
NET_FAIL_RESET_THRESHOLD = int(os.getenv("HERMES_NET_FAIL_RESET", "3"))


def _probe_port(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """探测本地端口是否有服务在监听（TCP 握手）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_url(url: str, proxy: str | None, timeout: float = 4.0) -> bool:
    """用 http_get_json 的底层逻辑探测某代理路径能否访问 url。"""
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-netprobe/1.0"})
    handlers = [urllib.request.HTTPSHandler(context=_ssl_ctx)]
    if proxy:
        handlers.insert(0, urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.insert(0, urllib.request.ProxyHandler({}))  # 强制直连，忽略环境残留
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout):
            return True
    except Exception:
        return False


def _detect_proxy_once() -> str | None:
    """自动识别当前网络下可用的代理。

    优先级:
      1. 显式环境变量 HERMES_PROXY（用户指定，最高优先）
      2. 常见本地代理端口中有服务监听且能访问探针的（自动识别）
      3. 都没有 → 返回 None（强制直连）
    """
    # 1. 显式指定
    explicit = os.getenv("HERMES_PROXY", "").strip() or None
    if explicit:
        return explicit

    # 2. 从环境变量中找可能残留的本地代理（http_proxy 等），作为候选
    candidates: list[str] = []
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        v = os.getenv(key, "").strip()
        if v and v.startswith("http://127.0.0.1:") and v not in candidates:
            candidates.append(v)
    # 3. 常见代理端口
    for port in _COMMON_PROXY_PORTS:
        if _probe_port(port):
            cand = f"http://127.0.0.1:{port}"
            if cand not in candidates:
                candidates.append(cand)

    # 逐一验证候选能否真正访问数据源（防止端口活着但代理已失效）
    for cand in candidates:
        if _probe_url(_PROBE_URLS[0], cand):
            return cand

    return None  # 全都不通 → 直连


def _auto_proxy() -> str | None:
    """带缓存 + TTL 的网络自动识别。返回可用代理，None=直连。

    8/10 修复: 长驻进程首次探测后永久缓存，网络切换不会自愈。
    现在超过 NET_PROXY_TTL(默认300s) 即重新探测；用锁避免多线程竞态。
    """
    with _net_lock:
        now = time.time()
        if not _net_cache["done"] or (now - _net_cache["ts"] > NET_PROXY_TTL):
            _net_cache["proxy"] = _detect_proxy_once()
            _net_cache["done"] = True
            _net_cache["ts"] = now
            log(f"🌐 网络自适应: 使用 {'代理 ' + _net_cache['proxy'] if _net_cache['proxy'] else '直连'}")
        return _net_cache["proxy"]


def get_proxy() -> str | None:
    """对外统一入口：显式 PROXY 优先，否则自动探测。"""
    if PROXY:
        return PROXY
    return _auto_proxy()


def reset_net_cache() -> None:
    """重置网络探测缓存（网络切换后调用，重新识别）。"""
    with _net_lock:
        _net_cache["proxy"] = None
        _net_cache["done"] = False
        _net_cache["ts"] = 0.0
        _net_cache["fail"] = 0

# —— 日志 ——
DATA_DIR.mkdir(parents=True, exist_ok=True)
_logger = logging.getLogger("hermes")
if not _logger.handlers:
    _fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%m-%d %H:%M:%S")
    # stdout handler（launchd 会捕获到各自 cron.log）
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _logger.addHandler(_sh)
    # 统一文件 handler（所有模块共享，用户只需 tail 一个文件）
    _fh = logging.FileHandler(DATA_DIR / "hermes_app.log", encoding="utf-8")
    _fh.setFormatter(_fmt)
    _logger.addHandler(_fh)
    _logger.setLevel(logging.INFO)


def log(msg: str, level: str = "I") -> None:
    """统一日志函数，level: I/W/E"""
    level_map = {"I": logging.INFO, "W": logging.WARNING, "E": logging.ERROR}
    _logger.log(level_map.get(level, logging.INFO), msg)


def fetch_book_momentum(slug: str, dm: int, min_samples: int = 2,
                        snap_path: Path | None = None) -> dict | None:
    """旁路盘感：读盘口快照 jsonl（book_snapshot.py 采集），计算决策点前窗口内 CLOB 动能。

    只读旁路信号，供 predict_hourly.py（影子记录字段）与 hermes_1h_bot.py（决策门控）共用。
      samples           窗口内采样数（< min_samples 或无数据返回 None）
      up_ask_delta      UP ask1 窗口首末变化（+ = 买 UP 动能强）
      down_ask_delta    DOWN ask1 窗口首末变化
      up_bid_delta      UP bid1 窗口首末变化
      max_down_bid1_sz  窗口内 DOWN bid1 最大挂单量（诱空大单探测，>=300 手可疑）
      flag              判定：DUMP_TRAP 诱空拉涨 / ASK_SURGE 买UP动能 /
                        ASK_FADE UP走弱 / FLAT 无显著动能
    snap_path: 测试注入用；默认读 data/book_snapshots.jsonl
    """
    SNAP = snap_path if snap_path is not None else DATA_DIR / "book_snapshots.jsonl"
    if not SNAP.exists():
        return None
    rows = []
    for line in SNAP.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        # <= dm 防止把决策点之后的"未来"快照纳入（推理在 :dm:xx 运行）
        if r.get("slug") == slug and r.get("minutes_in_hour", 999) <= dm:
            rows.append(r)
    if len(rows) < min_samples:
        return None
    rows.sort(key=lambda x: x["minutes_in_hour"])

    def ask1(r: dict, side: str) -> float:
        a = r[side].get("asks") or []
        return a[0][0] if a else 0.0

    def bid1(r: dict, side: str) -> float:
        b = r[side].get("bids") or []
        return b[0][0] if b else 0.0

    def bid1_sz(r: dict, side: str) -> float:
        b = r[side].get("bids") or []
        return b[0][1] if b else 0.0

    first, last = rows[0], rows[-1]
    up_ask_delta = round(ask1(last, "up") - ask1(first, "up"), 4)
    down_ask_delta = round(ask1(last, "down") - ask1(first, "down"), 4)
    up_bid_delta = round(bid1(last, "up") - bid1(first, "up"), 4)
    max_down_bid1_sz = round(max(bid1_sz(r, "down") for r in rows), 1)

    if up_ask_delta >= 0.04 and max_down_bid1_sz >= 300:
        flag = "DUMP_TRAP"      # DOWN bid 巨额挂单等待成交 + UP 盘口快速上移 = 诱空拉涨剧本
    elif up_ask_delta >= 0.04:
        flag = "ASK_SURGE"      # UP 卖盘被扫，盘口快速上移
    elif up_ask_delta <= -0.04:
        flag = "ASK_FADE"       # UP 盘口下移，买 UP 动能衰减
    else:
        flag = "FLAT"
    return {
        "samples": len(rows), "flag": flag,
        "up_ask_delta": up_ask_delta, "down_ask_delta": down_ask_delta,
        "up_bid_delta": up_bid_delta, "max_down_bid1_sz": max_down_bid1_sz,
    }


def fetch_pm_feats(slug: str, dm: int, shadow_path: Path | None = None) -> dict | None:
    """盘感代理特征：从影子记录读当小时 Polymarket 盘口价演变（pm_up/pm_down）。

    每个决策点（:00~:55）推理时都会把当时的 PM 盘口价写入 ml_shadow_predictions.jsonl，
    这里取「决策点 dm 前 <=dm 的全部 pm_up 样本」构造两个特征：
      pm_up_dec   决策点实际可用的最新 pm_up（NaN 兜底）
      pm_delta    pm_up 演变 = 最新 - 该小时最早（压价/拉升过程的量化）
    shadow 无该 slug / 无 <=dm 样本 / 文件缺失 → None（训练行与历史行均视为特征缺失）。
    shadow_path: 测试注入用；默认读 data/ml_shadow_predictions.jsonl
    """
    SHADOW = shadow_path if shadow_path is not None else DATA_DIR / "ml_shadow_predictions.jsonl"
    if not SHADOW.exists():
        return None
    rows = []
    for line in SHADOW.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("slug") != slug or r.get("pm_up") is None:
            continue
        if r.get("decision_min", 999) <= dm:
            rows.append(r)
    if not rows:
        return None
    rows.sort(key=lambda x: x["decision_min"])
    dec, early = rows[-1], rows[0]
    return {
        "pm_up_dec": float(dec["pm_up"]),
        "pm_delta": round(float(dec["pm_up"]) - float(early["pm_up"]), 4),
    }


def now_et() -> datetime:
    """返回当前 ET 时间（自动处理 EDT/EST）"""
    return datetime.now(timezone.utc).astimezone(ET)


def get_et_offset() -> float:
    """返回当前 ET 相对 UTC 的偏移（小时，正值），用于兼容旧代码"""
    return abs(now_et().utcoffset().total_seconds() / 3600)


def tz_offset_str() -> str:
    """返回当前 ET 时区偏移字符串，如 '-04:00' 或 '-05:00'（动态适配 EDT/EST）"""
    offset = now_et().strftime("%z")  # 如 "-0400"
    return f"{offset[:3]}:{offset[3:]}"


def build_slug(et_dt: datetime) -> str:
    """根据 ET 时间构造 Polymarket 1H 合约 slug"""
    month_name = calendar.month_name[et_dt.month].lower()
    h = et_dt.hour
    if h == 0:
        h12, ampm = 12, "am"
    elif h < 12:
        h12, ampm = h, "am"
    elif h == 12:
        h12, ampm = 12, "pm"
    else:
        h12, ampm = h - 12, "pm"
    return f"bitcoin-up-or-down-{month_name}-{et_dt.day}-{et_dt.year}-{h12}{ampm}-et"


# 9/2 审查(异味): 删除死代码 pid_matches_process——全仓无调用方，且用 `ps` 命令
# （Windows 不存在，异常时宽松返回 True），留着只有误导维护的风险。


# —— 线程安全的 HTTP 工具（不污染全局 os.environ）——

def _is_conn_error(e: Exception) -> bool:
    """判断异常是否为"连接层网络错误"（代理失效/DNS/拒连/超时等）。

    仅对连接层错误计数并触发重探；HTTP 4xx/5xx（HTTPError）是业务/服务端问题，
    不属于网络切换，不应触发重探。
    9/2 审查修复(B1): 8/31 起 http_get_json 底层已从 urllib 换成 requests，
    但排除表只排了 urllib.error.HTTPError。requests.exceptions.HTTPError 继承链
    是 RequestException(IOError)→OSError，isinstance(e, OSError) 为 True →
    Polymarket 503 "trading is disabled"（服务端维护）被误判为连接层错误，
    连续 3 次即 reset_net_cache 丢弃代理缓存强制重探——维护期每次请求都在
    重探网络（与 docstring 声明的口径直接矛盾）。现补排除 requests 的 HTTPError。"""
    if isinstance(e, urllib.error.HTTPError):
        return False
    if isinstance(e, requests.exceptions.HTTPError):
        return False  # raise_for_status 抛出的 4xx/5xx，业务/服务端问题，不算网络切换
    if isinstance(e, (urllib.error.URLError, OSError, socket.timeout, TimeoutError)):
        return True
    return False


def http_get_json(url: str, timeout: int = 10, proxy: str | None = None, user_agent: str = "hermes/1.0", return_error: bool = False) -> Any | None:
    """
    线程安全的 HTTP GET JSON。
    使用 ProxyHandler 而非修改全局 os.environ，避免多线程竞态。
    成功返回解析后的 dict/list，失败返回 None。
    return_error=True 时返回 (data, err) 元组，便于调用方拿到异常类型做诊断。

    8/9 网络自适应: proxy=None 时自动识别网络——HERMES_PROXY 显式设置优先，
    否则探测本地常见代理端口，都没有则强制直连(ProxyHandler({}) 忽略环境残留)。
    8/10 长驻进程自愈: 连接层异常连续失败 NET_FAIL_RESET_THRESHOLD(默认3) 次
    自动 reset_net_cache() 强制重探，避免"缓存了刚失效的代理"后长期不可用。
    """
    # 网络自适应：proxy 未显式指定时，自动识别可用代理（含 TTL 缓存）
    if proxy is None:
        proxy = get_proxy()
    # 2026-08-31 patch (.22): urllib.request -> requests (JA3 fingerprint)
    # NOTE: do NOT pass proxies={"http": None, ...} - triggers polymarket 10054 reset.
    # Only pass proxies when proxy is truthy (uses requests env vars otherwise).
    import requests as _requests_lib
    headers = {"User-Agent": user_agent}
    try:
        if proxy:
            resp = _requests_lib.get(url, headers=headers, proxies={"http": proxy, "https": proxy}, timeout=timeout)
        else:
            resp = _requests_lib.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        # success resets failure count
        with _net_lock:
            if _net_cache["fail"]:
                _net_cache["fail"] = 0
        return (data, None) if return_error else data
    except Exception as e:
        if _is_conn_error(e):
            with _net_lock:
                _net_cache["fail"] += 1
                if _net_cache["fail"] >= NET_FAIL_RESET_THRESHOLD:
                    log(f"🌐 网络自适应: 连接层连续失败 {_net_cache['fail']} 次，强制重探网络", level="W")
                    _net_cache["proxy"] = None
                    _net_cache["done"] = False
                    _net_cache["ts"] = 0.0
                    _net_cache["fail"] = 0
        return (None, e) if return_error else None


def write_hourly_prediction(signal: dict[str, Any]) -> None:
    """写每小时预测结果到文件，不管 ORDER 还是 SKIP 都包含方向判断。

    文件: data/hermes_hourly_prediction.json
    内容: timestamp, slug, direction(UP/DOWN), probability, action, reason, ...
    """
    try:
        # 防 None/缺失值导致 JSON 序列化异常
        safe_float = lambda v: round(float(v), 4) if v is not None else None
        pred: dict[str, Any] = {
            "timestamp": str(signal.get("timestamp", "") or ""),
            "slug": str(signal.get("slug", "") or ""),
            "direction": str(signal.get("side", signal.get("direction", "")) or ""),
            "probability": safe_float(signal.get("probability", 0)) or 0,
            "action": str(signal.get("action", "SKIP") or "SKIP"),
            "reason": str(signal.get("reason", "") or ""),
            "anchor_price": safe_float(signal.get("anchor_price")),
            "market_price": safe_float(signal.get("market_price")),
            "ev": safe_float(signal.get("ev")),
            "source": str(signal.get("source", "") or ""),
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件再 rename，防止崩溃产生半截 JSON
        tmp = HOURLY_PREDICTION.with_suffix(".tmp")
        # 9/2 审查修复(A3): 补 encoding——ensure_ascii=False 的中文 reason 在 Windows GBK
        # 默认编码下写入即崩（与 bot _load_risk_stats 同款事故模式），且被下方 except 吞掉
        tmp.write_text(json.dumps(pred, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HOURLY_PREDICTION)
    except Exception:
        pass


# —— Polymarket API 凭据缓存（8/19 根治认证端点派生风暴）——
# SecureClient.create 不传 credentials 时每次都 POST /auth/api-key 派生（dashboard 10s 轮询
# ≈ 360 次/小时轰炸该端点，叠加 bot 重连重试触发 Polymarket 间歇故障/tarpit）。
# 缓存派生结果后 create 走 GET /auth/api-keys 验证（L2 HMAC，正常端点），零依赖派生端点。
PM_CREDS_FILE = DATA_DIR / "hermes_pm_creds.json"


def load_pm_creds() -> dict[str, Any] | None:
    """读缓存的 Polymarket API 凭据。损坏/不存在返回 None。"""
    try:
        d = json.loads(PM_CREDS_FILE.read_text(encoding="utf-8"))
        if d.get("key") and d.get("secret") and d.get("passphrase"):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return None


def save_pm_creds(creds: dict[str, Any]) -> None:
    """原子写凭据缓存（tmp + replace），权限 0600（含 secret，与 .env 同级安全级别）。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PM_CREDS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(creds), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(PM_CREDS_FILE)
    except OSError:
        pass
