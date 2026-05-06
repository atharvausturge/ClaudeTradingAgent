"""
Broad US large-cap universe (~250 liquid stocks across all S&P 500 sectors).
Used by research.py to scan for the best weekly opportunities.
"""

UNIVERSE = [
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
    "CHD", "HRL", "SJM", "MKC", "MNST", "KHC", "STZ", "TAP", "BF.B",
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
    # Semiconductors (extra coverage — high-beta, high-opportunity)
    "INTC", "ON", "SWKS", "QRVO", "MPWR", "ENTG",
]


def get_universe():
    return list(dict.fromkeys(UNIVERSE))  # deduplicate while preserving order
