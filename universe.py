"""
Dynamically fetches the S&P 1500 universe (S&P 500 + S&P 400 + S&P 600) from
Wikipedia using pandas.read_html. Auto-updates on index reconstitutions.
Falls back to a hardcoded large-cap list if the fetch fails.
"""

import pandas as pd

_WIKI_INDEXES = [
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
]

# Possible column names across the three Wikipedia tables
_SYMBOL_COLS = ["Symbol", "Ticker symbol", "Ticker"]

# Fallback in case Wikipedia is unreachable
_FALLBACK = [
    # Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "CSCO", "IBM", "QCOM",
    "TXN", "MU", "AMAT", "LRCX", "KLAC", "ADI", "MRVL", "NOW", "PANW", "SNPS",
    "CDNS", "INTU", "ADBE", "PLTR", "FTNT", "DELL", "HPE", "CRWD", "ZS", "NET",
    "DDOG", "SNOW", "MDB", "WDAY", "ACN", "CTSH", "EPAM", "GLW", "KEYS", "ANSS",
    # Communication & Internet
    "GOOGL", "META", "AMZN", "NFLX", "UBER", "BKNG", "ABNB", "COIN",
    "SPOT", "EA", "DIS", "CMCSA", "T", "VZ",
    # Consumer Discretionary
    "TSLA", "HD", "LOW", "MCD", "SBUX", "NKE", "LULU", "TGT", "COST", "WMT",
    "CMG", "YUM", "DRI", "DPZ", "QSR", "ORLY", "AZO", "BBY", "ROST", "TJX",
    "F", "GM", "APTV", "BWA",
    # Consumer Staples
    "PG", "KO", "PEP", "PM", "MO", "MDLZ", "GIS", "K", "CPB", "CL",
    "CHD", "HRL", "SJM", "MKC", "MNST", "KHC", "STZ", "TAP", "BF-B",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "USB", "PNC", "TFC", "COF",
    "AXP", "V", "MA", "PYPL", "SCHW", "BLK", "BX", "KKR", "APO", "SPGI",
    "MCO", "ICE", "CME", "CBOE", "AIG", "MET", "PRU", "AFL", "ALL", "PGR",
    "TRV", "HIG", "CB", "MMC", "AON",
    # Healthcare
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "ABT", "TMO", "DHR", "MDT",
    "BMY", "AMGN", "GILD", "VRTX", "REGN", "BIIB", "IQV", "BSX", "EW", "SYK",
    "ZBH", "BDX", "BAX", "HOLX", "IDXX", "MTD", "RMD", "CI", "HUM", "CVS",
    "MOH", "ELV", "CNC",
    # Industrials
    "CAT", "DE", "EMR", "ETN", "GE", "HON", "ITW", "MMM", "PH", "ROK",
    "RTX", "NOC", "GD", "LMT", "BA", "TDG", "CARR", "OTIS", "AME", "FTV",
    "GWW", "CMI", "PCAR", "IR", "XYL", "PNR", "SNA", "TT", "VRSK", "CTAS",
    "FAST", "URI", "RSG", "WM",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "HAL", "MPC", "VLO", "PSX", "HES",
    "DVN", "OXY", "APA", "FANG", "BKR", "NOV",
    # Utilities & Clean Energy
    "NEE", "D", "SO", "DUK", "AEP", "EXC", "XEL", "PPL", "WEC", "AWK",
    # Materials
    "LIN", "APD", "SHW", "ECL", "NEM", "FCX", "NUE", "STLD", "ALB", "CF",
    "MOS", "IFF", "PPG", "VMC", "MLM",
    # Real Estate
    "AMT", "PLD", "CCI", "EQIX", "PSA", "SPG", "O", "VICI",
    # Semiconductors
    "INTC", "ON", "SWKS", "QRVO", "MPWR", "ENTG",
]


def _normalize(ticker: str) -> str:
    return ticker.replace(".", "-").strip().upper()


def fetch_sp1500() -> list:
    symbols = set()
    for url in _WIKI_INDEXES:
        try:
            tables = pd.read_html(url)
            for df in tables:
                for col in _SYMBOL_COLS:
                    if col in df.columns:
                        raw = df[col].dropna().astype(str).tolist()
                        symbols.update(raw)
                        break
        except Exception as e:
            print(f"  Warning: could not fetch {url}: {e}")
    return sorted({_normalize(s) for s in symbols if s and s != "NAN"})


def get_universe() -> list:
    symbols = fetch_sp1500()
    if len(symbols) < 100:
        print(f"  Warning: only {len(symbols)} symbols fetched — using fallback universe")
        return list(dict.fromkeys(_FALLBACK))
    print(f"  Loaded {len(symbols)} symbols from S&P 1500 (Wikipedia)")
    return symbols
