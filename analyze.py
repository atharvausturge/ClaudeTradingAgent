"""
Calls the Gemini API with research_data.json and writes trade_plan.json.
Uses Google's free tier (gemini-2.0-flash) — no cost for low-volume usage.
"""
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import google.generativeai as genai

ET = ZoneInfo("America/New_York")
RESEARCH_FILE = "research_data.json"
PLAN_FILE = "trade_plan.json"
MEMORY_FILE = "memory.json"


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default or {}


SYSTEM_PROMPT = """You are an autonomous swing trading agent. Your job is to analyze weekly research data and produce a precise JSON trade plan.

You have three signal layers to evaluate for each candidate:

**TECHNICALS:**
- score: higher = stronger dip in a stronger uptrend. Best candidates are 0.05+
- rs_60d: % outperformance vs SPY over 60 days — the long-term trend filter
- dip_5d: % pullback over last 5 days — negative means it dipped (good entry signal)
- rsi: 35-50 is ideal — trending up but not overbought

**NEWS SENTIMENT:**
- Flag negative catalysts: earnings miss, downgrade, regulatory action, CEO departure
- Flag positive catalysts: earnings beat, new contract, upgrade, buyback, partnership
- Avoid stocks with earnings THIS week — high binary risk

**SOCIAL SENTIMENT (Stocktwits):**
- bull_pct / bear_pct: what % of tagged posts are bullish vs bearish
- overall: strongly_bullish / bullish / mixed / bearish / strongly_bearish
- Read recent_posts for qualitative color — retail often reacts before analysts
- Mixed/bearish Stocktwits despite good technicals = caution

**Score each candidate:**
- STRONG BUY: good technicals + positive/neutral news + bullish social + no earnings this week
- BUY: good technicals + neutral news + no major red flags
- AVOID: bad news OR strongly bearish social OR earnings this week

**Portfolio rules:**
- Max 3 total positions (new buys + existing holds)
- 20% of portfolio per position (qty_pct: 0.20)
- Do not re-enter same stock more than 2x this calendar month (check recent_weekly_summaries)
- If nothing clears all three bars, set buys to [] — capital preservation is valid

**Output:** Return ONLY a valid JSON object. No markdown, no explanation outside the JSON."""


def build_user_prompt(research: dict, memory: dict) -> str:
    today = datetime.now(ET).strftime("%Y-%m-%d")
    recent_summaries = memory.get("weekly_summaries", [])[-4:]

    # Strip full_scores (254-stock dump) — top_candidates already has the pre-ranked best
    trimmed = {k: v for k, v in research.items() if k != "full_scores"}

    return f"""Today is {today}. Analyze the research data below and produce a trade plan.

Recent trade history (last 4 weeks):
{json.dumps(recent_summaries, indent=2)}

Research data (top 20 candidates with technicals, news, and social sentiment):
{json.dumps(trimmed, indent=2)}

Return a JSON object in exactly this shape:
{{
  "week_of": "{today}",
  "week_thesis": "2-3 sentences: macro context this week + why these specific stocks stand out",
  "buys": [
    {{
      "symbol": "TICKER",
      "reasoning": "cite specific news headlines, Stocktwits bull%, and technical setup",
      "confidence": "high|medium|low",
      "qty_pct": 0.20
    }}
  ],
  "close_positions": [],
  "close_reasoning": {{}},
  "hold_positions": [],
  "skip_reasoning": {{}}
}}"""


def extract_json(text: str) -> dict:
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    # Find first { to last }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start:end])


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    research = load_json(RESEARCH_FILE)
    if not research:
        raise RuntimeError(f"{RESEARCH_FILE} not found or empty — run research.py first")

    memory = load_json(MEMORY_FILE, {})

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=SYSTEM_PROMPT,
    )

    print("Sending research data to Gemini for analysis...")
    response = model.generate_content(build_user_prompt(research, memory))

    raw = response.text
    print(f"Gemini response ({len(raw)} chars)")

    plan = extract_json(raw)

    with open(PLAN_FILE, "w") as f:
        json.dump(plan, f, indent=2)

    print(f"\nTrade plan written to {PLAN_FILE}")
    print(f"  Week thesis: {plan.get('week_thesis', '')[:120]}...")
    print(f"  Buys: {[b['symbol'] for b in plan.get('buys', [])]}")
    print(f"  Closes: {plan.get('close_positions', [])}")
    print(f"  Holds: {plan.get('hold_positions', [])}")


if __name__ == "__main__":
    main()
