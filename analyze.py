"""
Scores each top candidate on news + sentiment using Groq (1-10),
combines with the technical score using configurable weights,
and selects the top trades by combined score.

The AI is a scorer, not a picker — the math makes the final call.
"""
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from groq import Groq
import notify

ET = ZoneInfo("America/New_York")
RESEARCH_FILE = "research_data.json"
PLAN_FILE = "trade_plan.json"
MEMORY_FILE = "memory.json"
CONFIG_FILE = "config.json"


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default or {}


SYSTEM_PROMPT = """You are a buy-side equity analyst. For each stock you receive structured fields plus headlines and social sentiment. Score each stock 1-10 on near-term swing-trade attractiveness (1-2 week hold).

Each input line contains, in order:
  - Valuation (P/S vs sector median)
  - Sector momentum (its SPDR ETF's 60-day return vs SPY)
  - Analyst consensus target ($, % upside or downside from current)
  - Earnings recency (days since last report, surprise %, CHASE RISK flag if <10d)
  - Recent news headlines
  - Stocktwits sentiment

Scoring guide:
  9-10  Strong catalyst, sector leading, analyst upside, no chase risk, bullish sentiment
  7-8   Good setup with mild concerns (modest sector, neutral targets, etc.)
  5-6   Mixed — no clear edge; pass unless nothing better is available
  3-4   Multiple red flags (sector lagging, trades above analyst targets, post-earnings chase)
  1-2   Hard avoid (earnings this week, scandal, sector breakdown, analyst -20%+ downside)

Weighting rules — apply these mechanically, then add news/sentiment color:
  - Stock trades >10% ABOVE mean analyst target → max score 5 (Wall Street thinks it's overvalued)
  - Stock trades >15% BELOW mean analyst target → +1 (analyst upside)
  - CHASE RISK flag (within 10 days of earnings, even if beat) → max score 6
  - Sector ETF -3% or worse vs SPY (60d) → max score 6 (don't fight a falling sector)
  - Sector ETF +5% or better vs SPY → +1 (riding the wave)
  - Reddit sentiment: strongly_bullish with ≥10 mentions → +1; strongly_bearish with ≥10 mentions → -1
  - Reddit thin_data or no_data → ignore (no signal)
  - Absence of news is neutral (score 5), not negative

Return ONLY a valid JSON object, no markdown:
{
  "scores": {
    "TICKER": {"score": 7, "reason": "one sentence citing the SPECIFIC signals you weighted"},
    ...
  },
  "week_thesis": "2-3 sentences: what's the macro this week, which sectors are leading, what themes stand out"
}"""


def build_scoring_prompt(candidates: dict, recent_summaries: list) -> str:
    lines = []
    for symbol, data in candidates.items():
        news = data.get("news", [])
        rd = data.get("reddit", {})
        val = data.get("valuation", {})
        mc = data.get("market_context", {})
        an = mc.get("analyst", {}) or {}
        ea = mc.get("earnings", {}) or {}

        headlines = " | ".join(
            a["headline"] for a in news[:5] if "headline" in a
        ) or "No recent news"

        if rd.get("bull_pct") is not None:
            sentiment_str = (
                f"reddit {rd['overall']} "
                f"({rd['bull_pct']}% bull / {rd.get('bear_pct')}% bear, "
                f"{rd['mention_count']} mentions)"
            )
        elif rd.get("mention_count", 0) > 0:
            sentiment_str = f"reddit {rd.get('overall', 'thin_data')} ({rd['mention_count']} mentions, low signal)"
        else:
            sentiment_str = "reddit no_data"

        val_str = f"P/S {val['ps']} ({val['ratio']}x {val['sector']} median)" if val.get("ps") and val.get("sector_median") else "P/S n/a"

        sector_str = (
            f"{mc.get('sector_etf', '?')} sector {mc.get('sector_vs_spy_60d_pct', 0):+.1f}% vs SPY 60d"
            if mc.get("sector_vs_spy_60d_pct") is not None else "sector n/a"
        )

        if an.get("mean_target") and an.get("pct_vs_current") is not None:
            arrow = "upside" if an["pct_vs_current"] > 0 else "downside"
            analyst_str = f"analyst target ${an['mean_target']} ({an['pct_vs_current']:+.1f}% {arrow}, n={an.get('analyst_count', '?')})"
        else:
            analyst_str = "analyst target n/a"

        if ea.get("days_since") is not None:
            chase = " [CHASE RISK]" if ea.get("chase_risk") else ""
            surp = f", surprise {ea['surprise_pct']:+.1f}%" if ea.get("surprise_pct") is not None else ""
            earnings_str = f"earnings d+{ea['days_since']}{surp}{chase}"
        else:
            earnings_str = "earnings n/a"

        lines.append(
            f"{symbol}: {val_str} | {sector_str} | {analyst_str} | {earnings_str}\n"
            f"   news: {headlines[:250]}\n"
            f"   social: {sentiment_str}"
        )

    history_str = json.dumps(recent_summaries, indent=2) if recent_summaries else "None"

    return f"""Recent 4-week trade history (avoid re-entering same stocks at monthly cap):
{history_str}

Score the news and sentiment for each of these candidates:
{chr(10).join(lines)}"""


def extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start:end])


def normalize(values: dict) -> dict:
    """Min-max normalize a {symbol: float} dict to 0–1 range."""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def main():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")

    research = load_json(RESEARCH_FILE)
    if not research:
        raise RuntimeError(f"{RESEARCH_FILE} not found or empty — run research.py first")

    config = load_json(CONFIG_FILE, {})
    memory = load_json(MEMORY_FILE, {})

    candidates = research.get("top_candidates", {})
    today = datetime.now(ET).strftime("%Y-%m-%d")
    max_positions = config.get("max_positions", 3)
    tech_weight = config.get("tech_weight", 0.5)
    sentiment_weight = config.get("sentiment_weight", 0.5)
    max_monthly = config.get("max_entries_per_symbol_per_month", 2)
    recent_summaries = memory.get("weekly_summaries", [])[-4:]

    # --- Empty market: skip Groq, write empty plan ---
    if not candidates:
        print("No candidates passed technical filters — writing empty plan.")
        empty_thesis = "No stocks passed the technical filters this week. Capital preservation."
        with open(PLAN_FILE, "w") as f:
            json.dump({
                "week_of": today, "week_thesis": empty_thesis,
                "buys": [], "close_positions": [], "close_reasoning": {},
                "hold_positions": [], "skip_reasoning": {},
            }, f, indent=2)
        notify.weekly_plan(week_of=today, thesis=empty_thesis, buys=[], holds=[], closes=[])
        return

    # --- Step 1: Groq scores each candidate's news + sentiment (1–10) ---
    client = Groq(api_key=api_key)
    print(f"Scoring {len(candidates)} candidates on news + sentiment (Groq llama-3.3-70b)...")

    ai_scores = {}
    week_thesis = "AI scoring unavailable this week. Trades selected on technical signal only."

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1024,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_scoring_prompt(candidates, recent_summaries)},
            ],
        )
        result = extract_json(response.choices[0].message.content)
        ai_scores = result.get("scores", {})
        week_thesis = result.get("week_thesis", week_thesis)
        print(f"  Groq scored {len(ai_scores)} of {len(candidates)} candidates")
    except Exception as e:
        print(f"  Groq scoring failed ({e}) — falling back to pure technical ranking")

    # --- Step 2: Normalize technical and sentiment scores independently ---
    tech_raw = {sym: data["technicals"]["score"] for sym, data in candidates.items()}
    tech_norm = normalize(tech_raw)

    sentiment_raw = {
        sym: float(ai_scores[sym]["score"])
        for sym in candidates
        if sym in ai_scores and isinstance(ai_scores[sym].get("score"), (int, float))
    }
    sentiment_norm = normalize(sentiment_raw)

    # --- Step 3: Combine into a single ranked score ---
    combined = {}
    for sym in candidates:
        t = tech_norm.get(sym, 0.5)
        s = sentiment_norm.get(sym, 0.5)  # neutral default if Groq didn't score it
        combined[sym] = round(tech_weight * t + sentiment_weight * s, 4)

    ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)

    print(f"\nCombined rankings (tech {tech_weight:.0%} / sentiment {sentiment_weight:.0%}):")
    for sym, score in ranked[:10]:
        t = tech_norm.get(sym, 0)
        s = sentiment_norm.get(sym, 0.5)
        ai_info = ai_scores.get(sym, {})
        raw_sentiment = ai_info.get("score", "—")
        reason = ai_info.get("reason", "no AI score")
        print(f"  {sym:<6} combined={score:.3f}  tech={t:.3f}  sentiment={s:.3f}  (AI raw={raw_sentiment})  {reason}")

    # --- Step 4: Select buys, respecting monthly cap ---
    this_month = today[:7]
    monthly_counts: dict = {}
    for entry in memory.get("trade_history", []):
        if entry.get("action") == "BUY" and entry.get("time", "")[:7] == this_month:
            sym = entry.get("symbol", "")
            monthly_counts[sym] = monthly_counts.get(sym, 0) + 1

    buys = []
    skip_reasoning = {}
    for sym, score in ranked:
        if len(buys) >= max_positions:
            break
        if monthly_counts.get(sym, 0) >= max_monthly:
            skip_reasoning[sym] = f"monthly entry cap ({max_monthly}) reached"
            continue
        ai_info = ai_scores.get(sym, {})
        buys.append({
            "symbol": sym,
            "combined_score": score,
            "tech_score": round(tech_norm.get(sym, 0), 3),
            "sentiment_score": round(sentiment_norm.get(sym, 0.5), 3),
            "reasoning": ai_info.get("reason", "selected by technical score"),
            "confidence": "high" if score > 0.66 else "medium" if score > 0.33 else "low",
            "qty_pct": config.get("max_position_pct", 0.20),
        })

    plan = {
        "week_of": today,
        "week_thesis": week_thesis,
        "buys": buys,
        "close_positions": [],
        "close_reasoning": {},
        "hold_positions": [],
        "skip_reasoning": skip_reasoning,
        "scoring_breakdown": {
            sym: {
                "combined": combined[sym],
                "tech_norm": round(tech_norm.get(sym, 0), 3),
                "sentiment_norm": round(sentiment_norm.get(sym, 0.5), 3),
                "ai_raw_score": ai_scores.get(sym, {}).get("score"),
                "ai_reason": ai_scores.get(sym, {}).get("reason"),
            }
            for sym in candidates
        },
    }

    with open(PLAN_FILE, "w") as f:
        json.dump(plan, f, indent=2)

    print(f"\nTrade plan written to {PLAN_FILE}")
    print(f"  Week thesis: {week_thesis[:120]}")
    print(f"  Buys: {[b['symbol'] for b in buys]}")
    if skip_reasoning:
        print(f"  Skipped: {skip_reasoning}")

    notify.weekly_plan(week_of=today, thesis=week_thesis, buys=buys, holds=[], closes=[])


if __name__ == "__main__":
    main()
