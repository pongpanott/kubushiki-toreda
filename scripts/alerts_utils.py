"""Lightweight alert analysis utilities.

Stores recent price points locally and computes simple indicators (MA, RSI,
momentum) from the cached price series. Designed to avoid extra Finnhub
API calls by reusing the quotes polled every minute.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import List, Tuple

CACHE_DIR = Path(__file__).parent.parent / "state" / "ohlcv_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_file(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}.jsonl"


def append_price_point(ticker: str, ts: datetime, price: float) -> None:
    """Append a simple price point (timestamp, price) to cache file."""
    p = _cache_file(ticker)
    obj = {"ts": ts.replace(tzinfo=timezone.utc).isoformat(), "price": float(price)}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_price_series(ticker: str, minutes: int = 240) -> List[Tuple[datetime, float]]:
    """Load recent price points within the past `minutes` from cache.

    Returns list of (datetime, price) sorted ascending by time.
    """
    p = _cache_file(ticker)
    if not p.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    out: List[Tuple[datetime, float]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                ts = datetime.fromisoformat(obj["ts"]).astimezone(timezone.utc)
                if ts >= cutoff:
                    out.append((ts, float(obj["price"])))
            except Exception:
                continue
    out.sort(key=lambda x: x[0])
    return out


def _rsi_from_prices(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    # use simple average over last `period` entries
    gains_avg = mean(gains[-period:]) if len(gains) >= period else mean(gains) if gains else 0.0
    losses_avg = mean(losses[-period:]) if len(losses) >= period else mean(losses) if losses else 0.0
    if losses_avg == 0:
        return 100.0 if gains_avg > 0 else 50.0
    rs = gains_avg / losses_avg
    rsi = 100 - (100 / (1 + rs))
    return float(rsi)


def analyze_chart(ticker: str, lookback_minutes: int = 240) -> dict:
    """Analyze recent price series and return a small analysis dict.

    Analysis is price-only (no volume) so it's conservative but avoids extra
    Finnhub calls. Returned dict contains: score (0-100), summary_th, metrics.
    """
    series = load_price_series(ticker, minutes=lookback_minutes)
    prices = [p for _, p in series]
    if not prices:
        return {"score": 0, "summary_th": "ไม่มีข้อมูลราคาเพียงพอสำหรับการวิเคราะห์", "metrics": {}}

    latest = prices[-1]
    n = len(prices)

    def ma(window: int) -> float | None:
        if n >= window:
            return mean(prices[-window:])
        return None

    ma50 = ma(50)
    ma200 = ma(200)
    recent_return = (latest - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0.0
    rsi = _rsi_from_prices(prices)

    score = 50.0
    # Trend bonus
    if ma50 and latest > ma50:
        score += 15
    if ma200 and ma50 and ma50 > ma200:
        score += 10
    # Momentum / recent return
    if recent_return > 1.0:
        score += min(10, recent_return)
    # RSI balance: avoid extremes
    if 50 < rsi < 70:
        score += 10
    elif rsi <= 30:
        score -= 10
    elif rsi >= 70:
        score -= 5

    # Normalize
    score = max(0, min(100, int(score)))

    parts = []
    if ma50:
        parts.append(f"ราคา {'เหนือ' if latest>ma50 else 'ใต้'} MA50 ({ma50:.2f})")
    if ma200:
        parts.append(f"MA50 {'>' if ma50 and ma50>ma200 else '<='} MA200")
    parts.append(f"ผลตอบแทนย้อนหลัง {lookback_minutes}m: {recent_return:+.2f}%")
    parts.append(f"RSI14: {rsi:.1f}")

    summary = " · ".join(parts)

    return {
        "score": score,
        "summary_th": summary,
        "metrics": {"latest": latest, "ma50": ma50, "ma200": ma200, "rsi": rsi, "recent_return_pct": recent_return, "points": n},
    }
