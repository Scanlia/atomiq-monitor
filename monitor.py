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
REQUEST_TIMEOUT   = int(os.environ.get("REQUEST_TIMEOUT", "10"))       # seconds per HTTP request

# Thresholds
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

# ── Alert state (exponential backoff, tracks time-down) ──────────────────
#
# Backoff schedule (capped at 1 h): 5 m → 10 m → 20 m → 40 m → 60 m → 60 m …
_BACKOFF_STEPS = [300, 600, 1200, 2400, 3600]  # seconds
_MAX_INTERVAL  = 3600

# Per-key state: {"first": float, "last": float, "step": int}
alert_state: dict[str, dict] = {}

def _fmt_duration(seconds: float) -> str:
    """Return a human-readable duration string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, rem = divmod(m, 60)
    return f"{h}h {rem}m" if rem else f"{h}h"

def should_alert(key: str) -> bool:
    now = time.time()
    state = alert_state.get(key)
    if state is None:
        # First occurrence
        alert_state[key] = {"first": now, "last": now, "step": 0}
        return True
    interval = _BACKOFF_STEPS[min(state["step"], len(_BACKOFF_STEPS) - 1)]
    if now - state["last"] >= interval:
        state["last"] = now
        state["step"] = min(state["step"] + 1, len(_BACKOFF_STEPS) - 1)
        return True
    return False

def time_down(key: str) -> str:
    """Return formatted duration the alert has been active, or empty string."""
    state = alert_state.get(key)
    if state is None:
        return ""
    return _fmt_duration(time.time() - state["first"])

def clear_alert(key: str):
    """Call when a previously failing check recovers."""
    alert_state.pop(key, None)

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
        log.info("Email sent: %s", subject)
    except Exception as e:
        log.warning("Email send failed: %s", e)

# Pending email digests accumulated within a single run_checks() cycle
_pending_alerts: list[str] = []
_pending_recoveries: list[str] = []

def alert(key: str, message: str, level: str = "🔴"):
    if not should_alert(key):
        return
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    td  = time_down(key)
    td_str = f"  ⏱ Down for: **{td}**" if td else ""
    full = f"{level} **[Atomiq Monitor]** `{ts}`\n{message}{td_str}"
    log.warning("ALERT [%s] (down %s): %s", key, td or "<1m", message)
    send_discord(full)
    send_telegram(full)
    send_homeassistant("Atomiq Monitor Alert", full)
    _pending_alerts.append(f"[{key}] (down {td or '<1m'})\n{message}")

def recover(key: str, message: str):
    if key not in alert_state:
        return  # Was never in alert state — no need to notify
    td = time_down(key)
    clear_alert(key)
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    td_str = f"  ⏱ Was down for: **{td}**" if td else ""
    full = f"✅ **[Atomiq Monitor]** `{ts}`\n{message}{td_str}"
    log.info("RECOVERED [%s] (was down %s): %s", key, td or "<1m", message)
    send_discord(full)
    send_telegram(full)
    send_homeassistant("Atomiq Monitor Recovered", full)
    _pending_recoveries.append(f"{message}  (was down {td or '<1m'})")

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
        log.info("OK [api.health]: %s", data.get("status"))
        recover("api.health", "API is healthy again.")


def check_blockchain_status():
    """GET /status — block production / sync state."""
    data = get("/status")
    if data is None:
        alert("blockchain.status", f"Blockchain status endpoint unreachable.\nURL: `{BASE_URL}/status`")
        return
    log.info("OK [blockchain.status]: endpoint reachable")
    recover("blockchain.status", "Blockchain status endpoint is responding again.")

    catching_up = data.get("sync_info", {}).get("catching_up", False)
    if catching_up:
        alert("blockchain.sync", "Blockchain node is catching up / behind.")
    else:
        recover("blockchain.sync", "Blockchain node is in sync.")


def check_settlement_health():
    """GET /api/settlement/health — endpoint reachability only.
    Processor/queue checks removed: settlement processing is now handled
    directly by the blockchain node.
    """
    data = get("/api/settlement/health")
    if data is None:
        alert("settlement.health", f"Settlement health endpoint unreachable.\nURL: `{BASE_URL}/api/settlement/health`")
        return
    log.info("OK [settlement.health]: endpoint reachable")
    recover("settlement.health", "Settlement health endpoint is responding again.")


def check_casino_stats():
    """GET /api/casino/stats — bankroll guard."""
    data = get("/api/casino/stats")
    if data is None:
        alert("casino.stats", f"Casino stats endpoint unreachable.\nURL: `{BASE_URL}/api/casino/stats`")
        return
    log.info("OK [casino.stats]: endpoint reachable")
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
        log.info("OK [metrics.endpoint]: HTTP %s", r.status_code)
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
    global _pending_alerts, _pending_recoveries
    _pending_alerts = []
    _pending_recoveries = []
    log.info("── Running %d checks against %s ──", len(CHECKS), BASE_URL)
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as e:
            log.error("Unhandled error in check '%s': %s", name, e)
    # Send a single digest email if anything fired
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if _pending_alerts:
        subject = f"[Atomiq Monitor] {len(_pending_alerts)} Alert(s) — {ts}"
        body = f"Atomiq Monitor detected {len(_pending_alerts)} issue(s) at {ts}:\n"
        body += "(Repeat notifications: 5m → 10m → 20m → 40m → 60m intervals, capped at 1h)\n\n"
        body += "\n\n".join(f"🔴 {a}" for a in _pending_alerts)
        if _pending_recoveries:
            body += "\n\n── Recoveries ──\n" + "\n".join(f"✅ {r}" for r in _pending_recoveries)
        send_email(subject, body)
    elif _pending_recoveries:
        subject = f"[Atomiq Monitor] {len(_pending_recoveries)} Recovery(s) — {ts}"
        body = f"Atomiq Monitor recoveries at {ts}:\n\n"
        body += "\n".join(f"✅ {r}" for r in _pending_recoveries)
        send_email(subject, body)

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
