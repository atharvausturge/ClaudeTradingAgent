"""
Reddit sentiment: replaces Stocktwits with a richer retail-trader signal.

Searches r/wallstreetbets, r/stocks, r/investing, r/StockMarket for posts
mentioning each ticker in the past week. Classifies bullish/bearish via
keyword heuristics, weights by upvote ratio, and returns structured output
matching the old Stocktwits schema so analyze.py needs minimal changes.

Uses Reddit's public JSON API — no auth needed. One request per symbol
(multi-subreddit combined search) keeps it under rate limits.
"""
import re
import time
import requests

SUBREDDITS = "wallstreetbets+stocks+investing+StockMarket"
SEARCH_URL = "https://www.reddit.com/r/{subs}/search.json"
HEADERS = {"User-Agent": "trading-agent/1.0 (research bot; +https://github.com)"}
TIMEOUT = 10
DELAY_BETWEEN_REQUESTS = 1.2  # Reddit allows ~60/min unauth; we stay well under

BULLISH_TOKENS = {
    "bullish", "buy", "buying", "long", "calls", "call", "moon", "rocket",
    "🚀", "📈", "💎", "🙌", "breakout", "beat", "beats", "raised", "upgrade",
    "outperform", "overweight", "yolo", "loaded", "loading", "accumulate",
    "dip buying", "btfd", "all in", "bag holder no",
}
BEARISH_TOKENS = {
    "bearish", "sell", "selling", "short", "shorts", "puts", "put", "crash",
    "dump", "dumping", "📉", "🩸", "miss", "missed", "downgrade", "underperform",
    "underweight", "bag holder", "bagholder", "rugpull", "rug pull", "tanking",
    "exit", "exiting", "stop loss", "loss porn", "guh",
}


def _classify(text: str) -> str:
    """Return 'bullish' / 'bearish' / 'neutral' based on keyword counts."""
    t = text.lower()
    bull = sum(1 for tok in BULLISH_TOKENS if tok in t)
    bear = sum(1 for tok in BEARISH_TOKENS if tok in t)
    if bull > bear and bull >= 1:
        return "bullish"
    if bear > bull and bear >= 1:
        return "bearish"
    return "neutral"


def _fetch_posts(symbol: str) -> list:
    """Return list of post dicts from Reddit search, or [] on failure."""
    # Search for both "$SYMBOL" and bare ticker to catch both conventions
    query = f'"${symbol}" OR "{symbol}"'
    try:
        resp = requests.get(
            SEARCH_URL.format(subs=SUBREDDITS),
            headers=HEADERS,
            params={
                "q": query, "restrict_sr": "on", "sort": "new",
                "t": "week", "limit": 50,
            },
            timeout=TIMEOUT,
        )
        if resp.status_code == 429:
            time.sleep(30)
            resp = requests.get(
                SEARCH_URL.format(subs=SUBREDDITS),
                headers=HEADERS,
                params={"q": query, "restrict_sr": "on", "sort": "new", "t": "week", "limit": 50},
                timeout=TIMEOUT,
            )
        if resp.status_code != 200:
            return []
        return [c["data"] for c in resp.json().get("data", {}).get("children", [])]
    except Exception:
        return []


DAILY_THREAD_RE = re.compile(r"daily\s+(discussion|thread)|fundamentals\s+friday|what.+watching", re.IGNORECASE)
MIN_TAGGED_FOR_VERDICT = 3  # require at least N bullish+bearish posts before calling sentiment


def _is_relevant(post: dict, symbol: str) -> bool:
    """
    Strict relevance: ticker must appear in title OR with $ prefix in body.
    Filters out posts that only mention the ticker in passing in a long body.
    """
    title = post.get("title", "")
    body = post.get("selftext", "")
    if DAILY_THREAD_RE.search(title):
        return False
    # As standalone token in title (e.g. "AAPL earnings", "Bought AAPL today")
    title_re = re.compile(rf"(^|[^A-Za-z0-9]){re.escape(symbol)}([^A-Za-z0-9]|$)")
    if title_re.search(title):
        return True
    # OR $TICKER anywhere (highest-confidence ticker mention)
    if f"${symbol}" in title or f"${symbol}" in body:
        return True
    return False


def fetch_reddit_sentiment(symbols: list) -> dict:
    """
    Returns {symbol: sentiment_dict} where sentiment_dict has the same shape
    as the old Stocktwits output so analyze.py can read it transparently.
    """
    result = {}
    for symbol in symbols:
        posts = _fetch_posts(symbol)
        relevant = [p for p in posts if _is_relevant(p, symbol)]

        if not relevant:
            result[symbol] = {
                "mention_count": 0, "bull_pct": None, "bear_pct": None,
                "overall": "no_data", "subreddit_breakdown": {}, "top_posts": [],
            }
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        bull, bear, neutral = 0, 0, 0
        subreddit_counts = {}
        upvote_ratios = []
        for p in relevant:
            text = (p.get("title", "") + " " + p.get("selftext", ""))[:2000]
            verdict = _classify(text)
            if verdict == "bullish":
                bull += 1
            elif verdict == "bearish":
                bear += 1
            else:
                neutral += 1
            sub = p.get("subreddit", "?")
            subreddit_counts[sub] = subreddit_counts.get(sub, 0) + 1
            ratio = p.get("upvote_ratio")
            if ratio is not None:
                upvote_ratios.append(ratio)

        tagged = bull + bear
        bull_pct = round(bull / tagged * 100, 1) if tagged > 0 else None
        bear_pct = round(bear / tagged * 100, 1) if tagged > 0 else None

        # Require a meaningful sample before claiming a sentiment direction
        if tagged < MIN_TAGGED_FOR_VERDICT:
            overall = "thin_data"
        elif bull_pct is None:
            overall = "neutral"
        elif bull_pct >= 70:
            overall = "strongly_bullish"
        elif bull_pct >= 55:
            overall = "bullish"
        elif bear_pct >= 70:
            overall = "strongly_bearish"
        elif bear_pct >= 55:
            overall = "bearish"
        else:
            overall = "mixed"

        top_sorted = sorted(relevant, key=lambda p: p.get("score", 0), reverse=True)[:5]
        top_posts = [
            {
                "title": p.get("title", "")[:200],
                "subreddit": p.get("subreddit"),
                "score": p.get("score"),
                "upvote_ratio": p.get("upvote_ratio"),
                "num_comments": p.get("num_comments"),
            }
            for p in top_sorted
        ]

        result[symbol] = {
            "mention_count": len(relevant),
            "bull_pct": bull_pct,
            "bear_pct": bear_pct,
            "neutral_count": neutral,
            "avg_upvote_ratio": round(sum(upvote_ratios) / len(upvote_ratios), 2) if upvote_ratios else None,
            "subreddit_breakdown": subreddit_counts,
            "overall": overall,
            "top_posts": top_posts,
        }

        time.sleep(DELAY_BETWEEN_REQUESTS)

    return result
