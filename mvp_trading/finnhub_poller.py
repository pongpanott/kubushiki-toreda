#!/usr/bin/env python3
"""Finnhub 1-minute poller that reads OHLC bars, computes simple signals,
and posts alerts to Discord on significant changes.

Usage:
  FINNHUB_API_KEY=xxx DISCORD_WEBHOOK_URL=xxx SYMBOL=AAPL python mvp_trading/finnhub_poller.py

Run as a long-running process (launchd, systemd, or screen/tmux). The script
polls every 60 seconds and avoids duplicate alerts for the same bar timestamp.
"""
import os
import sys
import time
import json
import math
import logging
from datetime import datetime, timezone

import requests

LOG = logging.getLogger('finnhub_poller')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


FINNHUB_REST = 'https://finnhub.io/api/v1/stock/candle'


def utc_ts():
    return int(datetime.now(timezone.utc).timestamp())


def fetch_candles(symbol: str, minutes: int, token: str):
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
    # Finnhub returns arrays: c, h, l, o, t, v and a status 's'
    if data.get('s') != 'ok':
        LOG.warning('Finnhub returned status=%s payload=%s', data.get('s'), data)
        return []
    # Build bars as list of dicts, oldest-first
    bars = []
    for i, t in enumerate(data['t']):
        bars.append({
            't': int(t),
            'o': float(data['o'][i]),
            'h': float(data['h'][i]),
            'l': float(data['l'][i]),
            'c': float(data['c'][i]),
            'v': float(data['v'][i]),
        })
    return bars


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def detect_significant(bars, pct_threshold=0.4, sma_short=3, sma_long=8):
    # bars: oldest-first
    if len(bars) < 3:
        return None
    last = bars[-1]
    prev = bars[-2]
    # percent change between last close and prev close
    try:
        pct = (last['c'] - prev['c']) / prev['c'] * 100.0
    except Exception:
        pct = 0.0

    closes = [b['c'] for b in bars]
    sma_s = sma(closes, sma_short)
    sma_l = sma(closes, sma_long)

    reasons = []
    if abs(pct) >= pct_threshold:
        reasons.append(f'price_move_pct={pct:.2f}%')

    if sma_s is not None and sma_l is not None:
        # detect simple crossover in last two points
        prev_s = sum(closes[-(sma_short+1):-1]) / sma_short if len(closes) >= sma_short+1 else None
        prev_l = sum(closes[-(sma_long+1):-1]) / sma_long if len(closes) >= sma_long+1 else None
        if prev_s is not None and prev_l is not None:
            # golden cross
            if prev_s <= prev_l and sma_s > sma_l:
                reasons.append('sma_cross=golden')
            if prev_s >= prev_l and sma_s < sma_l:
                reasons.append('sma_cross=death')

    if reasons:
        return {
            'time': last['t'],
            'last_close': last['c'],
            'prev_close': prev['c'],
            'pct': pct,
            'sma_short': sma_s,
            'sma_long': sma_l,
            'reasons': reasons,
        }
    return None


def send_discord(webhook: str, symbol: str, sig: dict):
    if not webhook:
        LOG.info('No Discord webhook configured; skipping send')
        return False
    ts = datetime.fromtimestamp(sig['time'], tz=timezone.utc).isoformat()
    content = f'Alert for {symbol}: {", '.join(sig["reasons"])}'
    embed = {
        'title': f'{symbol} alert',
        'description': content,
        'fields': [
            {'name': 'Time (UTC)', 'value': ts, 'inline': True},
            {'name': 'Last', 'value': f"{sig['last_close']}", 'inline': True},
            {'name': 'Prev', 'value': f"{sig['prev_close']}", 'inline': True},
            {'name': 'Pct', 'value': f"{sig['pct']:.3f}%", 'inline': True},
        ],
        'color': 15105570,
    }
    payload = {'embeds': [embed]}
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        if r.status_code >= 400:
            LOG.error('Discord webhook failed: %s %s', r.status_code, r.text)
            return False
        LOG.info('Discord alert sent')
        return True
    except Exception as e:
        LOG.error('Discord send exception: %s', e)
        return False


def recommend_action(sig: dict, pct_threshold=0.4):
    """Return ('BUY'|'SELL'|'HOLD', confidence_float 0..1, reason_str)"""
    pct = sig.get('pct', 0.0)
    sma_s = sig.get('sma_short')
    sma_l = sig.get('sma_long')
    reasons = list(sig.get('reasons', []))

    cross = None
    if sma_s is not None and sma_l is not None:
        if sma_s > sma_l:
            cross = 'golden'
        elif sma_s < sma_l:
            cross = 'death'

    action = 'HOLD'
    score = 0.0

    # basic momentum
    if pct is not None:
        mag = abs(pct)
        score = min(1.0, mag / max(0.0001, pct_threshold) * 0.5)

    # crossover influence
    if cross == 'golden' and pct > 0:
        action = 'BUY'
        score = min(1.0, score + 0.5)
        reasons.append('golden_sma')
    elif cross == 'death' and pct < 0:
        action = 'SELL'
        score = min(1.0, score + 0.5)
        reasons.append('death_sma')
    else:
        # if strong momentum without cross
        if pct >= pct_threshold:
            action = 'BUY'
        elif pct <= -pct_threshold:
            action = 'SELL'

    if action == 'HOLD' and reasons:
        # if reasons exist but no clear direction, set hold with low confidence
        score = max(score, 0.1)

    return action, float(score), ", ".join(reasons)


def send_analysis_discord(webhook: str, symbol: str, sig: dict, pct_threshold=0.4):
    if not webhook:
        LOG.info('No analysis webhook configured; skipping analysis post')
        return False
    action, confidence, reasons = recommend_action(sig, pct_threshold=pct_threshold)
    ts = datetime.fromtimestamp(sig['time'], tz=timezone.utc).isoformat()
    description = f'Recommendation: **{action}** (confidence {confidence:.2f})\nReasons: {reasons}'
    embed = {
        'title': f'{symbol} - Technical Analysis',
        'description': description,
        'fields': [
            {'name': 'Time (UTC)', 'value': ts, 'inline': True},
            {'name': 'Last Close', 'value': f"{sig['last_close']}", 'inline': True},
            {'name': 'Prev Close', 'value': f"{sig['prev_close']}", 'inline': True},
            {'name': 'Pct', 'value': f"{sig['pct']:.3f}%", 'inline': True},
            {'name': 'SMA short', 'value': f"{sig.get('sma_short')}", 'inline': True},
            {'name': 'SMA long', 'value': f"{sig.get('sma_long')}", 'inline': True},
            {'name': 'Confidence', 'value': f"{confidence:.2f}", 'inline': True},
        ],
        'color': 3066993 if action == 'BUY' else (15105570 if action == 'SELL' else 8444679),
    }
    payload = {'embeds': [embed]}
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        if r.status_code >= 400:
            LOG.error('Analysis webhook failed: %s %s', r.status_code, r.text)
            return False
        LOG.info('Analysis posted to Discord for %s: %s %s', symbol, action, confidence)
        return True
    except Exception as e:
        LOG.error('Analysis send exception: %s', e)
        return False


def run_loop(symbols, webhook, poll_minutes=5, pct_threshold=0.4):
    # symbols: list of symbol strings
    # per-symbol API key via env FINNHUB_API_KEY_<SYMBOLUPPER> or fallback to FINNHUB_API_KEY
    last_alert_ts = {s: None for s in symbols}
    last_call_per_key = {}  # token -> last_call_ts
    calls_today = {}  # token -> int
    backoff = {}  # token -> backoff_seconds

    def get_token_for(symbol):
        envname = f'FINNHUB_API_KEY_{symbol.upper()}'
        return os.getenv(envname) or os.getenv('FINNHUB_API_KEY')

    def ensure_day_counts():
        # reset counters at UTC midnight
        today = datetime.now(timezone.utc).date()
        if not hasattr(ensure_day_counts, 'day') or ensure_day_counts.day != today:
            ensure_day_counts.day = today
            for k in list(calls_today.keys()):
                calls_today[k] = 0

    LOG.info('Starting multi-symbol poller symbols=%s poll_minutes=%s', symbols, poll_minutes)
    while True:
        ensure_day_counts()
        loop_start = time.time()
        for symbol in symbols:
            token = get_token_for(symbol)
            if not token:
                LOG.error('No token for symbol %s (check FINNHUB_API_KEY or FINNHUB_API_KEY_%s)', symbol, symbol.upper())
                continue

            now = time.time()
            last = last_call_per_key.get(token, 0)
            elapsed = now - last
            if elapsed < 60:
                wait = 60 - elapsed
                LOG.debug('Respecting per-key pacing for token ending.. sleeping %.1fs before %s', wait, symbol)
                time.sleep(wait)

            # perform fetch with per-key backoff handling
            bks = backoff.get(token, 1)
            try:
                start = time.time()
                bars = fetch_candles(symbol, poll_minutes, token)
                calls_today[token] = calls_today.get(token, 0) + 1
                last_call_per_key[token] = time.time()
                backoff[token] = 1

                if not bars:
                    LOG.info('%s: no bars returned', symbol)
                    continue

                sig = detect_significant(bars, pct_threshold=pct_threshold)
                if sig:
                    if sig['time'] != last_alert_ts.get(symbol):
                        LOG.info('Significant detected %s: %s', symbol, sig)
                        sent = send_analysis_discord(webhook, symbol, sig, pct_threshold=pct_threshold)
                        if sent:
                            last_alert_ts[symbol] = sig['time']
                    else:
                        LOG.debug('%s: already alerted for ts=%s', symbol, sig['time'])

            except requests.exceptions.HTTPError as he:
                status = getattr(he.response, 'status_code', None)
                LOG.warning('HTTP error for %s: %s', symbol, he)
                if status == 429:
                    # rate limited — increase backoff
                    bks = min(backoff.get(token, 1) * 2, 300)
                    jitter = min(10, bks * 0.1)
                    sleep_sec = bks + (jitter * (0.5 - os.urandom(1)[0]/255.0))
                    LOG.warning('429 for token: backing off %.1fs', sleep_sec)
                    backoff[token] = bks
                    time.sleep(max(1, sleep_sec))
                else:
                    time.sleep(5)
            except Exception as e:
                LOG.exception('Error fetching %s: %s', symbol, e)
                # transient sleep
                time.sleep(5)

        # after looping symbols, sleep until next 60s boundary
        elapsed_total = time.time() - loop_start
        sleep_for = max(1, 60 - elapsed_total)
        time.sleep(sleep_for)


def compute_expected_usage(symbols, poll_interval_seconds=60):
    # Returns dict: token -> {'symbols': [...], 'expected_calls_per_day': n}
    token_map = {}
    for symbol in symbols:
        envname = f'FINNHUB_API_KEY_{symbol.upper()}'
        token = os.getenv(envname) or os.getenv('FINNHUB_API_KEY')
        if not token:
            token = f'__MISSING__:{symbol}'
        token_map.setdefault(token, []).append(symbol)

    calls_per_symbol_per_day = int(86400 / poll_interval_seconds)
    per_token = {}
    per_key_cap = int(86400 / poll_interval_seconds)  # enforce 1 call per interval per key
    for token, syms in token_map.items():
        total = calls_per_symbol_per_day * len(syms)
        expected = min(total, per_key_cap)
        per_token[token] = {'symbols': syms, 'expected_calls_per_day': expected}
    return per_token


def check_quotas(per_token_map, poll_interval_seconds=60):
    # Compare expected usage to env quotas: FINNHUB_DAILY_QUOTA_{SYMBOL} or FINNHUB_DAILY_QUOTA
    problems = []
    for token, info in per_token_map.items():
        # Try to determine a quota: if single symbol, check FINNHUB_DAILY_QUOTA_{SYMBOL}
        quota = None
        for sym in info['symbols']:
            envq = os.getenv(f'FINNHUB_DAILY_QUOTA_{sym.upper()}')
            if envq:
                try:
                    quota = int(envq)
                    break
                except Exception:
                    pass
        if quota is None:
            g = os.getenv('FINNHUB_DAILY_QUOTA')
            try:
                quota = int(g) if g else int(86400 / poll_interval_seconds)
            except Exception:
                quota = int(86400 / poll_interval_seconds)

        expected = info['expected_calls_per_day']
        pct = expected / max(1, quota) * 100.0
        info['quota'] = quota
        info['pct_of_quota'] = pct
        if pct > 95.0:
            problems.append((token, info))

    return problems


def main():
    # Parse configuration
    symbols_env = os.getenv('SYMBOLS') or os.getenv('SYMBOL') or 'AAPL'
    symbols = [s.strip().upper() for s in symbols_env.split(',') if s.strip()]
    if not symbols:
        LOG.error('No symbols configured (set SYMBOLS or SYMBOL)')
        sys.exit(1)

    webhook = os.getenv('DISCORD_WEBHOOK_URL')
    analysis_webhook = os.getenv('DISCORD_ANALYSIS_WEBHOOK_URL') or os.getenv('DISCORD_WEBHOOK_URL')
    poll_minutes = int(os.getenv('POLL_MINUTES', '5'))
    pct_threshold = float(os.getenv('PCT_THRESHOLD', '0.4'))
    poll_interval_seconds = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))

    per_token_map = compute_expected_usage(symbols, poll_interval_seconds=poll_interval_seconds)
    LOG.info('Expected usage per token:')
    for token, info in per_token_map.items():
        LOG.info(' token=%s symbols=%s expected/day=%s quota=%s', token if not token.startswith('__MISSING__') else token, info['symbols'], info['expected_calls_per_day'], os.getenv(f'FINNHUB_DAILY_QUOTA') or 'unset')

    problems = check_quotas(per_token_map, poll_interval_seconds=poll_interval_seconds)
    if problems:
        LOG.error('Quota warnings: some keys will be near or exceed their quotas:')
        for token, info in problems:
            LOG.error(' token=%s symbols=%s expected=%s quota=%s (%.1f%%)', token, info['symbols'], info['expected_calls_per_day'], info['quota'], info['pct_of_quota'])
        allow = os.getenv('ALLOW_EXCEED', '').lower() in ('1', 'true', 'yes')
        if not allow:
            LOG.error('Set ALLOW_EXCEED=true to override and continue despite quota exceedance')
            sys.exit(2)

    run_loop(symbols, analysis_webhook, poll_minutes=poll_minutes, pct_threshold=pct_threshold)


if __name__ == '__main__':
    main()
