#!/usr/bin/env python3
"""
Atomiq Server Monitor
Polls key endpoints every CHECK_INTERVAL seconds.
Sends Discord/Telegram/email alerts on failure or degraded performance.
"""

import os
import time
import json
import logging
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

# ── Configuration (override via environment variables) ────────────────────────

BASE_URL          = os.environ.get("ATOMIQ_BASE_URL", "http://host.docker.internal:8080")
CHECK_INTERVAL    = int(os.environ.get("CHECK_INTERVAL", "60"))        # seconds
ALERT_COOLDOWN    = int(os.environ.get("ALERT_COOLDOWN", "300"))       # seconds between repeat alerts per check
REQUEST_TIMEOUT   = int(os.environ.get("REQUEST_TIMEOUT", "10"))       # seconds per HTTP request

# Thresholds
MAX_PROCESSOR_STALE_SECS  = int(os.environ.get("MAX_PROCESSOR_STALE_SECS", "120"))   # alert if processor hasn't heartbeated
MAX_PENDING_SETTLEMENTS   = int(os.environ.get("MAX_PENDING_SETTLEMENTS", "50"))     # alert if queue backs up
MAX_FAILED_SETTLEMENTS    = int(os.environ.get("MAX_FAILED_SETTLEMENTS", "5"))       # alert on any stuck failures
BANKROLL_WARN_THRESHOLD   = float(os.environ.get("BANKROLL_WARN_THRESHOLD", "10.0")) # SOL — warn if bankroll runs low

# Notification channels (set at least one)
DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
# Home Assistant: long-lived token + the HA base URL
HA_BASE_URL        = os.environ.get("HA_BASE_URL", "")          # e.g. http://homeassistant:8123
HA_TOKEN           = os.environ.get("HA_TOKEN", "")             # long-lived access token
HA_NOTIFY_SERVICE  = os.environ.get("HA_NOTIFY_SERVICE", "notify.persistent_notification")
# Email
EMAIL_RECIPIENTS   = [r.strip() for r in os.environ.get("EMAIL_RECIPIENTS", "").split(",") if r.strip()]
SMTP_HOST          = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT          = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER          = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD      = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM         = os.environ.get("EMAIL_FROM", SMTP_USER)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")

# ── Alert state (cooldown tracking) ──────────────────────────────────────────

last_alerted: dict[str, float] = {}

def should_alert(key: str) -> bool:
    now = time.time()
    if now - last_alerted.get(key, 0) >= ALERT_COOLDOWN:
        last_alerted[key] = now
        return True
    return False

def clear_alert(key: str):
    """Call when a previously failing check recovers."""
    last_alerted.pop(key, None)

# ── Notification senders ─────────────────────────────────────────────────────

def send_discord(message: str):
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
    except Exception as e:
        log.warning("Discord send failed: %s", e)

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log.warning("Telegram send failed: %s", e)

def send_homeassistant(title: str, message: str):
    if not HA_BASE_URL or not HA_TOKEN:
        return
    try:
        service_path = HA_NOTIFY_SERVICE.replace(".", "/", 1)  # notify.X -> notify/X
        url = f"{HA_BASE_URL.rstrip('/')}/api/services/{service_path}"
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        requests.post(url, json={"title": title, "message": message}, headers=headers, timeout=10)
    except Exception as e:
        log.warning("Home Assistant send failed: %s", e)

def send_email(subject: str, body: str):
    if not EMAIL_RECIPIENTS or not SMTP_USER or not SMTP_PASSWORD:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = ", ".join(EMAIL_RECIPIENTS)
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_RECIPIENTS, msg.as_string())
    except Exception as e:
        log.warning("Email send failed: %s", e)

def alert(key: str, message: str, level: str = "🔴"):
    if not should_alert(key):
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    full = f"{level} **[Atomiq Monitor]** `{ts}`\n{message}"
    log.warning("ALERT [%s]: %s", key, message)
    send_discord(full)
    send_telegram(full)
    send_homeassistant("Atomiq Monitor Alert", full)
    send_email(f"[Atomiq Monitor] Alert: {key}", full)

def recover(key: str, message: str):
    if key not in last_alerted:
        return  # Was never in alert state — no need to notify
    clear_alert(key)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    full = f"✅ **[Atomiq Monitor]** `{ts}`\n{message}"
    log.info("RECOVERED [%s]: %s", key, message)
    send_discord(full)
    send_telegram(full)
    send_homeassistant("Atomiq Monitor Recovered", full)
    send_email(f"[Atomiq Monitor] Recovered: {key}", full)

# ── Individual checks ─────────────────────────────────────────────────────────

def get(path: str) -> dict | None:
    try:
        r = requests.get(f"{BASE_URL}{path}", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug("GET %s failed: %s", path, e)
        return None


def check_api_health():
    """GET /health — basic liveness check."""
    data = get("/health")
    if data is None or data.get("status") != "healthy":
        alert("api.health", f"API health check failed.\nURL: `{BASE_URL}/health`\nResponse: `{data}`")
    else:
        recover("api.health", "API is healthy again.")


def check_blockchain_status():
    """GET /status — block production / sync state."""
    data = get("/status")
    if data is None:
        alert("blockchain.status", f"Blockchain status endpoint unreachable.\nURL: `{BASE_URL}/status`")
        return
    recover("blockchain.status", "Blockchain status endpoint is responding again.")

    catching_up = data.get("sync_info", {}).get("catching_up", False)
    if catching_up:
        alert("blockchain.sync", "Blockchain node is catching up / behind.")
    else:
        recover("blockchain.sync", "Blockchain node is in sync.")


def check_settlement_health():
    """GET /api/settlement/health — processor liveness + queue depth."""
    data = get("/api/settlement/health")
    if data is None:
        alert("settlement.health", f"Settlement health endpoint unreachable.\nURL: `{BASE_URL}/api/settlement/health`")
        return
    recover("settlement.health", "Settlement health endpoint is responding again.")

    # Processor heartbeat staleness
    stale_secs = data.get("processor_last_seen_secs", 9999)
    if stale_secs > MAX_PROCESSOR_STALE_SECS:
        alert(
            "settlement.processor",
            f"Settlement processor has not heartbeated for **{stale_secs}s** (threshold: {MAX_PROCESSOR_STALE_SECS}s).\n"
            f"The processor may be down.",
        )
    else:
        recover("settlement.processor", f"Settlement processor is heartbeating normally ({stale_secs}s ago).")

    # Stuck / has_stuck flag
    if data.get("has_stuck"):
        alert("settlement.stuck", "Settlement queue has **stuck** transactions that cannot be processed.")
    else:
        recover("settlement.stuck", "No stuck settlements detected.")

    # Pending queue depth
    pending = data.get("pending_count", 0)
    if pending > MAX_PENDING_SETTLEMENTS:
        alert(
            "settlement.pending",
            f"Settlement pending queue is backing up: **{pending}** pending (threshold: {MAX_PENDING_SETTLEMENTS}).",
        )
    else:
        recover("settlement.pending", f"Settlement pending queue is normal ({pending} pending).")

    # Failed count
    failed = data.get("failed_count", 0)
    if failed > MAX_FAILED_SETTLEMENTS:
        alert(
            "settlement.failed",
            f"Settlement has **{failed}** failed transactions (threshold: {MAX_FAILED_SETTLEMENTS}).",
        )
    else:
        recover("settlement.failed", f"Settlement failed count is normal ({failed} failed).")


def check_casino_stats():
    """GET /api/casino/stats — bankroll guard."""
    data = get("/api/casino/stats")
    if data is None:
        alert("casino.stats", f"Casino stats endpoint unreachable.\nURL: `{BASE_URL}/api/casino/stats`")
        return
    recover("casino.stats", "Casino stats endpoint responding again.")

    bankroll = data.get("bankroll", 9999)
    if bankroll < BANKROLL_WARN_THRESHOLD:
        alert(
            "casino.bankroll",
            f"⚠️ Casino bankroll is critically low: **{bankroll:.4f} SOL** (threshold: {BANKROLL_WARN_THRESHOLD} SOL).\n"
            f"Top up the house wallet to avoid failed payouts.",
            level="🟡",
        )
    else:
        recover("casino.bankroll", f"Casino bankroll is healthy ({bankroll:.4f} SOL).")


def check_metrics():
    """GET /metrics — Prometheus endpoint sanity check."""
    try:
        r = requests.get(f"{BASE_URL}/metrics", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        recover("metrics.endpoint", "Metrics endpoint is responding.")
    except Exception as e:
        alert("metrics.endpoint", f"Prometheus metrics endpoint is unreachable.\nError: `{e}`")


# ── Main loop ─────────────────────────────────────────────────────────────────

CHECKS = [
    ("API health",           check_api_health),
    ("Blockchain status",    check_blockchain_status),
    ("Settlement health",    check_settlement_health),
    ("Casino stats/bankroll",check_casino_stats),
    ("Metrics endpoint",     check_metrics),
]

def run_checks():
    log.info("── Running %d checks against %s ──", len(CHECKS), BASE_URL)
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as e:
            log.error("Unhandled error in check '%s': %s", name, e)

def main():
    log.info("Atomiq Monitor starting (interval=%ds, base=%s)", CHECK_INTERVAL, BASE_URL)
    if not DISCORD_WEBHOOK and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) and not (HA_BASE_URL and HA_TOKEN) and not (EMAIL_RECIPIENTS and SMTP_USER and SMTP_PASSWORD):
        log.warning("No notification channel configured — alerts will only appear in logs.")

    # Run immediately on start, then on interval
    run_checks()
    while True:
        time.sleep(CHECK_INTERVAL)
        run_checks()


if __name__ == "__main__":
    main()
