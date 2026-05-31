#!/usr/bin/env python3
"""
Notify Discord channel about code/workflow updates and basic health check.

Reads repository commit info and runs a lightweight syntax check on Python files.
Sends an embed message to the webhook provided in env `DISCORD_ANALYSIS_WEBHOOK_URL`.
"""

import os
import subprocess
import json
from datetime import datetime
import requests


def run_cmd(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True).strip()
    except subprocess.CalledProcessError as e:
        return e.output or str(e)


def get_commit_info():
    sha = run_cmd("git rev-parse --short HEAD")
    author = run_cmd("git log -1 --pretty=format:'%an' HEAD").strip("'")
    email = run_cmd("git log -1 --pretty=format:'%ae' HEAD").strip("'")
    message = run_cmd("git log -1 --pretty=format:'%s' HEAD").strip("'")
    changed = run_cmd("git diff-tree --no-commit-id --name-only -r HEAD")
    changed_list = [f for f in changed.splitlines() if f]
    return {
        "sha": sha,
        "author": author,
        "email": email,
        "message": message,
        "changed": changed_list,
    }


# Discord-related scripts to health-check on every push
_DISCORD_SCRIPTS = [
    "scripts/nvda_discord_alert.py",
    "scripts/auto_analyze_and_update.py",
    "scripts/send_review_reminder.py",
    "scripts/notify_discord_code_update.py",
]


def syntax_check():
    """Syntax-check only the Discord alert scripts (avoids permission issues in examples/)."""
    present = [f for f in _DISCORD_SCRIPTS if os.path.exists(f)]
    if not present:
        return True, "No Discord scripts found to check"
    errors = []
    for path in present:
        result = subprocess.run(
            ["python3", "-m", "py_compile", path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            errors.append(f"{path}: {result.stderr.strip()}")
    if errors:
        return False, "\n".join(errors)
    return True, f"Syntax OK ({len(present)} Discord scripts checked)"


def send_webhook(webhook: str, commit: dict, health_ok: bool, health_msg: str):
    changed = commit.get("changed", [])
    changed_text = "\n".join(f"• {c}" for c in changed[:20]) or "—"
    if len(changed) > 20:
        changed_text += f"\n...and {len(changed)-20} more files"

    embed = {
        "title": "🔁 Repository Update — New Commit Pushed",
        "description": f"**{commit['message']}**\n{commit['sha']} by {commit['author']}",
        "color": 0x00FFAA if health_ok else 0xFF5500,
        "fields": [
            {"name": "Changed Files", "value": changed_text, "inline": False},
            {"name": "Health Check (Discord scripts)", "value": health_msg[:1024], "inline": False},
            {"name": "Time (UTC)", "value": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), "inline": True},
        ],
        "footer": {"text": "Code update notifier — claude-trading-skills"},
    }
    payload = {"embeds": [embed]}
    resp = requests.post(webhook, json=payload, timeout=10)
    resp.raise_for_status()
    print("Notification sent")


def main():
    webhook = os.environ.get("DISCORD_ANALYSIS_WEBHOOK_URL")
    if not webhook:
        print("Missing DISCORD_ANALYSIS_WEBHOOK_URL")
        raise SystemExit(1)

    commit = get_commit_info()
    ok, msg = syntax_check()
    health_msg = msg if isinstance(msg, str) else str(msg)
    send_webhook(webhook, commit, ok, health_msg)


if __name__ == '__main__':
    main()
