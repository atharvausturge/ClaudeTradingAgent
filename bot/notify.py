import os
import requests

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")


def _send(title, description, color, fields=None):
    if not WEBHOOK_URL:
        return
    embed = {"title": title, "description": description, "color": color}
    if fields:
        embed["fields"] = fields
    try:
        requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=5)
    except Exception:
        pass


def trade_buy(symbol, qty, price, stop_price, reasoning, confidence, portfolio_value, buying_power):
    _send(
        title="🟢 BUY ORDER PLACED",
        description=f"**{qty} {symbol}** @ ~${price:.2f}\nStop loss: **${stop_price:.2f}** (-5%)",
        color=3066993,
        fields=[
            {"name": "Confidence", "value": confidence.upper(), "inline": True},
            {"name": "Portfolio Value", "value": f"${portfolio_value:,.2f}", "inline": True},
            {"name": "Buying Power Left", "value": f"${buying_power:,.2f}", "inline": True},
            {"name": "Reasoning", "value": reasoning[:300], "inline": False},
        ],
    )


def trade_close(symbol, reason, pl_pct=None):
    desc = f"**{symbol}**\nReason: {reason}"
    if pl_pct is not None:
        desc += f"\nP&L: **{pl_pct:+.2f}%**"
    _send(title="🔴 POSITION CLOSED", description=desc, color=15158332)


def weekly_plan(week_of, thesis, buys, holds, closes):
    buy_lines = "\n".join([f"• **{b['symbol']}** ({b.get('confidence','?').upper()})" for b in buys]) or "None"
    _send(
        title=f"📋 WEEKLY PLAN — {week_of}",
        description=thesis,
        color=10181046,
        fields=[
            {"name": "Buying", "value": buy_lines, "inline": False},
            {"name": "Holding", "value": ", ".join(holds) or "None", "inline": True},
            {"name": "Closing", "value": ", ".join(closes) or "None", "inline": True},
        ],
    )


def friday_summary(portfolio_value, held, closed, weekly_return_pct=None, spy_return_pct=None):
    held_lines = "\n".join([f"✅ **{p['symbol']}** {p['pl_pct']:+.2f}%" for p in held]) or "None"
    closed_lines = "\n".join([f"🔴 **{p['symbol']}** {p['pl_pct']:+.2f}%" for p in closed]) or "None"
    perf = ""
    if weekly_return_pct is not None and spy_return_pct is not None:
        perf = f"\nThis week: Bot **{weekly_return_pct:+.2f}%** vs SPY **{spy_return_pct:+.2f}%**"
    _send(
        title="📊 FRIDAY WRAP",
        description=f"Portfolio: **${portfolio_value:,.2f}**{perf}",
        color=3447003,
        fields=[
            {"name": "Held into next week", "value": held_lines, "inline": True},
            {"name": "Closed (losers)", "value": closed_lines, "inline": True},
        ],
    )


def daily_pnl(equity, daily_pl_dollar, daily_pl_pct, positions, realized_today, cash, invested_pct=None):
    """Post-close daily snapshot. Green if up on the day, red if down."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    color = 3066993 if daily_pl_dollar >= 0 else 15158332  # green / red
    arrow = "📈" if daily_pl_dollar >= 0 else "📉"

    description = (
        f"Equity: **${equity:,.2f}**\n"
        f"Today: {arrow} **{daily_pl_pct:+.2f}%** (${daily_pl_dollar:+,.2f})"
    )
    if invested_pct is not None:
        flag = " ⚠️ ON MARGIN" if invested_pct > 100 else ""
        description += f"\nInvested: **{invested_pct:.0f}%** of equity ({invested_pct/100:.2f}× gross){flag}"

    if positions:
        open_lines = "\n".join(
            f"• **{p['symbol']}** {p['qty']}sh @ ${p['current_price']:.2f} "
            f"({p['intraday_pl_pct']:+.2f}%, ${p['intraday_pl_dollar']:+,.2f})"
            for p in positions
        )
    else:
        open_lines = "No open positions"

    fields = [
        {"name": f"Open positions ({len(positions)})", "value": open_lines, "inline": False},
    ]

    if realized_today:
        closed_lines = "\n".join(
            f"🔴 **{r['symbol']}** {r['qty']}sh @ ${r['exit_price']:.2f} "
            f"({r['pl_pct']:+.2f}%, ${r['pl_dollar']:+,.2f})"
            for r in realized_today
        )
        fields.append({"name": "Closed today", "value": closed_lines, "inline": False})

    fields.append({"name": "Cash", "value": f"${cash:,.2f}", "inline": True})

    _send(
        title=f"📊 DAILY P&L — {date_str}",
        description=description,
        color=color,
        fields=fields,
    )


def midweek_ok(portfolio_value, positions):
    lines = "\n".join([f"• **{p['symbol']}** {p['pl_pct']:+.2f}%" for p in positions]) or "No open positions"
    _send(
        title="📈 MID-WEEK CHECK",
        description=f"Portfolio: **${portfolio_value:,.2f}**\n\n{lines}",
        color=3447003,
    )
