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


def _dynamic_levels_from_prices(prices: List[float]) -> dict:
    """Derive adaptive buy/sell/stop/target levels from recent minute prices.

    Uses only cached prices (no extra API calls). This is intentionally simple
    and deterministic: recent support/resistance + short-term volatility.
    """
    if len(prices) < 20:
        return {}

    latest = prices[-1]
    lookback = prices[-60:] if len(prices) >= 60 else prices[:]
    support = min(lookback)
    resistance = max(lookback)

    diffs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    if diffs:
        vol = mean(diffs[-20:]) if len(diffs) >= 20 else mean(diffs)
    else:
        vol = max(latest * 0.002, 0.05)

    ma20 = mean(prices[-20:]) if len(prices) >= 20 else latest
    ma50 = mean(prices[-50:]) if len(prices) >= 50 else ma20
    trend_up = ma20 >= ma50

    buy_offset = 1.2 * vol if trend_up else 0.8 * vol
    sell_offset = 1.8 * vol if trend_up else 1.2 * vol

    buy_price = max(support + 0.2 * vol, latest - buy_offset)
    sell_price = min(resistance - 0.2 * vol, latest + sell_offset)

    if sell_price <= buy_price:
        sell_price = buy_price + max(vol, latest * 0.003)

    stop_loss = max(0.01, buy_price - 1.4 * vol)
    take_profit = sell_price + 0.8 * vol

    return {
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "volatility": round(vol, 4),
        "buy_price": round(buy_price, 2),
        "sell_price": round(sell_price, 2),
        "stop_loss": round(stop_loss, 2),
        "take_profit": round(take_profit, 2),
    }


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
    dynamic_levels = _dynamic_levels_from_prices(prices)

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
        "metrics": {
            "latest": latest,
            "ma50": ma50,
            "ma200": ma200,
            "rsi": rsi,
            "recent_return_pct": recent_return,
            "points": n,
            "dynamic_levels": dynamic_levels,
        },
    }


def recommend_trade_from_analysis(analysis: dict, current_price: float | None = None) -> dict:
    """Return simple actionable recommendation (buy/sell prices + reason) from analysis.

    Output keys: `buy_now`, `buy_on_pullback`, `sell_now`, `stop_loss`, `reason` (text).
    This is intentionally conservative and deterministic — suitable for short Discord
    guidance messages.
    """
    out = {"buy_now": None, "buy_on_pullback": None, "sell_now": None, "stop_loss": None, "reason": ""}
    if not analysis:
        return out

    metrics = analysis.get("metrics", {})
    dyn = metrics.get("dynamic_levels") or {}
    latest = float(metrics.get("latest") or 0.0)
    ma50 = metrics.get("ma50")
    ma200 = metrics.get("ma200")
    rsi = metrics.get("rsi")

    # Prefer dynamic levels if available
    support = float(dyn.get("support") or 0.0)
    resistance = float(dyn.get("resistance") or 0.0)
    buy_lvl = float(dyn.get("buy_price") or 0.0)
    sell_lvl = float(dyn.get("sell_price") or 0.0)
    stop = float(dyn.get("stop_loss") or 0.0)

    # Simple, beginner-friendly reason phrases (Thai)
    reasons: list[str] = []
    if ma50 and ma200:
        if ma50 > ma200:
            reasons.append("แนวโน้ม: ขาขึ้น — เทรนด์โดยรวมเป็นบวก")
        else:
            reasons.append("แนวโน้ม: ไม่ชัดเจน/ขาลง — ระมัดระวังการเข้าซื้อ")
    if rsi is not None:
        if rsi < 30:
            reasons.append("RSI: ต่ำ — ราคาถูกขายมาก อาจฟื้นได้")
        elif rsi > 70:
            reasons.append("RSI: สูง — ราคาถูกซื้อมาก เสี่ยงปรับฐาน")

    # Price position vs recent range
    if resistance and latest >= resistance:
        reasons.append("ราคาทะลุแนวต้าน — แรงซื้อมี แต่ควรรอการยืนยัน")
    elif support and latest <= support:
        reasons.append("ราคาอยู่ใกล้แนวรับ — โอกาสซื้อด้วย stop ใกล้ๆ")
    else:
        # inside recent range: simple proximity messages
        if resistance and latest > (support + (resistance - support) * 0.6):
            reasons.append("ราคาใกล้โซนบนของช่วง — อาจเกิดการพักตัว")
        elif resistance and latest < (support + (resistance - support) * 0.4):
            reasons.append("ราคาใกล้โซนล่างของช่วง — พิจารณาซื้อเมื่อยืนยันแนวรับ")

    # Compose suggested prices
    if buy_lvl and latest <= buy_lvl * 1.01:
        out["buy_now"] = round(latest if current_price and current_price <= buy_lvl * 1.01 else buy_lvl, 2)
    elif buy_lvl:
        out["buy_on_pullback"] = round(buy_lvl, 2)

    if sell_lvl:
        out["sell_now"] = round(sell_lvl, 2)

    if stop:
        out["stop_loss"] = round(stop, 2)

    out["reason"] = "; ".join(reasons) if reasons else "No clear technical reason"
    return out


# ---------------- Soft static-level suggestion helpers -----------------
SUGGEST_DIR = Path(__file__).parent.parent / "state" / "level_suggestions"
SUGGEST_DIR.mkdir(parents=True, exist_ok=True)


def _candles_from_series(series: List[Tuple[datetime, float]], tf_minutes: int = 5) -> list:
    """Aggregate minute price series into simple candles (close only) by tf_minutes.

    Returns list of {'ts': iso, 'close': float} ordered ascending.
    """
    if not series:
        return []
    out = []
    bucket = None
    last_close = None
    cur_ts = None
    for ts, price in series:
        epoch_min = int(ts.timestamp() // 60)
        key = epoch_min // tf_minutes
        if bucket is None:
            bucket = key
            last_close = price
            cur_ts = ts
        elif key == bucket:
            last_close = price
            cur_ts = ts
        else:
            out.append({"ts": cur_ts.isoformat(), "close": float(last_close)})
            bucket = key
            last_close = price
            cur_ts = ts
    if last_close is not None:
        out.append({"ts": cur_ts.isoformat(), "close": float(last_close)})
    return out


def _check_persistence(series: List[Tuple[datetime, float]], level_price: float, direction: str, tf_minutes: int = 5, required_closes: int = 2) -> bool:
    """Return True if last `required_closes` candles (tf_minutes) closed beyond level_price in `direction`.

    direction: 'above' or 'below'
    """
    candles = _candles_from_series(series, tf_minutes=tf_minutes)
    if len(candles) < required_closes:
        return False
    recent = candles[-required_closes:]
    if direction == 'above':
        return all(c['close'] >= level_price for c in recent)
    else:
        return all(c['close'] <= level_price for c in recent)


def _last_suggestion_timestamp(ticker: str, level_label: str) -> float:
    prefix = f"{ticker.upper()}_{level_label.replace(' ', '_')}"
    best = 0.0
    for p in SUGGEST_DIR.glob(f"{prefix}*.json"):
        try:
            ts = float(p.stat().st_mtime)
            if ts > best:
                best = ts
        except Exception:
            continue
    return best


def suggest_static_update_if_needed(ticker: str, level: dict, analysis: dict, *,
                                    volume_multiplier: float = 1.5,
                                    persistence_closes_5m: int = 2,
                                    persistence_closes_15m: int = 1,
                                    require_trend_confirm: bool = True,
                                    cooldown_hours: int = 24) -> dict | None:
    """Evaluate rules and write a soft suggestion JSON if criteria met.

    Returns suggestion dict if created, else None.
    Note: analysis is price-only in current code (may not include volume). If volume
    is required but unavailable, volume check is skipped and noted in suggestion.
    """
    try:
        series = load_price_series(ticker, minutes=240)
        latest = float(analysis.get('metrics', {}).get('latest') or 0.0)
        ma50 = analysis.get('metrics', {}).get('ma50')
        ma200 = analysis.get('metrics', {}).get('ma200')
        level_price = float(level.get('price') or 0.0)
        direction = level.get('direction')

        # cooldown
        last_ts = _last_suggestion_timestamp(ticker, level.get('label', ''))
        if time.time() - last_ts < cooldown_hours * 3600:
            return None

        # trend confirm
        if require_trend_confirm and ma50 is not None and ma200 is not None:
            if direction == 'above' and not (ma50 > ma200):
                return None
            if direction == 'below' and not (ma50 < ma200):
                return None

        # persistence: check 5m/15m closes
        ok_5m = _check_persistence(series, level_price, direction, tf_minutes=5, required_closes=persistence_closes_5m)
        ok_15m = _check_persistence(series, level_price, direction, tf_minutes=15, required_closes=persistence_closes_15m)
        if not (ok_5m or ok_15m):
            return None

        # volume check: current analysis has no volume; mark as unknown
        volume_checked = False
        volume_ok = None

        reason_parts = []
        reason_parts.append(f"Price {'closed above' if direction=='above' else 'closed below'} level {level_price}")
        if require_trend_confirm:
            reason_parts.append('Trend confirmed by MA50/MA200')
        if ok_5m:
            reason_parts.append(f'{persistence_closes_5m}x 5m close')
        if ok_15m:
            reason_parts.append(f'{persistence_closes_15m}x 15m close')
        if not volume_checked:
            reason_parts.append('Volume: not checked (no volume data)')

        suggestion = {
            'ticker': ticker.upper(),
            'level': level,
            'latest': latest,
            'metrics': analysis.get('metrics', {}),
            'reason': '; '.join(reason_parts),
            'created_at': datetime.now(timezone.utc).isoformat(),
            'params': {
                'volume_multiplier': volume_multiplier,
                'persistence_5m': persistence_closes_5m,
                'persistence_15m': persistence_closes_15m,
                'require_trend_confirm': require_trend_confirm,
                'cooldown_hours': cooldown_hours,
            }
        }

        # Write suggestion file
        stamp = int(time.time())
        safe_label = level.get('label', '').replace(' ', '_').replace('/', '_')[:80]
        fname = SUGGEST_DIR / f"{ticker.upper()}_{safe_label}_{stamp}.json"
        try:
            fname.write_text(json.dumps(suggestion, ensure_ascii=False, indent=2))
        except Exception:
            pass

        return suggestion
    except Exception:
        return None
