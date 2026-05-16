"""
Valuation reviewer: rejects stocks trading at extreme multiples vs. sector peers.
Catches "great company, terrible price" candidates that often round-trip after running.

Uses Price/Sales (trailing 12 months) — more reliable than P/E since it handles
unprofitable companies (PLTR, CRWD, etc.) and avoids one-time earnings noise.
"""
import yfinance as yf

# Sector median P/S — rough 2025 baselines. Refresh quarterly from sector ETF data.
# Source: SPDR sector ETF holdings + Bloomberg sector valuation tables.
SECTOR_MEDIAN_PS = {
    "Technology": 6.0,
    "Communication Services": 3.5,
    "Consumer Cyclical": 1.5,
    "Consumer Defensive": 1.2,
    "Financial Services": 2.5,
    "Healthcare": 1.8,
    "Industrials": 1.8,
    "Energy": 1.2,
    "Utilities": 2.0,
    "Basic Materials": 1.3,
    "Real Estate": 4.0,
}

# Reject if a stock's P/S exceeds this multiple of its sector's median.
# 2.5x is intentionally generous — catches extreme outliers only (e.g. PLTR at 16x sector).
MAX_PS_VS_SECTOR = 2.5


def assess_valuation(symbol: str) -> dict:
    """
    Returns {
        "verdict": "pass" | "reject" | "unknown",
        "ps": float | None,
        "sector": str | None,
        "sector_median": float | None,
        "ratio": float | None,
        "reason": str,
    }
    "unknown" never blocks — we don't want to lose candidates over missing yfinance data.
    """
    try:
        info = yf.Ticker(symbol).info
        ps = info.get("priceToSalesTrailing12Months")
        sector = info.get("sector")

        if ps is None or sector is None:
            return {
                "verdict": "unknown", "ps": ps, "sector": sector,
                "sector_median": None, "ratio": None,
                "reason": "missing P/S or sector — not blocking",
            }

        median = SECTOR_MEDIAN_PS.get(sector)
        if median is None:
            return {
                "verdict": "unknown", "ps": round(ps, 2), "sector": sector,
                "sector_median": None, "ratio": None,
                "reason": f"no median for sector '{sector}' — not blocking",
            }

        ratio = ps / median
        if ratio > MAX_PS_VS_SECTOR:
            return {
                "verdict": "reject", "ps": round(ps, 2), "sector": sector,
                "sector_median": median, "ratio": round(ratio, 2),
                "reason": f"P/S {ps:.1f} = {ratio:.1f}x {sector} median {median} (cap {MAX_PS_VS_SECTOR}x)",
            }
        return {
            "verdict": "pass", "ps": round(ps, 2), "sector": sector,
            "sector_median": median, "ratio": round(ratio, 2),
            "reason": f"P/S {ps:.1f} = {ratio:.1f}x {sector} median {median}",
        }
    except Exception as e:
        return {
            "verdict": "unknown", "ps": None, "sector": None,
            "sector_median": None, "ratio": None,
            "reason": f"yfinance error: {e}",
        }
