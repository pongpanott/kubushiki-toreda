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


def get_upcoming_events(days_ahead: int = 14) -> list[str]:
    """Return events within the next N days (loaded from config or fallback)."""
    today = datetime.now(timezone.utc).date()
    lines = []
    for ev in _load_upcoming_events():
        ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        delta = (ev_date - today).days
        if 0 <= delta <= days_ahead:
            lines.append(f"{ev['event']} — อีก **{delta} วัน** ({ev['date']})")
    return lines


def now_th() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def send_reminder(webhook_url: str, reason: str, detail: str) -> None:
    title, base_msg = REASON_LABELS.get(reason, REASON_LABELS["manual"])
    description = detail if detail else base_msg

    last_update = get_last_commit_info()
    events = get_upcoming_events()
    events_text = "\n".join(f"• {e}" for e in events) if events else "ไม่มี catalyst ใน 14 วันข้างหน้า"

    embed = {
        "title": title,
        "description": (
            f"{description}\n\n"
            f"**วิธีอัปเดต Alert Levels:**\n"
            f"1. เปิด Claude Code ใน repo นี้\n"
            f"2. พิมพ์: *\"วิเคราะห์ NVDA ตอนนี้ แนะนำ entry/stop/target ใหม่\"*\n"
            f"3. Claude แก้ `scripts/nvda_discord_alert.py` ให้เลย\n"
            f"4. `git push` → GitHub Actions รับ levels ใหม่ทันที"
        ),
        "color": 0x5865F2,
        "fields": [
            {
                "name": "📝 อัปเดตล่าสุด",
                "value": last_update,
                "inline": False,
            },
            {
                "name": "⚡ Catalysts ใน 14 วันข้างหน้า",
                "value": events_text,
                "inline": False,
            },
            {
                "name": "🕐 เวลาแจ้งเตือน (TH)",
                "value": now_th(),
                "inline": True,
            },
        ],
        "footer": {"text": "NVDA Alert Bot — ส่งโดย GitHub Actions"},
    }

    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()
    print(f"[{now_th()}] ✅ Reminder sent: {title}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send Claude workflow review reminder to Discord")
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("DISCORD_NVDA_ALERT_WEBHOOK", ""),
        help="Discord Webhook URL (หรือตั้ง DISCORD_NVDA_ALERT_WEBHOOK env var)",
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
        print("   ใช้ --webhook-url หรือ export DISCORD_NVDA_ALERT_WEBHOOK=...")
        raise SystemExit(1)

    send_reminder(args.webhook_url, args.reason, args.detail)


if __name__ == "__main__":
    main()
