"""
Reads data/trade_plan.json (written by bot.analyze) and executes it on Alpaca.
Attaches a GTC stop loss order to every buy automatically.

Scheduled several times across Monday's session; bot.schedule_guard makes sure
only the first attempt that lands while the market is open does anything.
Exits non-zero if any order step failed, so the workflow alerts Discord.
"""
import os
import sys
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame
from . import notify
from .orders import close_symbol, is_held, wait_until_flat, enum_val
from .schedule_guard import should_run, mark_ran

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CONFIG_FILE = "config.json"
MEMORY_FILE = "memory.json"
PLAN_FILE = "data/trade_plan.json"
JOB = "execute"
MAX_PLAN_AGE_DAYS = 4  # a plan older than this is last week's — don't trade it
FILL_WAIT_S = 60


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
    try:
        req = StockLatestBarRequest(symbol_or_symbols=symbol)
        bar = data_client.get_stock_latest_bar(req)
        return float(bar[symbol].close)
    except Exception:
        pass
    try:
        start = (datetime.now() - timedelta(days=7)).date()
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start)
        bars = data_client.get_stock_bars(req).df
        if not bars.empty:
            return float(bars.iloc[-1]["close"])
    except Exception:
        pass
    raise RuntimeError(f"Could not fetch price for {symbol}")


def calc_qty(equity, price, pct, remaining_budget):
    """Cash-aware sizing: a position targets `pct` of equity, but is never
    allowed to exceed `remaining_budget` (the dollars left under the gross
    exposure ceiling). This is what keeps the account off margin."""
    target_value = min(equity * pct, remaining_budget)
    return max(int(target_value / price), 0)


def wait_for_fill(trading, order_id, price):
    """Poll an order until filled / dead / timeout. Returns (filled, fill_price, status)."""
    status = "unknown"
    deadline = time.time() + FILL_WAIT_S
    while time.time() < deadline:
        time.sleep(2)
        try:
            order = trading.get_order_by_id(order_id)
            status = enum_val(order.status)
            if status == "filled":
                return True, float(order.filled_avg_price or price), status
            if status in ("rejected", "canceled", "expired"):
                return False, price, status
        except Exception as e:
            log.warning(f"status check failed ({e}); retrying")
    return False, price, status


def plan_is_fresh(plan, now) -> bool:
    try:
        week_of = datetime.strptime(plan["week_of"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return False
    return 0 <= (now.date() - week_of).days <= MAX_PLAN_AGE_DAYS


def main():
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_SECRET_KEY"]

    trading = TradingClient(api_key, api_secret, paper=True)
    data = StockHistoricalDataClient(api_key, api_secret)

    config = load_json(CONFIG_FILE, {"max_position_pct": 0.20, "stop_loss_pct": 0.05})
    plan = load_json(PLAN_FILE)
    memory = load_json(MEMORY_FILE, {"trade_history": [], "weekly_summaries": [], "portfolio_snapshots": []})
    for key in ("trade_history", "weekly_summaries", "portfolio_snapshots"):
        memory.setdefault(key, [])

    now = datetime.now(ET)
    if not should_run(JOB, memory, trading.get_clock(), now):
        return 0

    if not plan or not plan.get("week_of"):
        log.error("No valid data/trade_plan.json found. Did Sunday's research run?")
        notify.job_failed("Monday — Execute Trades", "No valid data/trade_plan.json — Sunday research did not produce a plan.")
        return 1
    if not plan_is_fresh(plan, now):
        log.error(f"Trade plan is stale (week_of={plan['week_of']}) — refusing to trade it")
        notify.job_failed("Monday — Execute Trades", f"Trade plan is stale (week_of {plan['week_of']}). Sunday research likely failed.")
        mark_ran(JOB, memory, now)
        save_json(MEMORY_FILE, memory)
        return 1

    # Mark first: if anything below crashes, a later retry attempt must not
    # re-run the same orders. Failures are surfaced via exit code + Discord.
    mark_ran(JOB, memory, now)
    errors = []
    actions = []

    # --- Close positions marked for exit (cancel their stops first) ---
    closing = []
    for symbol in plan.get("close_positions", []):
        reason = plan.get("close_reasoning", {}).get(symbol, "plan exit")
        try:
            order = close_symbol(trading, symbol)
            if order is None:
                continue
            closing.append(symbol)
            log.info(f"CLOSING {symbol} — {reason}")
            actions.append(f"CLOSED {symbol}: {reason}")
            notify.trade_close(symbol, reason)
            memory["trade_history"].append({
                "time": now.isoformat(), "action": "CLOSE", "symbol": symbol, "reason": reason,
            })
        except Exception as e:
            log.error(f"Failed to close {symbol}: {e}")
            errors.append(f"close {symbol}: {e}")

    still_held = wait_until_flat(trading, closing) if closing else []
    if still_held:
        log.warning(f"Closes not yet filled: {still_held} — budget below excludes their cash")

    # --- Execute buys ---
    max_monthly = config.get("max_entries_per_symbol_per_month", 2)
    this_month = now.strftime("%Y-%m")
    monthly_counts = {}
    for t in memory["trade_history"]:
        if t.get("action") == "BUY" and t.get("time", "")[:7] == this_month:
            sym = t.get("symbol", "")
            monthly_counts[sym] = monthly_counts.get(sym, 0) + 1

    # Guardrails: position cap + gross-exposure budget (no margin).
    account = trading.get_account()
    equity = float(account.portfolio_value)
    positions = trading.get_all_positions()
    gross_invested = sum(float(p.market_value) for p in positions)
    max_positions = config.get("max_positions", 5)
    max_gross = config.get("max_gross_exposure", 0.95)
    open_slots = max_positions - len(positions)
    remaining_budget = max(equity * max_gross - gross_invested, 0.0)
    log.info(
        f"Budget: equity ${equity:,.0f} | invested ${gross_invested:,.0f} "
        f"({gross_invested / equity * 100 if equity else 0:.0f}%) | "
        f"open slots {open_slots} | room ${remaining_budget:,.0f}"
    )

    for trade in plan.get("buys", []):
        symbol = trade["symbol"]
        pct = trade.get("qty_pct", config["max_position_pct"])

        if open_slots <= 0:
            log.info(f"Position cap reached ({max_positions}) — skipping remaining buys")
            break
        if remaining_budget <= 0:
            log.info(f"Gross-exposure cap reached ({max_gross:.0%}) — skipping remaining buys")
            break
        if is_held(trading, symbol):
            log.info(f"{symbol}: already held, skipping buy")
            continue
        if monthly_counts.get(symbol, 0) >= max_monthly:
            log.info(f"{symbol}: hit monthly entry cap ({max_monthly}), skipping")
            continue

        try:
            price = get_current_price(data, symbol)
        except Exception as e:
            log.error(f"{symbol}: could not fetch price — {e}")
            errors.append(f"price {symbol}: {e}")
            continue

        qty = calc_qty(equity, price, pct, remaining_budget)
        if qty == 0:
            log.warning(f"{symbol}: not enough room under the {max_gross:.0%} exposure cap, skipping")
            continue

        stop_price = round(price * (1 - config["stop_loss_pct"]), 2)

        try:
            buy_order = trading.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            ))
        except Exception as e:
            log.error(f"Failed to submit buy for {symbol}: {e}")
            errors.append(f"buy {symbol}: {e}")
            continue

        filled, fill_price, status = wait_for_fill(trading, buy_order.id, price)
        if filled:
            log.info(f"BUY FILLED: {qty} {symbol} @ ${fill_price:.2f}")
            stop_price = round(fill_price * (1 - config["stop_loss_pct"]), 2)
            open_slots -= 1
            remaining_budget = max(remaining_budget - qty * fill_price, 0.0)
        elif status in ("rejected", "canceled", "expired"):
            log.error(f"{symbol}: buy {status}")
            errors.append(f"buy {symbol}: {status}")
            continue
        else:
            # Still working — it may fill later with NO stop attached. Count it
            # against the budget and shout about it.
            log.error(f"{symbol}: buy not filled after {FILL_WAIT_S}s (status {status}) — no stop attached")
            errors.append(f"{symbol}: buy unfilled after {FILL_WAIT_S}s — position will be UNPROTECTED if it fills")
            open_slots -= 1
            remaining_budget = max(remaining_budget - qty * price, 0.0)

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
            "time": now.isoformat(), "action": "BUY", "symbol": symbol, "qty": qty,
            "price": fill_price, "stop_price": stop_price,
            "reasoning": trade.get("reasoning", ""), "confidence": trade.get("confidence", ""),
            "filled": filled,
        })
        actions.append(f"BUY {qty} {symbol} @ ${fill_price:.2f} | stop ${stop_price:.2f}")

        if filled:
            try:
                trading.submit_order(StopOrderRequest(
                    symbol=symbol, qty=qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC, stop_price=stop_price,
                ))
                log.info(f"  Stop loss set @ ${stop_price:.2f} (-{config['stop_loss_pct']*100:.0f}%)")
            except Exception as e:
                log.error(f"  STOP LOSS FAILED for {symbol}: {e}")
                errors.append(f"{symbol}: stop loss failed ({e}) — position is UNPROTECTED")

    # --- Snapshot ---
    account = trading.get_account()
    memory["portfolio_snapshots"].append({
        "time": now.isoformat(),
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
    })
    memory["portfolio_snapshots"] = memory["portfolio_snapshots"][-500:]

    if actions:
        memory["weekly_summaries"].append({
            "week_of": plan["week_of"], "thesis": plan.get("week_thesis", ""), "actions": actions,
        })

    save_json(MEMORY_FILE, memory)
    log.info("Execution complete. Memory updated.")

    if errors:
        notify.job_failed("Monday — Execute Trades", "\n".join(f"• {e}" for e in errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
