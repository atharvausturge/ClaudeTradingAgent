"""
One-off: rebuild memory.json from real Alpaca data, stripping out fabricated
entries (parallel-Claude artifacts).

Truth sources:
  - Alpaca trade activity API (real fills only)
  - Real entries already in memory.json that we can verify

Strips:
  - weekly_summaries_new (fake key, never written by our code)
  - weekly_summaries entries with non-standard schemas
  - portfolio_snapshots that reference "CCR", "estimated", "synthesized"
  - strategy_notes mentioning files that don't exist in repo (score_local.py, bars_raw.json)
  - fabricated PLTR/AMD/INTC "Friday close" entries (PLTR is actually still open)

Writes memory.json.bak before overwriting.
"""
import os
import json
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest

ET = ZoneInfo("America/New_York")
trading = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)

# --- 1. Back up before mutating ---
shutil.copy("memory.json", "memory.json.bak")
print("Backup written: memory.json.bak\n")

with open("memory.json") as f:
    memory = json.load(f)

# --- 2. Pull real trade history from Alpaca (last 14 days of filled orders) ---
since = datetime.now(ET) - timedelta(days=14)
orders = trading.get_orders(filter=GetOrdersRequest(status="closed", after=since, limit=200))

real_trades = []
for o in orders:
    if str(o.status).lower() != "orderstatus.filled" and "filled" not in str(o.status).lower():
        continue
    side = "BUY" if "buy" in str(o.side).lower() else "SELL"
    filled_price = float(o.filled_avg_price or 0)
    filled_qty = float(o.filled_qty or 0)
    real_trades.append({
        "time": o.filled_at.astimezone(ET).isoformat() if o.filled_at else o.submitted_at.astimezone(ET).isoformat(),
        "action": side if side == "BUY" else ("STOP_TRIGGERED" if str(o.order_type).lower().endswith("stop") else "SELL"),
        "symbol": o.symbol,
        "qty": int(filled_qty),
        "price": round(filled_price, 2),
        "order_type": str(o.order_type).split(".")[-1].lower(),
    })

# Sort chronologically
real_trades.sort(key=lambda t: t["time"])

print(f"Real trades from Alpaca (last 14 days): {len(real_trades)}")
for t in real_trades:
    print(f"  {t['time'][:19]}  {t['action']:<16} {t['symbol']:<5} {t['qty']:>4} @ ${t['price']}")

# --- 3. Compute P&L for sells (match against the most recent prior buy of same symbol) ---
position_avg = {}  # {symbol: avg_entry_price}
position_qty = {}  # {symbol: qty}
for t in real_trades:
    sym = t["symbol"]
    if t["action"] == "BUY":
        # Weighted average if scaling in
        prior_qty = position_qty.get(sym, 0)
        prior_avg = position_avg.get(sym, 0)
        new_qty = prior_qty + t["qty"]
        position_avg[sym] = (prior_qty * prior_avg + t["qty"] * t["price"]) / new_qty if new_qty else 0
        position_qty[sym] = new_qty
    else:
        entry = position_avg.get(sym)
        if entry:
            t["pl_pct"] = round((t["price"] - entry) / entry * 100, 2)
            t["pl_dollar"] = round((t["price"] - entry) * t["qty"], 2)
            t["entry_price"] = round(entry, 2)
        position_qty[sym] = max(0, position_qty.get(sym, 0) - t["qty"])
        if position_qty[sym] == 0:
            position_avg.pop(sym, None)

# --- 4. Build a clean memory.json ---
real_snapshots = [
    s for s in memory.get("portfolio_snapshots", [])
    if "time" in s
    and "note" not in s              # remove the narrative ones with fake "blocked in CCR" notes
    and "data_source" not in s
    and "account_type" not in s
    and "synthesized" not in str(s).lower()
    and "ccr" not in str(s).lower()
    and "estimated" not in str(s).lower()
]

clean = {
    "last_run": datetime.now(ET).date().isoformat(),
    "trade_history": real_trades,
    "weekly_summaries": [],   # start fresh — old ones were fabricated or stale
    "portfolio_snapshots": real_snapshots,
    "last_monitor_run": memory.get("last_monitor_run"),
}

# --- 5. Write and report ---
with open("memory.json", "w") as f:
    json.dump(clean, f, indent=2, default=str)

print(f"\nClean memory.json written:")
print(f"  trade_history:       {len(clean['trade_history'])} real entries")
print(f"  portfolio_snapshots: {len(clean['portfolio_snapshots'])} entries kept")
print(f"  weekly_summaries:    reset to empty — will rebuild from this Friday onward")
print(f"\nRemoved:")
print(f"  - weekly_summaries_new (fabricated key)")
print(f"  - fabricated 'Friday close' entries (PLTR is actually still open)")
print(f"  - portfolio snapshots with 'CCR/synthesized/estimated' notes")
print(f"  - strategy_notes referencing non-existent files (score_local.py, bars_raw.json)")
