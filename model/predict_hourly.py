#!/usr/bin/env python3
"""每小时影子推理（TRAINING_PLAN.md 第 3 步）。

在决策时点（H+decision_min）后运行：
    1. 拉取 Binance 最新 K线（1h/15m/5m/4h + 资金费率），特征口径与训练完全一致
       （复用 backfill_data._precompute / _row，防训练-推理漂移）。
    2. LightGBM 推理 P(up)。
    3. 拉取 Polymarket Gamma 当小时市场盘口价（pm_up/pm_down，失败记 null）。
    4. 追加写入 data/ml_shadow_predictions.jsonl（同 slug+decision_min 幂等去重）。

调度：Windows 计划任务 HermesBTC1HModelPredXX（每小时 :XX+1 触发，决策点 :XX，
      XX ∈ {00,05,10,15,20,25,30,35,40,45,50,55}），每 5 分钟一个决策时点网格。
      bot 在 :40-:41 分析下单；不动现有交易链路。

用法：
    python model/predict_hourly.py --decision-min 35          # 实时（当前小时）
    python model/predict_hourly.py --decision-min 35 --hours-ago 1   # 复盘上一小时
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "model"))

from backfill_data import (ET, _precompute, _row, fetch_fng,  # noqa: E402
                           fetch_funding, fetch_klines)
from book_snapshot import fetch_book  # noqa: E402
from hermes_common import (fetch_book_momentum, fetch_pm_feats,  # noqa: E402
                           http_get_json)

DATA_DIR = ROOT / "model" / "data"
REG_DIR = ROOT / "model" / "registry"
SHADOW = ROOT / "data" / "ml_shadow_predictions.jsonl"
PRED_LOG = ROOT / "data" / "model_pred.log"
HOUR_MS = 3600_000
GAMMA_API = "https://gamma-api.polymarket.com"


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).astimezone(ET).strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(PRED_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def build_slug(anchor_ms: int) -> str:
    """与 bot get_current_window() 同格式的 PM slug。"""
    dt = datetime.fromtimestamp(anchor_ms / 1000, tz=timezone.utc).astimezone(ET)
    hour12 = dt.hour % 12 or 12
    return (f"bitcoin-up-or-down-{dt.strftime('%B').lower()}-{dt.day}-"
            f"{dt.year}-{hour12}{'am' if dt.hour < 12 else 'pm'}-et")


def fetch_pm_prices(slug: str, proxy: str | None) -> tuple[float | None, float | None]:
    """Gamma 当小时市场盘口（Up, Down），失败返回 (None, None)。"""
    try:
        data = http_get_json(f"{GAMMA_API}/markets?slug={slug}", timeout=10, proxy=proxy) or []
        if not data:
            return None, None
        m = data[0]
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = json.loads(m.get("outcomePrices", "[]"))
        if len(prices) >= 2:
            i_up = outcomes.index("Up") if "Up" in outcomes else 0
            return float(prices[i_up]), float(prices[1 - i_up])
    except Exception as e:
        log(f"[!] PM 盘口获取失败: {str(e)[:80]}")
    return None, None


def fetch_clob_quotes(slug: str) -> dict:
    """决策时点 CLOB 可成交价：UP/DOWN 最优 ask/bid（买按 ask、卖按 bid）。

    复用 book_snapshot.fetch_book（Gamma→clobTokenIds→CLOB book top3）。
    失败返回空 dict，不阻断主流程（exec EV 记 null，Gamma mid 口径照常）。"""
    try:
        book = fetch_book(slug)
        if not book:
            return {}

        def lvl(side: dict, key: str) -> float | None:
            rows = side.get(key) or []
            return float(rows[0][0]) if rows else None

        q: dict[str, float | None] = {}
        for s in ("up", "down"):
            q[f"ask_{s}"] = lvl(book[s], "asks")
            q[f"bid_{s}"] = lvl(book[s], "bids")
        return q
    except Exception as e:
        log(f"[!] CLOB 盘口获取失败: {str(e)[:80]}")
        return {}


def compute_ev_exec(p: float, q: dict) -> tuple[float | None, float | None]:
    """可成交口径 EV：买 UP 按 ask_up、买 DOWN 按 ask_down（含半价差成本，mid 口径的诚实版）。
    EV = p*(1-ask) - (1-p)*ask；ask 缺失（单边空盘口）时对应 EV 为 None。"""
    ask_up, ask_dn = q.get("ask_up"), q.get("ask_down")
    ev_up = round(p * (1 - ask_up) - (1 - p) * ask_up, 4) if ask_up is not None else None
    ev_dn = round((1 - p) * (1 - ask_dn) - p * ask_dn, 4) if ask_dn is not None else None
    return ev_up, ev_dn


def predict_lstm(pc: dict, anchor_ms: int, r: dict, dm: int) -> tuple[float | None, str | None]:
    """LSTM 辅助模型推理（train_lstm.py 同口径），文件缺失/异常返回 (None, None) 不阻断主流程。

    序列 = 决策点前 168 根已收盘 1h K线 × 8 特征（pc["k1h"] 列），静态 = dev_pct/momentum_burst；
    标准化用 checkpoint 内全量 fit 的 scaler（训练-推理口径一致）。
    """
    try:
        suffix = "" if dm == 15 else f"_dm{dm}"  # dm15 数据集/模型无后缀（与 LGBM 同约定）
        pt_path = REG_DIR / f"lstm{suffix}_v1.pt"
        if not pt_path.exists():
            return None, None
        import torch  # 延迟导入：无 LSTM 模型时避免拖慢计划任务启动
        from train_lstm import SEQ_FEATS, SEQ_LEN, STATIC_FEATS, BtcLstm

        ck = torch.load(pt_path, map_location="cpu")
        cfg = ck["config"]
        kf = pc["k1h"]
        mat = np.column_stack([kf[f].to_numpy() for f in SEQ_FEATS]).astype(np.float32)
        i = int(np.searchsorted(kf["open_time"].to_numpy(), anchor_ms))
        lo = i - SEQ_LEN
        if lo < 0:
            log(f"[!] LSTM 序列热身不足（{i} < {SEQ_LEN}）")
            return None, None
        seq = np.nan_to_num(mat[lo:i][None, :, :])
        static = np.nan_to_num(np.array([[r.get(f, 0.0) for f in STATIC_FEATS]],
                                        dtype=np.float32))
        seq_w = (seq - np.array(cfg["scaler_seq_mean"], dtype=np.float32)) \
            / np.array(cfg["scaler_seq_std"], dtype=np.float32)
        stat_w = (static - np.array(cfg["scaler_static_mean"], dtype=np.float32)) \
            / np.array(cfg["scaler_static_std"], dtype=np.float32)
        model = BtcLstm()
        model.load_state_dict(ck["state_dict"])
        model.eval()
        with torch.no_grad():
            p = float(torch.sigmoid(
                model(torch.from_numpy(seq_w), torch.from_numpy(stat_w))).item())
        return p, pt_path.name
    except Exception as e:
        log(f"[!] LSTM 推理失败: {str(e)[:80]}")
        return None, None


def compute_ev(p: float, pm_up: float | None, pm_down: float | None):
    """模型 EV 信号：盈亏平衡买价 = 校准胜率。
    买涨 EV = p*(1-pm_up) - (1-p)*pm_up；买跌 EV = (1-p)*(1-pm_down) - p*pm_down。
    返回 (fair_up, fair_down, ev_up, ev_down)，盘口价缺失时对应 EV 为 None。
    """
    fair_up, fair_down = round(p, 4), round(1 - p, 4)
    ev_up = round(p * (1 - pm_up) - (1 - p) * pm_up, 4) if pm_up is not None else None
    ev_down = round((1 - p) * (1 - pm_down) - p * pm_down, 4) if pm_down is not None else None
    return fair_up, fair_down, ev_up, ev_down


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC 1h 涨跌影子推理")
    ap.add_argument("--decision-min", type=int, required=True,
                    choices=(0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55),
                    help="决策时点=H+该分钟数（0=整点）；实时跑需在决策点+1分钟后")
    ap.add_argument("--hours-ago", type=int, default=0, help="0=当前小时（默认），N=回看 N 小时")
    ap.add_argument("--no-cache", action="store_true", help="绕过 K线缓存（补录防半截缓存污染）")
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args()
    dm = args.decision_min
    suffix = "" if dm == 15 else f"_dm{dm}"

    # live 运行必须绕过 K线缓存：同 anchor 小时内 dm15 先跑会留下截至 :16 的
    # 缓存（cache key 与 dm35 完全相同），dm35 在 :36 命中后缺 :35 前最新蜡烛
    # 导致 _row 返回 None。仅回看模式（hours_ago>0，数据已固定）可用缓存。
    if args.hours_ago == 0 or args.no_cache:
        fetch_klines._no_cache = True

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    anchor_ms = (now_ms - args.hours_ago * HOUR_MS) // HOUR_MS * HOUR_MS
    t_dec = anchor_ms + dm * 60_000
    if args.hours_ago == 0 and now_ms < t_dec + 60_000:
        log(f"[X] 当前 {dm} 分决策点未到（需 >= :{dm}:01），本次跳过")
        return 1

    k_end = anchor_ms + HOUR_MS
    k1h = fetch_klines("BTCUSDT", "1h", anchor_ms - 1000 * HOUR_MS, k_end, args.proxy)
    k15 = fetch_klines("BTCUSDT", "15m", anchor_ms - 1000 * 900_000, k_end, args.proxy)
    k5 = fetch_klines("BTCUSDT", "5m", anchor_ms - 1000 * 300_000, k_end, args.proxy)
    k4h = fetch_klines("BTCUSDT", "4h", anchor_ms - 500 * 4 * HOUR_MS, k_end, args.proxy)
    funding = fetch_funding(anchor_ms - 35 * 24 * HOUR_MS, k_end, args.proxy)
    fng = fetch_fng(args.proxy)
    for name, df in (("1h", k1h), ("15m", k15), ("5m", k5), ("4h", k4h)):
        if df.empty:
            log(f"[X] {name} K线拉取失败")
            return 1

    pc = _precompute(k1h, k15, k5, k4h, funding, fng)
    k1h = pc["k1h"]
    idx = int(np.searchsorted(pc["t1h"], anchor_ms))
    if idx >= len(k1h) or int(pc["t1h"][idx]) != anchor_ms:
        log(f"[X] 目标小时 K线缺失 anchor={anchor_ms}")
        return 1
    r = _row(k1h, idx, anchor_ms, t_dec, anchor_ms + HOUR_MS,
             k15, pc["k5"], pc["k4h"], pc["k4h"],
             pc["t1h"], pc["t15"], pc["t5"], pc["t4h"], pc["c5"],
             pc["fund_t"], pc["fund_v"], pc["fng_dates"], pc["fng_vals"], 0.0)
    if r is None:
        log(f"[X] 决策点特征不可用（K线未齐/热身不足）decision_min={dm}")
        return 1

    meta = json.loads((REG_DIR / f"metadata{suffix}.json").read_text(encoding="utf-8"))
    feats = meta["features"]  # 训练时实际使用的特征表（已剔除高缺失列）
    model_path = REG_DIR / f"lgbm{suffix}_v1.txt"
    booster = lgb.Booster(model_file=str(model_path))
    p_raw = float(booster.predict(pd.DataFrame([r])[feats])[0])
    cal = meta.get("calibration")
    p = float(np.interp(p_raw, cal["x"], cal["y"])) if cal else p_raw  # OOS 池化 isotonic
    if not np.isfinite(p):  # BUG FIX: NaN 概率会写进影子文件污染下游统计，兜底 0.5
        log(f"[!] v1 概率 NaN（raw={p_raw}），兜底 0.5")
        p = 0.5

    slug = build_slug(anchor_ms)
    pm_up, pm_down = (None, None) if args.hours_ago else fetch_pm_prices(slug, args.proxy)
    p_lstm, lstm_model = predict_lstm(pc, anchor_ms, r, dm)
    # 旁路盘感（读 book_snapshot.py 采集的 CLOB 快照；回看模式不读，避免窗口污染）
    book_momentum = None if args.hours_ago else fetch_book_momentum(slug, dm)

    # 盘感代理特征影子推理（A/B）：gs 模型 = 正式特征 + pm_up_dec/pm_delta
    # 读 shadow 内当小时 Pm 盘口演变（pm_up），与 train_lgbm_gs.py 同口径；
    # registry 无 gs 模型或取数失败 → p_gs=None，不阻断主流程（正式链路不变）。
    p_gs = None
    pmf = fetch_pm_feats(slug, dm)
    gs_model_path = REG_DIR / f"lgbm{suffix}_gs_v1.txt"
    if pmf and gs_model_path.exists():
        try:
            meta_gs = json.loads((REG_DIR / f"metadata{suffix}_gs.json").read_text(encoding="utf-8"))
            r_gs = dict(r)
            r_gs["pm_up_dec"] = pmf["pm_up_dec"]
            r_gs["pm_delta"] = pmf["pm_delta"]
            booster_gs = lgb.Booster(model_file=str(gs_model_path))
            p_raw_gs = float(booster_gs.predict(
                pd.DataFrame([r_gs])[meta_gs["features"]])[0])
            cal_gs = meta_gs.get("calibration")
            # 9/5 审计：校准按 kind 还原——platt=sigmoid(coef*x+intercept)，
            # 老版 isotonic=阈值表 np.interp（向后兼容：先部署本文件再重训 gs 不会崩）
            if cal_gs and cal_gs.get("kind") == "platt":
                z = cal_gs["coef"][0] * p_raw_gs + cal_gs["intercept"]
                p_gs = float(1.0 / (1.0 + np.exp(-z)))
            elif cal_gs:
                p_gs = float(np.interp(p_raw_gs, cal_gs["x"], cal_gs["y"]))
            else:
                p_gs = p_raw_gs
            if not np.isfinite(p_gs):  # BUG FIX: NaN → 回退 v1，避免污染影子文件
                log(f"[!] gs 概率 NaN（raw={p_raw_gs}），回退 v1")
                p_gs = None
        except Exception as e:
            log(f"[!] gs 影子推理失败(跳过，正式链路不受影响): {str(e)[:80]}")
            p_gs = None

    # A/B 对照：gs_nomtm（剔除动量特征版）影子推理，写 p_gs_nomtm 字段；
    # 模型缺失/失败 → None，不阻断主流程；样本积累后由 cross_validate 裁决。
    p_gs_nomtm = None
    nm_model_path = REG_DIR / f"lgbm{suffix}_gs_nomtm_v1.txt"
    if pmf and nm_model_path.exists():
        try:
            meta_nm = json.loads((REG_DIR / f"metadata{suffix}_gs_nomtm.json").read_text(encoding="utf-8"))
            r_nm = dict(r)
            r_nm["pm_up_dec"] = pmf["pm_up_dec"]
            r_nm["pm_delta"] = pmf["pm_delta"]
            booster_nm = lgb.Booster(model_file=str(nm_model_path))
            p_raw_nm = float(booster_nm.predict(
                pd.DataFrame([r_nm])[meta_nm["features"]])[0])
            cal_nm = meta_nm.get("calibration")
            if cal_nm and cal_nm.get("kind") == "platt":
                z = cal_nm["coef"][0] * p_raw_nm + cal_nm["intercept"]
                p_gs_nomtm = float(1.0 / (1.0 + np.exp(-z)))
            elif cal_nm:
                p_gs_nomtm = float(np.interp(p_raw_nm, cal_nm["x"], cal_nm["y"]))
            else:
                p_gs_nomtm = p_raw_nm
            if not np.isfinite(p_gs_nomtm):
                p_gs_nomtm = None
        except Exception as e:
            log(f"[!] gs_nomtm 影子推理失败(跳过): {str(e)[:80]}")
            p_gs_nomtm = None

    # 转正（9/4 用户拍板）：模型主输出 = 盘感模型 p_gs；gs 缺失/未训练时回退 v1
    p_model = p_gs if p_gs is not None else p
    model_name = gs_model_path.name if p_gs is not None else model_path.name

    # 模型 EV 信号（跟随主输出 p_model）：盈亏平衡买价 = 校准胜率；盘口价 ≤ 胜率才正 EV
    fair_up, fair_down, ev_up, ev_down = compute_ev(p_model, pm_up, pm_down)

    # 可成交口径（9/6 升级）：决策时点 CLOB 实时 ask/bid，买按 ask 结算 exec EV；
    # 与 Gamma mid 口径 ev_up/ev_down 并存，影子数据双口径积累供裁决时对比价差损耗。
    quotes = {} if args.hours_ago else fetch_clob_quotes(slug)
    ev_up_exec, ev_down_exec = compute_ev_exec(p_model, quotes)

    rec = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "decision_min": dm, "slug": slug,
        "anchor_time_utc": datetime.fromtimestamp(anchor_ms / 1000, tz=timezone.utc).isoformat(),
        "anchor_price": r.get("anchor_price"),
        # 转正（9/4 用户拍板）：p_model = 模型主输出 = 盘感模型 p_gs（gs 缺失时回退 v1）
        "model": model_name, "p_model": round(p_model, 4),
        "direction_model": "UP" if p_model >= 0.5 else "DOWN",
        # 以下为 v1 对照（K线纯模型），历史口径字段保持一致
        "p_raw": round(p_raw, 4),
        "p_up": round(p, 4),
        "direction": "UP" if p >= 0.5 else "DOWN",
        "p_lstm": round(p_lstm, 4) if p_lstm is not None else None,
        "lstm_model": lstm_model,
        "p_gs": round(p_gs, 4) if p_gs is not None else None,
        "p_gs_nomtm": round(p_gs_nomtm, 4) if p_gs_nomtm is not None else None,
        "pm_up_dec": round(pmf["pm_up_dec"], 4) if pmf else None,
        "pm_delta": round(pmf["pm_delta"], 4) if pmf else None,
        "pm_up": pm_up, "pm_down": pm_down,
        "fair_price_up": fair_up, "fair_price_down": fair_down,
        "ev_up": ev_up, "ev_down": ev_down,
        # 可成交口径（9/6 升级）：CLOB 实时盘口 + exec EV（买按 ask），null=获取失败/回看模式
        "clob_ask_up": quotes.get("ask_up"), "clob_bid_up": quotes.get("bid_up"),
        "clob_ask_down": quotes.get("ask_down"), "clob_bid_down": quotes.get("bid_down"),
        "ev_up_exec": ev_up_exec, "ev_down_exec": ev_down_exec,
        "dev_pct": r.get("dev_pct"), "momentum_burst": r.get("momentum_burst"),
        "book_momentum": book_momentum,
        "hours_ago": args.hours_ago,
    }

    # 幂等：同 slug+decision_min 已有记录则跳过（定时任务重触发不重复写）
    # 注：文件含 UTF-8 BOM（历史遗留），须 utf-8-sig 读，否则首行幂等判断失效
    if SHADOW.exists():
        for line in SHADOW.read_text(encoding="utf-8-sig").splitlines():
            try:
                old = json.loads(line)
                if old.get("slug") == slug and old.get("decision_min") == dm \
                        and not old.get("hours_ago"):
                    log(f"= 已存在 | {slug} | dm={dm} | p={rec['p_up']} 跳过")
                    return 0
            except json.JSONDecodeError:
                continue

    with open(SHADOW, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    edge = f" pm={pm_up}/{pm_down}" if pm_up else ""
    ex = f" ask={quotes.get('ask_up')}/{quotes.get('ask_down')}" if quotes else ""
    log(f"[OK] {slug} | dm={dm} | P(up)={p:.4f} ({rec['direction']}){edge}{ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
