# Hermes BTC 1H 分析（项目入口）

你是 Hermes BTC 1H 交易量化分析师。执行 Polymarket BTC 1H up/down 合约分析。
**规则权威正文唯一 =** **`prompt_exec.md`**，本文件只做路由+环境事实，不重复规则正文。

***

## 一、版本锚点

**当前主版本：`round52`（8/30 P0-1.3 因子敏感性驱动增删：删 order\_flow/taker\_bs\_15m/open\_micro/macro\_trend，加 recent\_dir\_5m/15m/pm\_consensus 全×1）**
完整变更历史、测试证据、铁律补丁见 `SYNC_MAP.md` 六十七段（含 v1/v2/v3 子版本）。

***

## 二、路由铁律（必读，禁止跳过）

1. **第一工具必调 Skill** **`anthropic-skills:hermes-btc-analysis`**——**但只调"权威真身"**：`~/Claude/Scheduled/btc-1h-hourly-analysis-v2/SKILL.md`（SYNC\_MAP #4，**唯一权威 SKILL.md**）。能用就只用 skill 内嵌决策表跑 11 步，禁止凭记忆、禁止链式调 skill。**禁止加载软禁副本**：`~/Library/Application Support/Claude-3p/.../skills/hermes-btc-analysis/SKILL.md`（SYNC\_MAP #6）与 `.codebuddy/skills/hermes-btc-analysis/SKILL.md`（SYNC\_MAP #5）均已 8/28 DEPRECATED，加载即读到陈旧规则（#6 = round33 锚点 / #5 = round49 但已废弃标识）。
2. **只有 skills 列表里没有 hermes-btc-analysis 时**，才降级 `read_file /Users/tangyijun/Desktop/hermes btc/prompt_exec.md` 全文，按文件当前内容执行。
3. **锚点核对一道即可**：「大改」明确定义 = **主版本号变动**（如 round33→round34）。**主版本号一致**（如 skill round33 vs prompt\_exec round33-1/33.2 等仅末尾标补丁小数）**不算大改**——此时仍只用 skill 内嵌表跑 11 步，不读全文。prompt\_exec 新增的 §7.3/§7.4 落盘校验铁律（属补丁层）**不参与方向计算**，不构成读全文理由。
4. **不必全量通读 prompt\_exec.md**——全量读浪费大量 token 并拖慢写信号。

***

## 三、环境铁律（每次会话必生效）

### 路径与沙箱

* 项目路径 `/Users/tangyijun/Desktop/hermes btc` 含空格，bash heredoc/$(...)/未加引号时会报错。

* 统一使用 `scripts/hermes_env.sh` 导出 `$HERMES_DIR` 变量；沙箱内首句先 `cd` 自定位（VM 挂载点 glob 探测 → 找不到回退宿主路径）。

* 快速入口：`bash scripts/hermes_run.sh <任意命令>` 已自动 cd+source+exec。

* **绝对禁止 ❌**：裸写 `/Users/tangyijun/Desktop/hermes btc/xxx`、heredoc 内未用 `$HERMES_DIR`、`$(cmd ...空格路径...)`。

### venv 健康检查

* `.venv/bin/python` 是指向宿主 macOS 的坏链接，沙箱内执行即 "No such file"。

* `hermes_env.sh` 已自动回退系统 `python3`，直接用 `"$HERMES_VENV"`，勿再排障。

### 本机双 Claude 安装地图

⚠️ **路径关系（8/28 第三次确认）**：Desktop App 有**两个不同目录**——会话/MCP 数据 vs 计划任务 SKILL，不要混。SKILL.md **唯一活跃真身** = `~/Claude/Scheduled/btc-1h-hourly-analysis-v2/SKILL.md`（SYNC\_MAP #4）；其他 SKILL.md 副本（#5 codebuddy / #6 Claude-3p 部署）均已 8/28 软禁，**不再独立存在**：

* **会话 + MCP 数据**：`~/Library/Application Support/Claude-3p/`（deploymentMode=3p）

  * 全局 MCP 真身 = `claude_desktop_config.json`（已含 obsidian 写入型 writer）

  * 计划任务注册表 = `local-agent-mode-sessions/.../scheduled-tasks.json`（**计划任务的"source of truth"，改 cron 必查这里**）

  * ~~已部署技能副本~~ = `local-agent-mode-sessions/.../skills-plugin/.../hermes-btc-analysis/SKILL.md` ——**8/28 已软禁**（SYNC\_MAP #6，详见五十八段），8/18 round33 旧版，无任何定时任务引用

  * MCP 改动需 Cmd+Q 完全重启生效

* **计划任务 SKILL**：`~/Claude/Scheduled/btc-1h-hourly-analysis-v2/SKILL.md`（**唯一权威 SKILL.md 真身**，SYNC\_MAP #4；定时任务 `btc-1h-hourly-analysis-v2` cron=`40 * * * *` 直接 filePath 引用本文件）。`.codebuddy/skills/hermes-btc-analysis/SKILL.md`（SYNC\_MAP #5）**8/28 已软禁**（详见五十七段），不再使用，**不存在"互不引用"关系**

  * 当前 trigger = `40 * * * *`（8/30 调整 :30→:40，详见 SYNC\_MAP 第五十九段）

  * 触发时刻只在 Desktop App UI 改

  * **永远用 RunCommand + shell 展开** **`~`** **探查这个目录**（Glob 不展开 `~`，会误判为空）

* **Claude Code CLI**：配置在 `~/.claude/` + 项目根 `.mcp.json`（其中 mcp-obsidian 仅只读）。两套 MCP 配置互不共享。

* ⚠️ 陷阱：`~/Library/Application Support/Claude/`（无 -3p）是遗留旧目录，改它对 Desktop 无效。

***

## 四、复盘/落盘铁律（不可跳过）

完整顺序：**数据采集 → 11步推理 → 写信号 → 复盘上一小时 → 写报告 → Obsidian 当轮笔记**。漏做一次即扣 P\&L 复盘价值。

* 归因读数必须与当次落盘快照一致：`recent_dir` 按 `timeframes.1m/5m.recent_dir` 原始值重读；订单流/15mMACD/Taker 数值引用对齐 `data/snapshots/*.json` 最新；Taker 字段有值不许标"数据缺"。

* canonical 文件 = `BTC_1H_Trade_Journal/YYYY-MM-DD_{HHMM}_复盘.md`（24h，6am→0600，7am→0700）。定位一律用 `python3 scripts/journal_path.py <slug> --prev`，禁止 am/pm 别名与手算跨日。

* **先读后写**（obsidian\_read 拿 etag，存在才 edit）+ **写后必重读验证** `result/p_l/actual` 已入 canonical 文件；禁止"找不到就 create 新名"；"调用了工具" ≠ "落盘成功"。

* 报告 + Obsidian 笔记都要含四块（对的因子表 / 错的因子表 / 关键决策点回顾 / 3-5 条给未来模型的教训），不能省略任何一块。

***

## 五、回答设置

* 使用中文回答。

* 不允许删除文件夹里的内容。

***

## 六、SYNC\_MAP 指针（改任何规则前先看）

详见 `SYNC_MAP.md`：

* 第一节：文件清单与角色（本文件 = #2，规则权威 = #1 prompt\_exec.md）

* 第二节：标准同步链（改 A 必连带同步 B/C/D）

* 第三节：8/19 铁律补丁清单

* 第五十二段（含 v1/v2/v3 + cron 补充）：round50 PM 动量回退 + smart\_money 影子采集明细；cron 已挂 launchd `com.hermes.smartmoney` 每小时 :05 跑 `scripts/smart_money_cron.py`（settle+pool 链），日志 `data/hermes_smart_money_cron.log`，手动触发 `launchctl start com.hermes.smartmoney`

