#!/usr/bin/env python3
"""
engine_signal.py — 纯引擎小时信号流水线（round51.x 简化定时任务）

替代 Claude SKILL 的 AI 推理外壳，仅保留"采集→引擎判定→写信号→复盘"闭环：
  collect(root/hermes_collect.py --force)
    → 取当前小时 slug
    → signal_validator.validate(seed)  (内含 extract_factors + rule_engine.judge，方向/置信度/概率/仓位全由引擎定)
    → 回填引擎方向/置信度/概率
    -> write_signal.write_signal(out)  (追加 data/claude_signal.jsonl + 版本戳/EV/cap 校验)
    -> hermes_review.main()             (复盘保留：结算对错 + 因子归因 + 教训库)

用法：
  python3 scripts/engine_signal.py              # 正常：采集→判定→写信号→复盘
  python3 scripts/engine_signal.py --dry-run    # 只采集+判定+打印信号，绝不写队列/复盘（调试，防误下单）
  python3 scripts/engine_signal.py --skip-review# 跳过复盘，只采+判+写
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 路径：scripts/ 下的引擎模块 + 项目根下的 hermes_common/hermes_review/hermes_collect
THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path = [str(THIS_DIR), str(PROJECT_ROOT)] + [p for p in sys.path if p and p not in (str(THIS_DIR), str(PROJECT_ROOT))]

LOG_FILE = PROJECT_ROOT / "data" / "hermes_engine_signal.log"


def _log(msg: str, level: str = "I") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def _run_collect() -> tuple[int, str]:
    """调用项目根级的 hermes_collect.py --force（写 root/data，与校验器读取路径一致）。"""
    _log("采集: hermes_collect.py --force ...")
    try:
        r = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "hermes_collect.py"), "--force"],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return 1, "collect 超时(180s)"
    if r.returncode != 0:
        return r.returncode, (r.stderr or r.stdout)[-800:]
    _log(f"采集完成 rc={r.returncode}")
    return 0, (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""


def _judge_signal(slug: str):
    """引擎判定：用 signal_validator.validate 拿引擎的最终方向/置信度/概率/仓位。"""
    import signal_validator as sv
    md, source = sv.load_market_data(slug)
    if md is None:
        return None, {"skipped": f"无 {slug} 快照", "corrected": False}
    # source 必须是 bot 白名单(hermes_1h_bot.py ALLOWED_SOURCES)里的值，否则 write_signal 落盘后仍被 bot 白名单拦截。
    # engine 是纯机关真身，但为兼容 bot 消费队列，仍沿用 btc-agent 合法来源。
    seed = {"slug": slug, "source": "btc-agent", "action": "ORDER"}
    out, check = sv.validate(seed)   # 内含 extract_factors + judge 两趟 + 实时复核
    # 空 side 路径 validate 不写方向/置信度/概率，此处回填引擎输出（engine 就是事实源）
    # 9/3 P1-5 修复: engine_direction 为 None(skipped/数据缺失)时不再回填 None 落盘——
    # side=None 信号会让 bot 每 tick None.upper() 崩溃连环重试直至进程退出。
    # 现无有效方向直接返回失败，本小时不写信号（bot 按「无信号」安静等下一窗）。
    if not out.get("side"):
        _dir = check.get("engine_direction")
        if _dir not in ("UP", "DOWN"):
            return None, {"skipped": f"引擎无有效方向(engine_direction={_dir!r})", "corrected": False}
        out["side"] = out["signal"] = _dir
    if not out.get("confidence"):
        out["confidence"] = check.get("engine_confidence") or "低"
    if out.get("probability") is None and check.get("engine_probability") is not None:
        out["probability"] = round(check["engine_probability"] / 100, 3)
    # 组装可读归因串，供复盘/信号留档（替代原 AI reason）
    assump = check.get("assumptions") or []
    out["reason"] = (f"[engine] mode={check.get('mode')} 方向={check.get('engine_direction')} "
                     f"净分={check.get('engine_net', 0):+.0f} S0={check.get('engine_s0', 0):+.0f} "
                     f"PM行={check.get('engine_pm_row', 0):+.0f} "
                     f"| " + "; ".join(assump[-6:]))
    return out, check


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    skip_review = "--skip-review" in sys.argv

    from hermes_common import now_et, build_slug
    slug = build_slug(now_et())
    _log(f"引擎信号流水线启动 | slug={slug} | dry_run={dry_run}")

    # 1) 复盘上一小时（保留对错归因 + 教训库）。dry-run 不写复盘。
    if not dry_run and not skip_review:
        try:
            import hermes_review
            _log("运行复盘 hermes_review.main() ...")
            hermes_review.main()
        except Exception as e:  # 复盘失败不阻断本轮信号，留日志
            _log(f"复盘异常（不阻断）: {type(e).__name__}: {e}", "E")

    # 2) 采集当前小时
    rc, tail = _run_collect()
    if rc != 0:
        _log(f"采集失败 rc={rc}: {tail}", "E")
        return 2

    # 3) 引擎判定
    out, check = _judge_signal(slug)
    if out is None:
        _log(f"引擎判定失败: {check}", "E")
        return 2

    if dry_run:
        print(json.dumps({
            "dry_run": True,
            "slug": slug,
            "engine_check": check,
            "would_write": out,
            "note": "未写入 claude_signal.jsonl，未运行复盘（防误下单/防串场）",
        }, ensure_ascii=False, indent=2))
        _log(f"DRY-RUN 判定完成 方向={check.get('engine_direction')} 净分={check.get('engine_net')}")
        return 0

    # 4) 写信号（write_signal 内部注版本戳 + EV/cap 校验 + 引擎复验 + 落 claude_signal.jsonl）
    from write_signal import write_signal
    res = write_signal(out)
    _log(f"write_signal 结果 ok={res.get('ok')}: {json.dumps(res, ensure_ascii=False)[:400]}")
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())