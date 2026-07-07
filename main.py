
import os
import argparse
import json
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytz

import config
from data import fetcher, technicals, fundamentals, news
from analysis import edge_calculator, strike_validator, synthesizer
from output import renderer, emailer


# VEGA: JARVIS integration (non-blocking — scan completes even if tower unreachable)
try:
    from vega_ingest import post_to_jarvis
    VEGA_INGEST_ENABLED = True
except ImportError:
    VEGA_INGEST_ENABLED = False

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


# ─────────────── MAIN ENTRY BLOCK ───────────────
# (Moved to end of file to ensure all functions are defined)




def setup_logging() -> Path:
    log_dir = BASE_DIR / config.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run.log"

    handlers = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )
    return log_dir


def now_et() -> datetime:
    return datetime.now(pytz.timezone("US/Eastern"))


def check_session_window(session_type: str, ts: datetime) -> None:
    if session_type == "morning":
        target = time(9, 45)
        if ts.time() < target:
            logger.warning("Morning scan started before 9:45 AM ET.")
    elif session_type == "close":
        target = time(15, 0)
        if ts.time() < target:
            logger.warning("Close scan started before 3:00 PM ET.")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def append_scan_log(log_dir: Path, entry: Dict) -> None:
    path = log_dir / "scan_log.json"
    data = load_json(path, [])
    if not isinstance(data, list):
        data = []
    data.append(entry)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def build_market_context() -> Dict:
    vix = fetcher.get_vix()
    spy_data = fetcher.get_price_data("SPY", period="5d")
    spy = {"price": None, "prev_close": None, "day_change_pct": None}
    if spy_data is not None and not spy_data.empty:
        close = spy_data["Close"].dropna()
        if len(close) >= 2:
            price = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            spy["price"] = round(price, 2)
            spy["prev_close"] = round(prev, 2)
            spy["day_change_pct"] = round((price - prev) / prev * 100, 2) if prev else 0

    macro_news = fetcher.get_macro_news()
    macro_events = [a.get("title") for a in macro_news if a.get("title")][:5]

    bias = "NEUTRAL"
    if spy["day_change_pct"] is not None:
        if spy["day_change_pct"] > 0.5 and vix.get("current", 0) < 20:
            bias = "RISK-ON"
        elif spy["day_change_pct"] < -0.5 or vix.get("current", 0) > 25:
            bias = "RISK-OFF"

    summary = (
        f"VIX {vix.get('current', 0)} ({vix.get('label', 'UNKNOWN')}); "
        f"SPY {spy.get('day_change_pct', 0)}% on the day. Bias: {bias}."
    )

    return {
        "vix": vix,
        "spy": spy,
        "macro_events": macro_events,
        "bias": bias,
        "summary": summary,
    }


def estimate_current_iv(options: List[Dict], current_price: float) -> float:
    ivs = []
    for opt in options:
        iv = opt.get("iv")
        strike = opt.get("strike", 0)
        if iv and current_price > 0:
            if abs(strike - current_price) / current_price <= 0.03:
                ivs.append(float(iv))
    if not ivs:
        ivs = [float(opt.get("iv")) for opt in options if opt.get("iv")]
    if not ivs:
        return 0.0
    ivs.sort()
    return ivs[len(ivs) // 2]


def select_long_put_strike(options: List[Dict], short_strike: float) -> Optional[float]:
    strikes = sorted({opt.get("strike") for opt in options if opt.get("strike") and opt.get("strike") < short_strike})
    if not strikes:
        return None
    candidates = [s for s in strikes if 0 < (short_strike - s) <= config.MAX_SPREAD_WIDTH]
    if not candidates:
        return None
    return max(candidates)


def select_long_put_contract(options: List[Dict], short_strike: float, short_expiration: Optional[str] = None) -> Optional[Dict]:
    candidates = []
    for opt in options:
        if opt.get("type") != "put":
            continue
        if short_expiration and opt.get("expiration") != short_expiration:
            continue
        strike = opt.get("strike", 0)
        if not strike or strike >= short_strike:
            continue
        if 0 < (short_strike - strike) <= config.MAX_SPREAD_WIDTH:
            candidates.append(opt)

    if not candidates:
        return None

    candidates.sort(key=lambda opt: opt.get("strike", 0), reverse=True)
    return candidates[0]


def select_bull_put_pair(
    options: List[Dict],
    current_price: float,
    ticker: str,
    diagnostics: Optional[Dict] = None,
) -> Optional[Tuple[Dict, Dict, Dict]]:
    """Pick a short/long put pair that forms a valid same-expiration credit spread."""
    diag_counts: Dict[str, int] = {}

    def _bump(reason: str) -> None:
        diag_counts[reason] = diag_counts.get(reason, 0) + 1

    def _quote_is_tradeable(opt: Dict) -> bool:
        bid = float(opt.get("bid", 0) or 0)
        ask = float(opt.get("ask", 0) or 0)
        mid = float(opt.get("mid", 0) or 0)
        if bid <= 0 or ask <= 0 or mid <= 0:
            return False
        if ask < bid:
            return False
        spread_pct = (ask - bid) / mid if mid else 1.0
        return spread_pct <= config.MAX_QUOTE_SPREAD_PCT

    short_candidates: List[Tuple[float, Dict]] = []
    target_delta = config.SHORT_STRIKE_TARGET_DELTA
    is_spy_like = ticker.upper() in config.SPY_BUFFER_TICKERS
    target_dte = getattr(config, "PREFERRED_DTE_TARGET", 35)
    dte_tolerance = getattr(config, "PREFERRED_DTE_TOLERANCE", 7)
    min_vol = getattr(config, "MIN_OPTION_VOLUME", 100)
    min_oi = getattr(config, "MIN_OPTION_OPEN_INTEREST", 500)
    min_spread_width = (
        getattr(config, "MIN_SPREAD_WIDTH_SPY_LIKE", 2.0)
        if is_spy_like
        else getattr(config, "MIN_SPREAD_WIDTH_OTHER", 1.0)
    )

    for opt in options:
        if opt.get("type") != "put":
            continue

        strike = opt.get("strike", 0)
        delta = opt.get("delta")
        mid = opt.get("mid", 0)
        if not strike or delta is None or mid <= 0:
            _bump("short_missing_price_delta")
            continue
        if not _quote_is_tradeable(opt):
            _bump("short_quote_not_tradeable")
            continue
        if opt.get("volume", 0) < min_vol and opt.get("open_interest", 0) < min_oi:
            _bump("short_liquidity_below_floor")
            continue

        abs_delta = abs(delta)
        if abs_delta > config.SHORT_STRIKE_MAX_DELTA:
            _bump("short_delta_too_high")
            continue

        if is_spy_like:
            if (current_price - strike) < config.MIN_STRIKE_BUFFER_SPY:
                _bump("short_buffer_too_tight")
                continue
        else:
            min_buffer_pct = config.MIN_STRIKE_BUFFER_STOCK  # 5% for individual stocks
            if (current_price - strike) / current_price < min_buffer_pct:
                _bump("short_buffer_too_tight")
                continue

        dte = int(opt.get("dte", 0) or 0)
        dte_distance = abs(dte - target_dte)
        dte_outside_target = 0 if dte_distance <= dte_tolerance else 1
        short_candidates.append(((dte_outside_target, dte_distance, abs(abs_delta - target_delta)), opt))

    short_candidates.sort(key=lambda x: x[0])

    for _, short_put in short_candidates:
        long_candidates = [
            opt for opt in options
            if opt.get("type") == "put"
            and opt.get("expiration") == short_put.get("expiration")
            and 0 < (short_put.get("strike", 0) - opt.get("strike", 0)) <= config.MAX_SPREAD_WIDTH
        ]
        long_candidates.sort(key=lambda opt: opt.get("strike", 0), reverse=True)

        for long_put in long_candidates:
            if not _quote_is_tradeable(long_put):
                _bump("long_quote_not_tradeable")
                continue
            if long_put.get("volume", 0) < min_vol and long_put.get("open_interest", 0) < min_oi:
                _bump("long_liquidity_below_floor")
                continue

            spread_width = float(short_put.get("strike", 0) - long_put.get("strike", 0))
            if spread_width <= 0:
                _bump("spread_width_invalid")
                continue

            metrics = edge_calculator.calculate_spread_metrics(
                short_put,
                long_put.get("strike"),
                current_price,
                long_put_mid=long_put.get("mid"),
            )
            if not metrics:
                _bump("metrics_missing")
                continue
            if metrics.get("spread_invalid"):
                _bump("spread_pricing_invalid")
                continue
            if metrics.get("credit_per_share", 0) <= 0:
                _bump("credit_non_positive")
                continue
            if metrics.get("credit_usd", 0) < config.MIN_CREDIT_USD:
                _bump("credit_below_min_usd")
                continue

            # Universal credit-to-width quality gate — applies to ALL spreads regardless of width
            credit_per_share = float(metrics.get("credit_per_share", 0) or 0)
            min_ctw = getattr(config, "MIN_CREDIT_TO_WIDTH_PCT", 0.25)
            if spread_width > 0 and (credit_per_share / spread_width) < min_ctw:
                _bump("credit_to_width_below_min")
                continue

            if spread_width < min_spread_width:
                credit_per_share = float(metrics.get("credit_per_share", 0) or 0)
                credit_to_width = credit_per_share / spread_width if spread_width > 0 else 0
                allow_narrow = getattr(config, "ALLOW_NARROW_SPREAD_EXCEPTION", True)
                min_credit_to_width = getattr(config, "NARROW_SPREAD_MIN_CREDIT_TO_WIDTH", 0.30)
                if not (allow_narrow and credit_to_width >= min_credit_to_width):
                    _bump("narrow_spread_exception_failed")
                    continue

            if diagnostics is not None:
                diagnostics["short_candidates_count"] = len(short_candidates)
                diagnostics["top_reasons"] = sorted(diag_counts.items(), key=lambda x: x[1], reverse=True)[:6]
            return short_put, long_put, metrics

    if diagnostics is not None:
        diagnostics["short_candidates_count"] = len(short_candidates)
        diagnostics["top_reasons"] = sorted(diag_counts.items(), key=lambda x: x[1], reverse=True)[:6]
    return None


def screen_ticker(ticker: str, sentiment_map: Dict[str, Dict]) -> Tuple[Optional[Dict], Optional[Dict], Dict]:
    price_data = fetcher.get_price_data(ticker, period="2y")
    if price_data is None or price_data.empty:
        return None, {"reason": "No price history available", "category": "NO_DATA"}, technicals._empty_result(ticker)

    current_price = float(price_data["Close"].iloc[-1])

    fundamentals_data = fundamentals.get_fundamentals(ticker) if getattr(config, "FUNDAMENTALS_ENABLED", True) else {}
    fundamentals_eval = edge_calculator.calculate_fundamentals_score(
        fundamentals_data,
        is_etf=fundamentals_data.get("is_etf", False),
    ) if fundamentals_data else {"score": None, "blocking": False, "reasons": []}

    def _avoid(reason: str, category: str, tech_payload: Dict, extra: Optional[Dict] = None) -> Tuple[None, Dict, Dict]:
        payload = {
            "reason": reason,
            "category": category,
            "fundamentals_score": fundamentals_eval.get("score"),
            "fundamentals_reasons": fundamentals_eval.get("reasons", []),
            "fundamentals_blocking": fundamentals_eval.get("blocking", False),
        }
        if extra:
            payload.update(extra)
        return None, payload, tech_payload

    options = fetcher.get_options_chain(ticker, config.MIN_DTE, config.MAX_DTE)
    logger.debug(f"[screen] {ticker}: options chain length = {len(options)}")
    if not options:
        return _avoid("No options chain in DTE range", "NO_OPTIONS", technicals._empty_result(ticker))

    pair_diag: Dict = {}
    pair = select_bull_put_pair(options, current_price, ticker, diagnostics=pair_diag)
    if not pair:
        return _avoid(
            "No valid same-expiration credit spread found",
            "NO_VALID_SPREAD",
            technicals._empty_result(ticker),
            extra={"pair_selection_diagnostics": pair_diag},
        )
    short_put, long_put, metrics = pair

    current_iv = short_put.get("iv") or estimate_current_iv(options, current_price)
    logger.debug(f"[screen] {ticker}: current_iv = {current_iv}")
    tech = technicals.calculate_all(price_data, ticker, current_iv=current_iv, short_strike=short_put["strike"])

    if tech.get("iv_rank", 0) < config.MIN_IV_RANK:
        return _avoid(f"IV Rank {tech.get('iv_rank', 0):.1f} below minimum {config.MIN_IV_RANK}", "IV_RANK", tech)

    earnings_dt = fetcher.get_earnings_date(ticker)
    days_to_earnings = fundamentals.days_until_earnings(earnings_dt)

    sentiment = sentiment_map.get(ticker, {})
    sentiment_label = (sentiment.get("sentiment") or "NEUTRAL").upper()
    if config.NEWS_SENTIMENT_BLOCK and sentiment_label == "BLOCKING":
        return _avoid("News BLOCKING event detected", "NEWS_BLOCK", tech)

    if (
        getattr(config, "FUNDAMENTALS_ENABLED", True)
        and getattr(config, "FUNDAMENTALS_STRICT_BLOCK", False)
        and not getattr(config, "FUNDAMENTALS_SHADOW_MODE", True)
    ):
        if fundamentals_eval.get("blocking") or (fundamentals_eval.get("score", 10) < getattr(config, "MIN_FUNDAMENTALS_SCORE", 4)):
            return _avoid(
                "; ".join(fundamentals_eval.get("reasons", ["Fundamentals stability check failed"])),
                "FUNDAMENTALS_BLOCK",
                tech,
            )

    # Only bull_put_spread is currently implemented; roadmap strategies are disabled in config.
    strategy = "bull_put_spread"
    if strategy not in config.ENABLED_STRATEGIES:
        return _avoid("No supported strategy enabled", "STRATEGY_DISABLED", tech)

    if not metrics:
        return _avoid("Could not compute spread metrics", "SPREAD_ERROR", tech)

    # C2 FIX — two distinct probabilities instead of one mislabeled "true_pop":
    #   p_max_profit : P(price > short strike) = probability the short leg expires worthless
    #                  (FULL profit). Compared to the option's implied P(OTM)=1−|delta| to score edge.
    #   p_profit     : P(price > breakeven), breakeven = short strike − net credit. This is the REAL
    #                  probability of profit and is what MIN_PROBABILITY_OF_PROFIT should gate on.
    short_strike_val = short_put["strike"]
    credit_ps = float(metrics.get("credit_per_share", 0) or 0)
    breakeven_price = short_strike_val - credit_ps
    dte_val = short_put.get("dte", 0) or 0
    prices_hist = price_data["Close"]

    otm_distance_pct = (current_price - short_strike_val) / current_price if current_price else 0
    be_distance_pct = (current_price - breakeven_price) / current_price if current_price else 0

    p_maxprofit_res = edge_calculator.calculate_true_pop(
        strike_distance_pct=otm_distance_pct,
        expiration_days=dte_val,
        historical_prices=prices_hist,
    )
    p_profit_res = edge_calculator.calculate_true_pop(
        strike_distance_pct=be_distance_pct,
        expiration_days=dte_val,
        historical_prices=prices_hist,
    )
    p_max_profit = p_maxprofit_res.get("true_pop")
    p_profit = p_profit_res.get("true_pop")

    implied_pop = 1 - abs(short_put.get("delta", 0) or 0)

    # Edge measured at the short strike (apples-to-apples with delta-implied P(OTM)).
    edge_res = edge_calculator.calculate_edge_points(p_max_profit, implied_pop)
    edge_points = edge_res.get("edge_points", 0)

    # Gate on the REAL probability of profit (at breakeven), not P(max profit).
    if p_profit is None or p_profit < config.MIN_PROBABILITY_OF_PROFIT:
        return _avoid(
            f"POP {0 if p_profit is None else p_profit:.2f} (at breakeven) below minimum {config.MIN_PROBABILITY_OF_PROFIT}",
            "MIN_POP",
            tech,
        )

    edge_score = edge_calculator.calculate_edge_score(
        ticker=ticker,
        strategy=strategy,
        technical_score=tech.get("composite_score", 0),
        vrp_pct=tech.get("vrp", 0),
        edge_points=edge_points,
        news_sentiment=sentiment_label,
        earnings_days_away=days_to_earnings,
        fundamentals_score=fundamentals_eval.get("score"),
    )

    if not edge_score.get("qualified"):
        reason = edge_score.get("disqualification_reason") or "Edge score below minimum"
        return _avoid(reason, "EDGE_SCORE", tech)

    option_data = dict(short_put)
    option_data.update(metrics)


    validation = strike_validator.validate_strike(
        ticker=ticker,
        strategy=strategy,
        short_strike=short_put["strike"],
        current_price=current_price,
        delta=short_put.get("delta"),
        account_balance=config.ACCOUNT_BALANCE,
        option_data=option_data,
        tech_data=tech,
        days_to_earnings=days_to_earnings,
    )

    if not validation.get("valid"):
        return _avoid(
            validation.get("rejection_reason", "Validation failed"),
            validation.get("rejection_category", "VALIDATION"),
            tech,
        )

    warnings = list(validation.get("warnings", []))
    if sentiment_label == "NEGATIVE":
        warnings.append("Negative news sentiment - monitor closely")
    if getattr(config, "FUNDAMENTALS_SHADOW_MODE", True) and fundamentals_eval.get("blocking"):
        warnings.append("Fundamentals warning (shadow mode): " + "; ".join(fundamentals_eval.get("reasons", [])))

    news_status = "CLEAR" if sentiment_label in ("POSITIVE", "NEUTRAL") else sentiment_label

    profit_target_price = round(metrics.get("credit_per_share", 0) * (1 - config.TARGET_PROFIT_PCT), 2)

    trade_type = validation.get("trade_type", "standard_premium")
    commission_per_contract = float(getattr(config, "COMMISSION_PER_CONTRACT_PER_LEG", 0.65) or 0.0) * 2
    entry_slippage_ps = float(getattr(config, "ASSUMED_ENTRY_SLIPPAGE_PER_SHARE", 0.02) or 0.0)
    exit_slippage_ps = float(getattr(config, "ASSUMED_EXIT_SLIPPAGE_PER_SHARE", 0.02) or 0.0)
    estimated_entry_cost_per_contract = round(entry_slippage_ps * 100 + commission_per_contract, 2)
    estimated_exit_cost_per_contract = round(exit_slippage_ps * 100 + commission_per_contract, 2)
    estimated_round_trip_cost_per_contract = round(
        estimated_entry_cost_per_contract + estimated_exit_cost_per_contract, 2
    )
    gross_credit_per_share = float(metrics.get("credit_per_share", 0) or 0.0)
    net_credit_per_share = round(
        max(0.0, gross_credit_per_share - entry_slippage_ps - (commission_per_contract / 100.0)), 2
    )
    net_credit_usd = round(net_credit_per_share * 100, 2)

    trade = {
        "ticker": ticker,
        "strategy": strategy,
        "current_price": round(current_price, 2),
        "short_strike": short_put.get("strike"),
        "long_strike": long_put.get("strike"),
        "long_mid": long_put.get("mid"),
        "expiration": short_put.get("expiration"),
        "last_trade_date": short_put.get("last_trade_date"),
        "expiration_display": short_put.get("last_trade_date") or short_put.get("expiration"),
        "dte": short_put.get("dte"),
        "credit_per_share": metrics.get("credit_per_share"),
        "credit_usd": metrics.get("credit_usd"),
        "net_credit_per_share": net_credit_per_share,
        "net_credit_usd": net_credit_usd,
        "estimated_entry_cost_per_contract": estimated_entry_cost_per_contract,
        "estimated_exit_cost_per_contract": estimated_exit_cost_per_contract,
        "estimated_round_trip_cost_per_contract": estimated_round_trip_cost_per_contract,
        "max_loss_usd": metrics.get("max_loss_usd"),
        "contracts_allowed": metrics.get("contracts_allowed"),
        "risk_tiers": metrics.get("risk_tiers", []),
        "oversized_position": all(
            not t.get("viable", False) for t in metrics.get("risk_tiers", [{"viable": False}])
        ),
        "profit_target_usd": metrics.get("profit_target_usd"),
        "profit_target_price": profit_target_price,
        "stop_loss_close_price": metrics.get("stop_loss_close_price"),
        "delta": short_put.get("delta"),
        "iv_rank": tech.get("iv_rank"),
        "strike_distance_usd": metrics.get("strike_distance_usd"),
        "strike_distance_pct": metrics.get("strike_distance_pct"),
        "credit_to_width_pct": round(
            (metrics.get("credit_per_share", 0) / metrics.get("spread_width", 1)) * 100, 1
        ) if metrics.get("spread_width") else 0,
        "true_pop": p_profit,                       # C2: real probability of profit (at breakeven)
        "p_max_profit": p_max_profit,               # P(short expires OTM) — used for edge scoring
        "true_pop_confidence": p_profit_res.get("confidence"),
        "true_pop_drift_mode": p_profit_res.get("drift_mode"),
        "implied_pop": implied_pop,
        "edge_points": edge_points,
        "edge_score": edge_score.get("total_score"),
        "component_breakdown": edge_score.get("component_breakdown"),
        "vrp": tech.get("vrp"),
        "trend": tech.get("trend"),
        "rsi": tech.get("rsi"),
        "macd_crossover": tech.get("macd_crossover"),
        "sma20": tech.get("sma20"),
        "sma50": tech.get("sma50"),
        "sma200": tech.get("sma200"),
        "nearest_support": tech.get("nearest_support"),
        "news_sentiment": news_status,
        "news_summary": sentiment.get("market_impact_summary"),
        "fundamentals_score": fundamentals_eval.get("score"),
        "fundamentals_reasons": fundamentals_eval.get("reasons", []),
        "pe_ratio": fundamentals_data.get("pe_ratio"),
        "market_cap": fundamentals_data.get("market_cap"),
        "debt_to_equity": fundamentals_data.get("debt_to_equity"),
        "current_ratio": fundamentals_data.get("current_ratio"),
        "warnings": warnings,
        "auto_reasoning": f"IV Rank {tech.get('iv_rank', 0):.0f}, VRP {tech.get('vrp', 0):.1f}pp, edge {edge_points:.1f} pts.",
        "trade_type": trade_type,
    }

    return trade, None, tech


def compute_morning_signals(tech_map: Dict[str, Dict], sentiment_map: Dict[str, Dict]) -> List[Dict]:
    signals = []
    for ticker, tech in tech_map.items():
        change = tech.get("day_change_pct")
        vol_ratio = tech.get("vol_ratio")
        rsi = tech.get("rsi", 50)
        sentiment = (sentiment_map.get(ticker, {}).get("sentiment") or "NEUTRAL").upper()
        if change is None or vol_ratio is None:
            continue
        if change <= -1.0 and vol_ratio >= 1.2 and rsi <= 60 and sentiment not in ("NEGATIVE", "BLOCKING"):
            support = tech.get("nearest_support")
            support_txt = f"support ${support:.2f}" if support else "near support"
            signals.append({
                "ticker": ticker,
                "signal": f"Down {abs(change):.1f}% on {vol_ratio:.1f}x volume; watch {support_txt} for EOD stabilization.",
            })
    return signals


def compute_eod_setups(tech_map: Dict[str, Dict], sentiment_map: Dict[str, Dict]) -> List[Dict]:
    setups = []
    for ticker, tech in tech_map.items():
        change = tech.get("day_change_pct")
        vol_ratio = tech.get("vol_ratio")
        rsi = tech.get("rsi", 50)
        price = tech.get("price", 0)
        sma200 = tech.get("sma200")
        sentiment = (sentiment_map.get(ticker, {}).get("sentiment") or "NEUTRAL").upper()

        if change is None or vol_ratio is None:
            continue
        if not (-config.EOD_MAX_DROP_PCT <= change <= -config.EOD_MIN_DROP_PCT):
            continue
        if vol_ratio < config.EOD_MIN_VOLUME_RATIO:
            continue
        if not (45 <= rsi <= 55):
            continue
        if sma200 and price < sma200:
            continue
        if sentiment in ("NEGATIVE", "BLOCKING"):
            continue

        support = tech.get("nearest_support")
        entry_zone = None
        if support:
            entry_zone = f"Entry zone: ${support:.2f} to ${price:.2f}"

        setups.append({
            "ticker": ticker,
            "price": price,
            "day_change_pct": change,
            "vol_ratio": vol_ratio,
            "rsi": rsi,
            "nearest_support": support,
            "entry_zone": entry_zone,
            "note": "Potential overnight/next-day bounce setup if selling pressure fades into the close.",
        })

    return setups


def load_open_positions(log_dir: Path) -> List[Dict]:
    path = log_dir / "open_positions.json"
    data = load_json(path, [])
    return data if isinstance(data, list) else []


def compute_decay_alerts(open_positions: List[Dict]) -> List[Dict]:
    alerts = []
    for pos in open_positions:
        entry_credit = pos.get("entry_credit")
        current_price = pos.get("current_price")
        if entry_credit is None or current_price is None:
            continue
        if entry_credit <= 0:
            continue
        profit_pct = (entry_credit - current_price) / entry_credit
        if profit_pct >= config.TARGET_PROFIT_PCT:
            ticker = pos.get("ticker", "")
            strategy = pos.get("strategy", "")
            short_strike = pos.get("short_strike")
            long_strike = pos.get("long_strike")
            expiration = pos.get("expiration")
            target_price = round(entry_credit * (1 - config.TARGET_PROFIT_PCT), 2)
            message = (
                f"{strategy} {short_strike}/{long_strike} exp {expiration}: "
                f"mark ${current_price:.2f} vs target ${target_price:.2f} "
                f"({profit_pct*100:.0f}% of max profit)."
            )
            alerts.append({"ticker": ticker, "message": message})
    return alerts


def compute_weekly_summary(log_dir: Path, ts: datetime) -> Optional[Dict]:
    path = log_dir / "scan_log.json"
    data = load_json(path, [])
    if not data:
        return None

    start_of_week = (ts.date() - timedelta(days=ts.weekday()))
    end_of_week = start_of_week + timedelta(days=6)

    qualified_count = 0
    edge_scores = []

    for entry in data:
        try:
            entry_ts = datetime.fromisoformat(entry.get("timestamp"))
        except Exception:
            continue
        if not (start_of_week <= entry_ts.date() <= end_of_week):
            continue

        trades = entry.get("qualified_trades", [])
        qualified_count += len(trades)
        for t in trades:
            if "edge_score" in t:
                edge_scores.append(t["edge_score"])

    avg_edge = round(sum(edge_scores) / len(edge_scores), 1) if edge_scores else 0

    return {
        "qualified_count": qualified_count,
        "avg_edge_score": avg_edge,
        "account_balance": config.ACCOUNT_BALANCE,
        "note": "Review only the highest-edge setups and bank wins into account growth.",
    }


def _compute_regime_context(market_context: Dict) -> Dict:
    """
    Determine the current market regime and return a flag + note.

    Returns:
        regime_flag:  'NORMAL' | 'LOW_VOL' | 'ELEVATED_VOL' | 'HIGH_VOL'
        regime_note:  Human-readable description injected into scan output and JARVIS
        trade_suppressed: True when VIX < VIX_MIN_FOR_EDGE (not a hard stop — let
                          IV Rank gate work, but flag output so silence is explainable)
    """
    vix_level = (market_context.get("vix") or {}).get("current") or 0
    vix_min = getattr(config, "VIX_MIN_FOR_EDGE", 16)
    vix_max = getattr(config, "VIX_MAX_FOR_TRADES", 30)
    vix_elevated = getattr(config, "VIX_ELEVATED_THRESHOLD", 25)

    if vix_level <= 0:
        return {"regime_flag": "NORMAL", "regime_note": None, "trade_suppressed": False}

    if vix_level < vix_min:
        return {
            "regime_flag": "LOW_VOL",
            "regime_note": (
                f"LOW VOLATILITY REGIME — VIX {vix_level:.1f} is below the minimum edge threshold "
                f"({vix_min}). Premium is cheap; VRP edge is thin. Expect few or no qualifiers. "
                f"This is correct behavior — do not chase setups."
            ),
            "trade_suppressed": True,
        }
    elif vix_level > vix_max:
        return {
            "regime_flag": "HIGH_VOL",
            "regime_note": (
                f"⚠️ HIGH VOLATILITY REGIME — VIX {vix_level:.1f} exceeds {vix_max}. "
                f"Gamma risk is elevated. Size ALL positions at 50% or less. "
                f"Prefer SPY/QQQ over individual names. Consider standing aside."
            ),
            "trade_suppressed": False,
        }
    elif vix_level > vix_elevated:
        return {
            "regime_flag": "ELEVATED_VOL",
            "regime_note": (
                f"⚠️ ELEVATED VOLATILITY — VIX {vix_level:.1f}. Size down 50% on new positions."
            ),
            "trade_suppressed": False,
        }
    else:
        return {"regime_flag": "NORMAL", "regime_note": None, "trade_suppressed": False}


def _apply_sector_limit(qualified_trades: List[Dict]) -> List[Dict]:
    """
    Enforce MAX_TRADES_PER_SECTOR to prevent correlated position concentration.

    Tickers in SECTOR_LIMIT_EXEMPT are never capped (broad-market ETFs).
    Within each capped sector, only the top MAX_TRADES_PER_SECTOR by edge_score
    are kept. Dropped trades are logged at INFO level.
    """
    max_per_sector = getattr(config, "MAX_TRADES_PER_SECTOR", 2)
    exempt = getattr(config, "SECTOR_LIMIT_EXEMPT", {"broad_market"})
    sector_map = getattr(config, "TICKER_SECTORS", {})
    broad_corr_cap = getattr(config, "MAX_CORRELATED_BROAD_MARKET_TRADES", 1)
    broad_corr_tickers = getattr(config, "CORRELATED_BROAD_MARKET_TICKERS", {"SPY", "QQQ", "IWM"})

    sector_counts: Dict[str, int] = {}
    broad_corr_count = 0
    kept: List[Dict] = []
    dropped: List[str] = []

    # Sort by edge_score descending so highest-edge trades win within each sector
    sorted_trades = sorted(qualified_trades, key=lambda t: t.get("edge_score", 0), reverse=True)

    for trade in sorted_trades:
        ticker = trade.get("ticker", "").upper()

        if ticker in broad_corr_tickers:
            if broad_corr_count >= broad_corr_cap:
                dropped.append(f"{ticker} (broad_corr_cap)")
                continue
            broad_corr_count += 1

        sector = sector_map.get(ticker, "other")
        if sector in exempt:
            kept.append(trade)
            continue
        count = sector_counts.get(sector, 0)
        if count < max_per_sector:
            sector_counts[sector] = count + 1
            kept.append(trade)
        else:
            dropped.append(f"{ticker} ({sector})")

    if dropped:
        logger.info(
            f"[sector_limit] Dropped {len(dropped)} trade(s) due to sector cap "
            f"(max {max_per_sector}/sector): {', '.join(dropped)}"
        )

    return kept


def run_scan(session_type: str) -> None:
    ts = now_et()
    check_session_window(session_type, ts)

    tickers = [w["ticker"].upper() for w in config.WATCHLIST]
    logger.debug(f"[scan] Ticker list: {tickers}")

    logger.info(f"Starting {session_type} scan for {len(tickers)} tickers")

    source_health = fetcher.validate_polygon_connection("SPY") if config.POLYGON_API_KEY else {
        "enabled": False,
        "healthy": True,
        "mode": "yfinance_only",
        "reason": "POLYGON_API_KEY not set — using yfinance fallback",
    }
    logger.info(
        f"[scan] Data source mode={source_health.get('mode')} healthy={source_health.get('healthy')} reason={source_health.get('reason')}"
    )
    if not source_health.get("healthy", False):
        logger.warning(
            f"[scan] Data source probe degraded: mode={source_health.get('mode')} reason={source_health.get('reason')}"
        )

    market_context = build_market_context()
    sentiment_map = news.analyze_all_tickers(tickers)

    # ── Regime gate: classify VIX environment, inject note into output ──────
    regime = _compute_regime_context(market_context)
    if regime["regime_note"]:
        logger.info(f"[scan] Regime: {regime['regime_flag']} — {regime['regime_note']}")

    qualified_trades: List[Dict] = []
    avoided: List[Dict] = []
    tech_map: Dict[str, Dict] = {}
    errors: List[str] = []


    logger.debug(f"[scan] Tickers to screen: {tickers}")
    for ticker in tickers:
        logger.debug(f"[scan] Screening ticker: {ticker}")
        try:
            trade, avoid, tech = screen_ticker(ticker, sentiment_map)
            tech_map[ticker] = tech
            if trade:
                logger.debug(f"[scan] Qualified trade for {ticker}: edge={trade.get('edge_score')}")
                qualified_trades.append(trade)
            else:
                logger.debug(f"[scan] Avoided {ticker}: {avoid.get('category') if avoid else 'UNKNOWN'}")
                avoid_payload = {
                    "ticker": ticker,
                    "reason": avoid.get("reason") if avoid else "Unknown",
                    "category": avoid.get("category") if avoid else "UNKNOWN",
                }
                if avoid:
                    for k, v in avoid.items():
                        if k not in avoid_payload:
                            avoid_payload[k] = v
                avoided.append(avoid_payload)
        except Exception as e:
            logger.warning(f"[scan] Exception screening {ticker}: {e}")
            errors.append(f"{ticker}: {e}")
            avoided.append({"ticker": ticker, "reason": str(e), "category": "ERROR"})

    logger.debug(f"[scan] Qualified: {[t.get('ticker') for t in qualified_trades]}")
    logger.debug(f"[scan] Avoided: {[a.get('ticker') for a in avoided]}")
    logger.debug(f"[scan] Errors: {errors}")

    # Build shadow telemetry payload (non-blocking observability data)
    qualified_by_ticker = {str(t.get("ticker", "")).upper(): t for t in qualified_trades}
    avoided_by_ticker = {str(a.get("ticker", "")).upper(): a for a in avoided}
    shadow_evaluations: List[Dict] = []
    for ticker in tickers:
        qt = qualified_by_ticker.get(ticker)
        av = avoided_by_ticker.get(ticker)
        fundamentals_score = qt.get("fundamentals_score") if qt else av.get("fundamentals_score") if av else None
        fundamentals_reasons = qt.get("fundamentals_reasons") if qt else av.get("fundamentals_reasons") if av else []
        shadow_evaluations.append({
            "ticker": ticker,
            "evaluation_status": "evaluated" if fundamentals_score is not None else "missing",
            "qualified": qt is not None,
            "would_block": bool(av and av.get("fundamentals_blocking")),
            "fundamentals_score": fundamentals_score,
            "fundamentals_reasons": fundamentals_reasons or [],
            "rejection_category": av.get("category") if av else None,
            "rejection_reason": av.get("reason") if av else None,
            "edge_score": qt.get("edge_score") if qt else None,
            "vrp": qt.get("vrp") if qt else None,
            "iv_rank": qt.get("iv_rank") if qt else None,
            "true_pop": qt.get("true_pop") if qt else None,
            "news_sentiment": qt.get("news_sentiment") if qt else None,
            "component_breakdown": qt.get("component_breakdown") if qt else None,
        })

    evaluated_count = sum(1 for e in shadow_evaluations if e.get("evaluation_status") == "evaluated")
    missing_count = len(shadow_evaluations) - evaluated_count
    would_block_count = sum(1 for e in shadow_evaluations if e.get("would_block"))
    scored_values = [float(e["fundamentals_score"]) for e in shadow_evaluations if e.get("fundamentals_score") is not None]
    shadow_run = {
        "run_timestamp": ts.isoformat(),
        "session_type": session_type,
        "shadow_enabled": bool(getattr(config, "FUNDAMENTALS_ENABLED", False)),
        "strict_mode_enabled": bool(getattr(config, "FUNDAMENTALS_STRICT_BLOCK", False)),
        "expected_tickers": len(tickers),
        "evaluated_tickers": evaluated_count,
        "missing_evals": missing_count,
        "would_block_count": would_block_count,
        "coverage_ratio": round(evaluated_count / len(tickers), 4) if tickers else 0.0,
        "missing_ratio": round(missing_count / len(tickers), 4) if tickers else 0.0,
        "fallback_used_count": 0,
        "score_min": min(scored_values) if scored_values else None,
        "score_max": max(scored_values) if scored_values else None,
        "score_avg": round(sum(scored_values) / len(scored_values), 2) if scored_values else None,
        "config_snapshot": {
            "fundamentals_shadow_mode": getattr(config, "FUNDAMENTALS_SHADOW_MODE", True),
            "fundamentals_strict_block": getattr(config, "FUNDAMENTALS_STRICT_BLOCK", False),
            "min_fundamentals_score": getattr(config, "MIN_FUNDAMENTALS_SCORE", 4),
            "fundamentals_weight": getattr(config, "FUNDAMENTALS_WEIGHT", 10),
        },
    }

    # ── Sector correlation bucketing ─────────────────────────────────────
    qualified_trades = _apply_sector_limit(qualified_trades)


    synthesis = synthesizer.synthesize_tipsheet(
        session_type=session_type,
        qualified_trades=qualified_trades,
        market_context=market_context,
        account_balance=config.ACCOUNT_BALANCE,
        scan_timestamp=ts.isoformat(),
    )

    log_dir = BASE_DIR / config.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    open_positions = load_open_positions(log_dir)
    decay_alerts = compute_decay_alerts(open_positions) if session_type == "close" else []

    eod_setups = compute_eod_setups(tech_map, sentiment_map) if session_type == "close" else []
    morning_signals = compute_morning_signals(tech_map, sentiment_map) if session_type == "morning" else []

    weekly_summary = compute_weekly_summary(log_dir, ts) if (session_type == "close" and ts.weekday() == 4) else None

    logger.debug("[scan] Calling renderer.render()")
    output_path = renderer.render(
        session_type=session_type,
        qualified_trades=qualified_trades,
        avoided_tickers=avoided,
        market_context=market_context,
        synthesis=synthesis,
        account_balance=config.ACCOUNT_BALANCE,
        scan_timestamp=ts,
        eod_setups=eod_setups,
        weekly_summary=weekly_summary,
        morning_signals=morning_signals,
        decay_alerts=decay_alerts,
    )
    logger.debug(f"[scan] renderer.render() returned: {output_path}")

    scan_entry = {
        "timestamp": ts.isoformat(),
        "session_type": session_type,
        "tickers_scanned": tickers,
        "qualified_trades": [
            {"ticker": t.get("ticker"), "edge_score": t.get("edge_score")} for t in qualified_trades
        ],
        "rejected_trades": avoided,
        "api_calls": fetcher.get_api_call_log(),
        "tipsheet_file": str(output_path),
        "account_balance": config.ACCOUNT_BALANCE,
        "errors": errors,
        "regime_flag": regime["regime_flag"],
        "regime_note": regime["regime_note"],
        "source_health": {
            "polygon": source_health,
        },
        "shadow_run": shadow_run,
        "shadow_evaluations": shadow_evaluations,
    }
    append_scan_log(log_dir, scan_entry)

    # ── Gate 1: record each qualified trade as a "modeled" outcome row (non-blocking).
    # You later add the real fill + close via log_outcome.py; the report measures calibration.
    try:
        from analysis.outcome_logger import record_modeled_trades
        record_modeled_trades(ts.isoformat(), session_type, qualified_trades)
    except Exception as e:
        logger.warning(f"[scan] Outcome logging skipped (non-blocking): {e}")

    logger.info(f"Scan complete. Tip sheet saved to {output_path}")

    # ── VEGA: Push scan results to JARVIS tower ──────────────────────────
    if VEGA_INGEST_ENABLED and source_health.get("healthy", False):
        # Read tipsheet HTML if available
        tipsheet_html = None
        if output_path and Path(output_path).exists():
            try:
                tipsheet_html = Path(output_path).read_text(encoding="utf-8")
            except Exception:
                pass

        # Build full qualified trade dicts (not just ticker/score summaries)
        full_scan_entry = dict(scan_entry)
        full_scan_entry["qualified_trades"] = [
            {k: v for k, v in t.items() if k != "component_breakdown"}
            for t in qualified_trades
        ]

        post_to_jarvis(
            scan_entry=full_scan_entry,
            session_type=session_type,
            market_context=market_context,
            tipsheet_html=tipsheet_html,
        )
    elif VEGA_INGEST_ENABLED:
        logger.warning("[scan] Skipping JARVIS ingest because source health is degraded")

    if config.EMAIL_ENABLED:
        pass  # Email sending not configured


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Options Intelligence tip sheet generator")
    parser.add_argument("--session", choices=["morning", "close"], required=True)
    return parser.parse_args()


if __name__ == "__main__":
    setup_logging()
    args = parse_args()
    run_scan(args.session)
