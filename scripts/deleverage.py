"""
One-time fix: bring a leveraged, over-populated paper account back in line with
the guardrails — at most config["max_positions"] names and total invested under
config["max_gross_exposure"] (default 95% of equity).

Strategy (two stages):
  1. CUT THE COUNT — if holding more than max_positions, fully close the weakest
     ones (worst unrealized P&L first) until exactly max_positions remain.
     close_position auto-cancels each closed name's stop order.
  2. TRIM TO TARGET — if the survivors are still over the exposure ceiling,
     pro-rata trim them by an equal fraction; cancel each stop and re-place it at
     the same price for the reduced share count.
Finally, ensure every surviving position has a protective stop (placed at
-stop_loss_pct from avg entry if it had none).

Run once, during market hours, from the repo root:
    python scripts/deleverage.py

Reads config.json. Safe to re-run: exits early if already within both limits.
"""
import os
import sys
import json
import time
import math
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

# Allow `from bot import notify` whether run as a script or a module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CONFIG_FILE = "config.json"


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def find_stop_orders(trading):
    """Map symbol -> (order_id, stop_price) for open GTC sell-stop orders."""
    open_orders = trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200))
    stops = {}
    for o in open_orders:
        is_stop = str(o.order_type).lower().endswith("stop") and o.stop_price is not None
        is_sell = str(o.side).lower().endswith("sell")
        if is_stop and is_sell:
            stops[o.symbol] = (o.id, float(o.stop_price))
    return stops


def wait_for_fill(trading, order_id, timeout=30):
    """Poll until a market order fills (or terminal), up to `timeout` seconds."""
    for _ in range(timeout // 2):
        time.sleep(2)
        try:
            o = trading.get_order_by_id(order_id)
            status = str(o.status).lower()
            if "filled" in status:
                return True
            if any(s in status for s in ("rejected", "canceled", "expired")):
                return False
        except Exception as e:
            log.warning(f"  status check failed ({e}); retrying")
    return False


def main():
    trading = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    config = load_config()
    max_gross = config.get("max_gross_exposure", 0.95)
    max_positions = config.get("max_positions", 5)
    stop_pct = config.get("stop_loss_pct", 0.05)

    clock = trading.get_clock()
    if not clock.is_open:
        log.error("Market is CLOSED — aborting. Sells would queue without filling, "
                  "and cancelling stops now would leave positions unprotected. "
                  f"Re-run during market hours (next open {clock.next_open}).")
        return

    account = trading.get_account()
    equity = float(account.portfolio_value)
    cash = float(account.cash)
    positions = trading.get_all_positions()
    gross_invested = sum(float(p.market_value) for p in positions)
    target = equity * max_gross

    log.info(f"Equity ${equity:,.2f} | cash ${cash:,.2f} | invested ${gross_invested:,.2f} "
             f"({gross_invested / equity * 100 if equity else 0:.1f}%) | {len(positions)} positions")
    log.info(f"Targets: <= {max_positions} positions and <= {max_gross:.0%} of equity (${target:,.2f})")

    if gross_invested <= target and len(positions) <= max_positions:
        log.info("Already within both limits — nothing to do.")
        return

    closed, trimmed = [], []

    # --- Stage 1: cut the count. Close the weakest (worst P&L) until <= cap. ---
    n_to_close = max(len(positions) - max_positions, 0)
    if n_to_close:
        ranked = sorted(positions, key=lambda p: float(p.unrealized_plpc))  # worst first
        to_close = ranked[:n_to_close]
        log.info(f"Closing {n_to_close} weakest position(s) to reach the cap of {max_positions}:")
        for p in to_close:
            try:
                order = trading.close_position(p.symbol)  # auto-cancels its stop
                ok = wait_for_fill(trading, order.id)
                pl = float(p.unrealized_plpc) * 100
                log.info(f"  {'closed' if ok else 'CLOSE PENDING'} {p.symbol} "
                         f"({int(float(p.qty))} sh, {pl:+.1f}%)")
                closed.append((p.symbol, pl))
            except Exception as e:
                log.error(f"  failed to close {p.symbol}: {e}")

    # --- Stage 2: re-read survivors and pro-rata trim to the exposure target. ---
    account = trading.get_account()
    equity = float(account.portfolio_value)
    survivors = trading.get_all_positions()
    surv_gross = sum(float(p.market_value) for p in survivors)
    target = equity * max_gross
    stops = find_stop_orders(trading)

    trim_fraction = (surv_gross - target) / surv_gross if surv_gross > target else 0.0
    if trim_fraction > 0:
        log.info(f"\nSurvivors at ${surv_gross:,.2f} ({surv_gross/equity*100:.1f}%) — "
                 f"trimming each by {trim_fraction*100:.1f}% to reach {max_gross:.0%}")
    else:
        log.info(f"\nSurvivors at ${surv_gross:,.2f} ({surv_gross/equity*100:.1f}%) — already under the ceiling")

    for p in survivors:
        symbol = p.symbol
        qty = int(float(p.qty))
        stop_id, stop_price = stops.get(symbol, (None, None))
        if not stop_price:
            stop_price = round(float(p.avg_entry_price) * (1 - stop_pct), 2)

        # ceil so rounding always lands us UNDER the ceiling (never just over it).
        trim_qty = math.ceil(qty * trim_fraction) if trim_fraction > 0 else 0

        if trim_qty <= 0:
            # No trim needed — just make sure the position is protected.
            if not stop_id:
                _replace_stop(trading, symbol, qty, stop_price)
                log.info(f"  {symbol}: kept {qty} sh, added missing stop @ ${stop_price}")
            continue

        keep_qty = qty - trim_qty
        if stop_id:
            try:
                trading.cancel_order_by_id(stop_id)
            except Exception as e:
                log.error(f"  {symbol}: failed to cancel stop ({e}) — skipping to avoid oversell")
                continue
        try:
            sell = trading.submit_order(MarketOrderRequest(
                symbol=symbol, qty=trim_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            ))
        except Exception as e:
            log.error(f"  {symbol}: trim sell FAILED ({e}) — re-placing full stop")
            _replace_stop(trading, symbol, qty, stop_price)
            continue

        if not wait_for_fill(trading, sell.id):
            log.warning(f"  {symbol}: trim did not fill — re-placing stop for FULL {qty} sh")
            _replace_stop(trading, symbol, qty, stop_price)
            continue

        _replace_stop(trading, symbol, keep_qty, stop_price)
        log.info(f"  {symbol}: trimmed {qty}→{keep_qty} sh, stop @ ${stop_price}")
        trimmed.append((symbol, qty, keep_qty, stop_price))

    # --- Report ---
    account = trading.get_account()
    new_equity = float(account.portfolio_value)
    new_cash = float(account.cash)
    new_positions = trading.get_all_positions()
    new_gross = sum(float(p.market_value) for p in new_positions)
    new_pct = new_gross / new_equity * 100 if new_equity else 0

    log.info("\n=== AFTER ===")
    log.info(f"{len(new_positions)} positions | cash ${new_cash:,.2f} | "
             f"invested ${new_gross:,.2f} ({new_pct:.1f}% of equity)")

    closed_lines = "\n".join(f"🔴 **{s}** ({pl:+.1f}%)" for s, pl in closed) or "None"
    trim_lines = "\n".join(f"• **{s}** {old}→{keep} sh (stop ${sp})" for s, old, keep, sp in trimmed) or "None"
    notify._send(
        title="⚖️ DE-LEVERAGED",
        description=(f"Brought the book back to **{len(new_positions)} positions** and "
                     f"**{new_pct:.0f}%** invested (cap {max_positions} / {max_gross:.0%}).\n"
                     f"Cash: **${new_cash:,.2f}**"),
        color=15844367,  # gold
        fields=[
            {"name": "Closed (weakest)", "value": closed_lines, "inline": False},
            {"name": "Trimmed", "value": trim_lines, "inline": False},
        ],
    )


def _replace_stop(trading, symbol, qty, stop_price):
    if qty <= 0 or not stop_price:
        return
    try:
        trading.submit_order(StopOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, stop_price=stop_price,
        ))
    except Exception as e:
        log.error(f"{symbol}: FAILED to re-place stop ({e}) — position may be UNPROTECTED, add manually")


if __name__ == "__main__":
    main()
