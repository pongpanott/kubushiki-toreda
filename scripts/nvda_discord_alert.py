#!/usr/bin/env python3
"""
NVDA Price Alert via Discord Webhook

Usage:
    python3 scripts/nvda_discord_alert.py --webhook-url YOUR_WEBHOOK_URL

Or set environment variables:
    export DISCORD_NVDA_ALERT_WEBHOOK=YOUR_WEBHOOK_URL
    export FINNHUB_API_KEY=YOUR_FINNHUB_KEY
    python3 scripts/nvda_discord_alert.py

Alert levels (edit ALERT_LEVELS below to customize):
    BUY Zone A:    $205-208  (pullback support entry)
    BUY Zone B:    $222+     (Computex breakout)
    STOP WARNING:  $198.50   (stop-loss level)
    TARGET 1:      $222      (take partial profit)
    TARGET 2:      $235      (take full profit)
"""

import argparse
import json as _json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_CONFIG_PATH = Path(__file__).parent.parent / "config" / "nvda_alert_levels.json"

# ---------------------------------------------------------------------------
# Alert configuration — source of truth: config/nvda_alert_levels.json
# Edit that file directly, or let auto_analyze_and_update.py update it via AI.
# Hardcoded list below is the fallback if config file is missing.
# ---------------------------------------------------------------------------
_HARDCODED_ALERT_LEVELS = [
    {
        "price": 218.00,
        "direction": "above",
        "label": "🚀 BREAKOUT — Day High Cleared",
        "message": "NVDA ทะลุ $218 — ผ่าน Day High ของวันนี้!\nปริมาณ volume สูงหรือไม่? ถ้าใช่ → รอ confirm แล้วเข้า Zone B",
        "color": 0x00BFFF,  # blue
    },
    {
        "price": 208.00,
        "direction": "below",
        "label": "🟢 BUY ZONE A",
        "message": "NVDA แตะ $208 — เข้า Zone A (Pullback จาก $211)\nEntry: $205-208 | Stop: $198.50 | Target 1: $222",
        "color": 0x00FF00,  # green
    },
    {
        "price": 205.00,
        "direction": "below",
        "label": "🟢 BUY ZONE A (DEEP)",
        "message": "NVDA แตะ $205 — ใจกลาง Zone A ราคาดีมาก!\nEntry แนะนำ: ตอนนี้เลย | Stop: $198.50 | R/R = 2.7:1",
        "color": 0x00CC00,
    },
    {
        "price": 198.50,
        "direction": "below",
        "label": "🔴 STOP-LOSS ถูกทดสอบ",
        "message": "NVDA ลงใต้ $198.50 ⚠️\nถ้าถืออยู่: ตัดขาดทุนทันที อย่ารอ",
        "color": 0xFF0000,  # red
    },
    {
        "price": 222.00,
        "direction": "above",
        "label": "🟡 TARGET 1 (+5%)",
        "message": "NVDA แตะ $222 — TARGET 1 สำเร็จ!\nขาย 50% เพื่อ lock กำไร | ย้าย stop → $208 (breakeven zone)",
        "color": 0xFFFF00,  # yellow
    },
    {
        "price": 236.00,
        "direction": "above",
        "label": "🎯 TARGET 2 — 52-Week HIGH (+12%)",
        "message": "NVDA แตะ $236 — 52-Week High Zone!\nขายที่เหลือทั้งหมด หรือ trailing stop ที่ $225",
        "color": 0xFF8800,  # orange
    },
]

TICKER = "NVDA"
CHECK_INTERVAL_SECONDS = 60  # เช็คทุก 1 นาที


def _load_alert_levels() -> list[dict]:
    """Load levels from JSON config if available, else use hardcoded fallback."""
    if _CONFIG_PATH.exists():
        try:
            data = _json.loads(_CONFIG_PATH.read_text())
            levels = data.get("levels", [])
            for lvl in levels:
                if isinstance(lvl.get("color"), str):
                    lvl["color"] = int(lvl["color"].replace("0x", ""), 16)
            if levels:
                print(f"[config] Loaded {len(levels)} levels from config (updated {data.get('last_updated', '?')})")
                return levels
        except Exception as e:
            print(f"[config] Failed to load JSON config: {e} — using hardcoded fallback")
    return [
        {**lvl, "color": lvl["color"]}
        for lvl in _HARDCODED_ALERT_LEVELS
    ]


ALERT_LEVELS = _load_alert_levels()
# ---------------------------------------------------------------------------


def get_price_with_session(ticker: str, finnhub_key: str) -> tuple[float | None, str]:
    """Fetch real-time price from Finnhub and detect trading session."""
    try:
        resp = requests.get(
            FINNHUB_QUOTE_URL,
            params={"symbol": ticker, "token": finnhub_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get("c")  # current price (real-time, includes extended hours)
        if not price:
            return None, "N/A"

        # Determine session from current UTC time
        # EDT = UTC-4: pre-market 4:00-9:30 AM ET = 08:00-13:30 UTC
        #              regular   9:30-16:00 ET    = 13:30-20:00 UTC
        #              after-hrs 16:00-20:00 ET   = 20:00-00:00 UTC
        now_utc = datetime.now(timezone.utc)
        hour_utc = now_utc.hour + now_utc.minute / 60
        is_weekday = now_utc.weekday() < 5  # 0=Mon, 4=Fri

        if not is_weekday:
            session = "Weekend"
        elif 8.0 <= hour_utc < 13.5:
            session = "Pre-Market"
        elif 13.5 <= hour_utc < 20.0:
            session = "Regular"
        elif 20.0 <= hour_utc < 24.0:
            session = "After-Hours"
        else:
            session = "Closed"

        return float(price), session
    except Exception as e:
        print(f"[{now()}] Error fetching price: {e}")
        return None, "N/A"


def send_discord_alert(
    webhook_url: str, label: str, message: str, price: float, color: int, session: str = ""
) -> bool:
    """Send a rich embed message to Discord webhook."""
    embed = {
        "title": label,
        "description": message,
        "color": color,
        "fields": [
            {"name": "💵  ราคา", "value": f"**`${price:.2f}`**", "inline": True},
            {"name": "📊  Session", "value": f"`{session or '—'}`", "inline": True},
            {"name": "🕐  เวลา (TH)", "value": f"`{now()}`", "inline": True},
        ],
        "footer": {"text": "NVDA Price Alert  •  claude-trading-skills"},
    }
    payload = {"embeds": [embed]}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[{now()}] Discord send failed: {e}")
        return False


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send_startup_message(webhook_url: str, price: float) -> None:
    """Send startup confirmation with dynamically loaded levels."""
    above = sorted(
        [lv for lv in ALERT_LEVELS if lv["direction"] == "above"],
        key=lambda x: x["price"],
    )
    below = sorted(
        [lv for lv in ALERT_LEVELS if lv["direction"] == "below"],
        key=lambda x: x["price"],
        reverse=True,
    )

    def fmt(lst: list) -> str:
        return "\n".join(
            f"{lv['label']}  **`${lv['price']:.2f}`**" for lv in lst
        ) or "—"

    embed = {
        "title": "🟢  NVDA Alert Bot — กำลังทำงาน",
        "description": (
            f"ติดตาม **{TICKER}** ทุก `{CHECK_INTERVAL_SECONDS}s`  "
            f"•  {len(ALERT_LEVELS)} levels โหลดแล้ว"
        ),
        "color": 0x2ECC71,
        "fields": [
            {"name": "💵  ราคาเริ่มต้น", "value": f"**`${price:.2f}`**", "inline": True},
            {"name": "🕐  เริ่ม (TH)", "value": f"`{now()}`", "inline": True},
            {"name": "🎯  Targets  ↑", "value": fmt(above), "inline": True},
            {"name": "🟢  Support / Stop  ↓", "value": fmt(below), "inline": True},
        ],
        "footer": {"text": "NVDA Alert Bot  •  claude-trading-skills"},
    }
    requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description="NVDA Discord price alert bot")
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("DISCORD_NVDA_ALERT_WEBHOOK", ""),
        help="Discord Webhook URL (หรือตั้ง DISCORD_NVDA_ALERT_WEBHOOK env var)",
    )
    parser.add_argument(
        "--finnhub-key",
        default=os.environ.get("FINNHUB_API_KEY", ""),
        help="Finnhub API key (หรือตั้ง FINNHUB_API_KEY env var)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=CHECK_INTERVAL_SECONDS,
        help=f"เช็คราคาทุกกี่วินาที (default: {CHECK_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=0,
        help="หยุดอัตโนมัติหลังกี่นาที (0 = ไม่หยุด, ใช้สำหรับ GitHub Actions)",
    )
    args = parser.parse_args()

    if not args.webhook_url:
        print("❌ ต้องระบุ Discord Webhook URL")
        print("   ใช้ --webhook-url หรือ export DISCORD_NVDA_ALERT_WEBHOOK=...")
        raise SystemExit(1)
    if not args.finnhub_key:
        print("❌ ต้องระบุ Finnhub API key")
        print("   ใช้ --finnhub-key หรือ export FINNHUB_API_KEY=...")
        raise SystemExit(1)

    # Track which alerts have already been sent (reset when price moves away)
    triggered: set[str] = set()
    last_price: float | None = None
    start_time = time.monotonic()
    deadline = start_time + args.timeout_minutes * 60 if args.timeout_minutes > 0 else None

    print(f"[{now()}] 🚀 เริ่มติดตาม {TICKER} ทุก {args.interval} วินาที"
          + (f" (หยุดหลัง {args.timeout_minutes} นาที)" if deadline else ""))

    # Get initial price and send startup message
    price, session = get_price_with_session(TICKER, args.finnhub_key)
    if price:
        print(f"[{now()}] ราคาเริ่มต้น: ${price:.2f} [{session}]")
        send_startup_message(args.webhook_url, price)
        last_price = price
    else:
        print(f"[{now()}] ⚠️ ดึงราคาครั้งแรกไม่ได้ รอรอบถัดไป...")

    while True:
        if deadline and time.monotonic() >= deadline:
            print(f"[{now()}] ⏹ หมดเวลา {args.timeout_minutes} นาที — หยุดทำงาน")
            break
        time.sleep(args.interval)
        price, session = get_price_with_session(TICKER, args.finnhub_key)
        if price is None:
            continue

        if price != last_price:
            print(f"[{now()}] {TICKER}: ${price:.2f} [{session}]")
            last_price = price

        for level in ALERT_LEVELS:
            key = level["label"]
            hit = (
                (level["direction"] == "below" and price <= level["price"])
                or (level["direction"] == "above" and price >= level["price"])
            )

            if hit and key not in triggered:
                print(f"[{now()}] 🔔 ALERT: {key} @ ${price:.2f} [{session}]")
                sent = send_discord_alert(
                    args.webhook_url,
                    key,
                    level["message"],
                    price,
                    level["color"],
                    session,
                )
                if sent:
                    triggered.add(key)

            # Reset alert when price moves away (buffer 1%)
            elif not hit and key in triggered:
                buffer = level["price"] * 0.01
                far_enough = (
                    (level["direction"] == "below" and price > level["price"] + buffer)
                    or (level["direction"] == "above" and price < level["price"] - buffer)
                )
                if far_enough:
                    triggered.discard(key)


if __name__ == "__main__":
    main()
