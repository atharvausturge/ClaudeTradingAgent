"""
Backtests the weekly swing trading strategy over 2 years of historical data.
Scoring: relative strength vs SPY + RSI filter (no overbought buys).
Rules: buy top 2-3 stocks Monday, 5% stop loss daily, close losers Friday.
"""
import os
import json
from datetime import datetime, timedelta
from collections import Counter

import time
import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from universe import get_universe

# --- Config ---
WATCHLIST = get_universe()
BENCHMARK = "SPY"
INITIAL_CAPITAL = 100_000
MAX_POSITIONS = 3
POSITION_SIZE_PCT = 0.20
STOP_LOSS_PCT = 0.05
RSI_PERIOD = 14
RS_LOOKBACK = 60           # 60-day trend filter: is this stock in a long-term uptrend vs SPY?
DIP_LOOKBACK = 5           # 5-day return: did it pull back recently? (the entry signal)
WARMUP_DAYS = 80           # extra days before backtest start for indicator warmup
BACKTEST_YEARS = 2
MIN_RS_THRESHOLD = 0.0     # stock must beat SPY over 60 days (in uptrend)
FRIDAY_CLOSE_THRESHOLD = -0.02  # only close on Friday if down more than 2%
MAX_ENTRIES_PER_SYMBOL_PER_MONTH = 2  # cap repeated bets on the same stock


def compute_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def fetch_data_batched(api_key, api_secret):
    client = StockHistoricalDataClient(api_key, api_secret)
    end = datetime.now()
    start = end - timedelta(days=BACKTEST_YEARS * 365 + WARMUP_DAYS)

    all_symbols = list(set([BENCHMARK] + WATCHLIST))
    batch_size = 100
    batches = [all_symbols[i:i + batch_size] for i in range(0, len(all_symbols), batch_size)]
    print(f"Fetching {BACKTEST_YEARS} years of daily bars for {len(all_symbols)} symbols in {len(batches)} batches...")

    all_close_frames, all_low_frames = [], []

    for i, batch in enumerate(batches):
        print(f"  Batch {i+1}/{len(batches)} ({len(batch)} symbols)...")
        try:
            req = StockBarsRequest(symbol_or_symbols=batch, timeframe=TimeFrame.Day, start=start, end=end)
            raw = client.get_stock_bars(req).df
            if raw.empty:
                continue
            raw = raw.reset_index()
            all_close_frames.append(raw.pivot(index="timestamp", columns="symbol", values="close"))
            all_low_frames.append(raw.pivot(index="timestamp", columns="symbol", values="low"))
        except Exception as e:
            print(f"  Batch {i+1} error: {e}")
        if i < len(batches) - 1:
            time.sleep(0.5)

    close = pd.concat(all_close_frames, axis=1) if all_close_frames else pd.DataFrame()
    low = pd.concat(all_low_frames, axis=1) if all_low_frames else pd.DataFrame()

    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    low.index = pd.to_datetime(low.index).tz_localize(None).normalize()

    return close, low


def score_stocks(close_history, spy_history, symbols):
    """
    Trend + Dip scoring:
      1. 60-day RS > 0: stock is in a long-term uptrend vs SPY (trend filter)
      2. RSI 35-65: not overbought, not in freefall
      3. Score = magnitude of recent 5-day dip (bigger pullback in uptrend = better entry)
    Logic: buy temporary weakness in structurally strong stocks.
    """
    scores = {}
    for symbol in symbols:
        if symbol not in close_history.columns:
            continue
        sym = close_history[symbol].dropna()
        if len(sym) < RS_LOOKBACK + RSI_PERIOD:
            continue

        if float(sym.iloc[-1]) < 25:  # skip low-priced / speculative stocks
            continue

        rsi_val = compute_rsi(sym, RSI_PERIOD).iloc[-1]
        if pd.isna(rsi_val) or rsi_val > 65 or rsi_val < 35:
            continue  # skip overbought or in freefall

        # Long-term trend filter: must be outperforming SPY over 60 days
        sym_ret_60 = (sym.iloc[-1] - sym.iloc[-RS_LOOKBACK]) / sym.iloc[-RS_LOOKBACK]
        spy_ret_60 = (spy_history.iloc[-1] - spy_history.iloc[-RS_LOOKBACK]) / spy_history.iloc[-RS_LOOKBACK]
        rs_60 = float(sym_ret_60 - spy_ret_60)
        if rs_60 < MIN_RS_THRESHOLD:
            continue  # not in an uptrend vs market

        # Entry signal: how much has it pulled back in the last 5 days?
        if len(sym) < DIP_LOOKBACK + 1:
            continue
        dip_5d = float((sym.iloc[-1] - sym.iloc[-DIP_LOOKBACK]) / sym.iloc[-DIP_LOOKBACK])

        # Score: prefer stocks that dipped more (better entry) within the uptrend
        # Negative dip = pullback, so we negate it (bigger pullback = higher score)
        # Also reward stronger 60-day trend
        score = (-dip_5d * 0.6) + (rs_60 * 0.4)
        scores[symbol] = score

    return scores


def run_backtest(close, low):
    trading_days = close.index.sort_values()
    cutoff = trading_days[WARMUP_DAYS]  # start after warmup

    # Group trading days into calendar weeks
    week_map = {}
    for day in trading_days[trading_days >= cutoff]:
        wk = day.isocalendar()[:2]  # (year, week_number)
        week_map.setdefault(wk, []).append(day)

    spy_close = close[BENCHMARK]

    cash = INITIAL_CAPITAL
    positions = {}        # symbol -> {qty, entry_price, stop_price, entry_date}
    monthly_entries = {}  # (symbol, year, month) -> count
    portfolio_values = []
    weekly_returns = []
    trades = []

    for wk in sorted(week_map):
        week_days = sorted(week_map[wk])
        entry_day = week_days[0]
        exit_day = week_days[-1]

        entry_loc = trading_days.get_loc(entry_day)
        history = close.iloc[:entry_loc + 1]
        spy_hist = spy_close.iloc[:entry_loc + 1]

        # -- Monday: score and buy --
        scores = score_stocks(history, spy_hist, WATCHLIST)
        held = set(positions.keys())
        slots = MAX_POSITIONS - len(held)

        if slots > 0 and scores:
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            candidates = [
                s for s, _ in ranked
                if s not in held
                and monthly_entries.get((s, entry_day.year, entry_day.month), 0) < MAX_ENTRIES_PER_SYMBOL_PER_MONTH
            ][:slots]

            for symbol in candidates:
                entry_price = close.loc[entry_day, symbol] if symbol in close.columns else np.nan
                if pd.isna(entry_price) or entry_price <= 0:
                    continue

                port_value = cash + sum(
                    positions[s]["qty"] * close.loc[entry_day, s]
                    for s in positions
                    if s in close.columns and not pd.isna(close.loc[entry_day, s])
                )
                position_value = min(cash * 0.95, port_value * POSITION_SIZE_PCT)
                qty = int(position_value / entry_price)
                if qty <= 0:
                    continue

                cash -= qty * entry_price
                stop_price = round(entry_price * (1 - STOP_LOSS_PCT), 2)
                positions[symbol] = {"qty": qty, "entry_price": entry_price, "stop_price": stop_price, "entry_date": entry_day}
                key = (symbol, entry_day.year, entry_day.month)
                monthly_entries[key] = monthly_entries.get(key, 0) + 1
                trades.append({"date": entry_day, "symbol": symbol, "action": "BUY",
                               "qty": qty, "price": entry_price, "rs_score": round(scores[symbol], 4)})

        # -- Each day: check stop losses using daily low --
        for day in week_days:
            for symbol in list(positions.keys()):
                if symbol not in low.columns or day not in low.index:
                    continue
                day_low = low.loc[day, symbol]
                pos = positions[symbol]
                if not pd.isna(day_low) and day_low <= pos["stop_price"]:
                    cash += pos["qty"] * pos["stop_price"]
                    pl_pct = (pos["stop_price"] - pos["entry_price"]) / pos["entry_price"] * 100
                    trades.append({"date": day, "symbol": symbol, "action": "STOP_LOSS",
                                   "qty": pos["qty"], "price": pos["stop_price"], "pl_pct": round(pl_pct, 2)})
                    del positions[symbol]

        # -- Friday: close meaningful losers, hold winners and small dips --
        for symbol in list(positions.keys()):
            if symbol not in close.columns or exit_day not in close.index:
                continue
            exit_price = close.loc[exit_day, symbol]
            if pd.isna(exit_price):
                continue
            pos = positions[symbol]
            pl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
            if pl_pct < FRIDAY_CLOSE_THRESHOLD * 100:  # only close if down more than threshold
                cash += pos["qty"] * exit_price
                trades.append({"date": exit_day, "symbol": symbol, "action": "FRIDAY_CLOSE",
                               "qty": pos["qty"], "price": exit_price, "pl_pct": round(pl_pct, 2)})
                del positions[symbol]

        # -- Record weekly portfolio value --
        port_value = cash
        for symbol, pos in positions.items():
            if symbol in close.columns and exit_day in close.index:
                p = close.loc[exit_day, symbol]
                if not pd.isna(p):
                    port_value += pos["qty"] * p

        portfolio_values.append({"date": exit_day, "value": port_value})
        if len(portfolio_values) > 1:
            prev = portfolio_values[-2]["value"]
            weekly_returns.append((port_value - prev) / prev * 100)

    return portfolio_values, trades, weekly_returns


def print_results(portfolio_values, trades, weekly_returns, close):
    if not portfolio_values:
        print("No backtest results generated.")
        return

    start_date = portfolio_values[0]["date"]
    end_date = portfolio_values[-1]["date"]
    final_value = portfolio_values[-1]["value"]
    total_return = (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    n_years = len(portfolio_values) / 52
    annualized = ((final_value / INITIAL_CAPITAL) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0

    # Max drawdown
    peak, max_dd = INITIAL_CAPITAL, 0
    for v in [pv["value"] for pv in portfolio_values]:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Win rate & Sharpe
    win_rate = sum(1 for r in weekly_returns if r > 0) / len(weekly_returns) * 100 if weekly_returns else 0
    avg_r = np.mean(weekly_returns) if weekly_returns else 0
    std_r = np.std(weekly_returns) if weekly_returns else 1
    sharpe = (avg_r / std_r) * np.sqrt(52) if std_r > 0 else 0

    # SPY benchmark
    spy = close[BENCHMARK].dropna()
    spy_in_range = spy[(spy.index >= start_date) & (spy.index <= end_date)]
    spy_return = (spy_in_range.iloc[-1] - spy_in_range.iloc[0]) / spy_in_range.iloc[0] * 100 if len(spy_in_range) > 1 else 0

    # Trade breakdown
    buys = [t for t in trades if t["action"] == "BUY"]
    stops = [t for t in trades if t["action"] == "STOP_LOSS"]
    friday_closes = [t for t in trades if t["action"] == "FRIDAY_CLOSE"]
    all_closes = stops + friday_closes

    stop_pl = [t["pl_pct"] for t in stops]
    friday_pl = [t["pl_pct"] for t in friday_closes]
    all_pl = stop_pl + friday_pl
    profitable_closes = sum(1 for p in all_pl if p > 0)
    trade_win_rate = profitable_closes / len(all_pl) * 100 if all_pl else 0
    avg_win = np.mean([p for p in all_pl if p > 0]) if any(p > 0 for p in all_pl) else 0
    avg_loss = np.mean([p for p in all_pl if p < 0]) if any(p < 0 for p in all_pl) else 0

    print()
    print("=" * 58)
    print("   BACKTEST RESULTS — Weekly Swing Trader (2 Years)")
    print(f"   Universe: {len(WATCHLIST)} stocks  |  Friday close < {FRIDAY_CLOSE_THRESHOLD*100:.0f}%  |  Stop: {STOP_LOSS_PCT*100:.0f}%")
    print("=" * 58)
    print(f"   Period:            {start_date.date()} → {end_date.date()}")
    print(f"   Starting capital:  ${INITIAL_CAPITAL:>10,.0f}")
    print(f"   Final value:       ${final_value:>10,.0f}")
    print(f"   Total return:      {total_return:>+10.1f}%")
    print(f"   Annualized return: {annualized:>+10.1f}%")
    print(f"   SPY buy & hold:    {spy_return:>+10.1f}%")
    print(f"   Alpha vs SPY:      {total_return - spy_return:>+10.1f}%")
    print("-" * 58)
    print(f"   Weekly win rate:   {win_rate:>10.1f}%")
    print(f"   Trade win rate:    {trade_win_rate:>10.1f}%")
    print(f"   Sharpe ratio:      {sharpe:>10.2f}")
    print(f"   Max drawdown:      {-max_dd:>+10.1f}%")
    print(f"   Avg win:           {avg_win:>+10.2f}%")
    print(f"   Avg loss:          {avg_loss:>+10.2f}%")
    print("-" * 58)
    print(f"   Total buys:        {len(buys):>10}")
    print(f"   Stop losses hit:   {len(stops):>10}")
    print(f"   Friday closes:     {len(friday_closes):>10}")
    if weekly_returns:
        print(f"   Best week:         {max(weekly_returns):>+10.1f}%")
        print(f"   Worst week:        {min(weekly_returns):>+10.1f}%")
        print(f"   Avg weekly return: {avg_r:>+10.2f}%")
    print("=" * 58)

    print("\n   Most traded symbols:")
    for symbol, count in Counter(t["symbol"] for t in buys).most_common(6):
        sym_trades = [t for t in all_closes if t["symbol"] == symbol]
        sym_avg_pl = np.mean([t["pl_pct"] for t in sym_trades]) if sym_trades else 0
        print(f"     {symbol:<6} {count:>3} entries   avg exit: {sym_avg_pl:>+.2f}%")

    print("\n   Recent trades (last 10):")
    for t in trades[-10:]:
        pl = f"  {t['pl_pct']:>+.2f}%" if "pl_pct" in t else f"  rs={t.get('rs_score', 0):>+.3f}"
        print(f"     {str(t['date'].date()):<12} {t['action']:<14} {t['symbol']:<6} @ ${t['price']:.2f}{pl}")
    print()


def main():
    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_SECRET_KEY")

    if not api_key or not api_secret:
        # Try loading from .env
        try:
            from dotenv import load_dotenv
            load_dotenv()
            api_key = os.environ.get("ALPACA_API_KEY")
            api_secret = os.environ.get("ALPACA_SECRET_KEY")
        except ImportError:
            pass

    if not api_key or not api_secret:
        print("Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.")
        return

    close, low = fetch_data_batched(api_key, api_secret)
    print(f"Data loaded: {len(close)} trading days, {len(close.columns)} symbols\n")

    print("Running backtest...")
    portfolio_values, trades, weekly_returns = run_backtest(close, low)

    print_results(portfolio_values, trades, weekly_returns, close)


if __name__ == "__main__":
    main()
