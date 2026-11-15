"""Durable delivery attempts for an operator-configured HTTPS webhook."""
import json
from datetime import datetime, timezone
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from cameras import NoRedirect


def deliver(db, url, token, station_id, now=None):
    now = time.time() if now is None else now
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.fragment or any(not 32 <= ord(char) < 127 for char in token)):
        raise ValueError("Configure a valid HTTPS notification endpoint.")
    if not station_id or not station_id.isascii() or not station_id.isalnum():
        raise ValueError("Configure an alphanumeric notification station ID.")

    with db:
        db.execute("BEGIN IMMEDIATE")
        cutoff = datetime.fromtimestamp(now - 600, timezone.utc).isoformat(timespec="microseconds")
        db.execute("""INSERT OR IGNORE INTO alert_delivery(incident)
            SELECT id FROM incidents WHERE opened>=? AND
            category IN ('Camera offline','Frigate unavailable','Restricted zone')""", (cutoff,))
        row = db.execute("""SELECT i.id,i.camera,i.category,i.opened,a.attempts FROM alert_delivery a
            JOIN incidents i ON i.id=a.incident WHERE a.delivered IS NULL AND a.next_attempt<=? AND a.attempts<8
            ORDER BY a.next_attempt,i.id LIMIT 1""", (now,)).fetchone()
        if row is None:
            return None
        attempt = row["attempts"] + 1
        next_attempt = now + min(3600, 30 * 2 ** (attempt - 1))
        # Lease the record before the network call. Another worker cannot send during this attempt.
        db.execute("UPDATE alert_delivery SET attempts=?,next_attempt=? WHERE incident=?",
                   (attempt, next_attempt, row["id"]))

    key = f"{station_id}-incident-{row['id']}"
    body = json.dumps({"alert_id": key, "category": row["category"], "camera": row["camera"],
                       "opened_at": row["opened"]}).encode()
    headers = {"Content-Type": "application/json", "Idempotency-Key": key}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    error = None
    try:
        with build_opener(NoRedirect()).open(Request(url, data=body, headers=headers), timeout=5) as response:
            if not 200 <= response.status < 300:
                error = "Receiver did not accept the alert."
    except HTTPError as exc:
        error = f"Receiver returned HTTP {exc.code}."
        exc.close()
    except (URLError, TimeoutError, OSError, ValueError):
        error = "Receiver could not be reached."
    with db:
        db.execute("UPDATE alert_delivery SET delivered=?,error=? WHERE incident=? AND attempts=?",
                   (now if error is None else None, error, row["id"], attempt))
    return error is None
