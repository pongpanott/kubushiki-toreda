"""Shared helpers for Discord stock alert scripts.

This module centralizes common logic reused by ticker-specific alert bots
such as NVDA/SMCI and future symbols.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import requests

try:
    from zoneinfo import ZoneInfo

    _BKK_TZ = ZoneInfo("Asia/Bangkok")
except Exception:
    _BKK_TZ = timezone(timedelta(hours=7))


def now_bkk() -> str:
    """Return current time in Asia/Bangkok format."""
    try:
        return datetime.now(_BKK_TZ).strftime("%Y-%m-%d %H:%M:%S %z")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_price_with_session(
    ticker: str,
    finnhub_key: str,
    quote_url: str = "https://finnhub.io/api/v1/quote",
) -> tuple[float | None, str]:
    """Fetch current price from Finnhub and infer trading session."""
    try:
        resp = requests.get(
            quote_url,
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
    except Exception:
        return None, "N/A"


def send_discord_alert(
    webhook_url: str,
    label: str,
    message: str,
    price: float,
    color: int,
    session: str = "",
    footer_text: str = "Alert Bot",
) -> bool:
    """Send a standard Discord embed message."""
    description = (
        f"{message}\n\n"
        f"─────────────────────────────────────\n"
        f"💵 **ราคา**  `${price:.2f}`    "
        f"📊 **Session**  `{session or '—'}`\n"
        f"🕐 **เวลา (TH)**  `{now_bkk()}`"
    )
    embed = {
        "title": label,
        "description": description,
        "color": color,
        "footer": {"text": footer_text},
    }
    payload = {"embeds": [embed]}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False


def extract_dynamic_plan(analysis: dict | None) -> dict | None:
    if not analysis:
        return None
    metrics = analysis.get("metrics", {})
    plan = metrics.get("dynamic_levels") or {}
    required = ("buy_price", "sell_price", "stop_loss", "take_profit")
    if not all(k in plan for k in required):
        return None
    return plan


def plan_change_ratio(old_plan: dict | None, new_plan: dict | None) -> float:
    if not old_plan or not new_plan:
        return 1.0
    keys = ("buy_price", "sell_price", "stop_loss", "take_profit")
    ratios: list[float] = []
    for key in keys:
        old_val = float(old_plan.get(key, 0.0) or 0.0)
        new_val = float(new_plan.get(key, 0.0) or 0.0)
        if old_val <= 0:
            continue
        ratios.append(abs(new_val - old_val) / old_val)
    return max(ratios) if ratios else 0.0


def dynamic_plan_text(plan: dict | None) -> str:
    if not plan:
        return "ไม่พบข้อมูล dynamic levels เพียงพอ"
    return (
        f"BUY: ${plan['buy_price']:.2f} | SELL: ${plan['sell_price']:.2f}"
        f"\nSTOP: ${plan['stop_loss']:.2f} | TARGET: ${plan['take_profit']:.2f}"
        f"\nSUPPORT/RESIST: ${plan.get('support', 0.0):.2f}/${plan.get('resistance', 0.0):.2f}"
    )


def dynamic_plan_playbook(plan: dict | None, current_price: float, analysis: dict | None = None) -> str:
    """Format a readable buy/sell playbook for 1m dynamic updates."""
    if not plan:
        return "ไม่พบข้อมูล dynamic levels เพียงพอ"

    buy_price = float(plan.get("buy_price", 0.0) or 0.0)
    sell_price = float(plan.get("sell_price", 0.0) or 0.0)
    stop_loss = float(plan.get("stop_loss", 0.0) or 0.0)
    take_profit = float(plan.get("take_profit", 0.0) or 0.0)
    support = float(plan.get("support", 0.0) or 0.0)
    resistance = float(plan.get("resistance", 0.0) or 0.0)
    volatility = float(plan.get("volatility", 0.0) or 0.0)

    projected_high = resistance + (0.5 * volatility if volatility > 0 else 0.0)

    entry = buy_price if buy_price > 0 else current_price
    rr = None
    if entry > 0 and stop_loss > 0 and entry > stop_loss and sell_price > 0:
        rr = (sell_price - entry) / (entry - stop_loss)

    rr_text = f"1 : {rr:.2f} {'✅' if rr >= 1.5 else '⚠️'}" if rr is not None else "N/A"
    if current_price < buy_price:
        buy_hint = "รอ pullback ลงมาโซนนี้ก่อน"
    elif current_price <= resistance:
        buy_hint = "ราคาอยู่ใกล้โซนเข้า ลุ้นเด้งกลับ"
    else:
        buy_hint = "ราคาเลยจุดเข้าแล้ว รอจังหวะใหม่"

    breakout_low = min(projected_high, take_profit) if take_profit > 0 else projected_high
    breakout_high = max(projected_high, take_profit) if take_profit > 0 else projected_high

    return (
        "**แผน BUY**\n"
        f"🟢 เข้าซื้อ (ideal entry): `${buy_price:.2f}` — {buy_hint}\n"
        f"🛑 Stop loss: `${stop_loss:.2f}` — ตัดขาดทุนถ้าลงต่ำกว่านี้\n"
        f"💡 เป้าขาย 1st: `${sell_price:.2f}` — ใกล้ resistance\n"
        f"🎯 เป้าขาย 2nd: `${take_profit:.2f}` — ถ้าราคาแรงจริง\n"
        f"📊 คาด intraday high: `~${projected_high:.2f}`\n"
        f"⚖️ Risk/Reward: `{rr_text}`\n\n"
        "**แผน SELL (ถ้าถืออยู่)**\n"
        f"🔴 ทยอยขายทำกำไร: `${sell_price:.2f}`\n"
        f"🎯 ปิดเพิ่มเมื่อราคาวิ่งต่อ: `${take_profit:.2f}`\n"
        f"🛡️ ถ้าราคาอ่อนแรง: วาง stop ที่ `${stop_loss:.2f}`\n\n"
        "**แนวทาง 2 scenario:**\n"
        f"- Pullback: รอราคาลงมาโซน `${buy_price:.2f}`–`${support:.2f}` แล้วค่อยเข้าซื้อ (safe)\n"
        f"- Breakout: ถ้าราคาทะลุ `${resistance:.2f}` ชัดเจน อาจเข้าตาม momentum เป้า `${breakout_low:.2f}`–`${breakout_high:.2f}`"
    )
