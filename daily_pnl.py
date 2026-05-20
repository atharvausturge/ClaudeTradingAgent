"""
Post-close daily P&L snapshot to Discord.

Runs Mon-Fri shortly after the close via .github/workflows/daily-pnl.yml.
- Equity change today (account.equity vs account.last_equity)
- Open positions w/ intraday P&L
- Anything closed (filled) today, with realized P&L vs the prior BUY entry
- Cash + buying power

Also appends a tiny snapshot to memory["daily_pnl"] for trend tracking.
"""
import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, GetCalendarRequest

import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MEMORY_FILE = "memory.json"
MAX_DAILY_HISTORY = 60


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _avg_entry_for(symbol, trade_history):
    """Walk trade_history chronologically and compute the running weighted-avg entry."""
    qty = 0
    avg = 0.0
    for t in trade_history:
        if t.get("symbol") != symbol:
            continue
        if t.get("action") == "BUY":
            tq = int(t.get("qty", 0) or 0)
            tp = float(t.get("price", 0) or 0)
            new_qty = qty + tq
            if new_qty > 0:
                avg = (qty * avg + tq * tp) / new_qty
            qty = new_qty
        elif t.get("action") in ("SELL", "CLOSE", "STOP_TRIGGERED"):
            qty = max(0, qty - int(t.get("qty", 0) or 0))
            if qty == 0:
                avg = 0.0
    return avg if qty > 0 else None


def realized_trades_today(trading, trade_history, today_start_et):
    """Filled SELL orders since today 00:00 ET, with P&L computed against memory entries."""
    orders = trading.get_orders(
        filter=GetOrdersRequest(status="closed", after=today_start_et, limit=100)
    )
    out = []
    for o in orders:
        side = str(o.side).lower()
        status = str(o.status).lower()
        if "filled" not in status or "sell" not in side:
            continue
        if not o.filled_at:
            continue
        filled_et = o.filled_at.astimezone(ET)
        if filled_et < today_start_et:
            continue
        qty = int(float(o.filled_qty or 0))
        exit_price = float(o.filled_avg_price or 0)
        if qty == 0 or exit_price == 0:
            continue
        entry = _avg_entry_for(o.symbol, trade_history)
        if entry:
            pl_pct = (exit_price - entry) / entry * 100
            pl_dollar = (exit_price - entry) * qty
        else:
            pl_pct = 0.0
            pl_dollar = 0.0
        out.append({
            "symbol": o.symbol,
            "qty": qty,
            "exit_price": round(exit_price, 2),
            "entry_price": round(entry, 2) if entry else None,
            "pl_pct": round(pl_pct, 2),
            "pl_dollar": round(pl_dollar, 2),
        })
    return out


def main():
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_SECRET_KEY"]
    trading = TradingClient(api_key, api_secret, paper=True)

    now = datetime.now(ET)
    today = now.date()

    # Skip on weekends and market holidays. The calendar endpoint is authoritative:
    # it returns the trading session for `today` only if today is a trading day.
    calendar = trading.get_calendar(GetCalendarRequest(start=today, end=today))
    if not calendar:
        log.info(f"{today} is not a trading day — skipping.")
        return

    account = trading.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity)
    daily_pl_dollar = equity - last_equity
    daily_pl_pct = (daily_pl_dollar / last_equity * 100) if last_equity else 0.0

    positions_raw = trading.get_all_positions()
    positions = [{
        "symbol": p.symbol,
        "qty": int(float(p.qty)),
        "current_price": float(p.current_price),
        "intraday_pl_dollar": float(p.unrealized_intraday_pl or 0),
        "intraday_pl_pct": float(p.unrealized_intraday_plpc or 0) * 100,
    } for p in positions_raw]

    memory = load_json(MEMORY_FILE, {"trade_history": []})
    today_start_et = datetime.combine(today, datetime.min.time(), tzinfo=ET)
    realized = realized_trades_today(trading, memory.get("trade_history", []), today_start_et)

    cash = float(account.cash)
    realized_today_dollar = round(sum(r["pl_dollar"] for r in realized), 2)

    log.info(
        f"Equity ${equity:,.2f} ({daily_pl_pct:+.2f}%, ${daily_pl_dollar:+,.2f}) | "
        f"{len(positions)} open | {len(realized)} closed today"
    )

    notify.daily_pnl(
        equity=equity,
        daily_pl_dollar=daily_pl_dollar,
        daily_pl_pct=daily_pl_pct,
        positions=positions,
        realized_today=realized,
        cash=cash,
    )

    memory.setdefault("daily_pnl", []).append({
        "date": today.isoformat(),
        "equity": round(equity, 2),
        "daily_pl_dollar": round(daily_pl_dollar, 2),
        "daily_pl_pct": round(daily_pl_pct, 2),
        "open_positions": len(positions),
        "realized_today_dollar": realized_today_dollar,
    })
    memory["daily_pnl"] = memory["daily_pnl"][-MAX_DAILY_HISTORY:]
    save_json(MEMORY_FILE, memory)
    log.info("Daily P&L snapshot saved to memory.json")


if __name__ == "__main__":
    main()
