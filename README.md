# Autonomous Swing Trading Bot

A fully autonomous weekly swing trading bot that scans 254 large-cap stocks, uses AI to pick the best setups, and executes trades automatically on Alpaca — with Discord alerts.

Runs entirely on GitHub Actions. No server required.

---

## How It Works

Every Monday at 8:30am ET, the bot:

1. Scans 254 large-cap stocks across all S&P 500 sectors
2. Scores each on a **Trend + Dip** signal (60-day relative strength vs SPY + 5-day pullback)
3. Pulls **Alpaca News** and **Stocktwits sentiment** for the top 20 candidates
4. Sends the research to **Groq (Llama 3.3 70B)** for AI analysis
5. Places **market buy orders + GTC stop losses** on Alpaca Paper Trading
6. Sends a full trade plan to **Discord**

Wednesday at 9:30am: midweek portfolio snapshot.  
Friday at 3pm: closes any position down more than 2%, holds winners into next week.

---

## Strategy

**Signal: Trend + Dip on 254 stocks**

| Filter | Rule |
|--------|------|
| Long-term trend | 60-day return vs SPY > 0 (stock is outperforming the market) |
| Entry signal | 5-day pullback (buy temporary weakness in a strong stock) |
| RSI | 30–65 (not overbought, not in freefall) |
| Min price | $25+ (no speculative names) |

**Three layers Claude uses to pick 2–3 trades:**
- Technicals (score, RS60, dip5d, RSI)
- Alpaca News API (professional financial news)
- Stocktwits (retail social sentiment — bull%, bear%, recent posts)

**Portfolio rules:**
- Max 3 positions, 20% of portfolio each
- 5% stop loss attached as GTC order (auto-triggers even when bot isn't running)
- Max 2 entries per symbol per month
- Friday: close losers (down >2%), hold winners into next week

---

## Backtest Results (2 Years)

| Metric | Value |
|--------|-------|
| Total return | +98.5% |
| SPY buy & hold | +35.2% |
| Alpha vs SPY | +63.3% |
| Sharpe ratio | 1.16 |
| Max drawdown | -27.1% |
| Trade win rate | 58.4% |

> Paper trading only. Past backtest performance does not guarantee future results.

---

## File Structure

```
research.py        # Scans universe, scores stocks, fetches news + sentiment
analyze.py         # Sends research to Groq AI → writes trade_plan.json
execute.py         # Reads trade_plan.json, places orders on Alpaca
monitor.py         # Wed/Fri checks — closes losers, snapshots portfolio
notify.py          # Discord webhook notifications
score_local.py     # Pure computation — scores universe from pre-fetched bar data
universe.py        # 254 large-cap stocks across all S&P 500 sectors
backtest.py        # 2-year historical backtest
config.json        # Strategy parameters
memory.json        # Persistent trade history and weekly summaries
trade_plan.json    # Current week's AI-generated trade plan
```

---

## Automated Schedule (GitHub Actions)

| Workflow | Schedule | Does |
|----------|----------|------|
| `monday.yml` | Mon 8:30am ET | Research + AI analysis + execute trades + Discord |
| `execute-on-push.yml` | On `trade_plan.json` push | Places Alpaca orders + Discord alerts |
| `wednesday.yml` | Wed 9:30am ET | Midweek position snapshot |
| `friday.yml` | Fri 3pm ET | Close losers + Discord weekly wrap |
| `friday-execute-on-push.yml` | On `friday_closes.json` push | Executes Friday closes |

---

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/atharvausturge/ClaudeTradingAgent.git
cd ClaudeTradingAgent
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create a `.env` file
```
ALPACA_API_KEY=your_alpaca_paper_key
ALPACA_SECRET_KEY=your_alpaca_paper_secret
DISCORD_WEBHOOK_URL=your_discord_webhook
GROQ_API_KEY=your_groq_api_key
```

### 3. Add GitHub Actions secrets
Go to **Settings → Secrets → Actions** and add:

| Secret | Where to get it |
|--------|----------------|
| `ALPACA_API_KEY` | app.alpaca.markets → Paper Trading |
| `ALPACA_SECRET_KEY` | app.alpaca.markets → Paper Trading |
| `DISCORD_WEBHOOK_URL` | Discord → Server Settings → Integrations → Webhooks |
| `GROQ_API_KEY` | console.groq.com (free, no credit card) |

### 4. Run a backtest
```bash
python backtest.py
```

### 5. Trigger a manual test run
Go to **Actions → Monday — Research & Trade → Run workflow**

---

## Going Live

The bot runs on Alpaca **paper trading** by default. When you're ready for real money, change one line in `config.json`:

```json
"paper": false
```

And update your Alpaca API keys to your live account keys.

---

## Dependencies

- [alpaca-py](https://github.com/alpacahq/alpaca-py) — trading + market data
- [groq](https://console.groq.com) — free AI inference (Llama 3.3 70B)
- pandas, numpy — technical analysis
- requests — Stocktwits sentiment
