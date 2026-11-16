"""Persist camera health and alert transitions for the station review dashboard."""
from contextlib import closing
from datetime import datetime, timezone
import os
import time

from cameras import fetch_events, fetch_stats, origin
from notifications import deliver
from queue_tracking import start_subscription


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def save_events(db, events):
    db.executemany("""INSERT INTO camera_events VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET zones=excluded.zones, has_clip=excluded.has_clip""", events)


def observation(db, camera, healthy, checked, category):
    row = db.execute("SELECT failures FROM camera_health WHERE camera=?", (camera,)).fetchone()
    failures = 0 if healthy else (row[0] if row else 0) + 1
    state = "Online" if healthy else ("Offline" if failures >= 3 else "Checking")
    db.execute("""INSERT INTO camera_health VALUES (?,?,?,?)
        ON CONFLICT(camera) DO UPDATE SET failures=excluded.failures,state=excluded.state,checked=excluded.checked""",
        (camera, failures, state, checked))
    if failures == 3:
        db.execute("""INSERT OR IGNORE INTO incidents(camera,category,detail,opened)
            VALUES (?,?,?,?)""", (camera, category, "Three consecutive unsuccessful health checks.", checked))


def poll(db, base, token, expected_cameras, restricted_zones=()):
    stats = events = None
    try:
        stats = fetch_stats(base, token)
    except ValueError:
        pass
    if stats is not None:
        try:
            events = fetch_events(base, token)
        except ValueError:
            pass
    checked = timestamp()
    with db:
        db.execute("BEGIN IMMEDIATE")
        observation(db, "@Frigate", stats is not None, checked, "Frigate unavailable")
        if stats is not None:
            previous = {row[0] for row in db.execute("SELECT camera FROM camera_health WHERE camera<>'@Frigate'")}
            for camera in sorted(set(stats) | expected_cameras | previous):
                observation(db, camera, stats.get(camera, 0) > 0, checked, "Camera offline")
        if events is not None:
            save_events(db, events)
            for event_id, camera, label, started, zones, _ in events:
                overlap = sorted(set(zones.split(", ")) & set(restricted_zones))
                if overlap and label in ("person", "car", "truck", "motorcycle", "bus"):
                    db.execute("""INSERT OR IGNORE INTO incidents(camera,category,detail,event_id,opened)
                        VALUES (?,'Restricted zone',?,?,?)""",
                        (camera, f"Review {label} detected in {', '.join(overlap)}.", event_id, started))


def main():
    from station import connect, create_app

    app = create_app()
    base = os.getenv("NAVIQRA_FRIGATE_URL")
    if not base:
        raise SystemExit("Set NAVIQRA_FRIGATE_URL to enable monitoring.")
    base = origin(base)
    token = os.getenv("NAVIQRA_FRIGATE_TOKEN")
    expected = {name.strip() for name in os.getenv("NAVIQRA_EXPECTED_CAMERAS", "").split(",") if name.strip()}
    restricted = {name.strip() for name in os.getenv("NAVIQRA_RESTRICTED_ZONES", "").split(",") if name.strip()}
    subscriber = start_subscription(connect, app.config["DATABASE"])
    webhook = os.getenv("NAVIQRA_ALERT_WEBHOOK")
    try:
        while True:
            with closing(connect(app.config["DATABASE"])) as db:
                configured = {row[0] for row in db.execute("SELECT camera FROM camera_setup")}
                poll(db, base, token, expected | configured, restricted)
                if webhook:
                    deliver(db, webhook, os.getenv("NAVIQRA_ALERT_TOKEN", ""),
                            os.getenv("NAVIQRA_ALERT_STATION_ID", ""))
            time.sleep(30)
    finally:
        if subscriber:
            subscriber.loop_stop()
            subscriber.disconnect()


if __name__ == "__main__":
    main()
