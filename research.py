"""
Scans the full large-cap universe (~250 stocks), scores each on trend+dip signal,
fetches Alpaca news + Stocktwits social sentiment for the top N candidates.
Outputs research_data.json for Claude to analyze and pick 2-3 trades.
"""
import os
import json
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import yfinance as yf
from universe import get_universe

ET = ZoneInfo("America/New_York")
CONFIG_FILE = "config.json"
MEMORY_FILE = "memory.json"
OUTPUT_FILE = "research_data.json"
BATCH_SIZE = 100
RS_LOOKBACK = 60
DIP_LOOKBACK = 5
RSI_PERIOD = 14


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default or {}


def compute_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _parse_bars(raw):
    """Extract close and volume pivots from a bars DataFrame."""
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    if isinstance(raw.index, pd.MultiIndex):
        raw = raw.reset_index()
        close_pivot = raw.pivot(index="timestamp", columns="symbol", values="close")
        volume_pivot = raw.pivot(index="timestamp", columns="symbol", values="volume")
    else:
        close_pivot = raw[["close"]]
        volume_pivot = raw[["volume"]] if "volume" in raw.columns else pd.DataFrame()
    close_pivot.index = pd.to_datetime(close_pivot.index).tz_localize(None).normalize()
    if not volume_pivot.empty:
        volume_pivot.index = pd.to_datetime(volume_pivot.index).tz_localize(None).normalize()
    return close_pivot, volume_pivot


def fetch_bars_batch(data_client, symbols, limit=80):
    import re
    all_closes = {}
    all_avg_volumes = {}
    bad_symbols = set()
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]

    for i, batch in enumerate(batches):
        clean_batch = [s for s in batch if s not in bad_symbols]
        if not clean_batch:
            continue

        # Keep retrying the batch, stripping one bad symbol per attempt
        while clean_batch:
            try:
                bar_start = (datetime.now() - timedelta(days=int(limit * 2))).date()
                req = StockBarsRequest(symbol_or_symbols=clean_batch, timeframe=TimeFrame.Day, start=bar_start)
                raw_df = data_client.get_stock_bars(req).df
                close_pivot, volume_pivot = _parse_bars(raw_df)
                for sym in close_pivot.columns:
                    all_closes[sym] = close_pivot[sym].dropna()
                for sym in volume_pivot.columns:
                    all_avg_volumes[sym] = float(volume_pivot[sym].dropna().mean())
                break  # success
            except Exception as e:
                err = str(e)
                if "invalid symbol" in err.lower():
                    match = re.search(r'invalid symbol[:\s]+"?([A-Z0-9/.\-]+)"?', err, re.IGNORECASE)
                    if match:
                        bad_sym = match.group(1).strip()
                        bad_symbols.add(bad_sym)
                        print(f"  Skipping invalid symbol: {bad_sym}")
                        clean_batch = [s for s in clean_batch if s != bad_sym]
                    else:
                        print(f"  Batch {i+1} unrecognized invalid symbol error: {err}")
                        break
                else:
                    print(f"  Batch {i+1} error: {e}")
                    break

        if i < len(batches) - 1:
            time.sleep(0.3)

    if bad_symbols:
        print(f"  Removed {len(bad_symbols)} invalid symbol(s): {', '.join(sorted(bad_symbols))}")
    return all_closes, all_avg_volumes


MIN_AVG_VOLUME = 250_000  # min daily avg volume — filters truly illiquid names


def score_universe(closes, spy_closes, avg_volumes=None, min_rs_threshold=0.0):
    scores = {}
    avg_volumes = avg_volumes or {}
    min_bars = RS_LOOKBACK + RSI_PERIOD + DIP_LOOKBACK
    spy = spy_closes.dropna()
    if len(spy) < RS_LOOKBACK:
        return scores
    spy_ret_60 = float((spy.iloc[-1] - spy.iloc[-RS_LOOKBACK]) / spy.iloc[-RS_LOOKBACK])

    rejected = {"bars": 0, "volume": 0, "price": 0, "rsi": 0, "rs60": 0, "chase": 0}

    for symbol, prices in closes.items():
        if len(prices) < min_bars:
            rejected["bars"] += 1
            continue
        try:
            if avg_volumes.get(symbol, MIN_AVG_VOLUME) < MIN_AVG_VOLUME:
                rejected["volume"] += 1
                continue
            if float(prices.iloc[-1]) < 25:
                rejected["price"] += 1
                continue
            rsi_val = float(compute_rsi(prices, RSI_PERIOD).iloc[-1])
            if pd.isna(rsi_val) or rsi_val > 75 or rsi_val < 25:
                rejected["rsi"] += 1
                continue
            sym_ret_60 = float((prices.iloc[-1] - prices.iloc[-RS_LOOKBACK]) / prices.iloc[-RS_LOOKBACK])
            rs_60 = sym_ret_60 - spy_ret_60
            if rs_60 < min_rs_threshold:
                rejected["rs60"] += 1
                continue
            dip_5d = float((prices.iloc[-1] - prices.iloc[-DIP_LOOKBACK]) / prices.iloc[-DIP_LOOKBACK])
            # Anti-chase: skip stocks that just popped >10% in 5 days (post-earnings gap risk)
            if dip_5d > 0.10:
                rejected["chase"] += 1
                continue
            score = (-dip_5d * 0.6) + (rs_60 * 0.4)
            scores[symbol] = {
                "score": round(score, 4),
                "rs_60d": round(rs_60 * 100, 2),
                "dip_5d": round(dip_5d * 100, 2),
                "rsi": round(rsi_val, 1),
                "price": round(float(prices.iloc[-1]), 2),
            }
        except Exception:
            continue

    total = len(closes)
    passed = len(scores)
    print(f"  Filter breakdown ({total} symbols with bar data):")
    print(f"    Insufficient bars : {rejected['bars']}")
    print(f"    Low volume (<250k): {rejected['volume']}")
    print(f"    Price < $25       : {rejected['price']}")
    print(f"    RSI out of 25-75  : {rejected['rsi']}")
    print(f"    RS60 < {min_rs_threshold:.0%} (vs SPY): {rejected['rs60']}")
    print(f"    5d pop > +10% (chase): {rejected['chase']}")
    print(f"    PASSED all filters: {passed}")
    return scores


def fetch_alpaca_news(api_key, api_secret, symbols, days=5):
    start = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    result = {}
    for symbol in symbols:
        try:
            resp = requests.get(
                "https://data.alpaca.markets/v1beta1/news",
                headers=headers,
                params={"symbols": symbol, "start": start, "limit": 10, "sort": "desc"},
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


def has_upcoming_earnings(symbol: str, days: int = 7) -> bool:
    """Return True if the stock has a known earnings event within `days` calendar days."""
    try:
        cal = yf.Ticker(symbol).calendar
        if not cal:
            return False
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return False
        nearest = min(pd.Timestamp(d) for d in dates)
        days_until = (nearest.normalize() - pd.Timestamp.now().normalize()).days
        return 0 <= days_until <= days
    except Exception:
        return False  # if unknown, don't block


def main():
    load_dotenv()
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_SECRET_KEY"]

    trading = TradingClient(api_key, api_secret, paper=True)
    data_client = StockHistoricalDataClient(api_key, api_secret)

    config = load_json(CONFIG_FILE, {"benchmark": "SPY", "top_n_candidates": 20})
    memory = load_json(MEMORY_FILE, {})

    universe = get_universe()
    benchmark = config.get("benchmark", "SPY")
    top_n = config.get("top_n_candidates", 20)

    # --- Step 1: Score entire universe on technicals ---
    from market_research import get_all_sector_etfs, compute_sector_momentum, enrich_candidate
    sector_etfs = get_all_sector_etfs()
    print(f"Scanning {len(universe)} stocks + {len(sector_etfs)} sector ETFs...")
    all_symbols = list(set([benchmark] + universe + sector_etfs))
    closes, avg_volumes = fetch_bars_batch(data_client, all_symbols, limit=RS_LOOKBACK + RSI_PERIOD + DIP_LOOKBACK + 5)
    spy_closes = closes.pop(benchmark, pd.Series(dtype=float))
    avg_volumes.pop(benchmark, None)

    # Pull sector ETFs out of the scored universe — they're context, not candidates
    sector_closes = {etf: closes.pop(etf) for etf in sector_etfs if etf in closes}
    for etf in sector_etfs:
        avg_volumes.pop(etf, None)
    print(f"Bars fetched for {len(closes)} symbols. Scoring...")

    min_rs = config.get("min_rs_threshold", 0.0)
    scores = score_universe(closes, spy_closes, avg_volumes, min_rs_threshold=min_rs)
    ranked = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    top_candidates = [sym for sym, _ in ranked[:top_n]]

    # Compute sector ETF returns vs SPY for the market researcher
    spy_60d_return = float((spy_closes.iloc[-1] - spy_closes.iloc[-RS_LOOKBACK]) / spy_closes.iloc[-RS_LOOKBACK])
    sector_momentum = compute_sector_momentum(sector_closes, spy_60d_return, lookback=RS_LOOKBACK)

    print(f"{len(scores)} stocks passed filters. Top {top_n}: {', '.join(top_candidates)}")

    # --- Step 1b: Remove stocks with earnings in the next 7 days ---
    print("Checking earnings calendar for top candidates...")
    earnings_blackout = [sym for sym in top_candidates if has_upcoming_earnings(sym)]
    if earnings_blackout:
        print(f"  Earnings blackout (excluded): {', '.join(earnings_blackout)}")
        top_candidates = [sym for sym in top_candidates if sym not in earnings_blackout]
        remaining = [sym for sym, _ in ranked if sym not in top_candidates and sym not in earnings_blackout]
        top_candidates = (top_candidates + remaining)[:top_n]

    # --- Step 1c: Reject overvalued stocks (P/S > 2.5x sector median) ---
    print("Reviewing valuations for top candidates...")
    from valuation import assess_valuation
    valuation_results = {sym: assess_valuation(sym) for sym in top_candidates}
    valuation_blackout = [sym for sym, v in valuation_results.items() if v["verdict"] == "reject"]
    if valuation_blackout:
        for sym in valuation_blackout:
            print(f"  {sym} REJECTED: {valuation_results[sym]['reason']}")
        top_candidates = [sym for sym in top_candidates if sym not in valuation_blackout]
        already_excluded = set(top_candidates) | set(earnings_blackout) | set(valuation_blackout)
        remaining = [sym for sym, _ in ranked if sym not in already_excluded]
        # Also valuation-check the backfill candidates so we don't just replace bad with bad
        for sym in remaining:
            if len(top_candidates) >= top_n:
                break
            v = assess_valuation(sym)
            valuation_results[sym] = v
            if v["verdict"] != "reject":
                top_candidates.append(sym)
            else:
                print(f"  {sym} backfill REJECTED: {v['reason']}")
        top_candidates = top_candidates[:top_n]

    # --- Step 1d: Market research per candidate (sector, analyst targets, earnings recency) ---
    print(f"Enriching {len(top_candidates)} candidates with market context (sector / analyst / earnings)...")
    market_context = {}
    for sym in top_candidates:
        current_price = scores[sym]["price"] if sym in scores else None
        market_context[sym] = enrich_candidate(sym, current_price, sector_momentum)
        ctx = market_context[sym]
        an = ctx.get("analyst", {})
        ea = ctx.get("earnings", {})
        flags = []
        if an.get("pct_vs_current") is not None and an["pct_vs_current"] < -10:
            flags.append(f"analyst -{abs(an['pct_vs_current']):.0f}% downside")
        if ea.get("chase_risk"):
            flags.append(f"post-earnings d+{ea['days_since']}")
        if ctx.get("sector_vs_spy_60d_pct") is not None and ctx["sector_vs_spy_60d_pct"] < -3:
            flags.append(f"sector lagging ({ctx['sector_vs_spy_60d_pct']:+.1f}% vs SPY)")
        flag_str = f"  ⚠ {', '.join(flags)}" if flags else ""
        sec = ctx.get("sector_etf") or "?"
        print(f"  {sym:<6} {sec:<5} target={an.get('mean_target')} ({an.get('pct_vs_current')}%) earnings d+{ea.get('days_since')}{flag_str}")

    # --- Step 2: Fetch Alpaca news for top candidates ---
    print(f"Fetching Alpaca news for top {top_n} candidates...")
    news = fetch_alpaca_news(api_key, api_secret, top_candidates, days=5)

    # --- Step 3: Fetch Reddit social sentiment for top candidates ---
    from reddit_sentiment import fetch_reddit_sentiment
    print(f"Fetching Reddit sentiment (wsb/stocks/investing/StockMarket) for top {top_n} candidates...")
    reddit = fetch_reddit_sentiment(top_candidates)

    # --- Step 4: Account + positions ---
    account = trading.get_account()
    positions = {
        p.symbol: {
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_pl_pct": round(float(p.unrealized_plpc) * 100, 2),
        }
        for p in trading.get_all_positions()
    }

    # --- Step 5: Build research output ---
    research = {
        "generated_at": datetime.now(ET).isoformat(),
        "universe_size": len(universe),
        "stocks_passing_filter": len(scores),
        "account": {
            "portfolio_value": float(account.portfolio_value),
            "buying_power": float(account.buying_power),
            "cash": float(account.cash),
        },
        "current_positions": positions,
        "recent_weekly_summaries": memory.get("weekly_summaries", [])[-4:],
        "sector_momentum": sector_momentum,
        "top_candidates": {
            symbol: {
                "technicals": scores[symbol],
                "valuation": valuation_results.get(symbol, {}),
                "market_context": market_context.get(symbol, {}),
                "news": news.get(symbol, []),
                "reddit": reddit.get(symbol, {}),
            }
            for symbol in top_candidates
        },
        "full_scores": {sym: d for sym, d in ranked},
        "valuation_rejected": [
            {"symbol": sym, **valuation_results[sym]}
            for sym in valuation_blackout
        ],
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(research, f, indent=2, default=str)

    print(f"\nResearch data written to {OUTPUT_FILE}")
    print(f"\nTop 10 by technical score:")
    for sym, d in ranked[:10]:
        r = reddit.get(sym, {})
        if r.get("bull_pct") is not None:
            sent = f"{r['mention_count']}p {r['overall']}"
        else:
            sent = f"{r.get('mention_count', 0)}p {r.get('overall', 'no_data')}"
        print(f"  {sym:<6} score={d['score']:>+.4f}  RS60={d['rs_60d']:>+.1f}%  dip5d={d['dip_5d']:>+.1f}%  RSI={d['rsi']}  [{sent}]")


if __name__ == "__main__":
    main()
