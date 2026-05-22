"""
Reads trade_plan.json written by Claude and executes the orders via Alpaca.
Attaches a GTC stop loss order to every buy automatically.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
import time
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame
from . import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CONFIG_FILE = "config.json"
MEMORY_FILE = "memory.json"
PLAN_FILE = "data/trade_plan.json"


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default or {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_current_price(data_client, symbol):
    # Latest bar is always available (pre-market and market hours)
    try:
        req = StockLatestBarRequest(symbol_or_symbols=symbol)
        bar = data_client.get_stock_latest_bar(req)
        return float(bar[symbol].close)
    except Exception:
        pass
    # Fallback: last 5 days of daily bars
    try:
        start = (datetime.now() - timedelta(days=7)).date()
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start)
        bars = data_client.get_stock_bars(req).df
        if not bars.empty:
            return float(bars.iloc[-1]["close"])
    except Exception:
        pass
    raise RuntimeError(f"Could not fetch price for {symbol}")


def calc_qty(account, price, pct):
    max_value = float(account.portfolio_value) * pct
    affordable = min(float(account.buying_power) * 0.95, max_value)
    return max(int(affordable / price), 0)


def is_already_held(trading_client, symbol):
    try:
        trading_client.get_open_position(symbol)
        return True
    except Exception:
        return False


def main():
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_SECRET_KEY"]

    trading = TradingClient(api_key, api_secret, paper=True)
    data = StockHistoricalDataClient(api_key, api_secret)

    config = load_json(CONFIG_FILE, {"max_position_pct": 0.20, "stop_loss_pct": 0.05})
    plan = load_json(PLAN_FILE)
    memory = load_json(MEMORY_FILE, {"trade_history": [], "weekly_summaries": [], "portfolio_snapshots": []})

    if not plan or not plan.get("week_of"):
        log.error("No valid trade_plan.json found. Run research first.")
        return

    clock = trading.get_clock()
    if not clock.is_open:
        log.warning("Market is closed — orders will not execute until next open")

    now = datetime.now(ET)
    account = trading.get_account()
    actions = []

    # --- Close positions marked for exit ---
    for symbol in plan.get("close_positions", []):
        try:
            trading.close_position(symbol)
            reason = plan.get("close_reasoning", {}).get(symbol, "plan exit")
            log.info(f"CLOSED {symbol} — {reason}")
            actions.append(f"CLOSED {symbol}: {reason}")
            notify.trade_close(symbol, reason)
            memory["trade_history"].append({
                "time": now.isoformat(),
                "action": "CLOSE",
                "symbol": symbol,
                "reason": reason,
            })
        except Exception as e:
            log.error(f"Failed to close {symbol}: {e}")

    # --- Execute buys ---
    max_monthly = config.get("max_entries_per_symbol_per_month", 2)
    this_month = now.strftime("%Y-%m")
    monthly_counts = {}
    for t in memory.get("trade_history", []):
        if t.get("action") == "BUY" and t.get("time", "")[:7] == this_month:
            sym = t.get("symbol", "")
            monthly_counts[sym] = monthly_counts.get(sym, 0) + 1

    for trade in plan.get("buys", []):
        symbol = trade["symbol"]
        pct = trade.get("qty_pct", config["max_position_pct"])

        if is_already_held(trading, symbol):
            log.info(f"{symbol}: already held, skipping buy")
            continue

        if monthly_counts.get(symbol, 0) >= max_monthly:
            log.info(f"{symbol}: hit monthly entry cap ({max_monthly}), skipping")
            continue

        try:
            price = get_current_price(data, symbol)
        except Exception as e:
            log.error(f"{symbol}: could not fetch price — {e}")
            continue

        qty = calc_qty(account, price, pct)
        if qty == 0:
            log.warning(f"{symbol}: insufficient buying power for a {pct*100:.0f}% position, skipping")
            continue

        stop_price = round(price * (1 - config["stop_loss_pct"]), 2)

        # --- Submit buy ---
        try:
            buy_order = trading.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            ))
        except Exception as e:
            log.error(f"Failed to submit buy for {symbol}: {e}")
            continue

        # --- Wait for the buy to actually FILL (up to ~20s) ---
        fill_price = price
        filled = False
        for attempt in range(10):
            time.sleep(2)
            try:
                order = trading.get_order_by_id(buy_order.id)
                status = str(order.status).lower()
                if "filled" in status:
                    fill_price = float(order.filled_avg_price or price)
                    filled = True
                    log.info(f"BUY FILLED: {qty} {symbol} @ ${fill_price:.2f}")
                    break
                if any(s in status for s in ("rejected", "canceled", "expired")):
                    log.error(f"{symbol}: buy {status} — aborting")
                    break
            except Exception as e:
                log.warning(f"{symbol}: status check failed ({e}); retrying")

        if not filled:
            log.warning(f"{symbol}: buy did not fill within 20s — skipping stop loss but notifying anyway")

        # --- Notify on the buy regardless of stop outcome ---
        try:
            account = trading.get_account()
            notify.trade_buy(
                symbol=symbol, qty=qty, price=fill_price, stop_price=stop_price,
                reasoning=trade.get("reasoning", ""), confidence=trade.get("confidence", "medium"),
                portfolio_value=float(account.portfolio_value), buying_power=float(account.buying_power),
            )
        except Exception as e:
            log.error(f"Discord notify failed for {symbol}: {e}")

        memory["trade_history"].append({
            "time": now.isoformat(),
            "action": "BUY",
            "symbol": symbol,
            "qty": qty,
            "price": fill_price,
            "stop_price": stop_price,
            "reasoning": trade.get("reasoning", ""),
            "confidence": trade.get("confidence", ""),
            "filled": filled,
        })
        actions.append(f"BUY {qty} {symbol} @ ${fill_price:.2f} | stop ${stop_price:.2f}")

        # --- Attach stop loss (isolated — failure here does NOT roll back the buy) ---
        if filled:
            try:
                trading.submit_order(StopOrderRequest(
                    symbol=symbol, qty=qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC, stop_price=stop_price,
                ))
                log.info(f"  Stop loss set @ ${stop_price:.2f} (-{config['stop_loss_pct']*100:.0f}%)")
            except Exception as e:
                log.error(f"  STOP LOSS FAILED for {symbol}: {e} — position is UNPROTECTED, please add stop manually")

    # --- Snapshot ---
    account = trading.get_account()
    memory["portfolio_snapshots"].append({
        "time": now.isoformat(),
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
    })
    memory["portfolio_snapshots"] = memory["portfolio_snapshots"][-104:]

    if actions:
        memory["weekly_summaries"].append({
            "week_of": plan["week_of"],
            "thesis": plan.get("week_thesis", ""),
            "actions": actions,
        })

    save_json(MEMORY_FILE, memory)
    log.info("Execution complete. Memory updated.")


if __name__ == "__main__":
    main()
