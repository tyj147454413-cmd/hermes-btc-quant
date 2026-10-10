# -*- coding: utf-8 -*-
"""round41.8.19 final verification: anchors, residuals, selftests re-run."""
import json, os

print('== anchor scan:')
checks = [
    ('prompt_exec.md', 'prompt.md@2026-08-25-round41.8.19-翻向后PM基准回填'),
    ('prompt_exec.md', 'round41.8.19 兜底'),
    ('CLAUDE.md', '当前铁律锚点=**round41.8.19**'),
    ('CLAUDE.md', '(锚点 round41.8.19)'),
    ('.codebuddy/skills/hermes-btc-analysis/SKILL.md', 'rule_version: "round41.8.19"'),
    ('.codebuddy/skills/hermes-btc-analysis/SKILL.md', 'version: 2.1.9'),
    ('data/hermes_runtime_meta.json', '"strategy_version": "round41.8.19"'),
]
for p, nd in checks:
    txt = open(p, encoding='utf-8').read()
    print('  OK' if nd in txt else '  MISSING !!', p, '<-', nd[:44])

sk = open('.codebuddy/skills/hermes-btc-analysis/SKILL.md', encoding='utf-8').read()
print('  codebuddy anchor head:', 'round41.8.19-翻向后PM基准回填' in sk)
sp = os.path.expanduser('~/Claude/Scheduled/btc-1h-hourly-analysis-v2/SKILL.md')
st = open(sp, encoding='utf-8').read()
print('  scheduled anchor:', '【当前锚点】**round41.8.19**' in st)
print('  scheduled desc:', '锚点round41.8.19,' in st)

re_txt = open('scripts/rule_engine.py', encoding='utf-8').read()
print('  RULE_VERSION exec-41819:', re_txt.count('| 执行层exec-round41.8.19('))
print('  prior chain intact:', '前exec-round41.8.18(AI层momentum_burst因子行强制读数:' in re_txt)

sv = open('scripts/signal_validator.py', encoding='utf-8').read()
print()
print('== validator patch present:')
print('  impl rename:', '_validate_with_md_impl(sig: dict, md: dict, source: str)' in sv)
print('  wrapper:', 'def _validate_with_md(sig: dict, md: dict, source: str)' in sv)
print('  helper:', '_sync_pm_price_fields(out: dict, md: dict, check: dict)' in sv)
hb = open('hermes_1h_bot.py', encoding='utf-8').read()
print('  bot max_price_signal:', hb.count('max_price_signal'))

sm = open('SYNC_MAP.md', encoding='utf-8').read()
print('  SYNC_MAP 三十四:', '## 三十四、8/25 round41.8.19' in sm)

meta = json.load(open('data/hermes_runtime_meta.json'))
print('  runtime_meta JSON valid:', meta['strategy_version'] == 'round41.8.19')
