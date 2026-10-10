"""round43 文档同步脚本"""
import subprocess

# === 改 CLAUDE.md ===
with open('CLAUDE.md', 'r') as f:
    claude = f.read()

claude = claude.replace(
    '项目技能 .codebuddy/skills/hermes-btc-analysis/SKILL.md(锚点 round42.3)',
    '项目技能 .codebuddy/skills/hermes-btc-analysis/SKILL.md(锚点 round43)'
)
claude = claude.replace(
    '当前铁律锚点=**round42.3**',
    '当前铁律锚点=**round43**'
)

# 插入 round43 摘要到 round42.3 之前
ROUND43_HEAD = (
    '（8/26 **执行层双闸收紧：MAKER_CAP 0.80→0.65 + TAKER_MAX 0.88→0.65, '
    '用户指令 A+B 组合, 代码+文档级变更**：'
    '8/26 1pm ET 用户质询"是不是设置的价格有问题, 买到的都是高价格, 输要输 10"——'
    '审计最近 20 笔实盘 8/24-8/25, 9 笔 (45%) 在 0.78-0.80 高价成交, '
    '0.80 赔率 1.25:1 几乎平赌（输赢均 ≈$10）。'
    '修复=hermes_1h_bot.py L196 MAKER_CAP=0.80→0.65, '
    'L200 TAKER_MAX=0.88→0.65（用户决策 A+B 组合最严双闸），'
    '备份 .bak_20260826_pre_round43。'
    '**不动**：FAST_TAKE_MAX=0.75/MAKER_DISCOUNT=0.03/<0.25 硬底线/book 缺失 SKIP。'
    '**赔率修复**：0.65 吃单盈亏平衡胜率 65% 落入全历史盈利引擎 '
    '≤0.65 段 +$98 + 0.55-0.65 段 +$131 累计 +$229 范围, '
    '单笔最大亏损从 $10 收敛到 $6.5 (10 shares × $0.65, 输 -$6.5 / 赢 +$10, 赔率 1.54:1)。'
    '验证=py_compile ✅ + mock test_round43_dual_price_cap.py 15/15 通过 + '
    '残留扫描除历史回顾注释外活逻辑全部 0.65 + bot 重启加载新代码。'
    '明细 SYNC_MAP 四十一）。**前身 round42.3**（8/25 **采集器顶层 alias'
)
claude = claude.replace(
    '（8/25 **采集器顶层 alias',
    ROUND43_HEAD
)

with open('CLAUDE.md', 'w') as f:
    f.write(claude)
print('✅ CLAUDE.md 已更新')

# === 改 .codebuddy/skills/hermes-btc-analysis/SKILL.md ===
with open('.codebuddy/skills/hermes-btc-analysis/SKILL.md', 'r') as f:
    skill = f.read()

# rule_version 字段
skill = skill.replace('rule_version: "round42.3"', 'rule_version: "round43"')

# 顶部锚点字符串
skill = skill.replace(
    '`prompt.md@2026-08-25-round42.3-采集器顶层alias-Taker订单流1mtaker8字段平铺`',
    '`prompt.md@2026-08-26-round43-执行层双闸收紧-MAKER_CAP_TAKER_MAX双闸到0.65`'
)

# 顶部锚点段首插入 round43 摘要
ROUND43_BODY = (
    '当前版本锚点**:`prompt.md@2026-08-26-round43-执行层双闸收紧-MAKER_CAP_TAKER_MAX双闸到0.65`'
    '（**round43 为 8/26 执行层双闸收紧（用户指令 A+B 组合"买到的都是高价格, 输要输 10"，'
    '代码+文档级, 主版本升级 round42.3→round43）**:'
    '触发=审计最近 20 笔实盘 8/24-8/25：9 笔 (45%) 在 0.78-0.80 高价成交, '
    '0.80 赔率 1.25:1（输赢均 ≈$10）几乎平赌；'
    'round41.7 两段式+TAKER_MAX=0.88+maker 静置 0.80 组合实际把 0.80 变成新成交中枢（贵区 maker 挂 0.80 被立即 FILLED, 对手方急卖）。'
    '**修复（用户决策 A+B 组合, 最严双闸）**：① MAKER_CAP 0.80→**0.65**（第一段 maker 静置封顶）; '
    '② TAKER_MAX 0.88→**0.65**（第二段追价兜底封顶）。'
    '**不动**:FAST_TAKE_MAX=0.75（首段 taker 快道）, MAKER_DISCOUNT=0.03, <0.25 硬底线, book 缺失 SKIP 口径。'
    '**赔率修复**:0.65 吃单盈亏平衡胜率 65%（落入 ≤0.65 段 +$98 + 0.55-0.65 段 +$131 累计 +$229 利润引擎范围）, '
    '单笔最大亏损从 $10 收敛到 $6.5（10 shares × $0.65, 输 -$6.5 / 赢 +$10, 赔率 1.54:1）。'
    '**落地**:`hermes_1h_bot.py` L196/L200 常量改 + L172/L194/L478/L1191/L2152 注释同步, '
    '备份 `hermes_1h_bot.py.bak_20260826_pre_round43`。'
    '**验证**:py_compile ✅ + mock `scripts/test_round43_dual_price_cap.py` **15/15 通过**'
    '（T1-T15 覆盖 FAST_TAKE/MAKER/CHASE/<0.25 底线/SHADOW 所有路径含边界值）+ '
    '残留扫描除历史回顾注释外活逻辑全部 0.65。'
    '**SHADOW 影子区间 ask∈(0.65,0.95] 显著变宽**, 后续攒两周观察是否 0.65-0.75 区间有想吃但没敢吃的信号需手动裁决。'
    '**回滚**:恢复 `.bak_20260826_pre_round43` 备份 + 同步链反向替换。明细 SYNC_MAP 四十一）。'
    '**前身 round42.3 为 8/25 采集器顶层 alias 落地'
)
skill = skill.replace(
    '当前版本锚点**:`prompt.md@2026-08-25-round42.3-采集器顶层alias-Taker订单流1mtaker8字段平铺`'
    '（**round42.3 为 8/25 采集器顶层 alias 落地',
    ROUND43_BODY
)

# §5.4 执行层段
SEC54_OLD = (
    '**执行层（bot，round41.7 两段式进场 / 前身 round41.5+.2+.3）**:'
    '首段三路定价——① ask ≤ FAST_TAKE_MAX(0.75):直接按卖一价 taker 吃单（历史盈利引擎不为省 3 分钱冒踏空险）;'
    '② ask >0.75:maker 静置,挂限价 min(ask−0.03, MAKER_CAP=0.80) 等回落（ask≤挂价自动按 ask 撮合不多付）;'
    '③ book 缺失:SKIP（41.5.2 口径）。原「ask>上限立即 SKIP」撤销。'
    '第二段:静置 15min 未成交先实时复核——我方方向 Gamma <0.50=共识反超 → 撤单 SKIP（终态 RECHECK_CANCELLED）;'
    '查询失败保守不动;通过才追价跟随卖一价至硬顶 **TAKER_MAX=0.88**（round41.7 自 0.95 回滚）。'
    '影子记账 ask∈(0.88,0.95] 写 `data/hermes_shadow_taker.jsonl` 只记不交易，攒两周再裁决是否放开高价带。'
    '卖单全在 >0.90 观察带外返回真实最低卖一价（41.5.2）;账本真空/仅镜像单 → SKIP;'
    '**最终成交价 <0.25 硬底线一律不买，含翻向信号全路径**（41.5.3 保留）。'
    '依据:平衡线=入场价（0.95 需 >95% 胜率）、≤0.65 段 +$229 利润引擎、'
    '0.88→0.95 仅多覆盖 3% 决策时刻、未成交单方向正确率 91.5% 在 0.95 为负 EV。'
)
SEC54_NEW = (
    '**执行层（bot，round43 双闸收紧 / 前身 round41.7 两段式 / 前身 round41.5+.2+.3）**:'
    '首段三路定价——① ask ≤ FAST_TAKE_MAX(0.75):直接按卖一价 taker 吃单（历史盈利引擎不为省 3 分钱冒踏空险）;'
    '② ask >0.75:maker 静置,挂限价 min(ask−0.03, **MAKER_CAP=0.65**) 等回落（ask≤挂价自动按 ask 撮合不多付）;'
    '③ book 缺失:SKIP（41.5.2 口径）。原「ask>上限立即 SKIP」撤销。'
    '第二段:静置 15min 未成交先实时复核——我方方向 Gamma <0.50=共识反超 → 撤单 SKIP（终态 RECHECK_CANCELLED）;'
    '查询失败保守不动;通过才追价跟随卖一价至硬顶 **TAKER_MAX=0.65**（round43 自 0.88 收紧, 赔率平衡线 65%=全历史 ≤0.65 段利润引擎边界）。'
    '影子记账 ask∈(0.65,0.95] 写 `data/hermes_shadow_taker.jsonl` 只记不交易，攒两周再裁决是否放开高价带（**round43 起 SHADOW 区间变宽, 需后续按影子数据裁决**）。'
    '卖单全在 >0.90 观察带外返回真实最低卖一价（41.5.2）;账本真空/仅镜像单 → SKIP;'
    '**最终成交价 <0.25 硬底线一律不买，含翻向信号全路径**（41.5.3 保留）。'
    '依据:平衡线=入场价（0.88 需 >88% 胜率已逼近不可达, 0.65 仅需 >65% 落入全历史盈利引擎 '
    '≤0.65 段 +$98 + 0.55-0.65 段 +$131 累计 +$229 范围）。'
)
skill = skill.replace(SEC54_OLD, SEC54_NEW)

with open('.codebuddy/skills/hermes-btc-analysis/SKILL.md', 'w') as f:
    f.write(skill)
print('✅ .codebuddy/skills/hermes-btc-analysis/SKILL.md 已更新')

# 残留扫描
print('--- 残留扫描 ---')
for f in ['CLAUDE.md', '.codebuddy/skills/hermes-btc-analysis/SKILL.md']:
    r = subprocess.run(['grep', '-n', 'round42\\.3', f], capture_output=True, text=True)
    print(f'{f}: {r.stdout if r.stdout else "无残留 ✅"}')

# 锚点核对
print('--- 当前锚点核对 ---')
for f in ['CLAUDE.md', '.codebuddy/skills/hermes-btc-analysis/SKILL.md']:
    r = subprocess.run(['grep', '-n', '当前.*round43\\|当前铁律.*round43', f], capture_output=True, text=True)
    print(f'{f}: {r.stdout[:200] if r.stdout else "无匹配"}')
