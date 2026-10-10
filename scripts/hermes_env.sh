#!/usr/bin/env bash
# scripts/hermes_env.sh — Hermes BTC 项目路径环境变量定义
#
# 用途: 解决 "hermes btc" 文件夹名含空格导致 bash heredoc/命令替换报错的根因
#
# 用法 1(推荐): 在 bash 命令前 source
#   source scripts/hermes_env.sh
#   cat > "$HERMES_DIR/analysis_report.md" << 'EOF' ... EOF
#
# 用法 2(更简单): 用 hermes_run.sh 包装
#   bash scripts/hermes_run.sh cat > "$HERMES_DIR/file.md" << 'EOF' ... EOF
#
# 用法 3(直接): 用 hermes_run 自动 cd 到项目目录
#   bash scripts/hermes_run.sh ls data/snapshots/ | tail -5
#
# 自动检测本机/VM 路径,优先 VM 路径(Linux 容器)

# 本机路径(macOS 桌面)
HERMES_DIR_HOST="/Users/tangyijun/Desktop/hermes btc"

# VM 路径(Linux 容器挂载点)——动态探测当前活跃 session，不再写死单一 session 名
# （每次任务运行的 session 目录名是动态生成的，写死会导致失效并触发多余的路径探测）
HERMES_DIR_VM=""
for _cand in /sessions/*/mnt/"hermes btc"; do
    [ -d "$_cand" ] && HERMES_DIR_VM="$_cand" && break
done

# 自动选择可用的路径
if [ -n "$HERMES_DIR_VM" ] && [ -d "$HERMES_DIR_VM" ]; then
    HERMES_DIR="$HERMES_DIR_VM"
    HERMES_VENV="$HERMES_DIR_VM/.venv/bin/python"
elif [ -d "$HERMES_DIR_HOST" ]; then
    HERMES_DIR="$HERMES_DIR_HOST"
    HERMES_VENV="$HERMES_DIR_HOST/.venv/bin/python"
else
    echo "ERROR: 找不到 hermes btc 项目目录" >&2
    return 1 2>/dev/null || exit 1
fi

# Python 解释器健康检查回退:
# 在隔离沙箱(如 Linux 容器)里, .venv/bin/python 是指向宿主 macOS 的 uv 符号链接,
# 在本环境不存在 → 一执行即 "No such file"。此时自动回退到系统 python3。
# 在 macOS 本体上 .venv/bin/python 有效, 不触发回退, 行为完全不变。
if ! "$HERMES_VENV" --version >/dev/null 2>&1; then
    _broken_venv="$HERMES_VENV"
    if command -v python3 >/dev/null 2>&1; then
        HERMES_VENV="$(command -v python3)"
        echo "[hermes_env] ⚠️ $HERMES_DIR/.venv/bin/python 不可用(${_broken_venv}), 已回退系统 python3=$HERMES_VENV" >&2
    else
        echo "ERROR: 既无有效 .venv 也无系统 python3" >&2
        return 1 2>/dev/null || exit 1
    fi
fi

# 导出常用子目录(避免反复写 $HERMES_DIR/data/...)
HERMES_DATA="$HERMES_DIR/data"
HERMES_SNAPSHOTS="$HERMES_DIR/data/snapshots"
HERMES_SIGNALS="$HERMES_DIR/data/claude_signal.jsonl"
HERMES_LOGS="$HERMES_DIR/data"
HERMES_SCRIPTS="$HERMES_DIR/scripts"
HERMES_REPORTS="$HERMES_DIR"

export HERMES_DIR HERMES_DATA HERMES_SNAPSHOTS HERMES_SIGNALS HERMES_LOGS HERMES_SCRIPTS HERMES_REPORTS HERMES_VENV

# 验证提示
if [ -n "${BASH_SOURCE:-}" ]; then
    echo "[hermes_env] HERMES_DIR=$HERMES_DIR" >&2
fi
