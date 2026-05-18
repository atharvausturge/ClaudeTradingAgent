"""
Pure computation — no network calls.
Reads bars_raw.json (written by Claude via WebFetch), scores the universe,
outputs top_candidates.json for Claude to analyze.
"""
import json
import sys
import numpy as np
import pandas as pd

RS_LOOKBACK = 60
DIP_LOOKBACK = 5
RSI_PERIOD = 14
MIN_PRICE = 25
RSI_MIN = 30
RSI_MAX = 65
TOP_N = 20


def compute_rsi(prices, period=14):
    s = pd.Series(prices)
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return float((100 - (100 / (1 + rs))).iloc[-1])


def score(bars: dict, benchmark="SPY") -> list:
    spy_closes = bars.get(benchmark, [])
    if len(spy_closes) < RS_LOOKBACK:
        print(f"Not enough SPY data ({len(spy_closes)} bars)", file=sys.stderr)
        return []

    spy_ret_60 = (spy_closes[-1] - spy_closes[-RS_LOOKBACK]) / spy_closes[-RS_LOOKBACK]
    results = []

    for symbol, closes in bars.items():
        if symbol == benchmark:
            continue
        min_bars = RS_LOOKBACK + RSI_PERIOD + DIP_LOOKBACK + 1
        if len(closes) < min_bars:
            continue
        try:
            price = closes[-1]
            if price < MIN_PRICE:
                continue
            rsi_val = compute_rsi(closes)
            if np.isnan(rsi_val) or rsi_val > RSI_MAX or rsi_val < RSI_MIN:
                continue
            sym_ret_60 = (closes[-1] - closes[-RS_LOOKBACK]) / closes[-RS_LOOKBACK]
            rs_60 = sym_ret_60 - spy_ret_60
            if rs_60 < 0:
                continue
            dip_5d = (closes[-1] - closes[-DIP_LOOKBACK]) / closes[-DIP_LOOKBACK]
            score_val = (-dip_5d * 0.6) + (rs_60 * 0.4)
            results.append({
                "symbol": symbol,
                "score": round(score_val, 4),
                "rs_60d": round(rs_60 * 100, 2),
                "dip_5d": round(dip_5d * 100, 2),
                "rsi": round(rsi_val, 1),
                "price": round(price, 2),
            })
        except Exception as e:
            print(f"  {symbol}: {e}", file=sys.stderr)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def main():
    try:
        with open("bars_raw.json") as f:
            bars = json.load(f)
    except FileNotFoundError:
        print("bars_raw.json not found — WebFetch step must write this file first", file=sys.stderr)
        sys.exit(1)

    print(f"Scoring {len(bars)} symbols...")
    ranked = score(bars)
    top = ranked[:TOP_N]

    with open("top_candidates.json", "w") as f:
        json.dump({"top": top, "passing_filter": len(ranked)}, f, indent=2)

    print(f"{len(ranked)} passed filters. Top {TOP_N}:")
    for c in top:
        print(f"  {c['symbol']:<6} score={c['score']:+.4f}  RS60={c['rs_60d']:+.1f}%  dip5d={c['dip_5d']:+.1f}%  RSI={c['rsi']}")


if __name__ == "__main__":
    main()
