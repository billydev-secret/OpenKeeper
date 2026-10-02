#!/usr/bin/env python3
"""Watchdog — DMs the owner on Discord when the bot's systemd unit goes down.

Runs as its own systemd service (see deploy/dungeon-keeper-watchdog.service)
so it survives bot crashes. Deliberately stdlib-only and run with the system
python3, independent of the repo venv: a broken deploy can't kill it.

Reads the repo .env for:
    BOT_ENV                  "prod" (default) or "dev" — picks the token
    DISCORD_TOKEN_PROD/_DEV  bot token used to send the DM (REST only,
                             works even while the bot process is down)
    WATCHDOG_USER_ID         Discord user to DM (falls back to SUPPORT_USER_ID)
    WATCHDOG_UNIT            systemd unit to watch (default dungeon-keeper.service)
    WATCHDOG_POLL_SECONDS    poll interval (default 30)

Alert logic: a non-active state must survive a 10 s recheck before it DMs
(so a fast auto-restart doesn't page), then one DM per outage and one on
recovery — never a DM per poll once the alert has actually been delivered.
A DM that fails to send does not count as delivered and is retried on the
next poll: latching on a send that never arrived is how an outage goes
unreported.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://discord.com/api/v10"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watchdog")


def parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #")[0].strip().strip("'\"")
        env[key.strip()] = value
    return env


def unit_state(unit: str) -> str:
    """Return the systemd ActiveState: active/inactive/failed/activating/..."""
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip() or "unknown"


# Retries for the DM path. This is the only channel through which an outage
# gets reported, so a 429 dropped here means the bot is down and nobody hears
# about it — the failure mode the whole script exists to prevent.
MAX_ATTEMPTS = 6


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """Seconds Discord asked us to wait. A deliberate copy of discord_rest.

    The other standalone API tools in scripts/ share
    ``scripts/discord_rest.retry_after``; this one does not, and that is the
    point. The watchdog exists to report an outage, which is exactly when the
    tree may be mid-deploy or broken, so it takes no import at all beyond the
    stdlib — not from src/, not from a sibling. The duplication is the price
    of that guarantee, and ``tests/tooling/test_discord_rest.py`` pins the two to the
    same answers so they cannot drift apart unnoticed.

    Header first, then the JSON body. Edge-level 429s (Cloudflare) arrive as
    an HTML page with no retry_after anywhere, so the body parse has to
    tolerate that rather than explode inside an error handler — and when
    nothing told us a number, wait long rather than hammer the edge.
    """
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if raw is None:
        try:
            raw = json.loads(exc.read() or b"{}").get("retry_after")
        except (ValueError, AttributeError):  # HTML page, or JSON that isn't an object
            raw = None
        if raw is None:
            return 5.0  # nothing told us; Cloudflare's edge window is ~10s
    try:
        return float(raw) + 0.3
    except (TypeError, ValueError):
        return 1.0


def _post(token: str, path: str, payload: dict) -> dict:
    """POST to the Discord API, obeying 429 Retry-After and backing off on 5xx."""
    for attempt in range(MAX_ATTEMPTS):
        req = urllib.request.Request(
            f"{API}{path}",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "dungeon-keeper-watchdog (self-hosted, 1.0)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                delay = _retry_after(exc)
                log.warning("Discord rate-limited POST %s; waiting %.1fs", path, delay)
                time.sleep(delay)
                continue
            if exc.code >= 500 and attempt < MAX_ATTEMPTS - 1:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError(f"gave up after repeated rate limits on POST {path}")


def send_dm(token: str, user_id: str, content: str) -> bool:
    try:
        channel = _post(token, "/users/@me/channels", {"recipient_id": user_id})
        _post(token, f"/channels/{channel['id']}/messages", {"content": content})
        return True
    except urllib.error.HTTPError as exc:
        log.exception("Discord API rejected DM (%s): %s", exc.code, exc.read().decode()[:300])
    except Exception:
        log.exception("Failed to send DM")
    return False


def main() -> int:
    env = parse_env(ROOT / ".env")
    bot_env = env.get("BOT_ENV", "prod").lower()
    token = env.get(f"DISCORD_TOKEN_{bot_env.upper()}", "")
    user_id = env.get("WATCHDOG_USER_ID") or env.get("SUPPORT_USER_ID", "")
    unit = env.get("WATCHDOG_UNIT", "dungeon-keeper.service")
    poll = int(env.get("WATCHDOG_POLL_SECONDS", "30"))

    if not token or not user_id:
        log.error(
            "Missing DISCORD_TOKEN_%s or WATCHDOG_USER_ID/SUPPORT_USER_ID in %s",
            bot_env.upper(),
            ROOT / ".env",
        )
        return 1

    if "--test" in sys.argv[1:]:
        ok = send_dm(token, user_id, f"🧪 Watchdog test — I can reach you. Watching **{unit}**.")
        log.info("Test DM %s", "sent" if ok else "FAILED")
        return 0 if ok else 1

    log.info("Watching %s every %ss; will DM user %s", unit, poll, user_id)
    down_since: float | None = None
    alerted = False

    while True:
        state = unit_state(unit)

        if state == "active":
            if alerted:
                mins = (time.time() - (down_since or time.time())) / 60
                send_dm(
                    token,
                    user_id,
                    f"🟢 **{unit}** is back up (was down ~{mins:.0f} min).",
                )
                log.info("Recovered; DM sent")
            down_since = None
            alerted = False
        else:
            if down_since is None:
                down_since = time.time()
                time.sleep(10)  # debounce: fast auto-restarts shouldn't page
                continue
            if not alerted:
                sent = send_dm(
                    token,
                    user_id,
                    f"🔴 **{unit}** is **{state}** and has not come back after 10s. "
                    f"Check: `systemctl status {unit}` / `journalctl -u {unit} -n 50`",
                )
                if sent:
                    alerted = True
                    log.warning("Unit %s is %s; DM sent", unit, state)
                else:
                    # Don't latch alerted on a failed send (e.g. retries in
                    # _post exhausted during a Discord outage) — that would
                    # silence every future attempt for the rest of this
                    # outage, exactly the "nobody was told" failure mode the
                    # retry logic above exists to close. Leaving it False
                    # means the next poll tries again.
                    log.error("Unit %s is %s; DM FAILED — will retry next poll", unit, state)

        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
