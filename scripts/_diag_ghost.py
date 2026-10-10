# -*- coding: utf-8 -*-
"""诊断: 核实 0x187f3af262 在 CLOB 的真实状态（幽灵单验证）。"""
import os
import sys

sys.path.insert(0, ".")
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

from dotenv import load_dotenv
load_dotenv(".env")
from hermes_common import get_proxy, load_pm_creds

OID = "0x187f3af26258ffdc12"
TOKEN = "42107818117289378499789036394303642631170312174498367645929568586689532152458"

proxy = get_proxy()
if proxy:
    os.environ["http_proxy"] = proxy
    os.environ["https_proxy"] = proxy
from polymarket import SecureClient
pk = os.getenv("POLYMARKET_PK")
cached = load_pm_creds()
if cached:
    from polymarket.models.clob import ApiKeyCreds
    client = SecureClient.create(private_key=pk, credentials=ApiKeyCreds(**cached))
else:
    client = SecureClient.create(private_key=pk)

try:
    o = client.get_order(order_id=OID)
    print("get_order:", o)
except Exception as e:
    print("get_order 异常:", type(e).__name__, str(e)[:200])

try:
    orders = client.get_orders()
    live = [x for x in orders if str(getattr(x, "status", "")).lower() in ("live", "open")]
    print(f"活跃单 {len(live)} 笔:")
    for x in live:
        print(" ", str(getattr(x, "id", ""))[:20], getattr(x, "price", "?"),
              getattr(x, "size", "?"), getattr(x, "side", "?"), getattr(x, "status", "?"))
except Exception as e:
    print("get_orders 异常:", type(e).__name__, str(e)[:200])
