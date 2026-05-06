import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MEMORY_FILE = "memory.json"
CONFIG_FILE = "config.json"
ET = ZoneInfo("America/New_York")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_bars(data_client, symbol, limit=60):
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, limit=limit)
    bars = data_client.get_stock_bars(req)
    df = bars.df
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    return df.sort_index()


def compute_signals(df, short, long):
    df = df.copy()
    df["sma_s"] = df["close"].rolling(short).mean()
    df["sma_l"] = df["close"].rolling(long).mean()

    if len(df) < 2:
        return None

    cur, prev = df.iloc[-1], df.iloc[-2]
    buy = float(prev["sma_s"]) <= float(prev["sma_l"]) and float(cur["sma_s"]) > float(cur["sma_l"])
    sell = float(prev["sma_s"]) >= float(prev["sma_l"]) and float(cur["sma_s"]) < float(cur["sma_l"])

    return {
        "price": round(float(cur["close"]), 2),
        "sma_short": round(float(cur["sma_s"]), 2),
        "sma_long": round(float(cur["sma_l"]), 2),
        "buy": buy,
        "sell": sell,
    }


def get_position_qty(trading_client, symbol):
    try:
        return float(trading_client.get_open_position(symbol).qty)
    except Exception:
        return 0.0


def calc_buy_qty(account, price, max_pct):
    max_value = float(account.portfolio_value) * max_pct
    affordable = min(float(account.buying_power) * 0.95, max_value)
    return max(int(affordable / price), 0)


def main():
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_SECRET_KEY"]

    trading = TradingClient(api_key, api_secret, paper=True)
    data = StockHistoricalDataClient(api_key, api_secret)

    memory = load_json(MEMORY_FILE, {"last_run": None, "trade_history": [], "portfolio_snapshots": [], "strategy_notes": []})
    config = load_json(CONFIG_FILE, {"watchlist": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"], "max_position_pct": 0.20, "sma_short": 20, "sma_long": 50})

    now = datetime.now(ET)
    log.info(f"Run started: {now.isoformat()}")

    clock = trading.get_clock()
    if not clock.is_open:
        log.info("Market closed — skipping trade execution")
        memory["last_run"] = now.isoformat()
        memory["strategy_notes"].append(f"{now.date()}: Market closed at run time")
        save_json(MEMORY_FILE, memory)
        return

    account = trading.get_account()
    portfolio_value = float(account.portfolio_value)
    log.info(f"Portfolio: ${portfolio_value:,.2f} | Buying power: ${float(account.buying_power):,.2f}")

    actions = []

    for symbol in config["watchlist"]:
        try:
            df = get_bars(data, symbol, limit=config["sma_long"] + 10)
            if len(df) < config["sma_long"]:
                log.info(f"{symbol}: insufficient history, skipping")
                continue

            sig = compute_signals(df, config["sma_short"], config["sma_long"])
            if sig is None:
                continue

            qty_held = get_position_qty(trading, symbol)
            log.info(
                f"{symbol}: ${sig['price']} | SMA{config['sma_short']}={sig['sma_short']} "
                f"SMA{config['sma_long']}={sig['sma_long']} | held={qty_held} | buy={sig['buy']} sell={sig['sell']}"
            )

            if sig["buy"] and qty_held == 0:
                qty = calc_buy_qty(account, sig["price"], config["max_position_pct"])
                if qty > 0:
                    trading.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                    note = f"BUY {qty} {symbol} @ ~${sig['price']} (golden cross)"
                    log.info(note)
                    actions.append(note)
                    memory["trade_history"].append({"time": now.isoformat(), "action": "BUY", "symbol": symbol, "qty": qty, "price": sig["price"], "reason": "golden cross"})

            elif sig["sell"] and qty_held > 0:
                trading.submit_order(MarketOrderRequest(symbol=symbol, qty=qty_held, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
                note = f"SELL {qty_held} {symbol} @ ~${sig['price']} (death cross)"
                log.info(note)
                actions.append(note)
                memory["trade_history"].append({"time": now.isoformat(), "action": "SELL", "symbol": symbol, "qty": qty_held, "price": sig["price"], "reason": "death cross"})

        except Exception as e:
            log.error(f"{symbol}: {e}")

    memory["portfolio_snapshots"].append({"time": now.isoformat(), "portfolio_value": portfolio_value, "buying_power": float(account.buying_power)})
    memory["portfolio_snapshots"] = memory["portfolio_snapshots"][-168:]  # keep 1 week

    if actions:
        memory["strategy_notes"].append(f"{now.date()}: " + " | ".join(actions))

    memory["last_run"] = now.isoformat()
    save_json(MEMORY_FILE, memory)
    log.info("Memory saved. Run complete.")


if __name__ == "__main__":
    main()
