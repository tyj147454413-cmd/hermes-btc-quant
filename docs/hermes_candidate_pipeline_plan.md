# Hermes BTC 候选规则管道设计稿（Plan agent 输出归档，2026-08-21）

> **来源**：Plan agent（claude sonnet via mcp__claude-code-guide）于本轮 session 输出（任务：设计 P1 候选规则管道）。
> **地位**：架构设计参考稿；本轮 P0-1/P0-2 已落地的代码即按此设计。
> **锚点**：plan-v1（与 round36/37 并列，描述本管道设计本身；规则层仍以 prompt_exec.md 为权威）。
> **改动追溯**：本设计稿不参与运行链路，bot/cron 不读。

---

## 一、设计结论

当前已有三层学习框架 + P0 基础设施（P0 三件套 = strategy_version 字段 + fill_bias_backtest.py + hermes_review.py check_round37_fallback），但"教训"与"规则变更"之间仍缺隔离层。本设计把闭环固定为：

```
复盘事实 → candidate_patches.json（pending）
         → 反事实回放（≥30 场 + EV 正向 + Wilson 区间为正）
         → proposed → BOSS 拍板（checklist）
         → accepted → deployed → monitoring
         → kept / reverted（30 场窗口自动判定）
```

边界：**hermes_review.py 继续记录事实和教训，但不再直接改规则**。候选队列只能产生候选，不能直接修改 prompt.md / prompt_exec.md / rule_engine.py / signal_validator.py / bot。

---

## 二、candidate_patches.json 数据契约（v1 schema）

### 2.1 顶层结构

```json
{
  "schema_version": "candidate-patches.v1",
  "updated_at_utc": "2026-08-21T22:00:00+00:00",
  "candidates": []
}
```

### 2.2 单条候选必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | `cp-YYYY-MMDD-NNN` 格式，幂等键 |
| `schema_version` | const `"candidate-patch.v1"` | 防止旧/新格式混淆 |
| `created_at_utc` / `updated_at_utc` | ISO 8601 UTC | 所有时间统一 |
| `layer` | enum | 统计层 / 权重层 / 结构层 |
| `condition.expression` | string | 字符串化触发条件（**禁止引用模糊字段名**，round34.1 教训） |
| `condition.field_paths` | array | 引用字段的精确路径（如 `judge.net`、`extract_factors.order_flow`） |
| `action.type` | enum | add / modify / remove_table_cell |
| `action.target.file` | string | 目标文件（必须 allowlist 内） |
| `action.target.symbol` | string | 目标符号（函数/常量名） |
| `source.provenance_slugs` | array | 触发该候选的所有 slug（**不可只留最后一个**） |
| `source.strategy_version` | string | 信号产出时的策略版本 |
| `source.model_version` | string | 信号产出时的模型版本 |
| `verification_status` | enum | 流程状态：pending / proposed / accepted / deployed / monitoring / kept / reverted / rejected |

### 2.3 验证证据与流程状态分离

为避免旧 §6 把"证据"和"流程"混在 verification_status 一字段，新 schema 拆为两字段：

- `verification_status`（流程）：pending → proposed → accepted → deployed → monitoring → (kept | reverted)，另允许 proposed → rejected
- `verification_evidence`（证据）：pending / self_verified / engine_verified

迁移：旧记录 `verification_status: self-verified + 状态: proposed` 拆成 `verification_status: proposed + verification_evidence: self_verified`。

### 2.4 `action.type` 三枚举

- **add**：新增规则/阈值/格子/分支。`target` 含 file/symbol/table，`after.enabled=true`
- **modify**：修改已有单元格/阈值/权重。**必填 before/after**（before 用于回滚）
- **remove_table_cell**：删除/停用表格单元格。**必填 fallback**，避免 None 路径

---

## 三、n≥30 的定义

候选必须同时满足：

```
eligible_sample_count >= 30
behavior_changed >= 1
net_effect > 0
paired_ev_bootstrap95.lower > 0
```

结构层额外：至少 3 个跨 regime 场景（不可只拿来源场次）。

---

## 四、反事实回测接口

### 4.1 新脚本

```bash
python3 scripts/candidate_counterfactual.py \
  --candidate-id cp-2026-0821-001
```

可选：`--as-of-utc` / `--min-samples 30` / `--output` / `--universe signal|fillable|filled` / `--no-write`

### 4.2 核心函数（签名）

```python
def load_candidate(candidate_id, path) -> dict
def load_replay_samples(*, as_of_utc=None, strategy_version=None, require_settled=True) -> list[dict]
def apply_candidate(sample, candidate) -> dict
def compare_decisions(baseline, variant) -> dict
def summarize_replay(samples, comparisons, *, alpha=0.05, z=1.96) -> dict
def run_candidate_replay(candidate, *, as_of_utc=None, min_samples=30) -> dict
```

### 4.3 行为改变数定义

`changed = max(direction_changed, action_changed, ev_changed, cap_changed)`，每个样本 + 总计 + 维度分解。

### 4.4 净效果定义

- 方向净效果：`net_correct = flipped_correct − flipped_wrong`
- EV 净效果：`net_effect = variant_ev − baseline_ev`（按成交价标准化收益：方向对 → +1/price − 1，方向错 → −1，未成交/SKIP → 0）
- 报告同时给 Wilson 95% 与配对 bootstrap 95% 区间（**Wilson 不替代 bootstrap，但满足审计要求**）

### 4.5 复用现有回测基础设施

候选回测应**复用** `regime_split_replay.py` 和 `fill_bias_backtest.py` 的加载/重放/Wilson 函数（→ P0-2 抽 `scripts/replay_common.py`）。候选若涉及 cap/挂单价/成交资格，必须同时报告三个宇宙（信号宇宙 / taker 可成交宇宙 / 实际成交宇宙），不能只看方向胜率。

---

## 五、BOSS 拍板 + 30 场监控

### 5.1 BOSS checklist（机器可读）

```json
"approval": {
  "checklist": {
    "n_ge_30": true,
    "behavior_changed": true,
    "ev_positive": true,
    "wilson_ci_positive": true,
    "no_version_conflict": true,
    "structure_scenarios_ge_3": true,
    "selftest_passed": true,
    "live_window_planned": true
  }
}
```

### 5.2 30 场窗口

样本口径：**部署后新出现的 30 个已结算 + 快照可复算样本**（不是自然日 30 场，不是触发候选格子）。

### 5.3 自动判定

**kept**：n≥30 且 eligible≥30 且 changed≥1 且 variant_win_rate_lower>50% 且 variant_ev_delta>0 且 paired_bootstrap95.lower>0 且无 hard stop。

**reverted**（任一）：变体 Wilson 下界<50% / EV 配对 bootstrap 上界≤0 / flipped_wrong>flipped_correct / 受影响样本近 10 场方向正确率显著低于基线 / strategy_version 不一致 / selftest 失败 / 破坏 60/30/10 三层 / cap 部署超 allowlist / 数据污染 / BOSS 人工 revert。

### 5.4 回滚操作

`reverted` 不是改 JSON 状态就结束，必须按 `deployment.target_files + original_sha256` 实际回滚代码 + 双 selftest + 重启（避开整点 :15）。

---

## 六、drift_alerts.jsonl 扩展

加兼容字段：

```json
{
  "schema_version": "drift-alert.v1",
  "alert_id": "da-2026-0821-0005",
  "type": "cell_drift | round37_fallback | candidate_replay | candidate_monitoring | candidate_rollback",
  "candidate_id": "cp-...",
  "strategy_version": "round37",
  "model_version": "minimax/MiniMax-M3",
  "window": { "start_utc", "end_utc", "target_samples", "observed_samples" },
  "severity": "rollback_candidate",
  "consumed": true
}
```

`type=candidate_monitoring` 等新告警不要伪装成普通 cell_drift。

---

## 七、P0/P1/P2 实施顺序

| 阶段 | 步骤 | 内容 | 验收 |
|---|---|---|---|
| **P0-1** | 候选 schema 冻结 | 架构文档 §6 修订 + scripts/candidate_pipeline.py + scripts/candidate_schema.json + 空 data/candidate_patches.json | 旧/新记录可读；同 slug+hash 幂等；JSON 损坏不破坏 review |
| **P0-2** | 抽 replay_common.py | 从 regime_split_replay.py + fill_bias_backtest.py 抽出加载/重放/Wilson 函数 | 旧 CLI 输出不变；快照数与样本量一致 |
| **P0-3** | dry-run 测试夹具 | test_candidate_pipeline.py + test_candidate_counterfactual.py + fixtures/ | 覆盖：direction 变 cap 不变、ORDER→SKIP、n=29 拒绝、n=30 通过、Wilson 边界、无 snapshot、strategy_version 冲突 |
| **P1-1** | hermes_review 入队 | update_lessons 返回 lesson + enqueue_review_lesson + HERMES_CANDIDATE_PIPELINE 开关 | 仅对预测错误/weak_correct/AI-引擎分歧/drift alert/人工指定入队；不重复写 stats/results |
| **P1-2** | 反事实回测 CLI | scripts/candidate_counterfactual.py | 输出 behavior_changed/EV delta/Wilson/bootstrap |
| **P1-3** | 候选状态审批 | scripts/candidate_approve.py（只读审批 CLI） | pending→proposed 必带 replay hash；accepted→deployed 必带文件清单 |
| **P1-4** | 部署清单 + 回滚 | scripts/candidate_deploy_plan.py | 目标仅 allowlist；selftest 失败拒绝 deployed |
| **P1-5** | 30 场监控 | scripts/candidate_monitoring.py | 30 场前不判定；硬触发先写回滚建议 |
| **P2-1** | drift_alerts 自动化消费 | alert status: open/acknowledged/converted/... | cell_drift 自动转 pending 候选 |
| **P2-2** | 建议生成器 | LLM 只生成结构化 patch（含 field_paths） | 触发：漂移命中或 +50 场或周日 |
| **P2-3** | walk-forward benchmark | 时间序列切分（非随机） | 1000 场达（约 32 天全结算 / 75 天引擎可复算） |

---

## 八、LIVE 系统安全约束

### 8.1 绝对不修改

- `prompt.md`
- cron 时序
- 0.60 基础封顶
- 三层决策框架（60% 方向 + 30% 动量 + 10% 否决）
- `fill_bias_backtest.py` 只读属性
- 现有 P0 三件套（strategy_version 字段 / check_round37_fallback）
- candidate_patches.json 不被 bot 读取（bot 只读 `data/claude_signal.jsonl`）

### 8.2 bot 修改窗口

ET :25 至次日 :05（避开整点 :15 触发窗口）。重启/写队列/切换策略/更新 LIVE 进程都不在 :15 附近。

---

## 九、诚实评估盲区

这个管道能解决"n=1 教训直落规则"这类危险路径，但不能完全消除过拟合：

1. **同一批历史数据被反复查看**——研究者看过数据后选最有利的规则，反事实回放仍是"更严格的内部分析"，不是真正的前向验证
2. **多重候选选择偏差**——100 个失败候选中挑 1 个最好看的上线，单看胜率仍可能好看
3. **regime 非平稳性**——历史有效阈值未来因市场参与结构/波动率/PM 流动性/宏观环境变化失效
4. **数据泄漏风险**——快照归档/成交价/bot 追价/结果映射/分析时刻若不一致，回放得"过于乐观"结果
5. **成交偏差**——只看方向正确率不看成交价/cap/追价/成交率会高估改善；候选回测必须复用 `fill_bias_backtest.py` 的三宇宙
6. **EV 近似不总成立**——二元合约固定注额下胜率差与 EV 同向，但价格/滑点/追价/早退/cap 变化时胜率差不能替代真实 P&L
7. **人工审批不等于避免过拟合**——BOSS 可否决坏规则但也基于叙事选择规则，仍需冻结候选/时间切分/前向监控

综合判断：本管道把"经验驱动改规则"变成"可追溯、可回滚、默认不自动生效的规则工程流程"，**预计能减少 70% 左右的低级过拟合和误落地问题**；剩余 30% 需 P2-3 时间序列 walk-forward benchmark + 预注册候选集 + 候选外样本验证。

---

## 十、关键文件改动顺序

| 顺序 | 文件 | 动作 |
|---:|---|---|
| 1 | docs/hermes_learning_architecture.md | 修订 §6 为 v1 schema + 指向 candidate_schema.json |
| 2 | scripts/replay_common.py | 新增统一加载/重放/Wilson/路径常量 |
| 3 | scripts/regime_split_replay.py | 复用 replay_common |
| 4 | scripts/fill_bias_backtest.py | 复用 replay_common |
| 5 | scripts/candidate_pipeline.py | 候选 CRUD + 状态机 + 文件锁 |
| 6 | scripts/candidate_schema.json | JSON Schema 定义 |
| 7 | data/candidate_patches.json | 新增空队列 |
| 8 | SYNC_MAP.md | 登记新文件 + 锚点 plan-v1 |
| 9 | hermes_review.py | 复盘后入队（P1-1，本轮不做） |
| 10 | scripts/candidate_counterfactual.py | 反事实 CLI（P1-2，本轮不做） |

---

*本设计稿由 2026-08-21 Plan agent 输出，本轮 P0-1/P0-2 已按 §10 步骤 1-8 部分落地。完整 agent 输出见本轮 session transcript（context 内）。*
