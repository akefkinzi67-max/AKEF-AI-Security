"""Consume Frigate tracked-person updates without storing images or identities."""
import json
import math
import os
import re
import time

IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,120}")


def record_event(db, payload, now):
    if len(payload) > 1024 * 1024:
        return False
    try:
        event = json.loads(payload)
        action = event["type"]
        after = event["after"]
        camera, track = after["camera"], after["id"]
        if action not in ("new", "update", "end") or after["label"] != "person":
            return False
        if not all(isinstance(value, str) and IDENTIFIER.fullmatch(value) for value in (camera, track)):
            return False
        stamp = float(after["frame_time"])
        if not math.isfinite(stamp) or not now - 300 <= stamp <= now + 10:
            return False
        zones = after["current_zones"]
        if not isinstance(zones, list) or not all(isinstance(zone, str) for zone in zones):
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    setting = db.execute("SELECT queue_zone FROM camera_setup WHERE camera=?", (camera,)).fetchone()
    if not setting or not setting[0]:
        return False
    zone = setting[0] if action != "end" and after.get("false_positive") is False and setting[0] in zones else None
    # Keep exit records briefly so delayed updates cannot revive a track.
    db.execute("""INSERT INTO queue_tracks VALUES (?,?,?,?)
        ON CONFLICT(camera,track) DO UPDATE SET observed=excluded.observed,zone=excluded.zone
        WHERE excluded.observed>queue_tracks.observed
        OR (excluded.observed=queue_tracks.observed AND excluded.zone IS NULL)""", (camera, track, stamp, zone))
    db.execute("DELETE FROM queue_tracks WHERE observed<?", (now - 300,))
    return True


def estimates(db, now):
    return db.execute("""SELECT s.camera,s.queue_zone,COUNT(CASE WHEN t.zone=s.queue_zone THEN 1 END) AS waiting,
        MAX(t.observed) AS observed FROM camera_setup s
        LEFT JOIN queue_tracks t ON t.camera=s.camera AND t.observed BETWEEN ? AND ?
        WHERE s.queue_zone IS NOT NULL GROUP BY s.camera,s.queue_zone ORDER BY s.camera""",
        (now - 45, now + 10)).fetchall()


def start_subscription(connect, path):
    host = os.getenv("NAVIQRA_MQTT_HOST")
    if not host:
        return None
    import paho.mqtt.client as mqtt

    topic = os.getenv("NAVIQRA_MQTT_TOPIC", "frigate/events")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.tls_set(ca_certs=os.getenv("NAVIQRA_MQTT_CA") or None)
    user = os.getenv("NAVIQRA_MQTT_USERNAME")
    if user:
        client.username_pw_set(user, os.getenv("NAVIQRA_MQTT_PASSWORD"))

    def on_connect(connection, userdata, flags, reason_code, properties):
        if reason_code == 0:
            connection.subscribe(topic, qos=1)

    def on_message(connection, userdata, message):
        if message.topic != topic or message.retain:
            return
        from contextlib import closing
        import sqlite3
        with closing(connect(path)) as db:
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    record_event(db, message.payload, time.time())
            except sqlite3.Error:
                print("Queue update could not be recorded.", flush=True)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(host, int(os.getenv("NAVIQRA_MQTT_PORT", "8883")), 30)
    client.loop_start()
    return client
