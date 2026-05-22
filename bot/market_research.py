"""
Market researcher: per-candidate context that goes deeper than headlines.

For each top candidate, gathers:
  1. Sector momentum     — is the stock's SPDR sector ETF leading or lagging SPY?
  2. Analyst price target — is the stock trading above or below Wall Street consensus?
  3. Earnings recency     — days since last report + surprise % (chase risk flag)

All slow yfinance calls are batched into one Ticker per symbol for efficiency.
Designed to run on the ~20 top candidates only — not the full universe.
"""
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, timezone


# yfinance sector name → SPDR sector ETF ticker
SECTOR_ETFS = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
}

# Tag chase risk if earnings hit within this window
POST_EARNINGS_CHASE_DAYS = 10


def get_all_sector_etfs() -> list:
    """Return all sector ETFs so research.py can fetch their bars in one batch."""
    return list(SECTOR_ETFS.values())


def compute_sector_momentum(sector_closes: dict, spy_ret_60: float, lookback: int = 60) -> dict:
    """
    Given {etf_symbol: price_series} and SPY's 60-day return, return:
        {etf: {"return_60d_pct": x, "vs_spy_pct": y}}
    vs_spy_pct > 0 means the sector is leading the broad market.
    """
    out = {}
    for etf, prices in sector_closes.items():
        prices = prices.dropna()
        if len(prices) < lookback:
            continue
        ret = float((prices.iloc[-1] - prices.iloc[-lookback]) / prices.iloc[-lookback])
        out[etf] = {
            "return_60d_pct": round(ret * 100, 2),
            "vs_spy_pct": round((ret - spy_ret_60) * 100, 2),
        }
    return out


def _analyst_view(info: dict, current_price: float) -> dict:
    mean = info.get("targetMeanPrice")
    n = info.get("numberOfAnalystOpinions")
    if mean is None or not current_price:
        return {"mean_target": None, "pct_vs_current": None, "analyst_count": n}
    pct = (mean - current_price) / current_price * 100
    return {
        "mean_target": round(float(mean), 2),
        "pct_vs_current": round(pct, 2),  # positive = upside per analysts; negative = overvalued
        "analyst_count": int(n) if n else None,
        "high_target": info.get("targetHighPrice"),
        "low_target": info.get("targetLowPrice"),
    }


def _earnings_context(ticker: yf.Ticker) -> dict:
    """Days since last earnings + actual vs estimate surprise % (if available)."""
    try:
        ed = ticker.earnings_dates
        if ed is None or ed.empty:
            return {"days_since": None, "surprise_pct": None}
        # earnings_dates index is tz-aware datetime; we want most recent PAST date
        now = pd.Timestamp.now(tz=ed.index.tz)
        past = ed[ed.index < now]
        if past.empty:
            return {"days_since": None, "surprise_pct": None}
        most_recent = past.index.max()
        row = past.loc[most_recent]
        days = (now - most_recent).days
        surprise = row.get("Surprise(%)")
        return {
            "days_since": int(days),
            "surprise_pct": round(float(surprise), 2) if pd.notna(surprise) else None,
            "chase_risk": days <= POST_EARNINGS_CHASE_DAYS,
        }
    except Exception:
        return {"days_since": None, "surprise_pct": None}


def enrich_candidate(symbol: str, current_price: float, sector_momentum: dict) -> dict:
    """
    Per-symbol research: sector context + analyst view + earnings recency.
    Returns {} on failure rather than blocking — research data is supplemental.
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        sector = info.get("sector")
        etf = SECTOR_ETFS.get(sector) if sector else None
        sector_data = sector_momentum.get(etf, {}) if etf else {}

        return {
            "sector": sector,
            "sector_etf": etf,
            "sector_vs_spy_60d_pct": sector_data.get("vs_spy_pct"),
            "sector_return_60d_pct": sector_data.get("return_60d_pct"),
            "analyst": _analyst_view(info, current_price),
            "earnings": _earnings_context(ticker),
        }
    except Exception as e:
        return {"error": str(e)}
