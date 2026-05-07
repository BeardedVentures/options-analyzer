
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


def screen_ticker(ticker: str, sentiment_map: Dict[str, Dict]) -> Tuple[Optional[Dict], Optional[Dict], Dict]:
    price_data = fetcher.get_price_data(ticker, period="2y")
    if price_data is None or price_data.empty:
        return None, {"reason": "No price history available", "category": "NO_DATA"}, technicals._empty_result(ticker)

    current_price = float(price_data["Close"].iloc[-1])

    options = fetcher.get_options_chain(ticker, config.MIN_DTE, config.MAX_DTE)
    print(f"[DEBUG] {ticker}: options chain length = {len(options)}")
    if not options:
        return None, {"reason": "No options chain in DTE range", "category": "NO_OPTIONS"}, technicals._empty_result(ticker)

    short_put = edge_calculator.find_target_put(options, current_price, ticker)
    if not short_put:
        return None, {"reason": "No put meets delta/OTM/credit filters", "category": "NO_STRIKE"}, technicals._empty_result(ticker)

    long_strike = select_long_put_strike(options, short_put["strike"])
    if not long_strike:
        return None, {"reason": "No long strike within max spread width", "category": "NO_LONG_STRIKE"}, technicals._empty_result(ticker)

    current_iv = short_put.get("iv") or estimate_current_iv(options, current_price)
    print(f"[DEBUG] {ticker}: current_iv = {current_iv}")
    tech = technicals.calculate_all(price_data, ticker, current_iv=current_iv, short_strike=short_put["strike"])

    if tech.get("iv_rank", 0) < config.MIN_IV_RANK:
        return None, {"reason": f"IV Rank {tech.get('iv_rank', 0):.1f} below minimum {config.MIN_IV_RANK}", "category": "IV_RANK"}, tech

    earnings_dt = fetcher.get_earnings_date(ticker)
    days_to_earnings = fundamentals.days_until_earnings(earnings_dt)

    sentiment = sentiment_map.get(ticker, {})
    sentiment_label = (sentiment.get("sentiment") or "NEUTRAL").upper()
    if config.NEWS_SENTIMENT_BLOCK and sentiment_label == "BLOCKING":
        return None, {"reason": "News BLOCKING event detected", "category": "NEWS_BLOCK"}, tech

    strategy = edge_calculator.select_best_strategy(
        account_balance=config.ACCOUNT_BALANCE,
        trend=tech.get("trend", "NEUTRAL"),
        iv_rank=tech.get("iv_rank", 0),
        vix_level=tech.get("vrp", 0),
    )
    if strategy not in config.ENABLED_STRATEGIES:
        if "bull_put_spread" in config.ENABLED_STRATEGIES:
            strategy = "bull_put_spread"
        else:
            return None, {"reason": "No supported strategy enabled", "category": "STRATEGY_DISABLED"}, tech

    # Current implementation supports bull put spreads only
    if strategy != "bull_put_spread":
        strategy = "bull_put_spread"

    metrics = edge_calculator.calculate_spread_metrics(short_put, long_strike, current_price)
    if not metrics:
        return None, {"reason": "Could not compute spread metrics", "category": "SPREAD_ERROR"}, tech

    strike_distance_pct = metrics.get("strike_distance_pct", 0) / 100 if metrics.get("strike_distance_pct") is not None else 0
    true_pop_res = edge_calculator.calculate_true_pop(
        strike_distance_pct=strike_distance_pct,
        expiration_days=short_put.get("dte", 0),
        historical_prices=price_data["Close"],
    )
    true_pop = true_pop_res.get("true_pop")
    implied_pop = 1 - abs(short_put.get("delta", 0) or 0)

    edge_res = edge_calculator.calculate_edge_points(true_pop, implied_pop)
    edge_points = edge_res.get("edge_points", 0)

    if true_pop is None or true_pop < config.MIN_PROBABILITY_OF_PROFIT:
        return None, {"reason": f"True POP {0 if true_pop is None else true_pop:.2f} below minimum {config.MIN_PROBABILITY_OF_PROFIT}", "category": "MIN_POP"}, tech

    edge_score = edge_calculator.calculate_edge_score(
        ticker=ticker,
        strategy=strategy,
        technical_score=tech.get("composite_score", 0),
        vrp_pct=tech.get("vrp", 0),
        edge_points=edge_points,
        news_sentiment=sentiment_label,
        earnings_days_away=days_to_earnings,
    )

    if not edge_score.get("qualified"):
        reason = edge_score.get("disqualification_reason") or "Edge score below minimum"
        return None, {"reason": reason, "category": "EDGE_SCORE"}, tech

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
        return None, {"reason": validation.get("rejection_reason", "Validation failed"), "category": validation.get("rejection_category", "VALIDATION")}, tech

    warnings = list(validation.get("warnings", []))
    if sentiment_label == "NEGATIVE":
        warnings.append("Negative news sentiment - monitor closely")

    news_status = "CLEAR" if sentiment_label in ("POSITIVE", "NEUTRAL") else sentiment_label

    profit_target_price = round(metrics.get("credit_per_share", 0) * (1 - config.TARGET_PROFIT_PCT), 2)

    trade_type = validation.get("trade_type", "standard_premium")

    trade = {
        "ticker": ticker,
        "strategy": strategy,
        "current_price": round(current_price, 2),
        "short_strike": short_put.get("strike"),
        "long_strike": long_strike,
        "expiration": short_put.get("expiration"),
        "dte": short_put.get("dte"),
        "credit_per_share": metrics.get("credit_per_share"),
        "credit_usd": metrics.get("credit_usd"),
        "max_loss_usd": metrics.get("max_loss_usd"),
        "contracts_allowed": metrics.get("contracts_allowed"),
        "oversized_position": (
            metrics.get("contracts_allowed") == config.MIN_CONTRACTS
            and metrics.get("max_loss_usd", 0) > config.MAX_RISK_PER_TRADE_USD
        ),
        "profit_target_usd": metrics.get("profit_target_usd"),
        "profit_target_price": profit_target_price,
        "stop_loss_close_price": metrics.get("stop_loss_close_price"),
        "delta": short_put.get("delta"),
        "iv_rank": tech.get("iv_rank"),
        "strike_distance_usd": metrics.get("strike_distance_usd"),
        "strike_distance_pct": metrics.get("strike_distance_pct"),
        "true_pop": true_pop,
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


def run_scan(session_type: str) -> None:
    ts = now_et()
    check_session_window(session_type, ts)

    tickers = [w["ticker"].upper() for w in config.WATCHLIST]
    print(f"[DEBUG] Ticker list after config load: {tickers}")

    logger.info(f"Starting {session_type} scan for {len(tickers)} tickers")

    market_context = build_market_context()
    sentiment_map = news.analyze_all_tickers(tickers)

    qualified_trades: List[Dict] = []
    avoided: List[Dict] = []
    tech_map: Dict[str, Dict] = {}
    errors: List[str] = []


    print(f"[DEBUG] Tickers to screen: {tickers}")
    for ticker in tickers:
        print(f"[DEBUG] Screening ticker: {ticker}")
        try:
            trade, avoid, tech = screen_ticker(ticker, sentiment_map)
            tech_map[ticker] = tech
            if trade:
                print(f"[DEBUG] Qualified trade for {ticker}: {trade}")
                qualified_trades.append(trade)
            else:
                print(f"[DEBUG] Avoided {ticker}: {avoid}")
                avoided.append({
                    "ticker": ticker,
                    "reason": avoid.get("reason") if avoid else "Unknown",
                    "category": avoid.get("category") if avoid else "UNKNOWN",
                })
        except Exception as e:
            print(f"[DEBUG] Exception screening {ticker}: {e}")
            errors.append(f"{ticker}: {e}")
            avoided.append({"ticker": ticker, "reason": str(e), "category": "ERROR"})

    print(f"[DEBUG] Qualified trades: {qualified_trades}")
    print(f"[DEBUG] Avoided tickers: {avoided}")
    print(f"[DEBUG] Errors: {errors}")


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

    print("[DEBUG] Calling renderer.render()...")
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
    print(f"[DEBUG] renderer.render() returned: {output_path}")

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
    }
    append_scan_log(log_dir, scan_entry)

    logger.info(f"Scan complete. Tip sheet saved to {output_path}")

    # ── VEGA: Push scan results to JARVIS tower ──────────────────────────
    if VEGA_INGEST_ENABLED:
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

    if config.EMAIL_ENABLED:
        pass  # Email sending not configured
