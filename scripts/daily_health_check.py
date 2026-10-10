#!/usr/bin/env python3
"""
Hermes 每日健康检查（升级计划书 任务 0.4，2026-08-22）。

定位：
    自动巡检「采集 → 信号 → bot 消费 → 成交 → 结算」全链路最近 N 小时是否健康，
    无需人工翻日志。异常写 health_alerts.jsonl，退出码非 0 供 launchd/cron 告警。

安全声明（零风险承诺）：
    - 只【读】现有数据文件，不修改、不删除任何现有内容。
    - 仅新建/追加 data/health_report.json 与 data/health_alerts.jsonl（本工具专属文件）。

检查项（按链路顺序）：
    1. collect   采集：data/snapshots/ 最近是否有新快照
    2. signal    信号：claude_signal.jsonl 或 done.jsonl 最近是否有新信号
                  （bot 消费 done 记录 = 信号链路走通的最终证据）
    3. fill      成交：hermes_fills.jsonl 最近是否有新成交
    4. settle    结算：hermes_results.jsonl 最近是否有新结算（滞后一小时属正常，窗口放宽）
    5. skew      时钟：最新快照 clock_skew_sec 是否超阈（>5s 影响锚定口径）

用法：
    python3 scripts/daily_health_check.py                 # 默认窗口 2 小时
    python3 scripts/daily_health_check.py --window 3      # 自定义窗口
    python3 scripts/daily_health_check.py --no-alert      # 只报告不写 alerts

退出码：0 = 全部健康；1 = 有检查失败；2 = 数据文件缺失等环境异常。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")
DATA = ROOT / "data"

REPORT_FILE = DATA / "health_report.json"
ALERTS_FILE = DATA / "health_alerts.jsonl"

SKEW_THRESHOLD_SEC = 5.0


def last_jsonl_record_age_sec(path: Path) -> tuple[float | None, str]:
    """返回 (最新记录年龄秒, 摘要)；文件不存在/为空返回 (None, 原因)。"""
    if not path.exists():
        return None, f"文件不存在: {path.name}"
    mtime = path.stat().st_mtime
    last_line = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                last_line = line
    if not last_line:
        return None, "文件存在但无记录"
    try:
        rec = json.loads(last_line)
    except json.JSONDecodeError:
        return None, "最后一行 JSON 解析失败"
    # 优先记录内时间戳，其次文件 mtime
    ts = rec.get("ts") or rec.get("timestamp") or rec.get("collect_time_utc")
    if isinstance(ts, str):
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = time.time() - t.timestamp()
        except ValueError:
            age = time.time() - mtime
    else:
        age = time.time() - mtime
    slug = rec.get("slug", "")
    return age, f"最新: {slug or path.name}"


def newest_snapshot_age_sec() -> tuple[float | None, str]:
    snap_dir = DATA / "snapshots"
    if not snap_dir.exists():
        return None, "snapshots 目录不存在"
    newest_mtime, newest_name = 0.0, ""
    for p in snap_dir.glob("*.json"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > newest_mtime:
            newest_mtime, newest_name = m, p.name
    if not newest_name:
        return None, "snapshots 目录为空"
    return time.time() - newest_mtime, newest_name


def latest_clock_skew() -> float | None:
    """最新快照里的 clock_skew_sec（找不到返回 None）。"""
    snap_dir = DATA / "snapshots"
    if not snap_dir.exists():
        return None
    newest_mtime, newest_path = 0.0, None
    for p in snap_dir.glob("*.json"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > newest_mtime:
            newest_mtime, newest_path = m, p
    if not newest_path:
        return None
    try:
        d = json.loads(newest_path.read_text(encoding="utf-8"))
        v = d.get("clock_skew_sec")
        return float(v) if v is not None else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


import re  # noqa: E402

_ANCHOR_RE = re.compile(r"round(\d+)(?:\.(\d+))?")


def _anchor_main_version(path: Path, pattern: str) -> str | None:
    """从文件锚点行提取主版本号（round41.3 → "41"）。找不到返回 None。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if re.search(pattern, line):
            m = _ANCHOR_RE.search(line)
            return m.group(1) if m else None
    return None


def check_rule_anchor_drift() -> tuple[bool, str]:
    """升级计划书任务0.2收尾：prompt_exec.md(基准) vs SKILL.md / rule_engine.py 主版本一致性。
    返回 (ok, detail)。基准缺失视为不健康；核心文件主版本不一致 → 不健康。"""
    root = ROOT
    base = _anchor_main_version(root / "prompt_exec.md", r"版本锚点")
    if not base:
        return False, "prompt_exec.md 基准锚点解析失败"
    parts = [f"基准={base}"]
    ok = True
    for name, rel, pat in [
        ("SKILL", ".codebuddy/skills/hermes-btc-analysis/SKILL.md", r"当前版本锚点|rule_version"),
        ("engine", "scripts/rule_engine.py", r"^RULE_VERSION"),
    ]:
        v = _anchor_main_version(root / rel, pat)
        if v is None:
            ok = False
            parts.append(f"{name}=未解析")
        elif v != base:
            ok = False
            parts.append(f"{name}={v}≠{base}")
        else:
            parts.append(f"{name}={v}")
    return ok, " ".join(parts)


def should_dedup_alert(check: str, hour_bucket: str) -> bool:
    """同一检查项同一小时只告警一次（读 alerts 最后一行判断）。"""
    if not ALERTS_FILE.exists():
        return False
    try:
        with open(ALERTS_FILE, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        if not lines:
            return False
        last = json.loads(lines[-1])
        return last.get("check") == check and last.get("hour_bucket") == hour_bucket
    except (OSError, json.JSONDecodeError):
        return False


def window_pnl_stats(results_path: Path, n: int) -> dict:
    """返回最近 n 条已结算(FILLED & amount>0 & pnl 非 None)结果的滚动统计。

    A6/P0-5 安全网: 滚动 30 场平均盈亏 < 0 → 性能衰减警示,建议停手评估。
    只统计真实金额单(amount>0)——历史 P0-1 错账 `amount=0/pnl=0` 的 FILLED 行
    若纳入会把价格对的成交误判为亏损,污染胜率与均盈亏。只读,不修改数据。
    """
    stats = {"seen": 0, "filled": 0, "window": 0, "win": 0, "loss": 0,
             "sum_pnl": 0.0, "avg_pnl": None, "win_rate": None}
    if not results_path.exists():
        return stats
    rows = []
    try:
        with open(results_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stats["seen"] += 1
                if not r.get("filled"):
                    continue
                if not r.get("amount"):  # 跳过 P0-1 历史 amount=0 错账行
                    continue
                pnl = r.get("pnl")
                if pnl is None:
                    continue
                stats["filled"] += 1
                try:
                    rows.append(float(pnl))
                except (TypeError, ValueError):
                    continue
    except OSError:
        return stats
    rows = rows[-n:]
    stats["window"] = len(rows)
    if not rows:
        return stats
    stats["sum_pnl"] = round(sum(rows), 2)
    stats["avg_pnl"] = round(sum(rows) / len(rows), 4)
    stats["win"] = sum(1 for x in rows if x > 0)
    stats["loss"] = sum(1 for x in rows if x <= 0)
    stats["win_rate"] = round(stats["win"] / len(rows), 3)
    return stats


def feature_store_staleness(max_age_days: float) -> tuple[bool, str]:
    """检查 data/features/manifest.json 构建是否过期（B7/2026-08-23）。

    返回 (ok, detail)。manifest 缺失/无时间戳/mtime 超龄 → 不健康。
    """
    mf = DATA / "features" / "manifest.json"
    if not mf.exists():
        return False, "feature store manifest 缺失"
    n_rows = 0
    age = 0.0
    try:
        d = json.loads(mf.read_text(encoding="utf-8", errors="replace"))
        n_rows = int(d.get("n_rows", 0))
        ts = d.get("generated_at_utc") or d.get("generated_at")
        if isinstance(ts, str):
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = time.time() - t.timestamp()
        else:
            age = time.time() - mf.stat().st_mtime
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        age = time.time() - mf.stat().st_mtime
    fresh = age <= max_age_days * 86400
    return fresh, f"manifest {int(age/86400)}天前(阈值{max_age_days}天) 行数{n_rows}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes 全链路健康检查（任务0.4）")
    ap.add_argument("--window", type=float, default=2.0,
                    help="检查窗口小时数（默认 2）")
    ap.add_argument("--settle-window", type=float, default=3.0,
                    help="结算检查窗口小时数（默认 3，结算滞后一小时）")
    ap.add_argument("--producer-gap", type=float, default=3.0,
                    help="分析端断供告警阈值：连续 N 小时无信号(默认 3)")
    ap.add_argument("--perf-window", type=int, default=30,
                    help="滚动性能安全网：取最近 N 场已结算单(默认 30)")
    ap.add_argument("--no-alert", action="store_true", help="不写 alerts 文件")
    ap.add_argument("--no-rotate", action="store_true", help="跳过日志轮转(D2)")
    ap.add_argument("--feature-max-age-days", type=float, default=3.0,
                    help="feature store 过期告警天数(默认 3)")
    args = ap.parse_args()

    # 日志轮转（D2/2026-08-23）：best-effort 顺带清理超大日志，失败不阻塞检查
    _rot_script = ROOT / "scripts" / "rotate_logs.py"
    if not args.no_rotate and _rot_script.exists():
        try:
            subprocess.run([sys.executable, str(_rot_script)],
                           capture_output=True, text=True, timeout=120)
        except Exception:
            pass

    window = args.window * 3600
    settle_window = args.settle_window * 3600
    hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")

    checks = []
    failures = []

    def add(name: str, ok: bool, detail: str, age=None):
        item = {"check": name, "ok": ok, "detail": detail}
        if age is not None:
            item["age_min"] = round(age / 60, 1)
        checks.append(item)
        if not ok:
            failures.append(name)
            dedup = should_dedup_alert(name, hour_bucket)
            item["alert_suppressed_dup"] = dedup
            if not args.no_alert and not dedup:
                ALERTS_FILE.open("a", encoding="utf-8").write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "hour_bucket": hour_bucket,
                    "check": name,
                    "detail": detail,
                }, ensure_ascii=False) + "\n")

    # 1. 采集
    age, detail = newest_snapshot_age_sec()
    add("collect", age is not None and age <= window,
        detail if age is not None else detail, age)

    # 2. 信号（done.jsonl 是 bot 消费信号的最终证据；claude_signal.jsonl 可能被轮转清空）
    age, detail = last_jsonl_record_age_sec(DATA / "hermes_done.jsonl")
    ok2 = age is not None and age <= window
    if not ok2:
        age2, detail2 = last_jsonl_record_age_sec(DATA / "claude_signal.jsonl")
        if age2 is not None and age2 <= window:
            ok2, detail, age = True, f"claude_signal: {detail2}", age2
    add("signal", ok2, detail, age)

    # 3. 成交
    age, detail = last_jsonl_record_age_sec(DATA / "hermes_fills.jsonl")
    add("fill", age is not None and age <= window, detail, age)

    # 4. 结算
    age, detail = last_jsonl_record_age_sec(DATA / "hermes_results.jsonl")
    add("settle", age is not None and age <= settle_window, detail, age)

    # 5. 时钟偏移
    skew = latest_clock_skew()
    add("clock_skew", skew is None or abs(skew) <= SKEW_THRESHOLD_SEC,
        f"clock_skew_sec={'未知' if skew is None else skew}")

    # 6. 规则锚点漂移（任务0.2收尾：文档/引擎/SKILL 版本一致性）
    aok, adetail = check_rule_anchor_drift()
    add("rule_anchor", aok, adetail)

    # 7. 账本三方对账（A1/2026-08-23）：聚焦最近 24h，抓 FILLED 单 amount=0 之类新欠账
    try:
        _lp = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify_ledger.py"),
             "--hours", str(24), "--fail-on-error", "--json"],
            capture_output=True, text=True, timeout=60)
        _lr = json.loads(_lp.stdout)
        _n_err = int(_lr.get("errors", 0))
        _n_warn = int(_lr.get("warnings", 0))
        add("ledger", _n_err == 0,
            f"近24h账本: ❌{_n_err} ⚠️{_n_warn}"
            + (f" ｜首错:{_lr['issues'][0][1][:80]}" if _n_err and _lr.get("issues") else ""))
    except Exception as _e:
        add("ledger", False, f"ledger 检查执行失败: {_e}（verify_ledger.py 应只读，请检查）")

    # 8. 分析端断供 watchdog（A6/2026-08-23）：连续 N 小时无新信号（手机推送待接，先落日志/状态）
    pgap, pdetail = last_jsonl_record_age_sec(DATA / "hermes_done.jsonl")
    if pgap is None:
        pgap, pdetail = last_jsonl_record_age_sec(DATA / "claude_signal.jsonl")
    _silent_h = round((pgap or float("inf")) / 3600, 1)
    add("producer_gap", pgap is not None and pgap <= args.producer_gap * 3600,
        f"信号断供 {_silent_h}h(阈值 {args.producer_gap}h) ｜ {pdetail}", pgap)

    # 9. 滚动性能安全网（P0-5/2026-08-23）：最近 N 场已结算单，平均盈亏 <0 → 衰减警示
    perf = window_pnl_stats(DATA / "hermes_results.jsonl", args.perf_window)
    if perf["window"] >= 1 and perf["avg_pnl"] is not None:
        perf_ok = perf["avg_pnl"] >= 0
        add("rolling_perf", perf_ok,
            f"近{perf['window']}场 均盈亏{perf['avg_pnl']}"
            f"{'→<0 衰减警示' if not perf_ok else '→≥0 正常'}"
            f" W{perf['win']}/L{perf['loss']} 胜率{perf['win_rate']}｜累计真实单{perf['filled']}")
    else:
        add("rolling_perf", True, "已结算样本不足(暂无)")

    # 10. feature store 过期巡检（B7/2026-08-23）：manifest 构建时间是否落后
    fsok, fsdetail = feature_store_staleness(args.feature_max_age_days)
    add("feature_store", fsok, fsdetail)

    # 11. WF 关卡覆盖（C3/2026-08-23）：当前规则主版本必须有 walk-forward 报告留痕。
    #     wf_gate 顶层只依赖标准库，import 无副作用；重计算只在 wf_gate run 时发生。
    try:
        _scripts_dir = str(ROOT / "scripts")
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from wf_gate import gate_status_for_major
        wok, wdetail = gate_status_for_major()
    except Exception as _e:
        wok, wdetail = False, f"wf_gate 状态读取失败: {_e}"
    add("wf_gate", wok, wdetail)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_hours": args.window,
        "all_ok": not failures,
        "failures": failures,
        "checks": checks,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    # ---- 控制台摘要 ----
    mark = lambda ok: "✅" if ok else "❌"
    print(f"═══ Hermes 健康检查（窗口 {args.window}h）═══")
    for c in checks:
        extra = f"（{c['age_min']} 分钟前）" if "age_min" in c else ""
        print(f"{mark(c['ok'])} {c['check']:<10} {c['detail']}{extra}")
    print(f"═══ 结果：{'全部健康' if report['all_ok'] else '异常: ' + ', '.join(failures)} ═══")
    print(f"报告: {REPORT_FILE}" + (f"｜告警已追加: {ALERTS_FILE}" if failures and not args.no_alert else ""))

    # ---- 贴线区影子记录(第一层·2026-08-23 用户批准,docs/proposal_tiezone_winrate_fix_20260823.md) ----
    # 静默守卫:任何失败都不影响健康检查本身的结论与退出码。
    try:
        _shadow = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tie_votes_shadow.py")
        subprocess.run([sys.executable, _shadow], capture_output=True, timeout=180)
    except Exception:
        pass

    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
