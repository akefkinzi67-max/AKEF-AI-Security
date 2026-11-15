"""Read-only access to an operator-configured Frigate instance."""
from datetime import datetime, timezone
import json
import math
import re
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(value):
    parts = urlsplit(value)
    if (parts.scheme not in ("https", "http") or not parts.hostname
        or parts.username or parts.password or parts.path not in ("", "/")
        or parts.query or parts.fragment):
        raise ValueError("Configure a valid Frigate origin.")
    return value.rstrip("/")


def fetch_events(base, token):
    headers = {"Accept": "application/json"}
    if token:
        if urlsplit(base).scheme != "https":
            raise ValueError("Frigate credentials require HTTPS.")
        headers["Cookie"] = "frigate_token=" + token
    endpoint = origin(base) + "/api/events?limit=100"
    try:
        with build_opener(NoRedirect()).open(Request(endpoint, headers=headers), timeout=5) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError
        events = json.loads(payload)
        if not isinstance(events, list) or len(events) > 100:
            raise ValueError
        validated = []
        for event in events:
            key, camera, label = (event[field] for field in ("id", "camera", "label"))
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", key) or key in (".", ".."):
                raise ValueError
            if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", v)
                       for v in (camera, label)):
                raise ValueError
            start = float(event["start_time"])
            end = event["end_time"]
            if not math.isfinite(start) or (end is not None and
                                           (not math.isfinite(float(end)) or float(end) < start)):
                raise ValueError
            stamp = datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="microseconds")
            zones = event["zones"]
            if not isinstance(zones, list) or not all(isinstance(z, str) for z in zones):
                raise ValueError
            if event.get("false_positive") is not True:
                validated.append((key, camera, label, stamp, ", ".join(zones)[:500],
                                  int(event["has_clip"] is True)))
        return validated
    except (URLError, TimeoutError, ValueError, KeyError, TypeError, OverflowError, OSError):
        raise ValueError("Could not read a valid event feed from the configured Frigate server.") from None


def recording_url(base, camera, occurred_at):
    stamp = int(datetime.fromisoformat(occurred_at).timestamp())
    return f"{origin(base)}/api/{quote(camera, safe='')}/start/{stamp-20}/end/{stamp+20}/clip.mp4"
