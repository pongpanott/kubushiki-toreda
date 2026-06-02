# Finnhub poller (MVP)

This script polls Finnhub 1-minute candles, computes simple signals, and posts alerts to Discord.

Setup

1. Create a Python venv and install deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r mvp_trading/requirements.txt
```

2. Export environment variables:

```bash
export FINNHUB_API_KEY=your_finnhub_key
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
export SYMBOL=AAPL
# optional
export POLL_MINUTES=5
export PCT_THRESHOLD=0.4
```

Run

```bash
python mvp_trading/finnhub_poller.py
```

Run continuously

- Use `launchd` on macOS or run inside `tmux`/`screen`/systemd on Linux.
- To run every 60 seconds with `launchd`, set `StartInterval` to `60` in the agent plist and point `ProgramArguments` to the venv python and this script.

Collector service

- To centralize Finnhub requests (single request per symbol per interval) run the collector:

```bash
# start collector (background service recommended)
uvicorn mvp_trading.collector_service:app --host 127.0.0.1 --port 8000 --reload
```

- The collector polls Finnhub once per `POLL_INTERVAL_SECONDS` and exposes `/latest?symbol=NVDA`.
- Point `mvp_trading/finnhub_poller.py` to use the local collector by setting `LOCAL_COLLECTOR_URL=http://127.0.0.1:8000`.

Security

- Do not commit API keys. Use macOS Keychain or export them in the shell for the service user.

Limitations

- This is an MVP: it uses simple percent-move and small SMA cross detection. Tweak thresholds and logic for your strategy.
- Finnhub rate limits apply; avoid excessive polling or large request windows.
