"""
Mid-week and Friday position monitor.
On Friday: closes losing positions, holds winners into next week.
Any day: logs portfolio state to memory.
"""
import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MEMORY_FILE = "memory.json"


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default or {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def main():
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_SECRET_KEY"]

    trading = TradingClient(api_key, api_secret, paper=True)
    memory = load_json(MEMORY_FILE, {"trade_history": [], "portfolio_snapshots": []})

    clock = trading.get_clock()
    if not clock.is_open:
        log.info("Market is closed. Skipping monitor.")
        return

    now = datetime.now(ET)
    is_friday = now.weekday() == 4
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

        if is_friday and pl_pct < 0:
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
        elif is_friday and pl_pct >= 0:
            log.info(f"  → HOLDING {symbol} into next week ({pl_pct:+.2f}%)")
            held.append({"symbol": symbol, "pl_pct": pl_pct})

    if is_friday:
        notify.friday_summary(float(account.portfolio_value), held, closed)
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
