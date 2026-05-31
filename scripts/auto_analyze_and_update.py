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
GITHUB_MODELS_ENDPOINT = os.environ.get("GITHUB_MODELS_ENDPOINT", "https://models.inference.ai.azure.com")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


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


def _days_until(date_str: str, today) -> int:
    """Days from today to date_str (YYYY-MM-DD).  Returns -1 if past or parse error."""
    try:
        return (datetime.strptime(date_str, "%Y-%m-%d").date() - today).days
    except ValueError:
        return -1


def send_analysis_discord(webhook_url: str, analysis: dict, price: float) -> None:
    """Send four focused Discord embeds: Market Pulse · Alert Levels · Copilot · Catalysts."""
    today = datetime.now().date()

    def pct(target_price: float) -> str:
        if price <= 0:
            return ""
        return f"{(target_price - price) / price * 100:+.1f}%"

    # ── Categorise + sort levels ───────────────────────────────────────────
    levels = analysis.get("levels", [])
    above = sorted(
        [lv for lv in levels if lv["direction"] == "above"],
        key=lambda x: x["price"],
    )  # nearest target first
    below = sorted(
        [lv for lv in levels if lv["direction"] == "below"],
        key=lambda x: x["price"],
        reverse=True,
    )  # nearest support/stop first

    def level_lines(lst: list) -> str:
        if not lst:
            return "—"
        return "\n".join(
            f"{lv['label']}  **`${lv['price']:.2f}`**  `{pct(lv['price'])}`"
            for lv in lst
        )

    # ── Embed 1 — Market Pulse ─────────────────────────────────────────────
    embed1 = {
        "title": "📡  NVDA — Market Pulse",
        "description": analysis.get("analysis_summary", "—"),
        "color": 0x5865F2,
        "fields": [
            {
                "name": "💵  ราคา ณ เวลาวิเคราะห์",
                "value": f"**`${price:.2f}`**",
                "inline": True,
            },
            {
                "name": "🕐  อัปเดต (TH)",
                "value": datetime.now().strftime("%d %b %Y  %H:%M"),
                "inline": True,
            },
        ],
        "footer": {"text": "Claude AI + Finnhub  •  claude-trading-skills"},
    }

    # ── Embed 2 — Alert Levels ─────────────────────────────────────────────
    lvl_fields = []
    if above:
        lvl_fields.append(
            {"name": "🎯  Targets  ↑ above", "value": level_lines(above), "inline": False}
        )
    if below:
        lvl_fields.append(
            {
                "name": "🟢  Buy Zones / Stop  ↓ below",
                "value": level_lines(below),
                "inline": False,
            }
        )
    embed2 = {
        "title": "📊  Alert Levels",
        "color": 0x2ECC71,
        "fields": lvl_fields or [{"name": "Levels", "value": "—", "inline": False}],
        "footer": {"text": f"Reference  ${price:.2f}"},
    }

    # ── Embed 3 — Pro Trader Copilot ───────────────────────────────────────
    notes = analysis.get("pro_trader_notes", {})
    note_fields = [
        {"name": label, "value": (notes.get(key) or "").strip()[:1024], "inline": False}
        for key, label in [
            ("current_setup", "📌  Setup ตอนนี้"),
            ("action_now",    "✅  ทำตอนนี้"),
            ("watch_for",     "👁  จับตา"),
            ("avoid",         "🚫  อย่าทำ"),
            ("catalyst_note", "⚡  Catalyst"),
            ("risk_rule",     "🛡  Risk Rule"),
        ]
        if (notes.get(key) or "").strip()
    ]
    embed3 = {
        "title": "🧠  Pro Trader Copilot",
        "description": "คำแนะนำ AI สำหรับ position ตอนนี้",
        "color": 0xFF8C00,
        "fields": note_fields or [{"name": "—", "value": "ไม่มีข้อมูล", "inline": False}],
        "footer": {
            "text": "Rules: ไม่ไล่ราคา  •  ไม่ FOMO  •  ไม่ average down  •  take profit T1 แล้ว rotate"
        },
    }

    # ── Embed 4 — Upcoming Catalysts (future events only) ─────────────────
    embeds = [embed1, embed2, embed3]
    future_events = [
        ev for ev in analysis.get("upcoming_events", [])
        if _days_until(ev.get("date", ""), today) >= 0
    ]
    if future_events:
        event_lines = []
        for ev in future_events[:8]:
            days = _days_until(ev.get("date", ""), today)
            if days == 0:
                countdown = "**วันนี้** 🔔"
            elif days <= 7:
                countdown = f"อีก **{days} วัน** ⚠️"
            else:
                countdown = f"อีก {days} วัน"
            event_lines.append(
                f"`{ev.get('date', '?')}`  {ev.get('event', '?')}  —  {countdown}"
            )
        embeds.append(
            {
                "title": "📅  Upcoming Catalysts",
                "description": "\n".join(event_lines),
                "color": 0xF39C12,
                "footer": {"text": "เฉพาะ catalysts ที่ยังไม่ผ่าน"},
            }
        )

    resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=10)
    resp.raise_for_status()
    print(f"✅ Discord notification sent ({len(embeds)} embeds)")


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
    parser.add_argument(
        "--min-move-pct",
        type=float,
        default=0.0,
        help="Skip AI analysis if price moved less than this %% from last config (0 = always run)",
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

    if args.min_move_pct > 0:
        last_ref = float(current_config.get("price_at_update") or 0)
        if last_ref > 0:
            move_pct = abs(current_price - last_ref) / last_ref * 100
            if move_pct < args.min_move_pct:
                print(
                    f"⏭️  Price moved {move_pct:.1f}% from last ref ${last_ref:.2f} "
                    f"— below {args.min_move_pct}% threshold. Skipping AI analysis."
                )
                raise SystemExit(0)
            print(f"✅ Price moved {move_pct:.1f}% ≥ {args.min_move_pct}% — proceeding with analysis.")

    print(f"🤖 Analyzing with Claude Sonnet via GitHub Models ({CLAUDE_MODEL}) at {GITHUB_MODELS_ENDPOINT}...")
    try:
        analysis = analyze_with_claude(args.github_token, price_data, news, earnings, current_config)
    except Exception as exc:
        print(f"❌ Claude API call failed: {type(exc).__name__}: {exc}")
        print(f"   Model used: {CLAUDE_MODEL}")
        print(f"   Endpoint:   {GITHUB_MODELS_ENDPOINT}")
        print("   Tip: set CLAUDE_MODEL and/or GITHUB_MODELS_ENDPOINT env vars to override defaults.")
        raise SystemExit(1) from exc

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
