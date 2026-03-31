"""
output/emailer.py — Send the rendered tip sheet HTML by email.

Uses Python's built-in smtplib (no extra dependencies beyond requirements.txt).
Configured entirely via environment variables — see .env.example.

Gmail users: generate an App Password at myaccount.google.com → Security.
Call send_tipsheet() after renderer.render() in main.py.
"""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def send_tipsheet(
    html_path: Path,
    session_type: str,
    scan_timestamp: datetime,
) -> bool:
    """
    Send the tip sheet HTML file as an inline HTML email.

    Args:
        html_path:       Path to the rendered HTML file.
        session_type:    "morning" or "close".
        scan_timestamp:  Datetime of the scan (used in subject line).

    Returns:
        True if sent successfully, False on any error.
    """
    if not config.EMAIL_ENABLED:
        logger.debug("[emailer] Email not configured — skipping send.")
        return False

    if not html_path.exists():
        logger.warning(f"[emailer] Tip sheet not found at {html_path} — cannot send.")
        return False

    try:
        html_body = html_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"[emailer] Could not read tip sheet: {e}")
        return False

    date_str = scan_timestamp.strftime("%b %d, %Y")
    session_label = "Morning" if session_type.lower() == "morning" else "Close"
    subject = f"Options Intelligence — {session_label} Tip Sheet {date_str}"

    plain_text = (
        f"Options Intelligence {session_label} Tip Sheet — {date_str}\n\n"
        "This email contains an HTML tip sheet. Please view it in an HTML-capable email client.\n\n"
        + config.DISCLAIMER
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_USER
    msg["To"] = ", ".join(config.EMAIL_RECIPIENTS)
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(config.EMAIL_USER, config.EMAIL_PASSWORD)
            smtp.sendmail(
                from_addr=config.EMAIL_USER,
                to_addrs=config.EMAIL_RECIPIENTS,
                msg=msg.as_string(),
            )
        logger.info(
            f"[emailer] Tip sheet sent to {len(config.EMAIL_RECIPIENTS)} recipient(s): "
            f"{', '.join(config.EMAIL_RECIPIENTS)}"
        )
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "[emailer] SMTP authentication failed. "
            "For Gmail, use an App Password — not your account password. "
            "See .env.example for setup instructions."
        )
        return False
    except smtplib.SMTPException as e:
        logger.error(f"[emailer] SMTP error: {e}")
        return False
    except Exception as e:
        logger.error(f"[emailer] Unexpected email error: {e}")
        return False
