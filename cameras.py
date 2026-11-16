"""Read-only access to an operator-configured Frigate instance."""
from datetime import datetime, timezone
import json
import math
import re
import time
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


CLIP_LIMIT = 16 * 1024 * 1024


def fetch(base, token, path, media_type, limit):
    headers = {"Accept": media_type}
    if token:
        if urlsplit(base).scheme != "https" or not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
            raise ValueError("Frigate credentials require HTTPS.")
        headers["Cookie"] = "frigate_token=" + token
    try:
        endpoint = origin(base) + path
        with build_opener(NoRedirect()).open(Request(endpoint, headers=headers), timeout=5) as response:
            payload = response.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("Frigate response exceeds the size limit.")
        return payload
    except (URLError, TimeoutError, OSError):
        raise ValueError("Could not reach the configured Frigate server.") from None


def fetch_stats(base, token):
    try:
        cameras = json.loads(fetch(base, token, "/api/stats", "application/json", 1024 * 1024))["cameras"]
        stats = {}
        for name, status in cameras.items():
            fps = float(status["camera_fps"])
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", name) or not math.isfinite(fps) or fps < 0:
                raise ValueError
            stats[name] = fps
        return stats
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError("Could not read valid camera statistics.") from None


def fetch_events(base, token):
    try:
        events = json.loads(fetch(base, token, "/api/events?limit=100", "application/json", 8 * 1024 * 1024))
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


def fetch_clip(base, token, event_id):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", event_id) or event_id in (".", ".."):
        raise ValueError("Invalid Frigate event ID.")
    clip = fetch(base, token, f"/api/events/{event_id}/clip.mp4", "video/mp4", CLIP_LIMIT)
    if len(clip) < 16 or clip[4:8] != b"ftyp":
        raise ValueError("Frigate did not return an MP4 clip.")
    return clip


def open_live_feed(base, token, camera):
    """Return Frigate's browser-ready MJPEG response for a configured camera."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", camera):
        raise ValueError("Invalid camera name.")
    headers = {"Accept": "multipart/x-mixed-replace"}
    if token:
        if urlsplit(base).scheme != "https" or not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
            raise ValueError("Frigate credentials require HTTPS.")
        headers["Cookie"] = "frigate_token=" + token
    endpoint = f"{origin(base)}/api/{camera}?fps=3&height=360"
    try:
        response = build_opener(NoRedirect()).open(Request(endpoint, headers=headers), timeout=5)
    except (URLError, TimeoutError, OSError):
        raise ValueError("Live camera feed is unavailable.") from None
    boundary = response.headers.get_param("boundary")
    if (response.headers.get_content_type() != "multipart/x-mixed-replace"
        or not boundary or not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", boundary)):
        response.close()
        raise ValueError("Frigate did not return an MJPEG feed.")
    return response, boundary


def live_jpegs(stream, boundary, seconds=45):
    delimiter = b"\r\n--" + boundary.encode("ascii")
    buffer = b"\r\n"
    limit = 2 * 1024 * 1024
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        chunk = stream.read1(16384)
        if not chunk:
            return
        buffer += chunk
        while True:
            first = buffer.find(delimiter)
            if first < 0:
                buffer = buffer[-len(delimiter):]
                break
            after = first + len(delimiter)
            last = buffer.find(delimiter, after)
            if last < 0:
                buffer = buffer[first:]
                break
            part = buffer[after:last]
            buffer = buffer[last:]
            headers, separator, jpeg = part.partition(b"\r\n\r\n")
            if (separator and b"content-type: image/jpeg" in headers.lower().split(b"\r\n")
                and jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
                and len(jpeg) <= limit):
                yield jpeg
        if len(buffer) > limit + 8192:
            return
