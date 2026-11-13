"""
Mid-week and Friday position monitor.
Wednesday: moves stop losses to breakeven for positions up >3%.
Friday: closes positions down more than the config threshold, holds winners.
"""
import os
import sys
import json
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from . import notify
from .orders import close_symbol, open_orders, is_stop_sell, enum_val, cancel_open_orders, has_live_stop
from .schedule_guard import should_run, mark_ran

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MEMORY_FILE = "memory.json"
CONFIG_FILE = "config.json"
BREAKEVEN_THRESHOLD_PCT = 3.0  # move stop to breakeven when position is up this %
FRIDAY_EARLIEST_ET = dtime(13, 0)  # Friday cleanup only in the afternoon session


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default or {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_spy_weekly_return(data_client):
    try:
        spy_start = (datetime.now(ET) - timedelta(days=15)).date()
        req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day, start=spy_start)
        bars = data_client.get_stock_bars(req).df
        if bars.empty or len(bars) < 2:
            return None
        closes = bars["close"].values
        return round((closes[-1] - closes[0]) / closes[0] * 100, 2)
    except Exception:
        return None


def get_portfolio_weekly_return(memory, current_value):
    snapshots = memory.get("portfolio_snapshots", [])
    if not snapshots:
        return None
    monday = datetime.now(ET).date() - timedelta(days=datetime.now(ET).weekday())
    for snap in reversed(snapshots):
        try:
            snap_date = datetime.fromisoformat(snap["time"]).date()
            if snap_date <= monday:
                start_value = snap["portfolio_value"]
                if start_value > 0:
                    return round((current_value - start_value) / start_value * 100, 2)
        except Exception:
            continue
    return None


def get_entry_price(memory, symbol):
    """Find the most recent BUY entry price for a symbol from trade history."""
    for entry in reversed(memory.get("trade_history", [])):
        if entry.get("symbol") == symbol and entry.get("action") == "BUY":
            return entry.get("price")
    return None


def replace_stop(trading, symbol, qty, stop_price):
    """Cancel the symbol's open orders (shares are held by them) and place a fresh GTC stop."""
    cancel_open_orders(trading, symbol)
    trading.submit_order(StopOrderRequest(
        symbol=symbol, qty=int(float(qty)), side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC, stop_price=round(stop_price, 2),
    ))


def move_stop_to_breakeven(trading, symbol, entry_price, qty):
    """Raise the stop to entry price (risk-free). Places one if none exists."""
    try:
        stop_orders = [o for o in open_orders(trading, symbol) if is_stop_sell(o)]
        current = max((float(o.stop_price or 0) for o in stop_orders), default=None)
        if current is not None and current >= entry_price:
            log.info(f"  {symbol}: stop already at ${current:.2f} (>= entry ${entry_price:.2f}), no change")
            return False
        replace_stop(trading, symbol, qty, entry_price)
        log.info(f"  {symbol}: stop {'moved from $%.2f' % current if current else 'placed'} → breakeven ${entry_price:.2f}")
        return True
    except Exception as e:
        log.error(f"  {symbol}: failed to move stop — {e}")
        raise


def detect_auto_closed_positions(trading, memory, now):
    """
    Find SELL orders that filled since last monitor run and weren't initiated by our code.
    These are typically stop-loss triggers — fire a Discord notification for each.
    """
    last_run = memory.get("last_monitor_run")
    if last_run:
        since = datetime.fromisoformat(last_run)
    else:
        since = now - timedelta(days=3)

    already_notified = {
        t.get("symbol") for t in memory.get("trade_history", [])
        if t.get("action") in ("STOP_TRIGGERED", "CLOSE", "FRIDAY_CLOSE")
        and t.get("time", "") > since.isoformat()
    }

    try:
        orders = trading.get_orders(filter=GetOrdersRequest(
            status="closed", after=since, limit=200,
        ))
    except Exception as e:
        log.warning(f"Could not fetch recent orders: {e}")
        return []

    triggered = []
    for order in orders:
        if enum_val(order.side) != "sell":
            continue
        if enum_val(order.status) != "filled":
            continue
        symbol = order.symbol
        if symbol in already_notified:
            continue
        # Find the buy entry price to compute realized P&L
        entry_price = get_entry_price(memory, symbol)
        fill_price = float(order.filled_avg_price or 0)
        qty = float(order.filled_qty or 0)
        pl_pct = round((fill_price - entry_price) / entry_price * 100, 2) if entry_price else None
        pl_dollar = round((fill_price - entry_price) * qty, 2) if entry_price else None

        reason = "stop loss triggered" if enum_val(order.order_type) in ("stop", "stop_limit", "trailing_stop") else "auto-closed"
        log.info(f"  {symbol}: {reason} @ ${fill_price:.2f} (P&L {pl_pct}% | ${pl_dollar})")
        notify.trade_close(symbol, reason, pl_pct)
        memory["trade_history"].append({
            "time": now.isoformat(),
            "action": "STOP_TRIGGERED",
            "symbol": symbol,
            "fill_price": fill_price,
            "pl_pct": pl_pct,
            "pl_dollar": pl_dollar,
            "reason": reason,
        })
        triggered.append(symbol)
    return triggered


def main():
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_SECRET_KEY"]

    trading = TradingClient(api_key, api_secret, paper=True)
    data_client = StockHistoricalDataClient(api_key, api_secret)
    config = load_json(CONFIG_FILE, {"friday_close_threshold": -0.02})
    memory = load_json(MEMORY_FILE, {"trade_history": [], "portfolio_snapshots": []})

    now = datetime.now(ET)
    is_friday = now.weekday() == 4
    job = "monitor_friday" if is_friday else "monitor_midweek"
    if not should_run(job, memory, trading.get_clock(), now,
                      earliest_et=FRIDAY_EARLIEST_ET if is_friday else None):
        return 0
    mark_ran(job, memory, now)
    memory.setdefault("trade_history", [])
    memory.setdefault("portfolio_snapshots", [])
    errors = []

    # Notify on any stop-losses that triggered since the last run
    auto_closed = detect_auto_closed_positions(trading, memory, now)
    if auto_closed:
        log.info(f"Auto-closed positions detected: {', '.join(auto_closed)}")
    close_threshold = config.get("friday_close_threshold", -0.02)
    stop_loss_pct = config.get("stop_loss_pct", 0.05)
    account = trading.get_account()
    positions = trading.get_all_positions()

    log.info(f"{'FRIDAY CLEANUP' if is_friday else 'MID-WEEK CHECK'} | {len(positions)} position(s) | Portfolio: ${float(account.portfolio_value):,.2f}")

    # Exposure tripwire: warn if gross invested has drifted over the ceiling.
    equity = float(account.portfolio_value)
    gross_invested = sum(float(p.market_value or 0) for p in positions)
    invested_pct = (gross_invested / equity * 100) if equity else 0.0
    max_gross = config.get("max_gross_exposure", 0.95)
    if equity and gross_invested > equity * max_gross:
        log.warning(f"OVER EXPOSURE CAP: invested {invested_pct:.0f}% > {max_gross:.0%}")
        notify._send(
            title="⚠️ OVER EXPOSURE CAP",
            description=(f"Invested **{invested_pct:.0f}%** of equity "
                        f"({invested_pct/100:.2f}× gross) — above the {max_gross:.0%} ceiling.\n"
                        f"Run `python scripts/deleverage.py` to trim back."),
            color=15158332,
        )

    position_snapshot = []
    held, closed = [], []

    for pos in positions:
        symbol = pos.symbol
        pl_pct = round(float(pos.unrealized_plpc) * 100, 2)
        pl_dollar = round(float(pos.unrealized_pl), 2)
        log.info(f"  {symbol}: {pl_pct:+.2f}% (${pl_dollar:+.2f})")
        position_snapshot.append({"symbol": symbol, "pl_pct": pl_pct, "pl_dollar": pl_dollar})

        if is_friday:
            if pl_pct < close_threshold * 100:
                try:
                    close_symbol(trading, symbol)
                    log.info(f"  → CLOSED {symbol} (Friday loser: {pl_pct:+.2f}%)")
                    notify.trade_close(symbol, "end-of-week loser purge", pl_pct)
                    closed.append({"symbol": symbol, "pl_pct": pl_pct})
                    memory["trade_history"].append({
                        "time": now.isoformat(),
                        "action": "FRIDAY_CLOSE",
                        "symbol": symbol,
                        "pl_pct": pl_pct,
                        "reason": "end-of-week loser purge",
                    })
                except Exception as e:
                    log.error(f"  Failed to close {symbol}: {e}")
                    errors.append(f"close {symbol}: {e}")
            else:
                log.info(f"  → HOLDING {symbol} into next week ({pl_pct:+.2f}%)")
                held.append({"symbol": symbol, "pl_pct": pl_pct})

        else:
            # Wednesday: move stop to breakeven for positions up > threshold
            entry_price = get_entry_price(memory, symbol) or float(pos.avg_entry_price)
            if pl_pct >= BREAKEVEN_THRESHOLD_PCT:
                if entry_price:
                    try:
                        moved = move_stop_to_breakeven(trading, symbol, entry_price, pos.qty)
                    except Exception as e:
                        moved = False
                        errors.append(f"breakeven stop {symbol}: {e}")
                    if moved:
                        memory["trade_history"].append({
                            "time": now.isoformat(),
                            "action": "STOP_TO_BREAKEVEN",
                            "symbol": symbol,
                            "pl_pct": pl_pct,
                            "new_stop": entry_price,
                        })
                else:
                    log.warning(f"  {symbol}: up {pl_pct:+.2f}% but no entry price available")
            elif not has_live_stop(trading, symbol):
                # Every position should carry a stop. Add the standard one if price allows.
                stop_price = round(entry_price * (1 - stop_loss_pct), 2)
                current_price = float(pos.current_price)
                if current_price > stop_price:
                    try:
                        replace_stop(trading, symbol, pos.qty, stop_price)
                        log.info(f"  {symbol}: had NO stop — placed GTC stop @ ${stop_price:.2f}")
                    except Exception as e:
                        errors.append(f"add missing stop {symbol}: {e}")
                else:
                    msg = (f"{symbol} has no stop and is already below its stop level "
                           f"(${current_price:.2f} < ${stop_price:.2f}) — Friday cleanup will close it")
                    log.warning("  " + msg)
                    errors.append(msg)

    if is_friday:
        portfolio_value = float(account.portfolio_value)
        weekly_return = get_portfolio_weekly_return(memory, portfolio_value)
        spy_return = get_spy_weekly_return(data_client)
        notify.friday_summary(portfolio_value, held, closed, weekly_return, spy_return)
        if weekly_return is not None and spy_return is not None:
            log.info(f"Weekly performance: Bot {weekly_return:+.2f}% vs SPY {spy_return:+.2f}%")
    else:
        notify.midweek_ok(float(account.portfolio_value), position_snapshot)

    memory["portfolio_snapshots"].append({
        "time": now.isoformat(),
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
        "positions": position_snapshot,
        "is_friday_close": is_friday,
    })
    memory["portfolio_snapshots"] = memory["portfolio_snapshots"][-500:]
    memory["last_monitor_run"] = now.isoformat()

    save_json(MEMORY_FILE, memory)
    log.info("Monitor complete. Memory updated.")

    if errors:
        notify.job_failed("Friday — Close Losers" if is_friday else "Wednesday — Midweek Check",
                          "\n".join(f"• {e}" for e in errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
