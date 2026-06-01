#!/usr/bin/env python3
"""
SMCI Price Alert via Discord Webhook

Usage:
    python3 scripts/smci_discord_alert.py --webhook-url YOUR_WEBHOOK_URL

Or set environment variables:
    export DISCORD_SMCI_ALERT_WEBHOOK=YOUR_WEBHOOK_URL
    export FINNHUB_SMCI_API_KEY=YOUR_FINNHUB_KEY
    python3 scripts/smci_discord_alert.py

Alert levels (edit ALERT_LEVELS below to customize):
    BUY Zone A:   $43-44  (gap fill support entry)
    Momentum:     $45+    (VWAP hold Setup B)
    STOP:         $41.50  (stop-loss level)
    TARGET 1:     $48     (retest today's high)
    TARGET 2:     $50     (psychological round number)
"""

import argparse
import json as _json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_CONFIG_PATH = Path(__file__).parent.parent / "config" / "smci_alert_levels.json"

# ---------------------------------------------------------------------------
# Alert configuration — source of truth: config/smci_alert_levels.json
# Edit that file directly, or let auto_analyze_and_update.py update it via AI.
# Hardcoded list below is the fallback if config file is missing.
# ---------------------------------------------------------------------------
_HARDCODED_ALERT_LEVELS = [
    {
        "price": 49.0,
        "direction": "above",
        "label": "🚀 BREAKOUT — New High Zone",
        "message": "SMCI ทะลุ $49 — breakout เหนือ today's high!\nSetup B entry confirm | Stop: $44 | Target: $54-55",
        "color": 0x00BFFF,
    },
    {
        "price": 45.0,
        "direction": "above",
        "label": "📍 MOMENTUM ENTRY — VWAP Hold",
        "message": "SMCI ถือเหนือ $45 หลัง 30 นาทีแรก — Setup B momentum!\nEntry 3-4 หุ้น | Stop: $43.50 | Target: $49",
        "color": 0x00AAFF,
    },
    {
        "price": 44.0,
        "direction": "below",
        "label": "🟢 BUY ZONE A — Gap Fill Support",
        "message": "SMCI แตะ $44 — Setup A! (gap fill zone / prev resistance)\nEntry 4-6 หุ้น | Stop: $41.50 | Target: $47-48",
        "color": 0x00FF00,
    },
    {
        "price": 43.0,
        "direction": "below",
        "label": "🟢 BUY ZONE A (DEEP) — Best R/R",
        "message": "SMCI แตะ $43 — ใจกลาง Zone A R/R ดีที่สุด!\nEntry: $43 | Stop: $41.50 | Target: $48 | R/R = 3.3:1",
        "color": 0x00CC00,
    },
    {
        "price": 41.5,
        "direction": "below",
        "label": "🔴 STOP-LOSS — ตัดขาดทุนทันที",
        "message": "SMCI ลงใต้ $41.50 ⚠️ Structure เสียแล้ว (ต่ำกว่า gap open)\nตัดขาดทุนทันที ไม่รอ ไม่ average down",
        "color": 0xFF0000,
    },
    {
        "price": 48.0,
        "direction": "above",
        "label": "🟡 TARGET 1 — Retest Today's High",
        "message": "SMCI แตะ $48 — TARGET 1 (retest today's high $48.34)!\nขาย 50% lock กำไร | ย้าย stop → $44 | รอ $50",
        "color": 0xFFFF00,
    },
    {
        "price": 50.0,
        "direction": "above",
        "label": "🎯 TARGET 2 — Psychological Round Number",
        "message": "SMCI แตะ $50 — TARGET 2! Psychological resistance\nขายที่เหลือทั้งหมด หรือ trailing stop $46",
        "color": 0xFF8800,
    },
]

TICKER = "SMCI"
CHECK_INTERVAL_SECONDS = 60


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
        price = data.get("c")
        if not price:
            return None, "N/A"

        now_utc = datetime.now(timezone.utc)
        hour_utc = now_utc.hour + now_utc.minute / 60
        is_weekday = now_utc.weekday() < 5

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
    description = (
        f"{message}\n\n"
        f"─────────────────────────────────────\n"
        f"💵 **ราคา** 　`${price:.2f}`　　"
        f"📊 **Session** 　`{session or '—'}`\n"
        f"🕐 **เวลา (TH)** 　`{now()}`"
    )
    embed = {
        "title": label,
        "description": description,
        "color": color,
        "footer": {"text": "SMCI Alert Bot  •  claude-trading-skills"},
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

    def fmt_levels(levels: list) -> str:
        return "\n".join(
            f"> `${lv['price']:>7.2f}`  {lv['label']}" for lv in levels
        ) or "> —"

    description = (
        f"ติดตาม **{TICKER}** ทุก `{CHECK_INTERVAL_SECONDS}s`"
        f"  •  **{len(ALERT_LEVELS)} levels** โหลดแล้ว\n\n"
        f"💵 **ราคาเริ่มต้น** ── `${price:.2f}`\n"
        f"🕐 **เริ่ม (TH)** ──── `{now()}`\n\n"
        f"━━━━━━━━━━━━  📈 TARGETS  ━━━━━━━━━━━━\n"
        f"{fmt_levels(above)}\n\n"
        f"━━━━━━━━━━━  📉 SUPPORT / STOP  ━━━━━━━━━━━\n"
        f"{fmt_levels(below)}"
    )

    embed = {
        "title": f"🟢  SMCI Alert Bot — กำลังทำงาน",
        "description": description,
        "color": 0x2ECC71,
        "footer": {"text": "SMCI Alert Bot  •  claude-trading-skills"},
    }
    requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description="SMCI Discord price alert bot")
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("DISCORD_SMCI_ALERT_WEBHOOK", ""),
        help="Discord Webhook URL (หรือตั้ง DISCORD_SMCI_ALERT_WEBHOOK env var)",
    )
    parser.add_argument(
        "--finnhub-key",
        default=os.environ.get("FINNHUB_SMCI_API_KEY", os.environ.get("FINNHUB_API_KEY", "")),
        help="Finnhub API key (หรือตั้ง FINNHUB_SMCI_API_KEY env var)",
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
        print("   ใช้ --webhook-url หรือ export DISCORD_SMCI_ALERT_WEBHOOK=...")
        raise SystemExit(1)
    if not args.finnhub_key:
        print("❌ ต้องระบุ Finnhub API key")
        print("   ใช้ --finnhub-key หรือ export FINNHUB_SMCI_API_KEY=...")
        raise SystemExit(1)

    triggered: set[str] = set()
    last_price: float | None = None
    start_time = time.monotonic()
    deadline = start_time + args.timeout_minutes * 60 if args.timeout_minutes > 0 else None

    print(f"[{now()}] 🚀 เริ่มติดตาม {TICKER} ทุก {args.interval} วินาที"
          + (f" (หยุดหลัง {args.timeout_minutes} นาที)" if deadline else ""))

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
