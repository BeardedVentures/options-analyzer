"""
send_vega_email.py — BeardedVentures VEGA Social Content Email Delivery

Fetches the latest generated content package from JARVIS and emails all 3
social media posts to beardedventurestx@gmail.com as a formatted HTML email.

This is the Phase 1 social delivery mechanism — Josh reviews in email,
copies to social platforms. Buffer/API automation is Phase 2.

USAGE:
    # From options_intelligence/ on any machine with JARVIS access:
    python send_vega_email.py

    # Or from JARVIS directly:
    python3 /path/to/options_intelligence/send_vega_email.py

ENVIRONMENT:
    Set these in your .env or shell before running:
    EMAIL_SMTP_HOST   (default: smtp.gmail.com)
    EMAIL_SMTP_PORT   (default: 587)
    EMAIL_USER        (default: beardedventurestx@gmail.com)
    EMAIL_PASSWORD    (your Gmail App Password — dyeq pfin vufl qajh)
    EMAIL_RECIPIENTS  (default: beardedventurestx@gmail.com)
    JARVIS_HOST       (default: http://192.168.0.222:8000)

CRON (runs after each scan, 15 min after GitHub Actions triggers):
    # Morning: 8:05 CT (9:05 ET) — after the 8:50 CT / 9:50 ET scan
    10 9 * * 1-5  cd /path/to/options_intelligence && python3 send_vega_email.py >> logs/email.log 2>&1
    # Close:  15:25 CT (4:25 ET) — after the 2:10 CT / 3:10 ET scan
    25 15 * * 1-5  cd /path/to/options_intelligence && python3 send_vega_email.py >> logs/email.log 2>&1
"""

import json
import logging
import os
import smtplib
import sys
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("EMAIL_USER", "beardedventurestx@gmail.com")
SMTP_PASS = os.environ.get("EMAIL_PASSWORD", "")
RECIPIENTS = os.environ.get("EMAIL_RECIPIENTS", "beardedventurestx@gmail.com").split(",")
JARVIS_HOST = os.environ.get("JARVIS_HOST", "http://192.168.0.222:8000")

# ── Fetch Content from JARVIS ─────────────────────────────────────────────────

def fetch_latest_content() -> dict:
    """GET /vega/content/latest from JARVIS."""
    url = f"{JARVIS_HOST.rstrip('/')}/vega/content/latest"
    logger.info(f"Fetching content from {url} ...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data


# ── HTML Email Builder ────────────────────────────────────────────────────────

def _platform_section(post: dict, platform: str, label: str) -> str:
    content = post.get("platforms", {}).get(platform, "")
    if not content:
        return ""
    char_note = f" <span style='color:#888;font-size:12px;'>({post.get('char_count_twitter',0)} chars)</span>" if platform == "twitter" else ""
    return f"""
        <div style="margin-bottom:16px;">
            <div style="font-weight:bold;color:#555;font-size:13px;text-transform:uppercase;
                        letter-spacing:1px;margin-bottom:6px;">{label}{char_note}</div>
            <div style="background:#f8f9fa;border-left:3px solid #0070f3;padding:12px 16px;
                        border-radius:4px;font-family:monospace;font-size:13px;
                        white-space:pre-wrap;line-height:1.5;">{content}</div>
        </div>
    """


def _post_card(i: int, post: dict) -> str:
    post_type = post.get("post_type", "").replace("_", " ").title()
    ticker = post.get("ticker", "")
    topic = post.get("topic", "")
    badge_text = ticker or topic or post_type
    badge_colors = {
        "market_context": "#0070f3",
        "setup_highlight": "#00a651",
        "educational": "#f5a623",
    }
    color = badge_colors.get(post.get("post_type", ""), "#555")

    twitter_section = _platform_section(post, "twitter", "Twitter / X")
    linkedin_section = _platform_section(post, "linkedin", "LinkedIn")

    return f"""
    <div style="border:1px solid #e5e7eb;border-radius:8px;padding:20px;
                margin-bottom:24px;background:#fff;">
        <div style="display:flex;align-items:center;margin-bottom:16px;gap:10px;">
            <span style="background:{color};color:white;padding:4px 10px;
                         border-radius:12px;font-size:12px;font-weight:bold;">
                POST {i}
            </span>
            <span style="font-size:16px;font-weight:600;color:#111;">{post_type}</span>
            {"<span style='color:#888;font-size:13px;'>· " + badge_text + "</span>" if badge_text != post_type else ""}
        </div>
        {twitter_section}
        {linkedin_section}
    </div>
    """


def build_email_html(content: dict) -> str:
    """Build the full HTML email body."""
    posts = content.get("posts", [])
    session_type = content.get("session_type", "morning").upper()
    date_str = content.get("date_str", datetime.now().strftime("%Y-%m-%d"))
    scan_summary = content.get("scan_summary", {})
    post_count = content.get("post_count", 0)
    generated_at = content.get("generated_at", "")[:16].replace("T", " ") if content.get("generated_at") else ""

    post_cards = "".join(_post_card(i + 1, p) for i, p in enumerate(posts))

    summary_line = (
        f"{scan_summary.get('tickers_scanned', '?')} tickers scanned · "
        f"{scan_summary.get('qualified_count', 0)} qualified · "
        f"{scan_summary.get('rejected_count', 0)} rejected"
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#f3f4f6;margin:0;padding:24px;">
  <div style="max-width:680px;margin:0 auto;">

    <!-- Header -->
    <div style="background:#111;color:#fff;padding:24px;border-radius:8px 8px 0 0;">
      <div style="font-size:22px;font-weight:700;">🔥 VEGA {session_type} Scan</div>
      <div style="font-size:15px;color:#aaa;margin-top:4px;">{date_str} · {summary_line}</div>
      <div style="font-size:12px;color:#666;margin-top:8px;">Generated {generated_at} CT</div>
    </div>

    <!-- Body -->
    <div style="background:#f3f4f6;padding:24px;">

      <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;
                  padding:12px 16px;margin-bottom:20px;font-size:13px;color:#856404;">
        <strong>Review before posting.</strong> Copy each post to the appropriate platform.
        All content includes the required disclaimer. Do NOT remove it.
      </div>

      {post_cards if post_cards else '<p style="color:#888;">No posts generated.</p>'}

    </div>

    <!-- Footer -->
    <div style="background:#e5e7eb;padding:16px;border-radius:0 0 8px 8px;
                font-size:12px;color:#6b7280;text-align:center;">
      Sent by VEGA · BeardedVentures · JARVIS tower · {JARVIS_HOST}
    </div>

  </div>
</body>
</html>"""


# ── Send Email ────────────────────────────────────────────────────────────────

def send_email(html: str, subject: str) -> None:
    if not SMTP_PASS:
        logger.error("EMAIL_PASSWORD not set — cannot send email.")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(html, "html"))

    logger.info(f"Connecting to {SMTP_HOST}:{SMTP_PORT} ...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, RECIPIENTS, msg.as_string())

    logger.info(f"✅ Email sent to: {', '.join(RECIPIENTS)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Fetch latest content from JARVIS
    try:
        content = fetch_latest_content()
    except Exception as e:
        logger.error(f"Failed to fetch content from JARVIS: {e}")
        sys.exit(1)

    if not content.get("posts"):
        logger.warning("No posts in latest content package — nothing to send.")
        if content.get("message"):
            logger.info(f"JARVIS says: {content['message']}")
        sys.exit(0)

    # Build email
    session_type = content.get("session_type", "morning").upper()
    date_str = content.get("date_str", datetime.now().strftime("%Y-%m-%d"))
    qualified = content.get("scan_summary", {}).get("qualified_count", 0)
    flag = "✅" if qualified > 0 else "📊"

    subject = f"{flag} VEGA {session_type} Scan Posts · {date_str} · {qualified} qualified"
    html = build_email_html(content)

    logger.info(f"Subject: {subject}")
    logger.info(f"Posts: {content.get('post_count', 0)}")

    # Send
    try:
        send_email(html, subject)
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
