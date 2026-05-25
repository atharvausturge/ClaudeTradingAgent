"""
score_local.py — offline scoring from bars_raw.json
Reads bars_raw.json, scores each symbol on trend+dip signal (same logic as
bot/research.py), writes top_candidates.json with the top 20 by score.
"""
import json
import numpy as np

INPUT_FILE   = "bars_raw.json"
OUTPUT_FILE  = "top_candidates.json"
TOP_N        = 20
RS_LOOKBACK  = 60
DIP_LOOKBACK = 5
RSI_PERIOD   = 14
MIN_PRICE    = 25.0
MIN_RS       = -0.05     # must beat SPY by at least -5pp over 60 days

# Symbols to exclude from scoring (earnings this week or known issues)
EARNINGS_BLACKOUT = {"CRM", "COST", "DELL"}   # reporting Wed/Thu this week
ALWAYS_SKIP       = set()


def compute_rsi(prices, period=14):
    prices = np.asarray(prices, dtype=float)
    deltas = np.diff(prices)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # EWM via Wilder smoothing (com = period-1)
    alpha = 1.0 / period
    def ewm(arr):
        result = np.zeros(len(arr))
        result[0] = arr[0]
        for i in range(1, len(arr)):
            result[i] = arr[i] * alpha + result[i-1] * (1 - alpha)
        return result
    avg_gain = ewm(gains)
    avg_loss = ewm(losses)
    with np.errstate(divide='ignore', invalid='ignore'):
        rs  = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi[-1])


def score_universe(bars: dict, spy_prices: list) -> dict:
    spy = np.asarray(spy_prices, dtype=float)
    if len(spy) < RS_LOOKBACK:
        raise ValueError("SPY has fewer than RS_LOOKBACK bars")

    spy_ret_60 = (spy[-1] - spy[-RS_LOOKBACK]) / spy[-RS_LOOKBACK]
    scores = {}

    min_bars = RS_LOOKBACK + RSI_PERIOD + DIP_LOOKBACK

    for sym, prices_raw in bars.items():
        if sym in EARNINGS_BLACKOUT or sym in ALWAYS_SKIP or sym == "SPY":
            continue

        prices = np.asarray(prices_raw, dtype=float)
        if len(prices) < min_bars:
            continue
        if prices[-1] < MIN_PRICE:
            continue

        rsi_val = compute_rsi(prices[-RSI_PERIOD - 1:], RSI_PERIOD)
        if np.isnan(rsi_val) or rsi_val > 75 or rsi_val < 25:
            continue

        sym_ret_60 = (prices[-1] - prices[-RS_LOOKBACK]) / prices[-RS_LOOKBACK]
        rs_60 = sym_ret_60 - spy_ret_60
        if rs_60 < MIN_RS:
            continue

        dip_5d = (prices[-1] - prices[-DIP_LOOKBACK]) / prices[-DIP_LOOKBACK]
        if dip_5d > 0.10:          # anti-chase: skip >10% 5-day pop
            continue

        score = (-dip_5d * 0.6) + (rs_60 * 0.4)
        scores[sym] = {
            "score":   round(float(score), 4),
            "rs_60d":  round(float(rs_60 * 100), 2),
            "dip_5d":  round(float(dip_5d * 100), 2),
            "rsi":     round(float(rsi_val), 1),
            "price":   round(float(prices[-1]), 2),
        }

    return scores


def main():
    with open(INPUT_FILE) as f:
        bars = json.load(f)

    spy_prices = bars.get("SPY")
    if not spy_prices:
        raise RuntimeError("SPY not found in bars_raw.json")

    print(f"Loaded {len(bars)} symbols from {INPUT_FILE}")
    print(f"SPY current close: ${spy_prices[-1]:.2f}")
    print(f"SPY 60d return: {((spy_prices[-1]-spy_prices[-60])/spy_prices[-60]*100):.1f}%")

    scores = score_universe(bars, spy_prices)

    if not scores:
        print("WARNING: No symbols passed filters — writing empty top_candidates.json")
        with open(OUTPUT_FILE, "w") as f:
            json.dump([], f, indent=2)
        return

    ranked = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    top_n  = ranked[:TOP_N]

    print(f"\n{len(scores)} symbols passed filters. Top {TOP_N}:")
    for rank, (sym, d) in enumerate(top_n, 1):
        flag = " ⚠EARNINGS" if sym in EARNINGS_BLACKOUT else ""
        print(f"  {rank:>2}. {sym:<6} score={d['score']:+.4f}  RS60={d['rs_60d']:+.1f}%  "
              f"dip5d={d['dip_5d']:+.1f}%  RSI={d['rsi']}  price=${d['price']:.2f}{flag}")

    output = [{"symbol": sym, **d, "earnings_blackout": sym in EARNINGS_BLACKOUT}
              for sym, d in top_n]

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\ntop_candidates.json written ({len(output)} candidates).")


if __name__ == "__main__":
    main()
