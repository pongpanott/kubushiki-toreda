#!/usr/bin/env python3
"""
Auto-analyze NVDA market conditions using Finnhub news + Claude Sonnet via GitHub Models,
then update config/nvda_alert_levels.json and notify Discord analysis channel.

Usage:
    python3 scripts/auto_analyze_and_update.py
    python3 scripts/auto_analyze_and_update.py --dry-run   # print only, no save/send

Secrets (env vars or CLI args):
    FINNHUB_API_KEY               Finnhub API key
    GITHUB_TOKEN                  Auto-provided in GitHub Actions (no extra secret needed)
    DISCORD_ANALYSIS_WEBHOOK_URL  Discord webhook for analysis channel
"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from openai import OpenAI

CONFIG_PATH = Path(__file__).parent.parent / "config" / "nvda_alert_levels.json"
FINNHUB_BASE = "https://finnhub.io/api/v1"
TICKER = "NVDA"
NEWS_DAYS = 5
# GitHub Models endpoint — uses GITHUB_TOKEN, no separate API key required
GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_price(api_key: str) -> dict:
    resp = requests.get(
        f"{FINNHUB_BASE}/quote",
        params={"symbol": TICKER, "token": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_news(api_key: str) -> list[dict]:
    to_d = datetime.now(timezone.utc).date()
    from_d = to_d - timedelta(days=NEWS_DAYS)
    resp = requests.get(
        f"{FINNHUB_BASE}/company-news",
        params={"symbol": TICKER, "from": str(from_d), "to": str(to_d), "token": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json() or []
    return [
        {"headline": n.get("headline", ""), "summary": (n.get("summary") or "")[:200]}
        for n in items[:10]
    ]


def fetch_upcoming_earnings(api_key: str) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    to_d = today + timedelta(days=180)
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/calendar/earnings",
            params={"symbol": TICKER, "from": str(today), "to": str(to_d), "token": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("earningsCalendar", [])[:3]
    except Exception as e:
        print(f"[warn] Could not fetch earnings calendar: {e}")
        return []


# ---------------------------------------------------------------------------
# Claude AI analysis
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict:
    """Extract JSON from Claude response, stripping markdown code fences."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"No valid JSON in Claude response: {text[:300]}")


def analyze_with_claude(
    github_token: str,
    price_data: dict,
    news: list[dict],
    earnings: list[dict],
    current_config: dict,
) -> dict:
    client = OpenAI(
        base_url=GITHUB_MODELS_ENDPOINT,
        api_key=github_token,
    )

    current_price = price_data.get("c", 0)
    news_text = "\n".join(f"- {n['headline']}: {n['summary']}" for n in news) or "No recent news"
    earnings_text = (
        "\n".join(f"- {e.get('date', '')}: EPS estimate {e.get('epsEstimate', 'N/A')}" for e in earnings)
        or "No upcoming earnings data"
    )
    current_levels_text = json.dumps(current_config.get("levels", []), indent=2, ensure_ascii=False)

    prompt = f"""Current {TICKER} price: ${current_price:.2f}
Change today: ${price_data.get('d', 0):.2f} ({price_data.get('dp', 0):.2f}%)
Day high: ${price_data.get('h', 0):.2f} | Day low: ${price_data.get('l', 0):.2f}

Recent news (last {NEWS_DAYS} days):
{news_text}

Upcoming earnings:
{earnings_text}

Current alert levels:
{current_levels_text}

Return ONLY a valid JSON object (no markdown, no extra text):
{{
  "analysis_summary": "<2-3 sentence Thai summary of market situation and why levels were adjusted>",
  "pro_trader_notes": {{
    "current_setup": "<Thai: describe whether a valid setup exists right now (e.g. 'ราคาอยู่กลางอากาศ ยังไม่มี setup')>",
    "action_now": "<Thai: exactly what to do RIGHT NOW — buy/wait/sell/nothing>",
    "watch_for": "<Thai: specific price level or condition to watch for the next entry signal>",
    "avoid": "<Thai: what NOT to do — e.g. อย่า FOMO, อย่า average down>",
    "catalyst_note": "<Thai: catalyst warning if any major event within 7 days, else empty string>",
    "risk_rule": "<Thai: applicable risk management reminder for this situation>"
  }},
  "levels": [
    {{
      "price": <float>,
      "direction": "above" or "below",
      "label": "<emoji> <short label in Thai/English>",
      "message": "<Thai action message with price context>",
      "color": "0xHEXCOLOR"
    }}
  ],
  "upcoming_events": [
    {{"date": "YYYY-MM-DD", "event": "<emoji> <event description>"}}
  ]
}}

Rules for levels:
- Include 1-2 buy zones (below), 1 stop-loss (~5-8% below current), 1-2 targets (above)
- Target 1: ~5-8% above current | Target 2: ~12-18% above current
- Colors: buy=0x00CC00, stop=0xFF0000, target1=0xFFFF00, target2=0xFF8800, breakout=0x00BFFF
- All messages in Thai
- upcoming_events: include earnings + known catalysts within 90 days"""

    response = client.chat.completions.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional swing trader and copilot for NVDA stock, "
                    "advising a small-account retail investor (budget ~10,000 THB, ~$280 USD). "
                    "You combine technical analysis with the following non-negotiable pro trader rules:\n"
                    "1. NEVER chase price mid-air — only enter at well-defined support/resistance or confirmed breakout with volume.\n"
                    "2. CATALYST AWARENESS — around major events (Computex, earnings, Fed) apply ‘buy the rumour, sell the news’ discipline; "
                    "wait for post-event price reaction before adding exposure.\n"
                    "3. RISK MANAGEMENT RULES — max 2% portfolio risk per trade; if 3 consecutive stop-outs occur, pause trading 1 week; "
                    "never average down into a losing position; always define stop before entry.\n"
                    "4. EX-DIVIDEND AWARENESS — warn when ex-div is within 5 trading days; "
                    "do not buy purely for dividend capture on a swing position.\n"
                    "5. SMALL-ACCOUNT DISCIPLINE — prefer taking partial profit at Target 1 and rotating capital "
                    "rather than holding for analyst price targets; a 5-8% gain compounded beats waiting for 40% upside.\n"
                    "Return ONLY valid JSON. Never include markdown, explanation, or text outside the JSON object."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    return extract_json(response.choices[0].message.content)


# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------

def send_analysis_discord(webhook_url: str, analysis: dict, price: float) -> None:
    levels_text = "\n".join(
        f"{'≥' if lv['direction'] == 'above' else '≤'} **${lv['price']:.2f}** — {lv['label']}"
        for lv in analysis.get("levels", [])
    )
    events_text = "\n".join(
        f"• {ev['event']} ({ev['date']})"
        for ev in analysis.get("upcoming_events", [])
    ) or "—"

    notes = analysis.get("pro_trader_notes", {})
    copilot_lines = []
    if notes.get("current_setup"):
        copilot_lines.append(f"📌 **Setup:** {notes['current_setup']}")
    if notes.get("action_now"):
        copilot_lines.append(f"✅ **ทำตอนนี้:** {notes['action_now']}")
    if notes.get("watch_for"):
        copilot_lines.append(f"👁 **จับตา:** {notes['watch_for']}")
    if notes.get("avoid"):
        copilot_lines.append(f"⚠️ **อย่าทำ:** {notes['avoid']}")
    if notes.get("catalyst_note"):
        copilot_lines.append(f"⚡ **Catalyst:** {notes['catalyst_note']}")
    if notes.get("risk_rule"):
        copilot_lines.append(f"🛡 **Risk rule:** {notes['risk_rule']}")
    copilot_text = "\n".join(copilot_lines) or "—"

    embeds = [
        {
            "title": "🤖 NVDA Alert Levels อัปเดตอัตโนมัติ",
            "description": analysis.get("analysis_summary", ""),
            "color": 0x5865F2,
            "fields": [
                {"name": "💵 ราคาตอนวิเคราะห์", "value": f"**${price:.2f}**", "inline": True},
                {"name": "🕐 เวลา (TH)", "value": datetime.now().strftime("%Y-%m-%d %H:%M"), "inline": True},
                {"name": "📊 Alert Levels ใหม่", "value": levels_text or "—", "inline": False},
                {"name": "⚡ Catalysts ที่อัปเดต", "value": events_text, "inline": False},
            ],
            "footer": {"text": "วิเคราะห์โดย Claude AI + Finnhub News | claude-trading-skills"},
        },
        {
            "title": "🧠 Pro Trader Copilot — ตอนนี้ต้องทำอะไร?",
            "description": copilot_text,
            "color": 0xFF8C00,
            "footer": {"text": "Pro rules: ไม่ไล่ | ไม่ FOMO | ไม่ average down | take profit T1 แล้ว rotate"},
        },
    ]
    resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=10)
    resp.raise_for_status()
    print("✅ Analysis + Pro Trader Copilot sent to Discord")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-analyze NVDA and update alert levels")
    parser.add_argument("--finnhub-key", default=os.environ.get("FINNHUB_API_KEY", ""))
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub token for GitHub Models API (auto-provided in Actions)",
    )
    parser.add_argument(
        "--analysis-webhook",
        default=os.environ.get("DISCORD_ANALYSIS_WEBHOOK_URL", ""),
        help="Discord webhook for analysis channel (separate from price alerts)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print result without saving or sending")
    args = parser.parse_args()

    missing = [name for name, val in [
        ("FINNHUB_API_KEY", args.finnhub_key),
        ("GITHUB_TOKEN", args.github_token),
    ] if not val]
    if missing:
        print(f"❌ Missing required keys: {', '.join(missing)}")
        raise SystemExit(1)

    print(f"📡 Fetching {TICKER} price and news from Finnhub...")
    price_data = fetch_price(args.finnhub_key)
    news = fetch_news(args.finnhub_key)
    earnings = fetch_upcoming_earnings(args.finnhub_key)
    current_config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

    current_price = price_data.get("c", 0)
    print(f"💵 Current price: ${current_price:.2f}  ({price_data.get('dp', 0):+.2f}%)")
    print(f"📰 News items: {len(news)} | Earnings entries: {len(earnings)}")

    print(f"🤖 Analyzing with Claude Sonnet via GitHub Models ({CLAUDE_MODEL})...")
    analysis = analyze_with_claude(args.github_token, price_data, news, earnings, current_config)

    new_config = {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "updated_by": "auto",
        "ticker": TICKER,
        "price_at_update": current_price,
        "analysis_summary": analysis.get("analysis_summary", ""),
        "levels": analysis.get("levels", []),
        "upcoming_events": analysis.get("upcoming_events", []),
    }

    if args.dry_run:
        print("\n[DRY RUN] Would write config:")
        print(json.dumps(new_config, indent=2, ensure_ascii=False))
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(new_config, indent=2, ensure_ascii=False) + "\n")
        print(f"✅ Saved → {CONFIG_PATH.relative_to(Path.cwd())}")

    if args.analysis_webhook:
        if not args.dry_run:
            send_analysis_discord(args.analysis_webhook, analysis, current_price)
        else:
            print("[DRY RUN] Would send Discord analysis message")
    else:
        print("⚠️  DISCORD_ANALYSIS_WEBHOOK_URL not set — skipping Discord notification")


if __name__ == "__main__":
    main()
