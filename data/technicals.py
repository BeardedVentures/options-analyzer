"""
data/technicals.py — Technical indicator calculations.

All indicators computed locally with pandas/numpy on yfinance OHLCV data.
No paid services required.
"""

import logging
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

import numpy as np
import pandas as pd

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# MOVING AVERAGES
# ─────────────────────────────────────────────

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


# ─────────────────────────────────────────────
# RSI
# ─────────────────────────────────────────────

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ─────────────────────────────────────────────
# MACD
# ─────────────────────────────────────────────

def _macd(close: pd.Series, fast: int = 12, slow: int = 26,
          signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


# ─────────────────────────────────────────────
# STOCHASTIC
# ─────────────────────────────────────────────

def _stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
                k_period: int = 14, d_period: int = 3) -> Tuple[pd.Series, pd.Series]:
    lowest_low = low.rolling(k_period).min()
    highest_high = high.rolling(k_period).max()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    k = 100 * (close - lowest_low) / denom
    k_smooth = k.rolling(d_period).mean()
    d = k_smooth.rolling(d_period).mean()
    return k_smooth, d


# ─────────────────────────────────────────────
# BOLLINGER BANDS
# ─────────────────────────────────────────────

def _bollinger_bands(close: pd.Series, period: int = 20,
                     std_dev: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = _sma(close, period)
    std = close.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


# ─────────────────────────────────────────────
# ATR
# ─────────────────────────────────────────────

def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    h_l = high - low
    h_pc = (high - close.shift(1)).abs()
    l_pc = (low - close.shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()


# ─────────────────────────────────────────────
# REALIZED VOLATILITY
# ─────────────────────────────────────────────

def _historical_vol(close: pd.Series, period: int) -> float:
    """Annualized historical realized volatility over [period] days."""
    log_returns = np.log(close / close.shift(1)).dropna()
    if len(log_returns) < period:
        return 0.0
    rv = log_returns.tail(period).std() * np.sqrt(252)
    return float(rv)


# ─────────────────────────────────────────────
# IV RANK — real percentile from stored IV history
# ─────────────────────────────────────────────

def _iv_rank_hv_approx(close: pd.Series, current_iv: float) -> float:
    """
    Bootstrapping fallback: approximate IV rank using the rolling HV distribution.
    Used only until IV_HISTORY_MIN_SAMPLES real IV points are stored per ticker.

    M1 fix: IV structurally sits above realized vol (that gap IS the volatility risk premium),
    so ranking current IV directly against the raw HV distribution returned ~100 almost every
    time and made the MIN_IV_RANK gate meaningless. We scale the HV distribution up by
    config.IV_HV_INFLATOR (typical IV/HV ratio) so a normal IV lands mid-distribution.
    This remains an approximation — the real percentile only kicks in once IV history accumulates.
    """
    if current_iv <= 0:
        return 0.0
    log_returns = np.log(close / close.shift(1)).dropna()
    if len(log_returns) < 60:
        return 50.0
    rolling_hv = []
    for i in range(30, len(log_returns)):
        window = log_returns.iloc[i - 30:i]
        hv = float(window.std() * np.sqrt(252))
        rolling_hv.append(hv)
    if not rolling_hv:
        return 50.0
    inflator = getattr(config, "IV_HV_INFLATOR", 1.2)
    arr = np.array(rolling_hv) * inflator
    return round(float(np.mean(arr <= current_iv) * 100), 1)


def estimate_atm_iv(options: List[Dict], current_price: float) -> float:
    """Median IV of the near-ATM (within 3%) contracts, falling back to the whole chain.

    Shared by every strategy path so they all rank IV off the same number: calculate_all()
    defaults current_iv to 0.0, which silently yields iv_rank 0 and fails any iv_rank_min
    gate, so a caller that skips this cannot surface a trade at all.
    """
    ivs = []
    for opt in options or []:
        iv = opt.get("iv")
        strike = opt.get("strike", 0)
        if iv and current_price > 0:
            if abs(strike - current_price) / current_price <= 0.03:
                ivs.append(float(iv))
    if not ivs:
        ivs = [float(opt.get("iv")) for opt in (options or []) if opt.get("iv")]
    if not ivs:
        return 0.0
    ivs.sort()
    return ivs[len(ivs) // 2]


def calculate_iv_rank(ticker: str, current_iv: float, close: pd.Series) -> dict:
    """
    Calculate IV Rank from stored historical IV samples for this ticker.

    Self-bootstrapping: each call appends today's IV to the history file.
    Once IV_HISTORY_MIN_SAMPLES exist, returns a real percentile.
    Until then, returns the HV-based approximation with iv_rank_method='APPROX'.

    Returns:
        iv_rank:        float 0-100
        iv_rank_method: 'HISTORY' (real) | 'APPROX' (bootstrapping)
        iv_history_count: number of IV samples on record
    """
    import json as _json
    from datetime import date as _date

    if current_iv <= 0:
        return {"iv_rank": 0.0, "iv_rank_method": "APPROX", "iv_history_count": 0}

    base_dir = Path(__file__).resolve().parent.parent
    history_dir = base_dir / config.IV_HISTORY_DIR
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / f"{ticker.upper()}.json"

    # Load existing history
    samples: list = []
    if history_file.exists():
        try:
            samples = _json.loads(history_file.read_text(encoding="utf-8"))
            if not isinstance(samples, list):
                samples = []
        except Exception:
            samples = []

    # Append today's observation (deduplicate by date)
    today_str = str(_date.today())
    existing_dates = {s.get("date") for s in samples if isinstance(s, dict)}
    if today_str not in existing_dates:
        samples.append({"date": today_str, "iv": round(current_iv, 6)})

    # Roll to max window
    max_samples = getattr(config, "IV_HISTORY_MAX_SAMPLES", 504)
    if len(samples) > max_samples:
        samples = samples[-max_samples:]

    # Persist updated history
    try:
        history_file.write_text(_json.dumps(samples, indent=None), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[iv_rank] Could not write IV history for {ticker}: {exc}")

    min_samples = getattr(config, "IV_HISTORY_MIN_SAMPLES", 30)
    if len(samples) < min_samples:
        approx = _iv_rank_hv_approx(close, current_iv)
        logger.debug(
            f"[iv_rank] {ticker}: bootstrapping ({len(samples)}/{min_samples} samples) "
            f"— using HV approx {approx:.1f}"
        )
        return {
            "iv_rank": approx,
            "iv_rank_method": "APPROX",
            "iv_history_count": len(samples),
        }

    # Compute real IV Rank: percentile of current_iv vs stored IV history
    iv_values = [s["iv"] for s in samples if isinstance(s, dict) and "iv" in s]
    if not iv_values:
        return {"iv_rank": 50.0, "iv_rank_method": "APPROX", "iv_history_count": 0}

    arr = np.array(iv_values)
    iv_rank = round(float(np.mean(arr <= current_iv) * 100), 1)
    logger.debug(
        f"[iv_rank] {ticker}: REAL percentile {iv_rank:.1f} "
        f"(current={current_iv:.4f}, history={len(iv_values)} samples)"
    )
    return {
        "iv_rank": iv_rank,
        "iv_rank_method": "HISTORY",
        "iv_history_count": len(iv_values),
    }



# ─────────────────────────────────────────────
# SUPPORT / RESISTANCE
# ─────────────────────────────────────────────

def _find_support_resistance(close: pd.Series,
                             high: pd.Series,
                             low: pd.Series,
                             lookback: int = 60) -> Dict:
    """
    Identify key support and resistance levels from recent swing highs/lows.
    Uses a simple local extrema approach over the lookback window.
    """
    if len(close) < lookback:
        lookback = len(close)

    recent_close = close.tail(lookback)
    recent_high = high.tail(lookback)
    recent_low = low.tail(lookback)

    current_price = float(close.iloc[-1])

    # Find swing lows (support)
    swing_lows = []
    for i in range(2, len(recent_low) - 2):
        val = recent_low.iloc[i]
        if (val <= recent_low.iloc[i - 1] and val <= recent_low.iloc[i - 2]
                and val <= recent_low.iloc[i + 1] and val <= recent_low.iloc[i + 2]):
            swing_lows.append(float(val))

    # Find swing highs (resistance)
    swing_highs = []
    for i in range(2, len(recent_high) - 2):
        val = recent_high.iloc[i]
        if (val >= recent_high.iloc[i - 1] and val >= recent_high.iloc[i - 2]
                and val >= recent_high.iloc[i + 1] and val >= recent_high.iloc[i + 2]):
            swing_highs.append(float(val))

    # Nearest support below current price
    supports = sorted([s for s in swing_lows if s < current_price], reverse=True)
    resistances = sorted([r for r in swing_highs if r > current_price])

    # Add round-number levels near current price
    round_levels = [round(current_price / step) * step
                    for step in [5, 10, 25, 50, 100]
                    if step < current_price * 0.2]

    for lvl in round_levels:
        if lvl < current_price and lvl not in supports:
            supports.append(lvl)
        elif lvl > current_price and lvl not in resistances:
            resistances.append(lvl)

    supports = sorted(supports, reverse=True)[:3]
    resistances = sorted(resistances)[:3]

    # 52-week high/low
    yr_high = float(high.tail(252).max()) if len(high) >= 252 else float(high.max())
    yr_low = float(low.tail(252).min()) if len(low) >= 252 else float(low.min())

    return {
        "supports": supports,
        "resistances": resistances,
        "nearest_support": supports[0] if supports else None,
        "nearest_resistance": resistances[0] if resistances else None,
        "52w_high": round(yr_high, 2),
        "52w_low": round(yr_low, 2),
    }


# ─────────────────────────────────────────────
# TREND CLASSIFICATION
# ─────────────────────────────────────────────

def _classify_trend(price: float, sma20: float, sma50: float, sma200: float,
                    rsi: float) -> str:
    """Return trend classification string."""
    above_20 = price > sma20 if sma20 else False
    above_50 = price > sma50 if sma50 else False
    above_200 = price > sma200 if sma200 else False

    score = sum([above_20, above_50, above_200])

    if score == 3 and rsi > 55:
        return "STRONG_UP"
    elif score >= 2:
        return "UP"
    elif score == 1:
        return "NEUTRAL"
    elif score == 0 and rsi < 45:
        return "STRONG_DOWN"
    else:
        return "DOWN"


# ─────────────────────────────────────────────
# COMPOSITE TECHNICAL SCORE
# ─────────────────────────────────────────────

def _composite_score(price: float,
                     sma20: Optional[float], sma50: Optional[float],
                     sma200: Optional[float],
                     rsi: float,
                     macd_hist: float,
                     bb_lower: float,
                     iv_rank: float,
                     nearest_support: Optional[float],
                     short_strike: Optional[float] = None) -> Dict:
    """
    Composite technical score (0-100).
    Returns score and breakdown.
    """
    breakdown = {}
    total = 0

    # Above 50 SMA: +10
    if sma50 and price > sma50:
        breakdown["above_sma50"] = 10
        total += 10
    else:
        breakdown["above_sma50"] = 0

    # RSI between 40-65 (healthy zone for selling): +15
    if 40 <= rsi <= 65:
        breakdown["rsi_healthy"] = 15
        total += 15
    else:
        breakdown["rsi_healthy"] = 0

    # MACD positive: +10
    if macd_hist > 0:
        breakdown["macd_positive"] = 10
        total += 10
    else:
        breakdown["macd_positive"] = 0

    # Price above lower Bollinger Band: +15
    if price > bb_lower:
        breakdown["above_bb_lower"] = 15
        total += 15
    else:
        breakdown["above_bb_lower"] = 0

    # IV Rank > 50: +20
    if iv_rank > 50:
        breakdown["iv_rank_high"] = 20
        total += 20
    elif iv_rank >= 45:
        breakdown["iv_rank_high"] = 10
        total += 10
    else:
        breakdown["iv_rank_high"] = 0

    # Not overbought (RSI < 72): +10
    if rsi < 72:
        breakdown["not_overbought"] = 10
        total += 10
    else:
        breakdown["not_overbought"] = 0

    # Short strike above key support: +20
    if short_strike is not None and nearest_support is not None:
        if short_strike > nearest_support:
            breakdown["strike_above_support"] = 20
            total += 20
        else:
            breakdown["strike_above_support"] = 0
    else:
        breakdown["strike_above_support"] = 15  # neutral — no data
        total += 15

    return {"total": total, "breakdown": breakdown}


# ─────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────

def calculate_all(price_data: pd.DataFrame,
                  ticker: str,
                  current_iv: float = 0.0,
                  short_strike: Optional[float] = None) -> Dict[str, Any]:
    """
    Calculate all technical indicators for a ticker.

    Args:
        price_data: DataFrame with OHLCV columns
        ticker: ticker symbol (for logging)
        current_iv: current ATM implied volatility (from options chain)
        short_strike: proposed short put strike (for support analysis)

    Returns:
        Dict with all indicator values and composite score
    """
    try:
        close = price_data["Close"].dropna()
        high = price_data["High"].dropna()
        low = price_data["Low"].dropna()
        volume = price_data["Volume"].dropna()

        if len(close) < 30:
            logger.warning(f"[technicals] Insufficient data for {ticker}: {len(close)} bars")
            return _empty_result(ticker)

        price = float(close.iloc[-1])

        # ── Moving Averages ──
        sma20_s = _sma(close, 20)
        sma50_s = _sma(close, 50)
        sma200_s = _sma(close, 200)
        ema9_s = _ema(close, 9)
        ema21_s = _ema(close, 21)

        sma20 = float(sma20_s.iloc[-1]) if not pd.isna(sma20_s.iloc[-1]) else None
        sma50 = float(sma50_s.iloc[-1]) if not pd.isna(sma50_s.iloc[-1]) else None
        sma200 = float(sma200_s.iloc[-1]) if not pd.isna(sma200_s.iloc[-1]) else None
        ema9 = float(ema9_s.iloc[-1]) if not pd.isna(ema9_s.iloc[-1]) else None
        ema21 = float(ema21_s.iloc[-1]) if not pd.isna(ema21_s.iloc[-1]) else None

        # % distance from each MA
        def pct_from(ma_val):
            if ma_val and ma_val > 0:
                return round((price - ma_val) / ma_val * 100, 2)
            return None

        # ── RSI ──
        rsi_s = _rsi(close, 14)
        rsi = float(rsi_s.iloc[-1]) if not pd.isna(rsi_s.iloc[-1]) else 50.0

        # ── MACD ──
        macd_line_s, signal_line_s, macd_hist_s = _macd(close)
        macd_line = float(macd_line_s.iloc[-1]) if not pd.isna(macd_line_s.iloc[-1]) else 0.0
        signal_line = float(signal_line_s.iloc[-1]) if not pd.isna(signal_line_s.iloc[-1]) else 0.0
        macd_hist = float(macd_hist_s.iloc[-1]) if not pd.isna(macd_hist_s.iloc[-1]) else 0.0
        macd_crossover = "bullish" if macd_line > signal_line else "bearish"

        # ── Stochastic ──
        stoch_k_s, stoch_d_s = _stochastic(high, low, close)
        stoch_k = float(stoch_k_s.iloc[-1]) if not pd.isna(stoch_k_s.iloc[-1]) else 50.0
        stoch_d = float(stoch_d_s.iloc[-1]) if not pd.isna(stoch_d_s.iloc[-1]) else 50.0

        # ── Bollinger Bands ──
        bb_upper_s, bb_mid_s, bb_lower_s = _bollinger_bands(close)
        bb_upper = float(bb_upper_s.iloc[-1]) if not pd.isna(bb_upper_s.iloc[-1]) else None
        bb_mid = float(bb_mid_s.iloc[-1]) if not pd.isna(bb_mid_s.iloc[-1]) else None
        bb_lower = float(bb_lower_s.iloc[-1]) if not pd.isna(bb_lower_s.iloc[-1]) else None

        # Band width as % of mid (squeeze detector)
        bb_width = None
        if bb_upper and bb_lower and bb_mid and bb_mid > 0:
            bb_width = round((bb_upper - bb_lower) / bb_mid * 100, 2)

        # Price position in band: 0% = lower, 100% = upper
        bb_position = None
        if bb_upper and bb_lower and (bb_upper - bb_lower) > 0:
            bb_position = round((price - bb_lower) / (bb_upper - bb_lower) * 100, 1)

        # ── ATR ──
        atr_s = _atr(high, low, close)
        atr = float(atr_s.iloc[-1]) if not pd.isna(atr_s.iloc[-1]) else None

        # ── Historical Realized Volatility ──
        rv_20 = _historical_vol(close, 20)
        rv_30 = _historical_vol(close, 30)

        # ── IV Rank — real percentile from stored IV history (self-bootstrapping) ──
        vrp_window = getattr(config, "VRP_HV_WINDOW", 35)
        iv_rank_result = calculate_iv_rank(ticker, current_iv, close) if current_iv > 0 else {
            "iv_rank": 0.0, "iv_rank_method": "APPROX", "iv_history_count": 0
        }
        iv_rank = iv_rank_result["iv_rank"]
        iv_rank_method = iv_rank_result["iv_rank_method"]

        # ── VRP — uses window matched to PREFERRED_DTE_TARGET ──
        rv_vrp = _historical_vol(close, vrp_window)
        vrp = round(current_iv - rv_vrp, 4) if current_iv > 0 and rv_vrp > 0 else 0.0
        vrp_pct = round(vrp * 100, 2)

        # ── Volume analysis ──
        vol_20d_avg = float(volume.tail(20).mean()) if len(volume) >= 20 else None
        vol_current = float(volume.iloc[-1])
        vol_ratio = round(vol_current / vol_20d_avg, 2) if vol_20d_avg and vol_20d_avg > 0 else None

        # ── Day change ──
        day_change_pct = None
        if len(close) >= 2:
            day_change_pct = round((price - float(close.iloc[-2])) / float(close.iloc[-2]) * 100, 2)

        # ── Support / Resistance ──
        sr = _find_support_resistance(close, high, low)
        nearest_support = sr["nearest_support"]

        # ── Trend Classification ──
        trend = _classify_trend(price, sma20, sma50, sma200, rsi)

        # ── Composite Score ──
        score_result = _composite_score(
            price=price,
            sma20=sma20, sma50=sma50, sma200=sma200,
            rsi=rsi,
            macd_hist=macd_hist,
            bb_lower=bb_lower or (price * 0.95),
            iv_rank=iv_rank,
            nearest_support=nearest_support,
            short_strike=short_strike,
        )

        # ── Signal flags ──
        signals = {
            "overbought": rsi > 70,
            "oversold": rsi < 30,
            "above_200sma": price > sma200 if sma200 else None,
            "above_50sma": price > sma50 if sma50 else None,
            "above_20sma": price > sma20 if sma20 else None,
            "macd_bullish": macd_line > signal_line,
            "bb_squeeze": bb_width is not None and bb_width < 3.0,
            "approaching_support": (nearest_support is not None
                                    and nearest_support > 0
                                    and (price - nearest_support) / price < 0.02),
        }

        return {
            "ticker": ticker,
            "price": round(price, 2),
            # MAs
            "sma20": round(sma20, 2) if sma20 else None,
            "sma50": round(sma50, 2) if sma50 else None,
            "sma200": round(sma200, 2) if sma200 else None,
            "ema9": round(ema9, 2) if ema9 else None,
            "ema21": round(ema21, 2) if ema21 else None,
            "pct_from_sma20": pct_from(sma20),
            "pct_from_sma50": pct_from(sma50),
            "pct_from_sma200": pct_from(sma200),
            # Momentum
            "rsi": round(rsi, 1),
            "macd_line": round(macd_line, 4),
            "macd_signal": round(signal_line, 4),
            "macd_hist": round(macd_hist, 4),
            "macd_crossover": macd_crossover,
            "stoch_k": round(stoch_k, 1),
            "stoch_d": round(stoch_d, 1),
            # Volatility
            "bb_upper": round(bb_upper, 2) if bb_upper else None,
            "bb_mid": round(bb_mid, 2) if bb_mid else None,
            "bb_lower": round(bb_lower, 2) if bb_lower else None,
            "bb_width": bb_width,
            "bb_position": bb_position,
            "atr": round(atr, 2) if atr else None,
            "rv_20d": round(rv_20 * 100, 2),
            "rv_30d": round(rv_30 * 100, 2),
            "current_iv": round(current_iv * 100, 2),
            "iv_rank": iv_rank,
            "iv_rank_method": iv_rank_method,
            "vrp": vrp_pct,         # percentage points: IV - RV (DTE-matched window)
            "vrp_raw": vrp,         # decimal
            # Volume
            "volume": int(vol_current),
            "vol_20d_avg": int(vol_20d_avg) if vol_20d_avg else None,
            "vol_ratio": vol_ratio,
            "day_change_pct": day_change_pct,
            # Support / Resistance
            "supports": sr["supports"],
            "resistances": sr["resistances"],
            "nearest_support": sr["nearest_support"],
            "nearest_resistance": sr["nearest_resistance"],
            "52w_high": sr["52w_high"],
            "52w_low": sr["52w_low"],
            # Trend
            "trend": trend,
            "signals": signals,
            # Composite score
            "composite_score": score_result["total"],
            "score_breakdown": score_result["breakdown"],
        }

    except Exception as e:
        logger.error(f"[technicals] Error calculating indicators for {ticker}: {e}", exc_info=True)
        return _empty_result(ticker)


def _empty_result(ticker: str) -> Dict:
    return {
        "ticker": ticker,
        "price": 0,
        "sma20": None, "sma50": None, "sma200": None,
        "ema9": None, "ema21": None,
        "rsi": 50.0,
        "macd_hist": 0, "macd_crossover": "neutral",
        "stoch_k": 50, "stoch_d": 50,
        "bb_upper": None, "bb_mid": None, "bb_lower": None,
        "bb_width": None, "bb_position": None,
        "atr": None,
        "rv_20d": 0, "rv_30d": 0,
        "current_iv": 0, "iv_rank": 0, "iv_rank_method": "APPROX",
        "vrp": 0, "vrp_raw": 0,
        "volume": 0, "vol_20d_avg": None, "vol_ratio": None,
        "day_change_pct": None,
        "supports": [], "resistances": [],
        "nearest_support": None, "nearest_resistance": None,
        "52w_high": 0, "52w_low": 0,
        "trend": "NEUTRAL",
        "signals": {},
        "composite_score": 0,
        "score_breakdown": {},
    }
