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
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    BKK_TZ = ZoneInfo("Asia/Bangkok")
except Exception:
    BKK_TZ = timezone(timedelta(hours=7))
from pathlib import Path
from datetime import datetime
import sys

# Ensure repository root is on sys.path so `from scripts...` imports work
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.alerts_utils import append_price_point, analyze_chart

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


def recommend_action_from_analysis(analysis: dict) -> tuple[str, float, str]:
    """Simple rule-based recommendation using analysis.metrics."""
    metrics = analysis.get('metrics', {})
    ma50 = metrics.get('ma50')
    ma200 = metrics.get('ma200')
    recent_pct = metrics.get('recent_return_pct', 0.0)
    rsi = metrics.get('rsi', 50.0)

    action = 'HOLD'
    confidence = 0.0
    reasons = []

    if ma50 and ma200:
        if ma50 > ma200:
            reasons.append('ma50>ma200')
            confidence += 0.3
        else:
            reasons.append('ma50<=ma200')
            confidence += 0.0

    if recent_pct is not None:
        if recent_pct > 0.5:
            confidence += min(0.5, recent_pct / 5.0)
        elif recent_pct < -0.5:
            confidence += min(0.5, abs(recent_pct) / 5.0)

    if rsi and rsi < 30:
        reasons.append('rsi_oversold')
        confidence += 0.1
    elif rsi and rsi > 70:
        reasons.append('rsi_overbought')
        confidence += 0.1

    # final decision
    if (ma50 and ma200 and ma50 > ma200) and recent_pct > 0.2:
        action = 'BUY'
    elif (ma50 and ma200 and ma50 < ma200) and recent_pct < -0.2:
        action = 'SELL'
    else:
        action = 'HOLD'

    confidence = max(0.0, min(1.0, confidence))
    return action, confidence, ', '.join(reasons)


def send_analysis_if_configured(base_webhook: str, symbol: str, analysis: dict):
    analysis_webhook = os.environ.get('DISCORD_ANALYSIS_WEBHOOK_URL') or os.environ.get('DISCORD_SMCI_ANALYSIS_WEBHOOK')
    if not analysis_webhook:
        return False

    if not analysis:
        return False

    action, conf, reasons = recommend_action_from_analysis(analysis)

    # Only notify on actionable signals (BUY or SELL)
    if action == 'HOLD':
        return False

    # minimal confidence threshold (env configurable)
    try:
        min_conf = float(os.environ.get('ANALYSIS_MIN_CONFIDENCE', '0.6'))
    except Exception:
        min_conf = 0.6
    if conf < min_conf:
        return False

    # persist last action to avoid repeated alerts + cooldown
    cache_dir = Path(__file__).parent.parent / 'state'
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f'recommendation_{symbol}.json'
    last = {}
    try:
        if cache_file.exists():
            last = _json.loads(cache_file.read_text())
    except Exception:
        last = {}

    cooldown_min = int(os.environ.get('ANALYSIS_COOLDOWN_MINUTES', '60'))
    now_ts = time.time()
    last_ts = 0.0
    try:
        last_ts = float(last.get('ts', 0.0))
    except Exception:
        last_ts = 0.0

    if last.get('action') == action and (now_ts - last_ts) < (cooldown_min * 60):
        return False

    # Build 'why this will beat market' message from analysis
    score = analysis.get('score')
    summary = analysis.get('summary_th', '')
    metrics = analysis.get('metrics', {})
    recent_pct = metrics.get('recent_return_pct')
    edge_parts = []
    if 'ma50' in metrics and 'ma200' in metrics and metrics.get('ma50') and metrics.get('ma200'):
        if metrics['ma50'] > metrics['ma200']:
            edge_parts.append('Uptrend: MA50 > MA200')
        else:
            edge_parts.append('Downtrend: MA50 <= MA200')
    if recent_pct is not None:
        edge_parts.append(f'Momentum: {recent_pct:+.2f}% over lookback')
    rsi = metrics.get('rsi')
    if rsi is not None:
        if rsi < 30:
            edge_parts.append('RSI oversold (possible rebound)')
        elif rsi > 70:
            edge_parts.append('RSI overbought (risk of pullback)')

    reasons_text = reasons or summary or 'Technical alignment'
    edge_text = '; '.join(edge_parts) or 'Price-action signal'

    description = (
        f"Recommendation: **{action}** (confidence {conf:.2f})\n"
        f"Score: {score} · {reasons_text}\n\n"
        f"Why this can beat the market: {edge_text}\n\n"
        f"Summary: {summary}"
    )

    embed = {
        'title': f'{symbol} — Trade Idea',
        'description': description,
        'fields': [
            {'name': 'Confidence', 'value': f"{conf:.2f}", 'inline': True},
        ],
        'color': 3066993 if action == 'BUY' else 15105570,
    }

    try:
        resp = requests.post(analysis_webhook, json={'embeds': [embed]}, timeout=10)
        resp.raise_for_status()
        # record last action
        try:
            cache_file.write_text(_json.dumps({'action': action, 'confidence': conf, 'ts': now_ts}))
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[{now()}] Analysis post failed: {e}")
        return False


def now() -> str:
    """Return current time in Asia/Bangkok (UTC+7) formatted as string."""
    try:
        return datetime.now(BKK_TZ).strftime("%Y-%m-%d %H:%M:%S %z")
    except Exception:
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
        try:
            append_price_point(TICKER, datetime.now(), price)
        except Exception:
            pass
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

            # Cache the price point for rolling analysis and run lightweight analysis every price change
            try:
                append_price_point(TICKER, datetime.now(), price)
            except Exception:
                pass
            try:
                analysis = analyze_chart(TICKER, lookback_minutes=240)
                # Send analysis to analysis webhook if configured (only sends on BUY/SELL and dedupes)
                try:
                    send_analysis_if_configured(None, TICKER, analysis)
                except Exception:
                    pass
            except Exception:
                analysis = None

        for level in ALERT_LEVELS:
            key = level["label"]
            hit = (
                (level["direction"] == "below" and price <= level["price"])
                or (level["direction"] == "above" and price >= level["price"])
            )

            if hit and key not in triggered:
                print(f"[{now()}] 🔔 ALERT: {key} @ ${price:.2f} [{session}]")
                try:
                    append_price_point(TICKER, datetime.now(), price)
                except Exception:
                    pass
                analysis = analyze_chart(TICKER, lookback_minutes=240)

                # If price has moved beyond the configured level, add a dynamic suggestion
                extra = ""
                try:
                    action, conf, reasons = recommend_action_from_analysis(analysis)
                except Exception:
                    action, conf, reasons = 'HOLD', 0.0, ''
                try:
                    if level.get("direction") == "below" and price > level.get("price", 0):
                        extra = (
                            f"\n\n⚠️ Level missed: current price ${price:.2f} > level ${level['price']:.2f}."
                            f"\nSuggested: wait for a pullback to ${level['price']:.2f} or consider SHORT if analysis recommends SELL (analysis: {action} conf={conf:.2f})."
                        )
                    elif level.get("direction") == "above" and price < level.get("price", 0):
                        extra = (
                            f"\n\n⚠️ Level not reached: current price ${price:.2f} < level ${level['price']:.2f}."
                            f"\nSuggested: wait for breakout above ${level['price']:.2f} or consider buy-on-pullback if analysis recommends BUY (analysis: {action} conf={conf:.2f})."
                        )
                except Exception:
                    extra = ""

                augmented_message = level["message"] + extra + "\n\n" + f"✅ วิเคราะห์: {analysis.get('summary_th', '')} (score={analysis.get('score')})"
                sent = send_discord_alert(
                    args.webhook_url,
                    key,
                    augmented_message,
                    price,
                    level["color"],
                    session,
                )
                if sent:
                    triggered.add(key)
                # Post analysis to analysis webhook if configured
                try:
                    send_analysis_if_configured(args.webhook_url, TICKER, analysis)
                except Exception:
                    pass

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
