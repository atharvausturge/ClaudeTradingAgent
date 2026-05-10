"""
Mid-week and Friday position monitor.
On Friday: closes losing positions (below config threshold), holds winners into next week.
Any day: logs portfolio state to memory.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MEMORY_FILE = "memory.json"
CONFIG_FILE = "config.json"


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
        req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day, limit=6)
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
    # Find the most recent Monday snapshot
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
    is_friday = now.weekday() == 4
    close_threshold = config.get("friday_close_threshold", -0.02)
    account = trading.get_account()
    positions = trading.get_all_positions()

    log.info(f"{'FRIDAY CLEANUP' if is_friday else 'MID-WEEK CHECK'} | {len(positions)} position(s) | Portfolio: ${float(account.portfolio_value):,.2f}")

    position_snapshot = []
    held, closed = [], []

    for pos in positions:
        symbol = pos.symbol
        pl_pct = round(float(pos.unrealized_plpc) * 100, 2)
        pl_dollar = round(float(pos.unrealized_pl), 2)
        log.info(f"  {symbol}: {pl_pct:+.2f}% (${pl_dollar:+.2f})")
        position_snapshot.append({"symbol": symbol, "pl_pct": pl_pct, "pl_dollar": pl_dollar})

        if is_friday and pl_pct < close_threshold * 100:
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
        elif is_friday:
            log.info(f"  → HOLDING {symbol} into next week ({pl_pct:+.2f}%)")
            held.append({"symbol": symbol, "pl_pct": pl_pct})

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

    save_json(MEMORY_FILE, memory)
    log.info("Monitor complete. Memory updated.")


if __name__ == "__main__":
    main()
