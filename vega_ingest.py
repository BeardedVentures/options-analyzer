"""
vega_ingest.py — POST scan results to JARVIS /vega/ingest endpoint.

Called at the end of run_scan() in main.py. Non-blocking — if the tower
is unreachable, the scan still completes and logs locally. JARVIS integration
is additive, never a dependency for the core scan to function.

Usage (in main.py, at end of run_scan):
    from vega_ingest import post_to_jarvis
    post_to_jarvis(scan_entry, session_type, market_context, tipsheet_html)
"""

import logging
import os
import json
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# JARVIS tower address — set via .env or GitHub Secret JARVIS_HOST
JARVIS_HOST = os.environ.get("JARVIS_HOST", "http://192.168.0.222:8000")
INGEST_ENDPOINT = f"{JARVIS_HOST}/vega/ingest"
TIMEOUT_SEC = 10
MAX_RETRIES = 2


def post_to_jarvis(
    scan_entry: Dict,
    session_type: str,
    market_context: Optional[Dict] = None,
    tipsheet_html: Optional[str] = None,
) -> bool:
    """
    POST scan results to JARVIS /vega/ingest.

    Args:
        scan_entry: the dict appended to scan_log.json (from main.py)
        session_type: 'morning' or 'close'
        market_context: dict from build_market_context() (vix, spy, bias)
        tipsheet_html: rendered HTML tipsheet content (optional, can be large)

    Returns:
        True if successfully ingested, False on any failure.
    """
    if not JARVIS_HOST:
        logger.debug("[vega_ingest] JARVIS_HOST not set — skipping ingest")
        return False

    # Build payload matching IngestPayload schema in vega_router.py
    payload = {
        "timestamp": scan_entry.get("timestamp"),
        "session_type": session_type,
        "tickers_scanned": scan_entry.get("tickers_scanned", []),
        "qualified_trades": _enrich_qualified_trades(scan_entry.get("qualified_trades", [])),
        "rejected_trades": scan_entry.get("rejected_trades", []),
        "shadow_run": scan_entry.get("shadow_run"),
        "shadow_evaluations": scan_entry.get("shadow_evaluations", []),
        "api_calls": scan_entry.get("api_calls", []),
        "tipsheet_file": scan_entry.get("tipsheet_file"),
        "tipsheet_html": tipsheet_html,
        "account_balance": scan_entry.get("account_balance", 500.0),
        "errors": scan_entry.get("errors", []),
        "triggered_by": "github_actions",
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
    }

    # Add market context if available
    if market_context:
        vix = market_context.get("vix", {})
        spy = market_context.get("spy", {})
        payload.update({
            "vix_level": vix.get("current"),
            "vix_label": vix.get("label"),
            "market_bias": market_context.get("bias"),
            "spy_price": spy.get("price"),
            "spy_change_pct": spy.get("day_change_pct"),
        })

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                INGEST_ENDPOINT,
                json=payload,
                timeout=TIMEOUT_SEC,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info(
                f"[vega_ingest] Ingested to JARVIS: scan_id={result.get('scan_id')} "
                f"qualified={result.get('qualified_count')} "
                f"attempt={attempt}"
            )
            return True

        except requests.exceptions.ConnectionError:
            logger.warning(
                f"[vega_ingest] Cannot reach JARVIS tower at {JARVIS_HOST} "
                f"(attempt {attempt}/{MAX_RETRIES}). Scan saved locally."
            )
        except requests.exceptions.Timeout:
            logger.warning(f"[vega_ingest] Timeout reaching JARVIS (attempt {attempt}/{MAX_RETRIES})")
        except requests.exceptions.HTTPError as e:
            logger.error(f"[vega_ingest] JARVIS returned error: {e} — {resp.text[:200]}")
            return False  # Don't retry on HTTP errors (4xx/5xx)
        except Exception as e:
            logger.error(f"[vega_ingest] Unexpected error: {e}")
            return False

        if attempt < MAX_RETRIES:
            time.sleep(2)

    return False


def _enrich_qualified_trades(trades: List[Dict]) -> List[Dict]:
    """
    The scan_log.json stores a lightweight version of qualified trades
    (just ticker + edge_score for log size). This function is a no-op
    for that format — the full trade dict is passed when available.
    """
    return trades
