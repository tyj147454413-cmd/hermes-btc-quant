# -*- coding: utf-8 -*-
import sys, json, glob
sys.path.insert(0, r"d:\hermes-btc\scripts")
import signal_validator as sv

fs = [f for f in sorted(glob.glob(r"d:\hermes-btc\data\snapshots\*.json")) if not f.endswith("._*")]
md = json.loads(open(fs[-1], encoding="utf-8").read())
slug = md.get("slug")
print("快照:", fs[-1].split("\\")[-1])
pp24 = md.get("price_position_24h") or {}
print("采集端 price_position_24h:", pp24)

# 用 validate 全链路取 check 里的中间态不可行，直接构造: 抓 judge 输入验证
# 简化: 调 extract_factors 后在 judge 的输入断言——通过 validate 出口 check.trace 里找 G4 双向核对文案
seed = {"slug": slug, "source": "btc-agent", "action": "ORDER"}
out, check = sv.validate(seed)
tr = "\n".join(check.get("trace") or [])
for line in tr.splitlines():
    if "24h位置" in line or "G4" in line:
        print("TRACE:", line[:120])
print("engine_direction:", check.get("engine_direction"))
