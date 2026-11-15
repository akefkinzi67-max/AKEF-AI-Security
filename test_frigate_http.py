"""Exercise the Frigate HTTP adapter against an explicit local test double."""
from contextlib import closing
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from backup import copy_database
from cameras import fetch_clip
from monitor import poll
from station import connect, migrate


class FrigateFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        clips = b"\x00\x00\x00\x10ftypisom0000"
        routes = {
            "/api/stats": {"cameras": {"cashier": {"camera_fps": 4}}},
            "/api/events?limit=100": [{"id": "pilot.fixture", "camera": "cashier", "label": "person",
                "start_time": 1767261600.0, "end_time": 1767261610.0, "zones": ["line"], "has_clip": True}],
            "/api/events/pilot.fixture/clip.mp4": clips,
        }
        data = routes.get(self.path)
        if data is None:
            self.send_error(404)
            return
        payload = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4" if isinstance(data, bytes) else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


class FrigateTransportTest(unittest.TestCase):
    def test_loopback_event_recording_and_restore(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FrigateFixture)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with TemporaryDirectory() as scratch:
            active = Path(scratch) / "station.sqlite3"
            migrate(active)
            with closing(connect(active)) as db:
                poll(db, base, None, {"cashier"}, {"line"})
                self.assertEqual(db.execute("SELECT state FROM camera_health WHERE camera='cashier'").fetchone()[0], "Online")
                self.assertEqual(db.execute("SELECT category FROM incidents").fetchone()[0], "Restricted zone")
                clip = fetch_clip(base, None, "pilot.fixture")
                with db:
                    db.execute("INSERT INTO incident_clips VALUES (1,?)", (clip,))
                with self.assertRaises(ValueError):
                    fetch_clip(base, None, "missing")
            restored = Path(scratch) / "restored.sqlite3"
            copy_database(active, restored)
            with closing(connect(restored)) as db:
                self.assertEqual(db.execute("SELECT content FROM incident_clips").fetchone()[0], clip)
