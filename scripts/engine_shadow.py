#!/usr/bin/env python3
"""engine_shadow.py — 引擎多决策时刻影子判定（round55，与模型 dm0~dm55 对齐）。

背景：正式引擎 HermesBTC1HEngine 每个整点只跑一次（:40 采集→判定→下单），历史只有 :40
一个时刻的引擎真身数据。为对比「引擎在哪个决策时刻分析更准」，本脚本在每个决策时点
（:01/:06/:11/.../:56，对应 dm0/dm5/.../dm55）独立采集当前时刻数据并做引擎判定，
只写 data/engine_shadow.jsonl 留档——绝不写真实信号、不下单、不触 bot、不跑复盘。

与正式引擎判定完全同源：hermes_collect.collect() 采集 + signal_validator._validate_with_md()
判定（validate() 内部就是 _validate_with_md）。差异仅为：
  - 采集输出写到独立文件 data/engine_shadow_md/{decision_min}_{slug}_{ts}.json，
    通过运行时替换 hermes_collect.OUTPUT 实现，不污染正式 data/hermes_market_data.json；
  - 判定使用本时刻采集的 md（validate() 只读正式单文件，所以这里直接调 _validate_with_md）。

用法：
  python scripts/engine_shadow.py --decision-min 40        # dm40 时刻影子判定（写盘）
  python scripts/engine_shadow.py --decision-min 40 --dry  # 只打印不写盘（调试）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path = [str(THIS_DIR), str(PROJECT_ROOT)] + [p for p in sys.path if p and p not in (str(THIS_DIR), str(PROJECT_ROOT))]

import hermes_collect  # noqa: E402
import signal_validator as sv  # noqa: E402
from hermes_common import now_et, build_slug  # noqa: E402

SHADOW_LOG = PROJECT_ROOT / "data" / "engine_shadow.jsonl"
SHADOW_MD_DIR = PROJECT_ROOT / "data" / "engine_shadow_md"
SHADOW_LOG_FILE = PROJECT_ROOT / "data" / "engine_shadow.log"


def _log(msg: str, level: str = "I") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    try:  # 日志文件留档（pythonw 无控制台环境下 print 可能抛 OSError 导致脚本退出）
        with SHADOW_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except OSError:
        pass


def _read_md_file(path: Path) -> str:
    """读影子快照：新采集为 UTF-8；旧版 collect 默认 GBK 写盘（含中文 warning 时），
    二进制探测回退兼容，避免 UnicodeDecodeError。"""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def _collect_md(slug: str, decision_min: int) -> dict | None:
    """独立采集当前时刻数据到独立文件，返回 md dict（失败 None）。"""
    SHADOW_MD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    out = SHADOW_MD_DIR / f"dm{decision_min:02d}_{slug[-36:]}_{stamp}.json"
    # 运行时替换模块级 OUTPUT，采集写到影子独立文件（collect 内部所有 OUTPUT 引用均生效）
    hermes_collect.OUTPUT = out
    ok = hermes_collect.collect(force=True, slim=False)
    if not ok or not out.exists():
        _log(f"采集失败（dm{decision_min:02d}）", "E")
        return None
    try:
        md = json.loads(_read_md_file(out))
    except (OSError, json.JSONDecodeError) as e:
        _log(f"影子快照读取失败: {e}", "E")
        return None
    md["_shadow_out"] = str(out)
    return md


def _judge(md: dict, slug: str) -> tuple[dict, dict]:
    """引擎判定（与 engine_signal 同源：seed btc-agent → _validate_with_md）。"""
    seed = {"slug": slug, "source": "btc-agent", "action": "ORDER"}
    return sv._validate_with_md(seed, md, md.get("_shadow_out", ""))


def _prob(v: object) -> float | None:
    """统一概率口径为 0~1 小数（engine_probability 源值是百分比，如 71.4）。"""
    if v is None:
        return None
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return round(p / 100, 4) if p > 1 else round(p, 4)


def _to_utc_iso(v: object) -> str | None:
    """把带时区 offset 的 iso 时间戳统一转 UTC ISO（md.timestamp 源是 ET 偏移串）。"""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return str(v)


def _pm_price(md: dict, key: str) -> float | None:
    """安全取 Polymarket 价格（polymarket 键可能缺失或为 None）。"""
    pm = md.get("polymarket")
    if not isinstance(pm, dict):
        return None
    return pm.get(key)


def main() -> int:
    parser = argparse.ArgumentParser(description="引擎多决策时刻影子判定")
    parser.add_argument("--decision-min", type=int, default=40, help="决策时刻分钟，0~55 的倍数，如 5/15/40/55")
    parser.add_argument("--dry", action="store_true", help="只打印不写盘")
    args = parser.parse_args()

    dm = args.decision_min
    if dm % 5 != 0 or not (0 <= dm <= 55):
        _log(f"非法 decision_min={dm}（需 0~55 的 5 的倍数）", "E")
        return 2

    slug = build_slug(now_et())
    _log(f"引擎影子启动 | dm{dm:02d} | slug={slug} | dry={args.dry}")

    md = _collect_md(slug, dm)
    if md is None:
        return 2

    out, check = _judge(md, slug)
    # 与 engine_signal._judge_signal 相同的方向/置信度回填口径；概率统一 _prob 转 0~1 小数
    if not out.get("side"):
        _dir = check.get("engine_direction")
        if _dir not in ("UP", "DOWN"):
            _log(f"引擎无有效方向（engine_direction={_dir!r}），本时点留档 skipped", "W")
            rec_side, rec_conf, rec_prob = None, None, None
        else:
            out["side"] = out["signal"] = _dir
            rec_side, rec_conf = _dir, check.get("engine_confidence") or "低"
    else:
        rec_side = out.get("side")
        rec_conf = out.get("confidence") or check.get("engine_confidence") or "低"
    rec_prob = None if rec_side is None else _prob(
        out.get("probability") if out.get("probability") is not None else check.get("engine_probability"))

    rec = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "decision_min": dm,
        "slug": slug,
        "anchor_time_utc": _to_utc_iso(md.get("timestamp")),
        "anchor_price": md.get("anchor_price"),
        "current_price": md.get("current_price"),
        "side": rec_side,
        "confidence": rec_conf,
        "probability_raw": rec_prob,
        "engine_net": check.get("engine_net"),
        "engine_s0": check.get("engine_s0"),
        "engine_pm_row": check.get("engine_pm_row"),
        "mode": check.get("mode"),
        "snapshot_source": check.get("snapshot_source"),
        "pm_up": _pm_price(md, "up_price"),
        "pm_down": _pm_price(md, "down_price"),
        "skipped": check.get("skipped"),
        "reason": out.get("reason", "")[:600],
    }

    if args.dry:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0

    with SHADOW_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _log(f"已落盘 engine_shadow.jsonl  dm{rec['decision_min']:02d} side={rec['side']} conf={rec['confidence']} "
         f"P={rec['probability_raw']} net={(rec['engine_net'] or 0):+.0f} s0={(rec['engine_s0'] or 0):+.0f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # 定时任务环境 traceback 走 stderr 不可见，必须落盘留档
        import traceback
        try:
            with SHADOW_LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now(timezone.utc).isoformat()}] [FATAL] {e}\n")
                f.write(traceback.format_exc())
        except OSError:
            pass
        sys.exit(1)