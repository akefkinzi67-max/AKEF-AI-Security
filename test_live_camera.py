"""Synthetic Frigate MJPEG source and camera wall integration checks."""
from base64 import b64decode
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
import unittest
from unittest.mock import patch

from station import connect, create_app
from werkzeug.security import generate_password_hash


FRAMES = tuple(map(b64decode, (
    "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",
)))


class SyntheticFeed(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/api/fixture_camera?"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        for index in range(self.server.frame_count):
            frame = FRAMES[index % 2]
            try:
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                )
                self.wfile.flush()
            except OSError:
                return
            if self.server.frame_count > 2:
                sleep(0.3)
        try:
            self.wfile.write(b"--frame--\r\n")
        except OSError:
            pass

    def log_message(self, *_):
        pass


def start_fixture(frame_count=2):
    server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticFeed)
    server.frame_count = frame_count
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_app(path, origin):
    with patch.dict(os.environ, NAVIQRA_FRIGATE_URL=origin):
        app = create_app({"TESTING": True, "DATABASE": path, "SECRET_KEY": "synthetic-wall-verification-only"})
    with closing(connect(path)) as db, db:
        db.execute("INSERT INTO station VALUES (1,'Synthetic Camera Wall','America/Chicago')")
        db.execute("INSERT INTO warehouses VALUES ('Main')")
        for username, role in (("manager", "manager"), ("employee", "employee")):
            db.execute("INSERT INTO users(username,password,role) VALUES (?,?,?)",
                       (username, generate_password_hash("synthetic-wall-only"), role))
    return app


class LiveCameraTest(unittest.TestCase):
    def test_wall_mjpeg_forwarding_and_permissions(self):
        with TemporaryDirectory() as scratch:
            server = start_fixture()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            path = Path(scratch) / "station.sqlite3"
            app = test_app(path, f"http://127.0.0.1:{server.server_port}")
            client = app.test_client()
            endpoint = "/cameras/fixture_camera/live.mjpeg"
            self.assertEqual(client.get(endpoint).status_code, 403)
            with client.session_transaction() as session:
                session["username"] = "manager"
            dashboard = client.get("/").data
            self.assertEqual(dashboard.count(b"data-camera-tile"), 12)
            self.assertEqual(dashboard.count(b"data-camera-tile hidden"), 8)
            self.assertIn(b"Not Connected", dashboard)
            self.assertEqual(client.get(endpoint).status_code, 404)
            with closing(connect(path)) as db, db:
                db.execute("INSERT INTO camera_setup VALUES ('fixture_camera','RTSP',NULL)")
            response = client.get(endpoint)
            self.assertIn("multipart/x-mixed-replace", response.content_type)
            self.assertIn(FRAMES[0], response.data)
            self.assertIn(FRAMES[1], response.data)
            response.close()
            with client.session_transaction() as session:
                session["username"] = "employee"
            self.assertEqual(client.get(endpoint).status_code, 403)

    def test_setup_errors_keep_public_fields_out_of_secret_payload(self):
        with TemporaryDirectory() as scratch:
            app = create_app({"TESTING": True, "DATABASE": Path(scratch) / "setup.sqlite3",
                              "SECRET_KEY": "synthetic-setup-verification-only"})
            client = app.test_client()
            client.get("/")
            with client.session_transaction() as session:
                csrf = session["csrf"]
            data = {"csrf": csrf, "token": "synthetic-incorrect-secret", "password": "synthetic-passphrase",
                    "name": "Synthetic Pilot Name", "warehouse": "BACK", "timezone": "America/Denver",
                    "username": "pilot-owner"}
            with patch.dict(os.environ, NAVIQRA_SETUP_TOKEN="synthetic-expected-secret"):
                response = client.post("/setup", data=data)
            self.assertEqual(response.status_code, 403)
            for value in ("Synthetic Pilot Name", "BACK", "pilot-owner", "America/Denver"):
                self.assertIn(value.encode(), response.data)
            self.assertNotIn(data["token"].encode(), response.data)
            self.assertNotIn(data["password"].encode(), response.data)


if __name__ == "__main__":
    import argparse

    from waitress import serve

    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-fixture", action="store_true", required=True)
    args = parser.parse_args()
    with TemporaryDirectory() as scratch, start_fixture(120) as server:
        path = Path(scratch) / "station.sqlite3"
        application = test_app(path, f"http://127.0.0.1:{server.server_port}")
        with closing(connect(path)) as db, db:
            db.execute("INSERT INTO camera_setup VALUES ('fixture_camera','RTSP',NULL)")
        try:
            serve(application, host="127.0.0.1", port=28690, threads=24)
        finally:
            server.shutdown()
