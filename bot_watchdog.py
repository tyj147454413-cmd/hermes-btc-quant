# -*- coding: utf-8 -*-
"""hermes bot 看门狗：由计划任务 HermesBotWatchdog 每 5 分钟调用一次。

职责：检测 hermes_1h_bot 进程是否存活，死亡则经计划任务自动拉起。
来历：9/2 深夜 bot 22:02 启动后被静默杀死 1h43m 无人发现，
     错过 10am/11am 两个交易窗口（信号队列积压、done.jsonl 断档）。

防风暴设计：拉起后 10 分钟（GUARD_S）内若仍检测不到进程，
判定"启动即崩"，跳过巡检并记日志，避免每 5 分钟反复重启。
"""
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BOT_TASK_NAME = "HermesBTC1HBot"
PID_FILE = DATA / "hermes_bot.pid"
STATE_FILE = DATA / "watchdog_state.json"
LOG_FILE = DATA / "hermes_watchdog.log"
GUARD_S = 600  # 重启保护期（秒）

# 状态只变化时才记日志，避免每 5 分钟刷屏
_LOGGED_STATUS = None


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S]") + " " + msg
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    STATE_FILE.write_text(
        json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def bot_alive():
    """按命令行匹配 hermes_1h_bot 进程（与 dashboard._bot_running_win 同口径）。
    注意：venv python.exe 是启动器 shim，会派生子进程，父子命令行都含 hermes_1h_bot，
    因此可能返回 2 个 pid——只要非空即视为存活。"""
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
              "Where-Object {$_.CommandLine -match 'hermes_1h_bot'} | "
              "Select-Object -ExpandProperty ProcessId")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=25)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception as e:
        log(f"W 进程检测异常: {e}")
        return []


def need_restart(alive, last_ts, now, guard_s=GUARD_S):
    """纯判定逻辑（独立成函数便于单测）。
    返回 (是否拉起, 原因)：alive=存活 / guard=保护期内 / dead=需要拉起"""
    if alive:
        return False, "alive"
    if last_ts and now - last_ts < guard_s:
        return False, "guard"
    return True, "dead"


def main():
    global _LOGGED_STATUS
    st = load_state()
    last_ts = st.get("last_restart", 0)
    pids = bot_alive()
    do, why = need_restart(bool(pids), last_ts, time.time())

    if why == "alive":
        _LOGGED_STATUS = "alive"
        if st.get("status") != "alive":
            log(f"OK bot 存活 pid={pids}")
            st["status"] = "alive"
            save_state(st)
        return

    if why == "guard":
        if st.get("status") != "guard":
            _LOGGED_STATUS = "guard"
            log(f"W bot 无进程且 {GUARD_S}s 内刚拉起过"
                f"（上次 {time.strftime('%H:%M:%S', time.localtime(last_ts))}），"
                f"疑似启动即崩，跳过本轮，请人工检查 data/hermes_bot.log")
            st["status"] = "guard"
            save_state(st)
        return

    # dead → 拉起
    _LOGGED_STATUS = "restarted"
    log(f"W bot 无进程（残留pid文件={'有' if PID_FILE.exists() else '无'}），"
        f"执行 schtasks /Run 拉起")
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", BOT_TASK_NAME],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"E schtasks /Run 失败: "
                f"{(r.stderr or r.stdout or '').strip()[:150]}")
    except Exception as e:
        log(f"E schtasks /Run 异常: {e}")
    # 无论 /Run 是否报错都写入保护期时间戳：拉起失败也防止每 5 分钟重试轰击
    st["last_restart"] = time.time()
    st["status"] = "restarted"
    save_state(st)
    for _ in range(8):  # 最多等 16s 确认进程出现（冷启动握手慢是已知现象）
        time.sleep(2)
        fresh = bot_alive()
        if fresh:
            log(f"OK 拉起成功 bot pid={fresh}")
            return
    log("E 拉起后 16s 仍未见进程，请人工检查 start_bot.bat / hermes_bot.log")


if __name__ == "__main__":
    main()
