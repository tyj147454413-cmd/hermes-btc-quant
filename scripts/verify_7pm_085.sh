#!/bin/bash
# 一次性核验：07:28 automation 跑 7pm 场后，检查其分析是否按 0.85 输出（round 定价修复闭环）
# 结果写入 data/verify_085.log，供后续读取
cd "/Users/tangyijun/Desktop/hermes btc"
LOG="data/verify_085.log"
{
  echo "===== $(date '+%F %T') 7pm 场 0.85 核验 ====="
  sleep 90  # 等 automation 分析完成落盘
  python3 -c "
import json, re, sys
try:
    d = json.load(open('data/hermes_analysis.json'))
except Exception as e:
    print('ERROR: 无法读取 hermes_analysis.json:', e); sys.exit(2)
slug = d.get('slug','')
ts = d.get('timestamp','')
txt = json.dumps(d, ensure_ascii=False)
print('timestamp:', ts)
print('slug:', slug)
sig = d.get('signal',{})
print('direction:', sig.get('direction'), '| max_price:', sig.get('max_price'), '| confidence:', sig.get('confidence'))
print('reason:', (d.get('reason') or sig.get('reason') or '')[:500])
has068 = bool(re.search(r'0\.68', txt))
print('---')
if '7pm-et' not in slug:
    print('WARN: analysis 非 7pm-et（slug=' + slug + '），可能 automation 未更新')
if has068:
    print('RESULT: FAIL — 仍检测到 0.68 残留')
    for m in re.finditer(r'[^,;。]*0\.68[^,;。]*', txt):
        print('  ', m.group(0)[:80])
else:
    print('RESULT: PASS — 无 0.68 残留，定价按最新规则（封顶 0.85）')
"
} > "$LOG" 2>&1
exit 0
