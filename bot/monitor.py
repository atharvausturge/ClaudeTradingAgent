"""
Mid-week and Friday position monitor.
Wednesday: moves stop losses to breakeven for positions up >3%.
Friday: closes positions down more than the config threshold, holds winners.
"""
import os
import json
import logging
from datetime import datetime, timedelta
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MEMORY_FILE = "memory.json"
CONFIG_FILE = "config.json"
BREAKEVEN_THRESHOLD_PCT = 3.0  # move stop to breakeven when position is up this %


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


def move_stop_to_breakeven(trading, symbol, entry_price, qty):
    """Cancel the existing stop loss and replace it at entry price (risk-free)."""
    try:
        open_orders = trading.get_orders(filter=GetOrdersRequest(symbols=[symbol], status="open"))
        stop_orders = [
            o for o in open_orders
            if str(o.order_type) in ("stop", "stop_limit") and str(o.side) == "sell"
        ]

        if not stop_orders:
            log.warning(f"  {symbol}: no open stop order found")
            return False

        for stop_order in stop_orders:
            current_stop = float(stop_order.stop_price or 0)
            if current_stop >= entry_price:
                log.info(f"  {symbol}: stop already at ${current_stop:.2f} (>= entry ${entry_price:.2f}), no change")
                return False

            trading.cancel_order_by_id(stop_order.id)
            trading.submit_order(StopOrderRequest(
                symbol=symbol,
                qty=int(float(qty)),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(entry_price, 2),
            ))
            log.info(f"  {symbol}: stop moved from ${current_stop:.2f} → breakeven ${entry_price:.2f}")
            return True

    except Exception as e:
        log.error(f"  {symbol}: failed to move stop — {e}")
    return False


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
        if str(order.side) != "OrderSide.SELL" and str(order.side) != "sell":
            continue
        if str(order.status) not in ("filled", "OrderStatus.FILLED"):
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

        reason = "stop loss triggered" if str(order.order_type) in ("stop", "OrderType.STOP") else "auto-closed"
        log.info(f"  {symbol}: {reason} @ ${fill_price:.2f} ({pl_pct:+.2f}% | ${pl_dollar:+.2f})")
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

    clock = trading.get_clock()
    if not clock.is_open:
        log.info("Market is closed. Skipping monitor.")
        return

    now = datetime.now(ET)

    # Notify on any stop-losses that triggered since the last run
    auto_closed = detect_auto_closed_positions(trading, memory, now)
    if auto_closed:
        log.info(f"Auto-closed positions detected: {', '.join(auto_closed)}")
    is_friday = now.weekday() == 4
    close_threshold = config.get("friday_close_threshold", -0.02)
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
                    trading.close_position(symbol)
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
            else:
                log.info(f"  → HOLDING {symbol} into next week ({pl_pct:+.2f}%)")
                held.append({"symbol": symbol, "pl_pct": pl_pct})

        else:
            # Wednesday: move stop to breakeven for positions up > threshold
            if pl_pct >= BREAKEVEN_THRESHOLD_PCT:
                entry_price = get_entry_price(memory, symbol)
                if entry_price:
                    moved = move_stop_to_breakeven(trading, symbol, entry_price, pos.qty)
                    if moved:
                        memory["trade_history"].append({
                            "time": now.isoformat(),
                            "action": "STOP_TO_BREAKEVEN",
                            "symbol": symbol,
                            "pl_pct": pl_pct,
                            "new_stop": entry_price,
                        })
                else:
                    log.warning(f"  {symbol}: up {pl_pct:+.2f}% but no entry price in memory")

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


if __name__ == "__main__":
    main()
