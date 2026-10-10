# Hermes BTC 分析 Skill 使用说明（v2.0）

> 本文档说明 `hermes-btc-analysis` skill 的路径、内容、以及如何把它交给 Claude 用于每小时 Polymarket BTC 1H 涨跌分析。

---

## 一、Skill 路径

| 项 | 值 |
|---|---|
| **Skill 绝对路径（主机）** | `/Users/tangyijun/.codebuddy/skills/hermes-btc-analysis/SKILL.md` |
| **Skill 项目内路径（Claude 可访问）** | `/Users/tangyijun/Desktop/hermes btc/.codebuddy/skills/hermes-btc-analysis/SKILL.md` |
| **Skill 名称** | `hermes-btc-analysis` |
| **当前版本** | `2.0.0` |
| **文件大小** | 246 行 / 22.9 KB |
| **规则锚点** | `prompt.md@2026-08-18-round33` |

> ✅ **8/18 已复制项目内副本**：为兼容 VM 沙箱的 Claude，`SKILL.md` 已复制到项目内 `.codebuddy/skills/hermes-btc-analysis/SKILL.md`（两文件 diff 完全一致）。Claude 优先读项目内副本。

---

## 二、给 Claude 的完整指令（可直接复制）

把下面这段发给 Claude，它就知道怎么用这个 skill：

```
从今天起，你执行 Polymarket BTC 1H 小时分析时，请使用 hermes-btc-analysis skill（v2.0）作为规则来源，按以下方式运行：

1. 【加载 skill】读取 /Users/tangyijun/Desktop/hermes btc/.codebuddy/skills/hermes-btc-analysis/SKILL.md（项目内副本，8/18 已复制，勿用 ~/.codebuddy 路径）。它内嵌了与 prompt_exec.md 完全同步的核心决策表（阶段标签、波动率约束、摆动市判定、贴线判定、信号生效矩阵、步骤8 评分表全部权重、步骤9 门槛层 G1-G6、9.7 调整项、步骤10 四维胜率表、§5.4 封顶 0.60、EV 计算、字段口径、8/18 事故补丁等）。直接用 skill 内嵌表分析，不必每次再读 prompt_exec.md 全文（可显著提速）。

2. 【规则变更校验】仅当 skill 版本号 < prompt_exec.md 锚点版本（当前 prompt.md@2026-08-18-round33）时，才 read_file /Users/tangyijun/Desktop/hermes btc/prompt_exec.md 全文并按最新内容执行；否则直接按 skill 分析。

3. 【执行流程】按以下 5 步：
   ① 采集数据：cd /Users/tangyijun/Desktop/hermes btc && .venv/bin/python hermes_collect.py --force --slim
   ② 读取 /tmp/hermes_market_data.json 全部字段
   ③ 按 skill 的 11 步流程分析（A 情境 → B 评分 → C 门槛 → D 结论）
   ④ 用 write_signal.py 写信号（见第 4 点）
   ⑤ 确认信号落盘到 data/claude_signal.jsonl

4. 【写信号】必须用以下命令，禁止直接写文件：
   cd /Users/tangyijun/Desktop/hermes btc && .venv/bin/python scripts/write_signal.py '<完整JSON>'
   - 这会写入 bot 消费队列 data/claude_signal.jsonl（8/18 已修复路径，勿写 /tmp）
   - 必含字段：slug / signal / side / probability / confidence / reason / risk / anchor_price / current_price / pm_up_price / pm_down_price / max_price / key_levels / ev
   - source 固定为 btc-agent；action 由 write_signal.py 自动补 ORDER
   - 禁止写 amount 字段

5. 【必须遵守的硬约束】
   - max_price 统一封顶 0.60（不是 0.69，0.69 已废弃）
   - pm_down_price/pm_up_price 必须填分析时刻快照的 PM 值（做 DOWN 写 pm_down_price，做 UP 写 pm_up_price），来源是采集 JSON 的 polymarket.down_price/up_price
   - 1m taker 信号直接读 timeframes.1m.taker_trend / taker_pct_last（买占比，0-1 小数），勿手算
   - EV 仅日志，不设 SKIP 门槛；只要方向存在即 ORDER
   - 方向强制 UP/DOWN，只有数据全失效才 SKIP

6. 【若与 prompt_exec.md 冲突】以 prompt_exec.md 当前内容为准，并告知我更新 skill。
```

---

## 三、Skill 内部结构（v2.0 包含什么）

Skill 文件 = 完整规则执行器，包含以下章节：

| 章节 | 内容 | 用途 |
|---|---|---|
| **frontmatter** | name / description / version 2.0.0 | 识别与版本 |
| **项目路径** | 项目根目录、Python、规则文件、采集输出、信号脚本 | 定位依赖 |
| **规则版本锚点** | 锚点 + 8/18 事故补丁说明 | 防规则漂移 |
| **执行流程（5 步）** | 采集→读数据→规则加载→11 步分析→写信号 | 主流程 |
| **核心决策表** | 阶段标签、波动率、摆动市、贴线、信号矩阵、评分表、G1-G6、9.7、四维胜率表、封顶 0.60、EV、字段口径、快照时效 | 分析所需全部规则 |
| **硬约束** | write_signal.py、source=btc-agent、封顶 0.60、pm 字段、单方向源铁律等 | 不可违反 |
| **失败处理** | 采集失败、字段缺失、写信号失败 | 容错 |
| **输出规范** | 结构化输出、非投资建议 | 格式 |
| **完成验证** | write_signal ok:true、data/claude_signal.jsonl 出现本时段 slug | 自检 |

---

## 四、Skill 与 prompt_exec.md 的关系

```
prompt_exec.md (803行/88KB, AI唯一权威全文)   ←  完整规则
        │ 内嵌同步
        ▼
SKILL.md (246行/22.9KB, 核心决策表)          ←  常驻上下文，提速用
        │
automation/Claude 每次分析
        │
   [规则变更校验]  skill 版本 < prompt_exec 锚点 ?
        │
    ├─ 是 → read_file prompt_exec.md 全文，按最新执行
    └─ 否 → 直接用 skill 内嵌决策表分析（省 3万 tokens 读取）
```

**核心思想**：skill 是"精简常驻版"，prompt_exec.md 是"完整权威版"。两者靠**版本锚点**保证同步。skill 提速的关键 = AI 不必每次读 88KB 全文。

---

## 五、如果 Claude 读不到 skill 路径（VM 沙箱情况）

Claude 运行环境可能是 `/sessions/...` 沙箱，访问不了用户 Mac 的 `/Users/tangyijun/...`。此时有 3 个方案：

| 方案 | 做法 | 适用 |
|---|---|---|
| **A. 复制到项目内**（✅ 已执行） | `SKILL.md` 已复制到 `/Users/tangyijun/Desktop/hermes btc/.codebuddy/skills/hermes-btc-analysis/SKILL.md`，Claude 读项目内副本 | Claude 能访问项目目录 |
| **B. 直接给全文** | 把 SKILL.md 内容粘贴给 Claude（约 22KB），让它按内嵌表分析 | 一次性会话 |
| **C. 维持 prompt_exec.md 兜底** | 让 Claude 直接读 prompt_exec.md 全文（不提速，但保证正确） | 无法访问 skill 时 |

> 给 Claude 的路径提示：skill 在 **`/Users/tangyijun/Desktop/hermes btc/.codebuddy/skills/hermes-btc-analysis/SKILL.md`**（项目内，非 ~/.codebuddy）。若仍读不到，用方案 B 粘贴全文。

---

## 六、验证 Skill 是否生效

每场分析后检查：

1. **信号落盘**：`data/claude_signal.jsonl` 应出现当前时段的 slug（不是 /tmp/hermes_signal.jsonl）
2. **bot 成交**：看 `data/hermes_bot_launchd.log` 是否出现 `🔥 [LIVE] ... → FILLED`
3. **字段完整**：信号含 `pm_down_price`/`pm_up_price` 快照值 + `max_price` ≤ 0.60
4. **提速确认**：bot 下单时刻应比之前（:23）更接近 :15 分析时刻

---

## 七、关键提醒

- **改 prompt_exec.md 规则时**：必须同步更新 skill 内嵌决策表 + automation 锚点校验（**三处勿漂移**）
- **写信号必须用 write_signal.py**：它写 `data/claude_signal.jsonl`（bot 消费队列），并自动补 action/source + 引擎校验
- **不要写 /tmp/hermes_signal.jsonl**：bot 已不读（8/16 v4 迁移），写了等于漏单
