#!/usr/bin/env python3
"""
信号写入器(tool / 定时任务调用,带重试)

用法:
  write_signal.py "{...}"

行为:
  - 追加到 data/claude_signal.jsonl (bot 8/16 v4 消费队列,同 slug 去重,8/8 修复)
  - 同步更新 hermes_analysis.json + hermes_hourly_prediction.json
  - 失败重试 5 次(2s/4s/8s/16s/32s)
  - 都失败报错

来源: Claude 桌面端定时任务(原 OpenClaw cron agentTurn 已弃用)调 write_signal 写信号
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import fcntl  # D4: 同 slug 读写加独占锁，与 bot 侧 clear_queue 的 fcntl 对称
except ImportError:  # Windows 兜底（本项目运行于 macOS/Linux，基本不触发）
    fcntl = None

# 8/18 修复: bot 消费队列已从 /tmp/hermes_signal.jsonl 迁移到 data/claude_signal.jsonl (8/16 v4)。
# 原 SIGNAL 指向 /tmp 导致 bot 读不到信号整小时漏单。改为与 bot hermes_1h_bot.py:32 QUEUE 一致。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SIGNAL = PROJECT_ROOT / "data" / "claude_signal.jsonl"

def _dash_path(name: str) -> Path:
    """仪表盘文件路径：优先部署路径；本地开发环境回退到仓库 data/ 目录。

    部署路径可能在不同 session 之间被锁（PermissionError），
    此时退回到当前 session 的项目 data/ 目录，避免阻塞信号写入。
    """
    local = Path(__file__).resolve().parent.parent / "data" / name
    # 8/18 优化: 动态探测当前活跃 session 的部署目录，不再写死单一 session 名
    #（每次任务运行的 session 目录名是动态生成的，写死会导致一直回退到 local）
    deploy_candidates = sorted(Path("/sessions").glob("*/mnt/hermes btc/data"))
    for cand in deploy_candidates:
        deploy = cand / name
        try:
            if deploy.parent.exists():
                # 可写性二次确认：能 resolve 不代表能 write
                with open(deploy.parent / ".hermes_write_test", "w", encoding="utf-8") as _t:
                    _t.write("ok")
                (deploy.parent / ".hermes_write_test").unlink()
                return deploy
        except (OSError, PermissionError):
            # 该部署目录不可写（最常见：不同 session 之间的权限隔离），尝试下一个
            continue
    return local

ANALYSIS = _dash_path("hermes_analysis.json")
PREDICTION = _dash_path("hermes_hourly_prediction.json")

# 8/21 A3: 策略/模型版本戳。数据源 data/hermes_runtime_meta.json（人工维护，规则轮次或模型切换时更新）。
# 目的：所有统计按 strategy_version 分段，终结"跨 36 个策略版本的混合归因"；model_version 留痕策略漂移事件。
RUNTIME_META = PROJECT_ROOT / "data" / "hermes_runtime_meta.json"

def _runtime_meta() -> dict:
    try:
        return json.loads(RUNTIME_META.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _inject_runtime_meta(signal_dict: dict) -> dict:
    """8/21 A3: 版本戳注入（缺失才补，不覆盖显式传入值）。write_signal 与 --dry-run 共用。"""
    _meta = _runtime_meta()
    if "strategy_version" not in signal_dict and _meta.get("strategy_version"):
        signal_dict["strategy_version"] = _meta["strategy_version"]
    if "model_version" not in signal_dict and _meta.get("model_version"):
        signal_dict["model_version"] = _meta["model_version"]
    return signal_dict

MAX_RETRIES = 5  # 8/3 BOSS 决定:5 次重试(2s/4s/8s/16s/32s)


def write_signal(signal_dict):
    """写信号 + dashboard 文件,带重试"""
    # Bot 兼容: 确保 side 字段存在且合法（bot 读 side, 不是 signal）。
    # 9/3 P1-5 修复: 原实现 "side" not in 判断对「键存在值为 None」失效(side=None 落盘 →
    # bot 每 tick None.upper() 崩溃连环重试 60 次进程退出)，且 side/signal 双缺省时
    # 默认 "UP" 属危险默认(方向字段禁止编造)。现规范化+校验，非法直接拒绝写入。
    _side = signal_dict.get("side") or signal_dict.get("signal")
    _side = str(_side).strip().upper() if _side is not None else ""
    if _side not in ("UP", "DOWN"):
        raise ValueError(f"signal_dict side 非法: {signal_dict.get('side')!r} (合法 UP/DOWN，禁止默认方向)")
    signal_dict["side"] = signal_dict["signal"] = _side
    # 标记来源: 8/8 修复 - 默认 btc-agent,防止上游漏带 source 时落到旧默认值导致监控漏检
    if "source" not in signal_dict:
        signal_dict["source"] = "btc-agent"
    # 8/21 A3: 版本戳注入（缺失才补，不覆盖显式传入值）。落档 done.jsonl/results，供分段统计。
    _inject_runtime_meta(signal_dict)
    # Bot 必需字段补全(action/signal/slug 缺一即被 bot 拒收,8/4 18:19 BOSS 修复)
    # 8/5 BOSS 指令: 分析确认方向即写 ORDER,no_trade 不再强制 SKIP
    if "action" not in signal_dict:
        signal_dict["action"] = "ORDER"
    if "signal" not in signal_dict and "side" in signal_dict:
        signal_dict["signal"] = signal_dict["side"]
    if "slug" not in signal_dict or not signal_dict["slug"]:
        raise ValueError("signal_dict 缺少 slug 字段")
    # 8/10 规范: 信号禁止携带 amount(prompt.md 5.6),基础金额由 bot 读 UI bet_amount;
    # 仓位缩放只能走结构化 position_scale 字段(1.0 / 0.5 / "min")
    signal_dict.pop("amount", None)
    _ps = signal_dict.get("position_scale")
    if _ps is not None:
        _ok = False
        if isinstance(_ps, str) and _ps.strip().lower() == "min":
            signal_dict["position_scale"] = "min"
            _ok = True
        else:
            try:
                _psf = float(_ps)
                if 0 < _psf <= 1.0:
                    signal_dict["position_scale"] = _psf
                    _ok = True
            except (TypeError, ValueError):
                pass
        if not _ok:
            print(f"⚠️ 非法 position_scale={_ps!r} 已丢弃(合法值: 1.0/0.5/\"min\")", file=sys.stderr)
            signal_dict.pop("position_scale", None)

    # round31: EV 仅日志，不设 SKIP 门槛（分析确认方向即写 ORDER）
    # 仍保留 EV 公式错误检查(|ev|>400 拒收),防止公式写错。单位铁律(round41.8.2):
    # ev 全链路统一 = 每$100本金期望美元(=百分点),如 16.8;机器层回写同口径
    if signal_dict.get("action") == "ORDER":
        ev_raw = signal_dict.get("ev")
        if ev_raw is not None:
            try:
                ev_val = float(ev_raw)
                if abs(ev_val) > 400:
                    raise ValueError(f"ev={ev_val} 超出 ±400 范围(每$100美元口径) — 疑似公式错误(payout应为1-max_price)")
            except (TypeError, ValueError) as e:
                if "ev=" in str(e):
                    raise
        # EV < 5% 不拦截（round31：EV 仅日志），记录供日志参考但不改 action
        if ev_raw is not None:
            try:
                ev_val = float(ev_raw)
                if ev_val < 5:
                    print(f"ℹ️ ev={ev_val:.2f} < $5/每$100 (BOSS 8/5: 仍写 ORDER,不拦截)", file=sys.stderr)
            except (TypeError, ValueError):
                pass

    # 8/11 防错补丁: 实盘引擎校验层（signal_validator → rule_engine 复算，向保守方向修正）。
    # fail-open 铁律: 校验层任何异常/数据缺失都不得阻塞信号写入，仅 risk 标注。
    engine_check = None
    try:
        from signal_validator import validate as _engine_validate
        signal_dict, engine_check = _engine_validate(signal_dict)
    except Exception as e:  # noqa: BLE001 - 双重 fail-open: import/运行异常同样放行
        engine_check = {"skipped": f"校验层异常 {type(e).__name__}: {e}", "corrected": False}
    if engine_check.get("skipped"):
        _note = f"⚠️引擎校验跳过：{engine_check['skipped']}"
        signal_dict["risk"] = f"{signal_dict['risk']}; {_note}" if signal_dict.get("risk") else _note
        print(f"ℹ️ {_note}（slug={signal_dict.get('slug')}）", file=sys.stderr)
    elif engine_check.get("corrected"):
        print(f"⚠️ 引擎校验修正（{signal_dict.get('slug')}）: {engine_check.get('correction_detail')}", file=sys.stderr)

    # round50 scoring_compare：构造 engine vs AI 评分对比块，落盘 result/scoring_compare/{ts}.json
    # 注入 signal_dict["scoring_compare"] 子字段（不动原字段，bot 不识别则忽略）。
    # fail-open：异常仅 stderr 警告，不阻塞信号写入。
    try:
        from scoring_compare import attach_and_persist as _sc_compare
        signal_dict = _sc_compare(signal_dict, engine_check)
    except Exception as _e:  # noqa: BLE001 - 与 validator/round36 一致纪律
        print(f"⚠️ scoring_compare 异常（放行）: {_e}", file=sys.stderr)

    # A4(2026-08-23) 兜底:ORDER 信号必须带 max_price/ev，缺则补标准封顶 0.60 而非留空让 bot 兜 0.69。
    # 与 signal_validator 出口一致；此处双保险（validator 被 skip/未加载时仍保完整）。
    if signal_dict.get("action") == "ORDER":
        try:
            if not isinstance(signal_dict.get("max_price"), (int, float)) or not (0 < float(signal_dict["max_price"]) < 1):
                signal_dict["max_price"] = 0.60
                print(f"⚠️ 缺 max_price，补标准封顶 0.60（slug={signal_dict.get('slug')}）", file=sys.stderr)
            if not isinstance(signal_dict.get("ev"), (int, float)):
                _p = signal_dict.get("probability")
                if isinstance(_p, (int, float)):
                    _pf = float(_p) / 100.0 if _p > 1 else float(_p)
                    signal_dict["ev"] = round((_pf - float(signal_dict["max_price"])) * 100, 2)
                    print(f"⚠️ 缺 ev，按 probability-封顶 重算 = {signal_dict['ev']}（每$100美元口径，slug={signal_dict.get('slug')}）", file=sys.stderr)
        except (TypeError, ValueError):
            pass  # 极端异常不放碍写入（fail-open 纪律）

    # round36（8/21）R2+R3：概率上报天花板 + 过热标注。均仅日志层——不改方向、不改挂价
    # （§5.4 封顶 0.60 不变）、不改金额（position_scale 仅日志，8/12 决策）。
    # 依据：模型宣称 75%+ 实测方向胜率 ~67%（8/13 审计概率上限 66% + 8/21 round36 校准 67%，双源一致）；
    # edge = probability_raw − pm_<side>_price ≥20pt 为过热区（实测胜率 50%，n=12）。
    # 任何异常放行（fail-open，与校验层同纪律）。
    try:
        _p = signal_dict.get("probability")
        if _p is not None:
            _pf = float(_p)
            if _pf > 1:  # 兜底：validator 已规范化小数，此处防百分比直传
                _pf = _pf / 100.0
            if 0 < _pf <= 1:
                if "probability_raw" not in signal_dict:
                    signal_dict["probability_raw"] = round(_pf, 4)
                if _pf > 0.67:
                    signal_dict["probability"] = 0.67
                    _note = f"⚠️round36 R2 概率天花板：{_pf:.2f}→0.67（实测校准上限，8/13+8/21 双源）"
                    try:
                        _mxf = float(signal_dict.get("max_price"))
                        if 0 < _mxf <= 1:
                            signal_dict["ev_raw"] = signal_dict.get("ev")
                            # ev 小数口径（P−price，与 bot ±2.0 校验/write_signal 0.05 阈值同口径）
                            signal_dict["ev"] = round((0.67 - _mxf) * 100, 2)
                            _note += f"，EV 按校准概率重算={signal_dict['ev']}（每$100美元口径）"
                    except (TypeError, ValueError):
                        pass
                    signal_dict["risk"] = f"{signal_dict['risk']}; {_note}" if signal_dict.get("risk") else _note
        # R3：过热标注（edge≥20pt）→ risk 强制标注 + position_scale≤0.5（仅日志）
        _pr = signal_dict.get("probability_raw", signal_dict.get("probability"))
        _side = (signal_dict.get("side") or signal_dict.get("signal") or "").upper()
        _pmk = "pm_down_price" if _side == "DOWN" else "pm_up_price"
        try:
            _ed = float(_pr) - float(signal_dict.get(_pmk))
        except (TypeError, ValueError):
            _ed = None
        if _ed is not None and _ed >= 0.20:
            _hot = (f"⚠️round36 R3 模型过热区（edge {_ed*100:.0f}pt，实测胜率仅50% n=12），"
                    f"position_scale=0.5（仅日志，金额不变）")
            signal_dict["risk"] = f"{signal_dict['risk']}; {_hot}" if signal_dict.get("risk") else _hot
            _cur = signal_dict.get("position_scale")
            _is_min = isinstance(_cur, str) and _cur.strip().lower() == "min"
            try:
                _need = _cur is None or float(_cur) > 0.5
            except (TypeError, ValueError):
                _need = not _is_min
            if _need and not _is_min:
                signal_dict["position_scale"] = 0.5
    except Exception as _e:  # noqa: BLE001 - fail-open：round36 层异常绝不阻塞信号
        print(f"⚠️ round36 R2/R3 处理异常(放行): {_e}", file=sys.stderr)

    # A5(2026-08-23): 高概率通道封顶显式化——praw≥0.70 时把通道价写进信号 price_cap 字段，
    # 让 bot 的「高概率通道提价」变得显式、可复盘可归因。
    # max_price 仍保持 0.60（基础封顶），price_cap 是通道覆盖值，bot 优先取之。
    # 9/3 P2-12 修复: 通道价 0.70 → 0.80，与 bot 侧 HIGH_PROB_CAP=0.80（9/2 调整）口径统一——
    # 原实现两档并存，信号带 price_cap 时 0.70、缺失时 bot 用 0.80，行为依赖字段存在性。
    try:
        if signal_dict.get("action") == "ORDER" and signal_dict.get("price_cap") is None:
            _pr = signal_dict.get("probability_raw")
            if not isinstance(_pr, (int, float)):
                _pr = signal_dict.get("probability")
            if isinstance(_pr, (int, float)):
                _prf = float(_pr) / 100.0 if float(_pr) > 1 else float(_pr)
                if _prf >= 0.70:
                    signal_dict["price_cap"] = 0.80
                    print(f"🔵 高概率通道：praw={_prf:.3f}≥0.70 → price_cap=0.80（显式写入）"
                          f"（slug={signal_dict.get('slug')}）", file=sys.stderr)
    except Exception:  # noqa: BLE001 - fail-open
        pass

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # 1. 写入 signal.jsonl (8/8: 同 slug 去重,只保留最新一条)
            # D4(2026-08-23): read-modify-write + append 全程持 fcntl 独占锁，
            #   锁 bot 侧 clear_queue 相同的 `<文件>.lock`，与 bot fcntl 对称，
            #   杜绝 write_signal ↔ bot 清队列互相覆盖丢信号的窗口。
            _slug = signal_dict.get("slug", "")
            _lock_fd = os.open(str(SIGNAL.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                if fcntl is not None:
                    fcntl.flock(_lock_fd, fcntl.LOCK_EX)
                _fd = os.open(str(SIGNAL), os.O_RDWR | os.O_CREAT | os.O_APPEND)
                try:
                    # 读当前内容（同一持锁 fd，避免其他进程在锁期间改 inode）
                    try:
                        _current = os.read(_fd, 64 << 20).decode("utf-8", errors="replace")
                    except OSError:
                        _current = ""
                    if _slug:
                        _lines = []
                        for _line in _current.splitlines():
                            if not _line.strip():
                                continue
                            try:
                                _old = json.loads(_line)
                                if _old.get("slug", "") != _slug:
                                    _lines.append(_line)
                            except json.JSONDecodeError:
                                _lines.append(_line)
                        _lines.append(json.dumps(signal_dict, ensure_ascii=False))
                        # 原子回写：截断 + 重写（容忍最终一行没有换行符）
                        os.ftruncate(_fd, 0)
                        os.lseek(_fd, 0, os.SEEK_SET)
                        os.write(_fd, ("\n".join(_lines) + "\n").encode("utf-8"))
                    else:
                        os.write(_fd, (json.dumps(signal_dict, ensure_ascii=False) + "\n").encode("utf-8"))
                finally:
                    os.close(_fd)
            finally:
                if fcntl is not None:
                    fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                os.close(_lock_fd)

            # 2. 同步写 dashboard 文件
            direction = signal_dict.get("signal", "UP")
            analysis = {
                "timestamp": signal_dict.get("timestamp", datetime.now(timezone.utc).isoformat()),
                "slug": signal_dict.get("slug"),
                "direction": direction,
                "probability": signal_dict.get("probability", 0.5),
                "probability_raw": signal_dict.get("probability_raw"),  # round36 R2：封顶前原值，复盘校准统计用
                "confidence": signal_dict.get("confidence", "medium"),
                "reasons": signal_dict.get("reason", "").split(" | "),
                "risks": [signal_dict.get("risk", "")] if signal_dict.get("risk") else [],
                "anchor_price": signal_dict.get("anchor_price"),
                "current_price": signal_dict.get("current_price"),
                "deviation": (signal_dict.get("current_price", 0) or 0) - (signal_dict.get("anchor_price", 0) or 0),
                "pm_market_price": signal_dict.get("pm_up_price" if direction == "UP" else "pm_down_price"),
                "max_price": signal_dict.get("max_price"),
                "position_scale": signal_dict.get("position_scale", 1.0),
                "action": signal_dict.get("action", "ORDER"),
                "source": signal_dict.get("source", "btc-agent"),
                "predicted_side": direction,
                "scores": {
                    "UP": signal_dict.get("probability", 0.5) * 100 if direction == "UP" else (1 - signal_dict.get("probability", 0.5)) * 100,
                    "DOWN": (1 - signal_dict.get("probability", 0.5)) * 100 if direction == "UP" else signal_dict.get("probability", 0.5) * 100,
                },
                "ev": 0,
                "key_levels": signal_dict.get("key_levels", {}),
                "engine_check": engine_check,  # 8/11 防错补丁: 引擎校验轨迹（修正/跳过/覆盖度），供复盘
                "market_data": {
                    "slug": signal_dict.get("slug"),
                    "anchor_price": signal_dict.get("anchor_price"),
                    "current_price": signal_dict.get("current_price"),
                },
            }
            ANALYSIS.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
            PREDICTION.write_text(json.dumps({
                "timestamp": analysis["timestamp"],
                "slug": analysis["slug"],
                "direction": direction,
                "probability": analysis["probability"],
                "action": signal_dict.get("action", "ORDER"),
                "reason": signal_dict.get("reason", ""),
                "anchor_price": signal_dict.get("anchor_price"),
                "market_price": analysis["pm_market_price"],
                "source": signal_dict.get("source", "btc-agent"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            return {"ok": True, "attempt": attempt}

        except (OSError, IOError) as e:
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt  # 2, 4, 8 秒
                print(f"  ⚠️ 写入失败(attempt {attempt}): {e},{wait}s 后重试", file=sys.stderr)
                time.sleep(wait)
            else:
                return {"ok": False, "error": str(e), "attempts": MAX_RETRIES}

    return {"ok": False, "error": "max retries exceeded"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="信号写入器(8 步骤流程)")
    parser.add_argument("signal_json", nargs="?", help="JSON 字符串(或用 --input)")
    parser.add_argument("--input", "-i", help="从文件读 JSON")
    parser.add_argument("--source", "-s", help="覆盖 source 字段")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验+预览将写入的目标与内容，绝不追加进实时队列 claude_signal.jsonl（调试用，杜绝污染下单队列）")
    args = parser.parse_args()

    # 读 signal: 从文件或字符串
    if args.input:
        try:
            with open(args.input, encoding="utf-8") as f:
                sig = json.loads(f.read())
        except (OSError, json.JSONDecodeError) as e:
            print(f"❌ 读文件失败: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.signal_json:
        try:
            sig = json.loads(args.signal_json)
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("用法: write_signal.py '<json_string>' 或 write_signal.py --input <file>", file=sys.stderr)
        sys.exit(1)

    # 命令行覆盖 source
    if args.source:
        sig["source"] = args.source

    # --dry-run: 只预览，不触碰磁盘（防止探针信号误写进 bot 实时队列）
    if args.dry_run:
        _inject_runtime_meta(sig)  # 8/21 A3: 预览须含版本戳，与真实写入一致
        print(json.dumps({
            "dry_run": True,
            "target": str(SIGNAL),
            "target_exists": SIGNAL.exists(),
            "current_lines": (len(SIGNAL.read_text(encoding="utf-8").splitlines()) if SIGNAL.exists() else 0),
            "would_append": json.dumps(sig, ensure_ascii=False),
            "note": "未写入任何文件；用 scripts/check_order.sh <slug> 做权威核对(done.jsonl/last_signal.json)"
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    result = write_signal(sig)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 2)
