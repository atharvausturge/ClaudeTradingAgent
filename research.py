"""
Fetches news, technicals, and account state for the weekly research session.
Outputs research_data.json for Claude to analyze and form a trade plan.
"""
import os
import json
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo("America/New_York")
CONFIG_FILE = "config.json"
MEMORY_FILE = "memory.json"
OUTPUT_FILE = "research_data.json"


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default or {}


def fetch_news(api_key, api_secret, symbols, days=5):
    start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    result = {}
    for symbol in symbols:
        try:
            resp = requests.get(
                "https://data.alpaca.markets/v1beta1/news",
                headers=headers,
                params={"symbols": symbol, "start": start, "limit": 15, "sort": "desc"},
                timeout=10,
            )
            articles = resp.json().get("news", [])
            result[symbol] = [
                {
                    "headline": a["headline"],
                    "summary": a.get("summary", "")[:300],
                    "published": a["created_at"],
                    "source": a.get("source", ""),
                }
                for a in articles
            ]
        except Exception as e:
            result[symbol] = [{"error": str(e)}]
    return result


def compute_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 1) if not rsi.empty else None


def fetch_technicals(data_client, symbols, benchmark):
    all_symbols = list(set([benchmark] + symbols))
    req = StockBarsRequest(symbol_or_symbols=all_symbols, timeframe=TimeFrame.Day, limit=60)
    bars_df = data_client.get_stock_bars(req).df

    def get_closes(symbol):
        if isinstance(bars_df.index, pd.MultiIndex):
            return bars_df.xs(symbol, level="symbol")["close"]
        return bars_df["close"]

    spy_close = get_closes(benchmark)
    spy_ret_20d = float((spy_close.iloc[-1] - spy_close.iloc[-20]) / spy_close.iloc[-20] * 100) if len(spy_close) >= 20 else 0

    technicals = {}
    for symbol in symbols:
        try:
            close = get_closes(symbol)
            price = round(float(close.iloc[-1]), 2)
            ret_5d = round(float((close.iloc[-1] - close.iloc[-5]) / close.iloc[-5] * 100), 2) if len(close) >= 5 else None
            ret_20d = round(float((close.iloc[-1] - close.iloc[-20]) / close.iloc[-20] * 100), 2) if len(close) >= 20 else None
            rel_strength = round(ret_20d - spy_ret_20d, 2) if ret_20d is not None else None
            rsi = compute_rsi(close)
            technicals[symbol] = {
                "price": price,
                "rsi_14": rsi,
                "return_5d_pct": ret_5d,
                "return_20d_pct": ret_20d,
                "relative_strength_vs_spy": rel_strength,
                "rsi_signal": "overbought" if rsi and rsi > 70 else ("oversold" if rsi and rsi < 30 else "neutral"),
            }
        except Exception as e:
            technicals[symbol] = {"error": str(e)}

    return technicals


def main():
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_SECRET_KEY"]

    trading = TradingClient(api_key, api_secret, paper=True)
    data = StockHistoricalDataClient(api_key, api_secret)

    config = load_json(CONFIG_FILE, {"watchlist": [], "benchmark": "SPY"})
    memory = load_json(MEMORY_FILE, {})

    watchlist = config["watchlist"]
    benchmark = config.get("benchmark", "SPY")

    account = trading.get_account()
    raw_positions = trading.get_all_positions()
    positions = {
        p.symbol: {
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_pl_pct": round(float(p.unrealized_plpc) * 100, 2),
        }
        for p in raw_positions
    }

    print(f"Fetching news for {len(watchlist)} symbols...")
    news = fetch_news(api_key, api_secret, watchlist, days=5)

    print("Fetching technicals...")
    technicals = fetch_technicals(data, watchlist, benchmark)

    research = {
        "generated_at": datetime.now(ET).isoformat(),
        "account": {
            "portfolio_value": float(account.portfolio_value),
            "buying_power": float(account.buying_power),
            "cash": float(account.cash),
        },
        "current_positions": positions,
        "recent_weekly_summaries": memory.get("weekly_summaries", [])[-4:],
        "recent_trade_history": memory.get("trade_history", [])[-20:],
        "watchlist_data": {
            symbol: {
                "technicals": technicals.get(symbol, {}),
                "recent_news": news.get(symbol, []),
            }
            for symbol in watchlist
        },
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(research, f, indent=2, default=str)

    print(f"Research data written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
