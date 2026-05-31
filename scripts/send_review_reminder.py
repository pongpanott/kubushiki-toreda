#!/usr/bin/env python3
"""
Send a scheduled reminder to Discord when it's time to re-run
the Claude market analysis workflow and update NVDA alert levels.

Usage:
    python3 scripts/send_review_reminder.py --reason weekly
    python3 scripts/send_review_reminder.py --reason pre_catalyst --detail "Computex keynote tomorrow"
    python3 scripts/send_review_reminder.py --reason post_move --detail "NVDA moved >5% today"
"""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "nvda_alert_levels.json"

# Hardcoded fallback — overridden by config/nvda_alert_levels.json when available
_FALLBACK_EVENTS = [
    {"date": "2026-06-02", "event": "🖥️ Computex 2026 เริ่ม (Jensen Huang keynote)"},
    {"date": "2026-06-04", "event": "💰 NVDA Ex-Dividend Date ($0.25/share)"},
    {"date": "2026-06-06", "event": "🖥️ Computex 2026 สิ้นสุด"},
    {"date": "2026-08-26", "event": "📊 NVDA Earnings Q2 FY27"},
]


def _load_current_levels() -> dict:
    """Load the latest AI-recommended trading levels from config.

    Returns a dict with keys:
      price_at_update, last_updated, analysis_summary, levels (list)
    or an empty dict if the config is missing / unreadable.
    """
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _load_upcoming_events() -> list[dict]:
    """Load events from config/nvda_alert_levels.json, fall back to hardcoded list."""
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text())
            events = data.get("upcoming_events", [])
            if events:
                return events
        except Exception:
            pass
    return _FALLBACK_EVENTS


REASON_LABELS = {
    "weekly":       ("📅 Weekly Review", "ถึงเวลา Weekly Review แล้ว — ตรวจสอบ NVDA alert levels"),
    "pre_catalyst": ("⚡ Pre-Catalyst Alert", "มี catalyst สำคัญใกล้มาแล้ว — ควร review levels ก่อน"),
    "post_move":    ("📈 Big Move Detected", "NVDA เคลื่อนไหวแรง — ควรอัปเดต levels ให้ตรงกับราคาใหม่"),
    "earnings":     ("📊 Pre-Earnings Review", "Earnings ใกล้มาแล้ว — ปรับ levels ก่อนตลาดผันผวน"),
    "manual":       ("🔔 Manual Reminder", ""),
}
REASON_COLORS = {
    "weekly":       0x5865F2,  # Discord blurple
    "pre_catalyst": 0xF39C12,  # orange warning
    "post_move":    0xE74C3C,  # red alert
    "earnings":     0xFF8C00,  # amber
    "manual":       0x95A5A6,  # grey
}
# ---------------------------------------------------------------------------


def get_last_commit_info() -> str:
    """Return last git commit message + date for alert script."""
    try:
        msg = subprocess.check_output(
            ["git", "log", "-1", "--format=%cr — %s", "--", "scripts/nvda_discord_alert.py"],
            text=True,
        ).strip()
        return msg or "ไม่พบข้อมูล commit"
    except Exception:
        return "ไม่สามารถดึงข้อมูล git ได้"


def get_upcoming_events(days_ahead: int = 14) -> list[dict]:
    """Return dicts {date, event, days} for events within the next N days."""
    today = datetime.now(timezone.utc).date()
    result = []
    for ev in _load_upcoming_events():
        try:
            delta = (datetime.strptime(ev["date"], "%Y-%m-%d").date() - today).days
            if 0 <= delta <= days_ahead:
                result.append({"date": ev["date"], "event": ev["event"], "days": delta})
        except ValueError:
            pass
    return result


def now_th() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _build_levels_embed(cfg: dict) -> dict | None:
    """Build a Discord embed showing current AI-recommended trading prices.

    Returns None when config is empty (no data to show).
    """
    levels: list[dict] = cfg.get("levels", [])
    if not levels:
        return None

    ref_price: float = cfg.get("price_at_update", 0.0)
    last_updated: str = cfg.get("last_updated", "?")
    summary: str = cfg.get("analysis_summary", "")

    def pct(p: float) -> str:
        if ref_price <= 0:
            return ""
        return f"{(p - ref_price) / ref_price * 100:+.1f}%"

    above = sorted(
        [lv for lv in levels if lv.get("direction") == "above"],
        key=lambda x: x.get("price", 0),
    )
    below = sorted(
        [lv for lv in levels if lv.get("direction") == "below"],
        key=lambda x: x.get("price", 0),
        reverse=True,
    )

    def fmt(lst: list) -> str:
        return "\n".join(
            f"{lv['label']}  **`${lv['price']:.2f}`**  `{pct(lv['price'])}`"
            for lv in lst
        ) or "—"

    fields: list[dict] = []
    if ref_price:
        fields.append(
            {"name": "💵  ราคา ณ เวลาวิเคราะห์", "value": f"**`${ref_price:.2f}`**", "inline": True}
        )
    if last_updated:
        fields.append(
            {"name": "📅  อัปเดตล่าสุด", "value": f"`{last_updated}`", "inline": True}
        )
    if above:
        fields.append(
            {"name": "🎯  Targets  ↑ above", "value": fmt(above), "inline": False}
        )
    if below:
        fields.append(
            {"name": "🟢  Buy Zones / Stop  ↓ below", "value": fmt(below), "inline": False}
        )

    return {
        "title": "📊  NVDA — Current AI Trading Levels",
        "description": summary or "ระดับราคา entry / stop / target ที่ AI แนะนำล่าสุด",
        "color": 0x2ECC71,
        "fields": fields,
        "footer": {"text": "Source: config/nvda_alert_levels.json  •  อัปเดตโดย auto-update-alerts workflow"},
    }


def send_reminder(webhook_url: str, reason: str, detail: str) -> None:
    title, base_msg = REASON_LABELS.get(reason, REASON_LABELS["manual"])
    color = REASON_COLORS.get(reason, 0x5865F2)
    description = detail if detail else base_msg

    events = get_upcoming_events()
    event_lines = []
    for ev in events:
        days = ev["days"]
        if days == 0:
            tag = "**วันนี้** 🔔"
        elif days <= 3:
            tag = f"อีก **{days} วัน** ⚠️"
        else:
            tag = f"อีก {days} วัน"
        event_lines.append(f"`{ev['date']}`  {ev['event']}  —  {tag}")

    last_update = get_last_commit_info()
    timestamp = datetime.now().strftime("%d %b %Y  %H:%M")

    fields: list[dict] = [
        {
            "name": "🕐  เวลา (TH)",
            "value": f"`{timestamp}`",
            "inline": True,
        },
    ]
    if event_lines:
        fields.append(
            {
                "name": "⚡  Upcoming Catalysts",
                "value": "\n".join(event_lines),
                "inline": False,
            }
        )
    fields.append(
        {
            "name": "📝  Last Commit",
            "value": f"`{last_update}`",
            "inline": False,
        }
    )
    fields.append(
        {
            "name": "🛠  วิธีอัปเดต",
            "value": (
                "เปิด Claude Code → พิมพ์:\n"
                "> **\"วิเคราะห์ NVDA ตอนนี้ แนะนำ entry/stop/target ใหม่\"**"
            ),
            "inline": False,
        }
    )

    embeds: list[dict] = [
        {
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": "Auto-reminder  •  GitHub Actions  •  claude-trading-skills"},
        }
    ]

    # ── Weekly-only: inject a second embed with current AI-recommended levels ──
    if reason == "weekly":
        cfg = _load_current_levels()
        lvl_embed = _build_levels_embed(cfg)
        if lvl_embed:
            embeds.append(lvl_embed)

    resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=10)
    resp.raise_for_status()
    print(f"[{now_th()}] ✅ Reminder sent: {title}  ({len(embeds)} embed(s))")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send Claude workflow review reminder to Discord")
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("DISCORD_ANALYSIS_WEBHOOK_URL", ""),
        help="Discord Webhook URL (หรือตั้ง DISCORD_ANALYSIS_WEBHOOK_URL env var)",
    )
    parser.add_argument(
        "--reason",
        choices=list(REASON_LABELS.keys()),
        default="weekly",
        help="ประเภทของ reminder",
    )
    parser.add_argument(
        "--detail",
        default="",
        help="รายละเอียดเพิ่มเติม (optional)",
    )
    args = parser.parse_args()

    if not args.webhook_url:
        print("❌ ต้องระบุ Discord Webhook URL")
        print("   ใช้ --webhook-url หรือ export DISCORD_ANALYSIS_WEBHOOK_URL=...")
        raise SystemExit(1)

    send_reminder(args.webhook_url, args.reason, args.detail)


if __name__ == "__main__":
    main()
