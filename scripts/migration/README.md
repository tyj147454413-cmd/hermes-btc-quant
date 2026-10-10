# Hermes BTC 1H Bot 迁移到 192.168.1.22 (Windows 10)

**目标**：把 Mac `/Users/tangyijun/Desktop/hermes btc` 整套系统搬到 `.22` (`Administrator`)，Mac 退役。
**时间盒**：今天跑通 (高强度)。
**生成日期**：2026-08-31

---

## 0. 现状速览（已实测）

| 维度 | Mac (本机) | `.22` (目标) |
|---|---|---|
| OS | macOS Darwin 24.6 | Win10 Pro 10.0.19045 (无 WSL) |
| Python | 3.13 (坏 venv 软链,见 CLAUDE.md §venv 健康检查) | **3.11.9 系统级** |
| Node | v24.18.0 (nvm) | **v24.14.0 (C:\Program Files\nodejs)** |
| Polymarket | 走 v2rayN+WARP | **直连 200,无需代理** |
| 信号源 | OpenClaw `btc-agent` + Claude Desktop plan task (当前 `enabled:false`) | Claude Code CLI v2.1.74 (npm 全局) + 手动 / 计划任务触发 |
| 服务编排 | launchd × 3-4 plist | **Task Scheduler** (已有 Hermes Agent × 3 模板可抄) |
| 已知基础设施 | 3 个 launchd plist + `/tmp` symlink | Hermes Agent 完整安装 + OpenClaw portable (dead state) + Claude Code CLI + Codex CLI |

**关键决策（用户已拍板）**：
- ✅ 独立 venv (隔离稳) — 不复用 `hermes-agent/venv`
- ✅ OpenClaw 退役 — `.22` 上 OpenClaw portable 不动
- ✅ 信号链路：先跑通 bot 基础设施，信号让用户后接（"其他 agent" 具体是什么用户后续决定）
- ✅ rsync + 脚本同步

---

## 1. 部署位置

| 路径 | 内容 |
|---|---|
| `D:\hermes-btc\` | bot 部署根（D 盘 565G 可用，与 Hermes Agent 隔离） |
| `D:\hermes-btc\.venv\` | 独立 venv (Python 3.11.9) |
| `D:\hermes-btc\data\`, `paper_trades\`, `predictions\`, `result\` | 数据目录 |
| `D:\hermes-btc\BTC_1H_Trade_Journal\` | 交易复盘 |
| `C:\Users\Administrator\Claude\Scheduled\btc-1h-hourly-analysis-v2\` | SKILL.md 真身 (从 Mac `~/Claude/Scheduled/...` 同步) |

**不部署的位置**：
- `D:\hermes-btc\launchd\` — macOS-only，跳过
- `D:\hermes-btc\node_modules\` — Node 端在 TradingView MCP，不在 bot 项目里
- `D:\hermes-btc\启动机器人.command` — macOS Finder launcher，跳过

---

## 2. 执行步骤（按顺序，每步独立可验证）

### Step 0: Preflight（不副作用 .22）
```bash
bash scripts/migration/00-preflight.sh
```
验证：SSH 通、Administrator key 免密、Python/Node 路径、磁盘空间、网络。

### Step 1: rsync 项目代码到 .22
```bash
bash scripts/migration/10-rsync-to-22.sh
```
把整个项目（排除 `.venv`/`__pycache__`/`.git/objects`/`node_modules`）推到 `D:\hermes-btc\`。

### Step 2: 在 .22 上建独立 venv + 装依赖
```bash
bash scripts/migration/20-install-venv-22.sh
```
建 `D:\hermes-btc\.venv\`，装 `numpy`/`flask`/`requests`/`portalocker`（替代 fcntl）。

### Step 3: 代码平台改造（在 .22 上原地打 patch）
```bash
bash scripts/migration/30-patch-fcntl-22.sh       # fcntl → portalocker
bash scripts/migration/31-fix-hardcoded-paths.sh   # 硬编码 /Users/... → 环境变量
```
改造后做冒烟：bot `--once --paper` 能起来不报 `ModuleNotFoundError` / `fcntl` 错误。

### Step 4: 注册 Windows Task Scheduler 4 个任务
```bash
bash scripts/migration/40-register-taskscheduler.sh
```
- `HermesBTC1HBot` — 主 bot (启动后 keepalive 由 Task Scheduler 监督)
- `HermesBTC1HDashboard` — dashboard.py :5050
- `HermesBTC1HCollect` — hermes_collect.py (整点触发,每小时 :02)
- `HermesBTC1HSignalCron` — (可选) 触发 Claude Code CLI 调 hermes-btc-analysis skill

### Step 5: Smoke test
```bash
bash scripts/migration/50-verify-smoke.sh
```
- bot `--paper --once` 跑一次不报错
- dashboard 起在 5050
- collect 跑一次有输出
- Task Scheduler 4 个任务 Ready

---

## 3. 关键改造点（per 文件）

### 3.1 `hermes_1h_bot.py` — fcntl 改造

```python
# 改造前
import fcntl
fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

# 改造后
try:
    import fcntl as _flock_mod
    _HAS_FCNTL = True
except ImportError:
    _flock_mod = None
    _HAS_FCNTL = False

import portalocker  # 新增依赖
# fcntl.flock() → portalocker.lock(f, portalocker.LOCK_EX)
```

`portalocker` 跨 Windows/Mac/Linux，接口跟 fcntl.flock 接近。

### 3.2 `hermes_collect.py` 路径参数化

```python
# 改造前 (line 87-91)
_TV_CLI = "/Users/tangyijun/tradingview-mcp/src/cli/index.js"
_TV_MCP_DIR = "/Users/tangyijun/tradingview-mcp"
_TV_NODE = "/Users/tangyijun/.nvm/versions/node/v24.18.0/bin/node"

# 改造后
import os
HERMES_DIR = Path(os.environ.get("HERMES_DIR", str(Path(__file__).resolve().parent)))
_TV_CLI = os.environ.get("TV_CLI", "")
_TV_MCP_DIR = os.environ.get("TV_MCP_DIR", "")
_TV_NODE = os.environ.get("TV_NODE", "node")  # 走系统 PATH
# 若 TV_CLI 为空,自动探测 ../../tradingview-mcp/src/cli/index.js
```

通过 `.env` 配置，路径不再硬编码。

### 3.3 `restart_hermes.sh` 的 `/tmp` symlink 改造

```bash
# 改造前
ln -sf "/Users/tangyijun/Desktop/hermes btc/data/claude_signal.jsonl" /tmp/hermes_signal.jsonl

# 改造后 (Windows .bat)
# 直接让代码读 $HERMES_DIR\data\claude_signal.jsonl
# 删除所有 /tmp/hermes_signal.jsonl 引用
```

### 3.4 `hermes_config.json` 加 .22 段

```json
{
  "windows": {
    "hermes_dir": "D:\\hermes-btc",
    "venv_python": "D:\\hermes-btc\\.venv\\Scripts\\python.exe",
    "dashboard_port": 5050,
    "tv_mcp_dir": "C:\\Users\\Administrator\\tradingview-mcp",
    "proxy": ""  // 直连 200,不需要代理
  }
}
```

---

## 4. Task Scheduler XML 模板

参考 `.22` 上现有 `HermesAgentGateway` 任务的注册方式（用 `schtasks /Create /XML`），4 个任务的 XML 在 `scheduled/` 目录。

每个任务的 `<Exec>` 引用 `bat/*.bat`，`<WorkingDirectory>` 设 `D:\hermes-btc\`。

---

## 5. 信号链路（暂未接入）

CLAUDE.md §8/4 BOSS 决策「只认 OpenClaw btc-agent 信号」= 已经**过时**（OpenClaw 退役）。新信号源待用户决定。

**短期方案**：让用户在 `.22` 手动跑
```cmd
claude --model claude-haiku-4-5 --skill /anthropic-skills/hermes-btc-analysis
```
把结果写到 `D:\hermes-btc\data\claude_signal.jsonl`，bot 直接读。

**中期方案**：注册 `HermesBTC1HSignalCron` 任务，用 schtasks 在每小时的 :40 触发 `claude` CLI 调 skill（替代 Mac 上的 Desktop plan task）。

**修改 bot 的 ALLOWED_SOURCES**：当新信号源确定后，改 `hermes_1h_bot.py` 第 32 行的白名单：
```python
ALLOWED_SOURCES = ("hermes-cli", "btc-agent-legacy", "BTC Sentinel", "btc_signal_watcher")
```
（"hermes-cli" 占位，等用户定名后改）

---

## 6. 回滚 / 安全门

每步脚本幂等可重跑。出问题时：
- `rsync` 不带 `--delete`，新文件可叠加，不删原 .22 文件
- `venv` 重建前先 `mv .venv .venv.bak-<时间戳>` 保留
- `Task Scheduler /Create` 加 `/F` 强制覆盖同名任务；删除用 `schtasks /Delete /TN ... /F`
- Mac 上原项目不动，可随时切回

---

## 7. Mac 退役清单（bot 跑通后执行）

- [ ] `launchctl bootout gui/$(id -u)/com.hermes.bot` (停 Mac bot)
- [ ] `launchctl bootout gui/$(id -u)/com.hermes.dashboard` (停 Mac dashboard)
- [ ] `launchctl bootout gui/$(id -u)/com.hermes.smartmoney` (停 Mac smartmoney)
- [ ] `launchctl bootout gui/$(id -u)/com.hermes.btc.healthcheck` (停 Mac healthcheck)
- [ ] 把 `hermes btc` 目录 tarball 备份到外置盘（决策性资产）
- [ ] 关 Claude Desktop plan task `btc-1h-hourly-analysis-v2`（虽然 `enabled:false`，清干净）

---

## 8. 笔记路径

完成后落库到 Obsidian：
- `[[daily/2026-08-31-btc-bot-migration-to-22]]` — 当天执行流水
- `[[reference/最近决策]]` — 追加迁移决策 (OpenClaw 退役 / 信号源待定 / 独立 venv 等)
- `[[reference/mac-to-windows-migration-2026-08]]` — 可复用模板
