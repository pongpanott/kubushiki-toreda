#!/usr/bin/env python3
"""Local collector service: polls Finnhub once per interval and serves cached latest bars.

Run as a background service (uvicorn) and point other processes to LOCAL_COLLECTOR_URL
so they can reuse a single Finnhub request per symbol/key.
"""
import os
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, Any

import requests
from fastapi import FastAPI, HTTPException

LOG = logging.getLogger('collector')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app = FastAPI()

# in-memory cache: symbol -> latest bar (oldest-first not needed)
CACHE: Dict[str, Dict[str, Any]] = {}
# token usage tracking
CALLS = {}

FINNHUB_REST = 'https://finnhub.io/api/v1/stock/candle'


def utc_ts():
    return int(datetime.now(timezone.utc).timestamp())


def fetch_candle_for(symbol: str, token: str, minutes: int = 5):
    to_ts = utc_ts()
    from_ts = to_ts - minutes * 60
    params = {
        'symbol': symbol,
        'resolution': '1',
        'from': str(from_ts),
        'to': str(to_ts),
        'token': token,
    }
    r = requests.get(FINNHUB_REST, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get('s') != 'ok':
        LOG.warning('Finnhub returned non-ok: %s', data)
        return None
    # return latest bar (newest)
    i = -1
    return {
        't': int(data['t'][i]),
        'o': float(data['o'][i]),
        'h': float(data['h'][i]),
        'l': float(data['l'][i]),
        'c': float(data['c'][i]),
        'v': float(data['v'][i]),
    }


def get_token_for(symbol: str):
    envname = f'FINNHUB_API_KEY_{symbol.upper()}'
    return os.getenv(envname) or os.getenv('FINNHUB_API_KEY')


def poll_loop(symbols, poll_interval_seconds=60, poll_minutes=5):
    LOG.info('Collector poll loop start symbols=%s interval=%s', symbols, poll_interval_seconds)
    while True:
        start = time.time()
        for symbol in symbols:
            token = get_token_for(symbol)
            if not token:
                LOG.error('No token for %s', symbol)
                continue
            try:
                bar = fetch_candle_for(symbol, token, minutes=poll_minutes)
                if bar:
                    CACHE[symbol] = {'fetched_at': utc_ts(), 'bar': bar}
                    CALLS[token] = CALLS.get(token, 0) + 1
                    LOG.debug('Updated cache %s -> %s', symbol, bar)
            except requests.exceptions.HTTPError as he:
                LOG.warning('HTTP error fetching %s: %s', symbol, he)
            except Exception as e:
                LOG.exception('Error polling %s: %s', symbol, e)

        elapsed = time.time() - start
        sleep_for = max(1, poll_interval_seconds - elapsed)
        time.sleep(sleep_for)


@app.get('/latest')
def latest(symbol: str):
    s = symbol.upper()
    if s not in CACHE:
        raise HTTPException(status_code=404, detail='no data')
    return CACHE[s]


@app.get('/health')
def health():
    return {'status': 'ok', 'cached_symbols': list(CACHE.keys())}


def start_background(symbols, poll_interval_seconds=60, poll_minutes=5):
    t = threading.Thread(target=poll_loop, args=(symbols, poll_interval_seconds, poll_minutes), daemon=True)
    t.start()


if __name__ == '__main__':
    import uvicorn
    symbols_env = os.getenv('SYMBOLS') or os.getenv('SYMBOL') or 'AAPL'
    symbols = [s.strip().upper() for s in symbols_env.split(',') if s.strip()]
    poll_interval_seconds = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))
    poll_minutes = int(os.getenv('POLL_MINUTES', '5'))
    start_background(symbols, poll_interval_seconds=poll_interval_seconds, poll_minutes=poll_minutes)
    uvicorn.run('mvp_trading.collector_service:app', host='127.0.0.1', port=8000, log_level='info')
