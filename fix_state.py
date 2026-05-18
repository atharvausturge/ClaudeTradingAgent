"""
One-off: add missing stop losses for today's positions and patch memory.json
so monitor.py recognizes them on Wed/Fri.

Run once, then delete.
"""
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce

ET = ZoneInfo("America/New_York")
STOP_PCT = 0.05  # -5%

trading = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)

# 1. Place missing stops on every long position without one
positions = trading.get_all_positions()
open_orders = trading.get_orders(filter=GetOrdersRequest(status="open"))
symbols_with_stops = {
    o.symbol for o in open_orders
    if str(o.order_type).lower().endswith("stop") and str(o.side).lower().endswith("sell")
}

print(f"Positions: {[p.symbol for p in positions]}")
print(f"Already protected: {symbols_with_stops}")

new_trades = []
for pos in positions:
    if pos.symbol in symbols_with_stops:
        print(f"  {pos.symbol}: stop already present, skipping")
        continue
    entry = float(pos.avg_entry_price)
    qty = int(float(pos.qty))
    stop_price = round(entry * (1 - STOP_PCT), 2)
    try:
        trading.submit_order(StopOrderRequest(
            symbol=pos.symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, stop_price=stop_price,
        ))
        print(f"  ✓ {pos.symbol}: stop placed @ ${stop_price} (entry ${entry})")
        new_trades.append({
            "symbol": pos.symbol, "qty": qty,
            "price": entry, "stop_price": stop_price,
        })
    except Exception as e:
        print(f"  ✗ {pos.symbol}: stop FAILED — {e}")

# 2. Patch memory.json so monitor.py knows about today's buys
with open("memory.json") as f:
    memory = json.load(f)

now = datetime.now(ET).isoformat()
existing = {(t.get("symbol"), t.get("time")) for t in memory.get("trade_history", [])}

for t in new_trades:
    key = (t["symbol"], now)
    if key in existing:
        continue
    memory["trade_history"].append({
        "time": now,
        "action": "BUY",
        "symbol": t["symbol"],
        "qty": t["qty"],
        "price": t["price"],
        "stop_price": t["stop_price"],
        "reasoning": "Backfilled by fix_state.py — original buy went through but execute.py did not record it",
        "confidence": "medium",
        "filled": True,
        "backfilled": True,
    })
    print(f"  ✓ {t['symbol']}: added to trade_history")

with open("memory.json", "w") as f:
    json.dump(memory, f, indent=2, default=str)

print("\nDone. Verify on Alpaca → Orders → Open that the new stops are showing.")
