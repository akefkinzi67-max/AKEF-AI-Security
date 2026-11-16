import io
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from backup import copy_database
from analytics import pos_exception_signal, register_forecast
from station import FIELDS, ROOT, connect, create_app, migrate
from commander import tlog_csv, import_plu, get_partner_xml
from monitor import poll
from notifications import deliver
from queue_tracking import estimates, record_event
from urllib.error import URLError


class StationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "station.db"
        app = create_app({"TESTING": True, "DATABASE": self.path, "SECRET_KEY": "a" * 40})
        self.client = app.test_client()
        self.assertEqual(self.client.get("/").status_code, 200)
        token = "synthetic-setup-token-for-tests"
        with patch.dict(os.environ, NAVIQRA_SETUP_TOKEN=token):
            self.assertEqual(self.post("setup", token=token, name="Synthetic test station",
                                      timezone="America/Los_Angeles", warehouse="A",
                                      username="owner", password="synthetic-password").status_code, 302)
        self.assertEqual(self.post("login", username="owner",
                                   password="synthetic-password").status_code, 302)

    def post(self, route, **data):
        self.client.get("/")
        with self.client.session_transaction() as session:
            data["csrf"] = session["csrf"]
        return self.client.post("/" + route, data=data)

    def upload(self, rows):
        data = (",".join(FIELDS) + "\n" + rows).encode()
        return self.post("import", source="fixture", export=(io.BytesIO(data), "synthetic.csv"))

    def test_repeatable_import_conflict_rollback_and_reports(self):
        row = "1,1,2026-01-01T09:00:00-08:00,Sale,,3.5,12.12,Regular,4\n"
        self.assertEqual(self.upload(row).status_code, 302)
        self.assertEqual(self.upload(row).status_code, 302)
        conflict = row.replace("12.12", "12.14")
        self.assertEqual(self.upload(row.replace("1,1,", "2,1,", 1) + conflict).status_code, 400)
        self.assertIn(b"$12.12", self.client.get("/").data)
        self.assertIn(b"3.500", self.client.get("/").data)
        result = self.client.get("/sales.csv")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.data.splitlines()), 2)

    def test_inventory_balances_transfer_and_backup(self):
        self.assertEqual(self.post("warehouse", warehouse="B").status_code, 302)
        self.assertEqual(self.post("product", sku="W", name="Water", barcode="ABC",
                                   reorder="3").status_code, 302)
        self.assertEqual(self.post("movement", sku="W", warehouse="A", reason="Receive",
                                   quantity="10").status_code, 302)
        self.assertIn(b"<td>B</td><td>0</td>", self.client.get("/").data)
        self.assertEqual(self.post("movement", sku="W", warehouse="A", reason="Transfer",
                                   quantity="7", target="B").status_code, 302)
        self.assertEqual(self.post("movement", sku="W", warehouse="A", reason="Transfer",
                                   quantity="2", target="missing").status_code, 409)
        copy = self.path.with_name("restore.db")
        copy_database(self.path, copy)
        with closing(connect(copy)) as db:
            rows = db.execute("SELECT warehouse,SUM(delta) FROM movements GROUP BY warehouse").fetchall()
            self.assertEqual([tuple(row) for row in rows], [("A", 3), ("B", 7)])

    def test_employee_and_csrf(self):
        self.assertEqual(self.client.post("/warehouse", data={"warehouse": "Z"}).status_code, 403)
        self.assertEqual(self.post("user", username="clerk", password="synthetic-password",
                                   role="employee").status_code, 302)
        self.post("logout")
        self.assertEqual(self.client.get("/sales.csv").status_code, 403)
        self.post("login", username="clerk", password="synthetic-password")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/sales.csv").status_code, 403)
        self.assertEqual(self.post("warehouse", warehouse="Z").status_code, 403)

    def test_owner_disables_user_and_invalidates_existing_session(self):
        self.assertEqual(self.post("user", username="reviewer", password="synthetic-password",
                                   role="manager").status_code, 302)
        owner = self.client
        reviewer = owner.application.test_client()
        self.client = reviewer
        self.assertEqual(self.post("login", username="reviewer", password="synthetic-password").status_code, 302)
        self.assertEqual(self.post("update-user", username="reviewer", role="manager", active="0").status_code, 403)
        self.assertEqual(reviewer.get("/sales.csv").status_code, 200)
        self.client = owner
        self.assertEqual(self.post("update-user", username="reviewer", role="employee", active="0").status_code, 302)
        self.assertEqual(reviewer.get("/sales.csv").status_code, 403)
        self.assertEqual(self.post("update-user", username="reviewer", role="manager", active="1").status_code, 302)
        self.assertEqual(reviewer.get("/sales.csv").status_code, 403)

    def test_register_advice_uses_distinct_transactions(self):
        rows = "".join(
            f"{day},{line},2026-01-{day}T09:00:00-08:00,Sale,A,1,1.00,,\n"
            for day in ("01", "08", "15") for line in (1, 2)
        )
        self.assertEqual(self.upload(rows).status_code, 302)
        self.assertIn(b"1.0 per hour", self.client.get("/").data)
        self.assertEqual(self.post("service-time", minutes="12").status_code, 302)
        self.assertIn(b"12 minute(s)", self.client.get("/").data)

    def test_peak_capacity_backtest_and_exception_signal(self):
        start = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        sales = [{"occurred_at": (start + timedelta(weeks=week)).isoformat()}
                 for week, count in enumerate((10, 10, 10, 34)) for _ in range(count)]
        peaks, error, hours = register_forecast(sales, timezone.utc, 10)
        self.assertEqual(peaks, [(3, 10, 4, 16.0, 2, True)])
        self.assertEqual((error, hours), (1.0, 24))
        exceptions = [{"occurred_at": (start + timedelta(days=day)).isoformat(),
                       "exception": int(day == 7 and transaction < 5)}
                      for day in range(8) for transaction in range(20)]
        self.assertEqual(pos_exception_signal(exceptions, timezone.utc)[1:], (5, 20, 25.0, 0.0))
        self.assertIsNone(pos_exception_signal(exceptions[:-20], timezone.utc))

    def test_forecast_stable_shift_and_sparse_history(self):
        start = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        for counts, expected_error in (((12, 12, 12, 12, 12, 12), 0), ((6, 6, 6, 30), 1)):
            rows = [{"occurred_at": (start + timedelta(weeks=week)).isoformat()}
                    for week, count in enumerate(counts) for _ in range(count)]
            _, error, hours, schedule = register_forecast(rows, timezone.utc, 3, include_profile=True)
            self.assertEqual(error, expected_error)
            self.assertEqual(hours, (len(counts) - 3) * 24)
            self.assertIsNone(schedule[0][10])
            self.assertEqual(schedule[3][10][2], len(counts))
            self.assertEqual(schedule[3][10][6], len(counts))
            self.assertEqual(schedule[3][11][6], 0)
        self.assertIn(b"Evidence is limited", self.client.get("/").data)

    def test_camera_setup_check_and_queue_track_lifecycle(self):
        with patch.dict(os.environ, NAVIQRA_FRIGATE_URL="https://fixture.invalid"):
            app = create_app({"TESTING": True, "DATABASE": self.path, "SECRET_KEY": "a" * 40})
        self.client = app.test_client()
        self.post("login", username="owner", password="synthetic-password")
        self.assertEqual(self.post("camera-setup", camera="cashier", protocol="ONVIF", queue_zone="line").status_code, 302)
        with patch("station.fetch_stats", return_value={"cashier": 7.5}):
            self.assertEqual(self.post("camera-check", camera="cashier").status_code, 302)
        self.assertIn(b"7.5 captured frames per second", self.client.get("/").data)

        def payload(action, stamp, zones, track="first", label="person"):
            return json.dumps({"type": action, "after": {"camera": "cashier", "id": track,
                "label": label, "frame_time": stamp, "current_zones": zones, "false_positive": False}})

        with closing(connect(self.path)) as db, db:
            for _ in range(3):
                self.assertTrue(record_event(db, payload("new", 1000, ["line"]), 1000))
            self.assertFalse(record_event(db, payload("new", 1000, ["line"], "car", "car"), 1000))
            self.assertEqual(estimates(db, 1000)[0]["waiting"], 1)
            record_event(db, payload("update", 1001, ["line"], "second"), 1001)
            self.assertEqual(estimates(db, 1001)[0]["waiting"], 2)
            record_event(db, payload("end", 1010, ["line"]), 1010)
            record_event(db, payload("update", 1005, ["line"]), 1010)
            self.assertEqual(estimates(db, 1010)[0]["waiting"], 1)
            self.assertIsNone(estimates(db, 1060)[0]["observed"])

    def test_webhook_retries_with_stable_id_and_no_resend_after_success(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with closing(connect(self.path)) as db:
            db.execute("""INSERT INTO incidents(camera,category,detail,opened)
                VALUES ('cashier','Camera offline','synthetic',?)""",
                (now.isoformat(timespec="microseconds"),))
            db.commit()
            with patch("notifications.build_opener") as opener:
                seen = []
                response = opener.return_value.open.return_value.__enter__.return_value
                response.status = 204

                def send(request, timeout):
                    seen.append((request.get_header("Idempotency-key"), json.loads(request.data)))
                    if len(seen) == 1:
                        raise URLError("synthetic timeout")
                    return opener.return_value.open.return_value

                opener.return_value.open.side_effect = send
                at = now.timestamp()
                send_alert = lambda stamp: deliver(db, "https://fixture.invalid/receive", "", "TEST", stamp)
                self.assertIs(send_alert(at), False)
                self.assertIsNone(send_alert(at + 1))
                self.assertIs(send_alert(at + 30), True)
                self.assertIsNone(send_alert(at + 120))
                self.assertEqual(seen[0], seen[1])
                self.assertEqual(seen[0][0], "TEST-incident-1")

    def test_invoice_sale_refund_cost_and_retry(self):
        self.assertEqual(self.post("product", sku="W", name="Water", barcode="",
                                   reorder=1).status_code, 302)
        self.assertEqual(self.post("supplier", name="Synthetic supplier").status_code, 302)
        order = dict(supplier=1, number="PO-1", ordered="2026-01-01", warehouse="A", items="W,10")
        self.assertEqual(self.post("purchase", **order).status_code, 302)
        invoice = dict(supplier=1, number="1", purchased="2026-01-01", warehouse="A",
                       items="W,10,1.25,2026-02-01", order=1)
        self.assertEqual(self.post("invoice", **dict(invoice, items="W,11,1.25,")).status_code, 400)
        self.assertEqual(self.post("invoice", **dict(invoice, items="W,4,1.25,2026-02-01")).status_code, 302)
        self.assertIn(b"4/10 units", self.client.get("/").data)
        self.assertEqual(self.post("invoice", **dict(invoice, number="1B", items="W,6,1.25,2026-02-01")).status_code, 302)
        self.assertEqual(self.post("cancel-purchase", order=1).status_code, 400)
        self.assertEqual(self.post("invoice", **dict(invoice, order="")).status_code, 409)
        broken = dict(invoice, order="", number="2", items="W,10,1.25,\nUNKNOWN,1,2.00,")
        self.assertEqual(self.post("invoice", **broken).status_code, 409)
        self.assertEqual(self.upload(
            "1,1,2026-01-01T23:45:00-08:00,Sale,W,4,12.00,,\n"
            "2,1,2026-01-02T00:05:00-08:00,Refund,W,-1,-3.00,,\n"
            "3,1,2026-01-02T00:06:00-08:00,Void,,0,0,,\n"
        ).status_code, 302)
        sale = dict(source="fixture", transaction_id="1", line_id="1", mode="sell", warehouse="A")
        self.assertEqual(self.post("post-sale", **sale).status_code, 302)
        self.assertEqual(self.post("post-sale", **sale).status_code, 302)
        refund = dict(source="fixture", transaction_id="2", line_id="1", mode="restock", original=1)
        self.assertEqual(self.post("post-sale", **refund).status_code, 302)
        self.assertEqual(self.post("post-sale", **dict(refund, transaction_id="3", mode="reverse")).status_code, 400)
        self.assertIn(b"$5.25", self.client.get("/").data)
        self.assertIn(b"$7.00", self.client.get("/?start=2026-01-01&end=2026-01-01").data)
        self.assertEqual(len(self.client.get("/sales.csv?start=2026-01-01&end=2026-01-01").data.splitlines()), 2)
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT SUM(remaining) FROM lots").fetchone()[0], 7)
            self.assertEqual(db.execute("SELECT SUM(delta) FROM movements").fetchone()[0], 7)

    def test_barcode_count_and_void_reversal_remain_consistent(self):
        self.assertEqual(self.post("product", sku="W", name="Water", barcode="00001", reorder=2).status_code, 302)
        self.assertEqual(self.post("product", sku="D", name="Duplicate", barcode="00001", reorder=0).status_code, 409)
        self.assertEqual(self.post("movement", sku="W", warehouse="A", reason="Receive", quantity=6).status_code, 302)
        self.assertEqual(self.post("movement", sku="W", warehouse="A", reason="Count", quantity=5).status_code, 302)
        self.assertEqual(self.post("movement", sku="W", warehouse="A", reason="Damage", quantity=6).status_code, 400)
        self.assertEqual(self.upload(
            "=literal,1,2026-01-01T12:00:00Z,Sale,W,2,6.00,,\n"
            "void,1,2026-01-01T12:02:00Z,Void,,0,0,,\n"
            "refund,1,2026-01-01T12:05:00Z,Refund,W,0,0,,\n"
        ).status_code, 302)
        self.assertIn(b"'=literal", self.client.get("/sales.csv").data)
        self.assertEqual(self.post("post-sale", source="fixture", transaction_id="=literal",
                                   line_id=1, mode="sell", warehouse="A").status_code, 302)
        reversal = dict(source="fixture", transaction_id="void", line_id=1, mode="reverse", original=1)
        self.assertEqual(self.post("post-sale", **reversal).status_code, 302)
        self.assertEqual(self.post("post-sale", **reversal).status_code, 302)
        self.assertEqual(self.post("post-sale", **dict(reversal, transaction_id="refund", mode="refund-only")).status_code, 400)
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT SUM(remaining) FROM lots").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT SUM(delta) FROM movements").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM stock_postings").fetchone()[0], 2)

    def test_concurrent_stock_posting_charges_once(self):
        self.assertEqual(self.post("product", sku="W", name="Water", barcode="", reorder=0).status_code, 302)
        self.assertEqual(self.post("movement", sku="W", warehouse="A", reason="Receive", quantity=5).status_code, 302)
        self.assertEqual(self.upload("race,1,2026-01-01T12:00:00Z,Sale,W,2,4.00,,\n").status_code, 302)
        app = self.client.application
        barrier = Barrier(2, timeout=5)

        def post_stock(_):
            client = app.test_client()
            with client.session_transaction() as session:
                session["username"] = "owner"
                session["csrf"] = "synthetic-concurrent-csrf"
            barrier.wait()
            return client.post("/post-sale", data={
                "csrf": "synthetic-concurrent-csrf", "source": "fixture", "transaction_id": "race",
                "line_id": 1, "mode": "sell", "warehouse": "A"
            }).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(post_stock, range(2))), [302, 302])
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT SUM(delta) FROM movements").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM stock_postings").fetchone()[0], 1)

    def test_existing_database_upgrade_preserves_stock(self):
        path = self.path.with_name("legacy.db")
        with closing(connect(path)) as db:
            db.executescript((ROOT / "migrations/001.sql").read_text())
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO users VALUES ('owner','unusable','owner')")
            db.execute("INSERT INTO warehouses VALUES ('A')")
            db.execute("INSERT INTO products VALUES ('W','Water',NULL,0)")
            db.execute("INSERT INTO movements(sku,warehouse,delta,reason,actor) VALUES ('W','A',8,'Receive','owner')")
            db.commit()
        migrate(path)
        migrate(path)
        copy = self.path.with_name("upgrade-copy.db")
        copy_database(path, copy)
        with closing(connect(copy)) as db:
            self.assertEqual(tuple(db.execute("SELECT remaining,unit_cents FROM lots").fetchone()), (8, None))

    def test_synthetic_commander_field_maps(self):
        payload = (b"<demo><event><id>1</id><stamp>2026-01-01T12:00:00Z</stamp>"
                   b"<line><id>2</id><kind>Sale</kind><qty>1</qty><amount>2.50</amount></line></event></demo>")
        mapping = {"events": "event", "lines": "line", "fields": {
            "transaction_id": {"scope": "event", "path": "id"},
            "line_id": {"scope": "line", "path": "id"},
            "occurred_at": {"scope": "event", "path": "stamp"},
            "kind": {"scope": "line", "path": "kind"},
            "quantity": {"scope": "line", "path": "qty"},
            "amount": {"scope": "line", "path": "amount"},
        }}
        self.assertIn("1,2,", tlog_csv(payload, mapping, FIELDS))
        incomplete = dict(mapping, fields={"transaction_id": mapping["fields"]["transaction_id"]})
        with self.assertRaisesRegex(ValueError, "required output columns"):
            tlog_csv(payload, incomplete, FIELDS)
        with self.assertRaises(ValueError):
            tlog_csv(b'<!DOCTYPE demo [<!ENTITY x "x">]>' + payload, mapping, FIELDS)
        with closing(connect(self.path)) as db, db:
            self.assertEqual(import_plu(db, b"Code,Name,Bar\nW,Water,123\n",
                {"sku": "Code", "name": "Name", "barcode": "Bar"}, lambda value: value), 1)
        with self.assertRaises(ValueError):
            get_partner_xml("http://fixture.invalid/report", "synthetic")
        response = io.BytesIO(payload)
        response.headers = Message()
        response.headers["Content-Type"] = "application/xml"
        with patch("commander.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(get_partner_xml("https://fixture.invalid/report", "Synthetic credential"), payload)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")

    def test_partner_import_requires_owner_and_deduplicates(self):
        self.assertEqual(self.post("user", username="manager", password="synthetic-password",
                                   role="manager").status_code, 302)
        config = self.path.with_name("synthetic-tlog.json")
        config.write_text(json.dumps({"events": "event", "lines": "line", "fields": {
            "transaction_id": {"scope": "event", "path": "id"},
            "line_id": {"scope": "line", "path": "id"},
            "occurred_at": {"scope": "event", "path": "stamp"},
            "kind": {"scope": "line", "path": "kind"},
            "quantity": {"scope": "line", "path": "qty"},
            "amount": {"scope": "line", "path": "amount"},
        }}), encoding="utf-8")
        with patch.dict(os.environ, NAVIQRA_TLOG_MAP=str(config),
                        NAVIQRA_PARTNER_XML_URL="https://fixture.invalid/read"):
            app = create_app({"TESTING": True, "DATABASE": self.path,
                              "SECRET_KEY": "synthetic-partner-test-key-abcdef"})
        self.client = app.test_client()
        imported = (b"<demo><event><id>1</id><stamp>2026-01-01T12:00:00Z</stamp>"
                    b"<line><id>1</id><kind>Sale</kind><qty>1</qty><amount>2.50</amount></line></event></demo>")
        self.post("login", username="manager", password="synthetic-password")
        with patch("station.get_partner_xml", return_value=imported) as retrieve:
            self.assertEqual(self.post("partner", source="Synthetic partner").status_code, 403)
            retrieve.assert_not_called()
            self.post("logout")
            self.post("login", username="owner", password="synthetic-password")
            self.assertEqual(self.post("partner", source="Synthetic partner").status_code, 302)
            self.assertEqual(self.post("partner", source="Synthetic partner").status_code, 302)
            retrieve.return_value = imported.replace(b"<amount>2.50</amount>", b"")
            self.assertEqual(self.post("partner", source="Synthetic partner").status_code, 400)
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*), SUM(cents) FROM sales").fetchone()[:], (1, 250))

    def test_queue_observation_requires_explicit_time_zone(self):
        self.assertEqual(self.post("queue", camera="Register 1", observed="2026-01-01T12:00:00",
                                   waiting=2).status_code, 400)
        self.assertEqual(self.post("queue", camera="Register 1", observed="2026-01-01T12:00:00-06:00",
                                   waiting=2).status_code, 302)
        with closing(connect(self.path)) as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM queue_observations").fetchone()),
                             ("Register 1", "2026-01-01T18:00:00.000000+00:00", 2))

    def test_unicode_csrf_rejected_and_login_throttled(self):
        self.assertEqual(self.client.get("/", headers={"Host": "untrusted.example"}).status_code, 400)
        self.assertEqual(self.client.post("/logout", data={"csrf": "اختبار"}).status_code, 403)
        self.post("logout")
        for _ in range(10):
            self.assertEqual(self.post("login", username="missing", password="synthetic").status_code, 401)
        throttled = self.post("login", username="owner", password="synthetic-password")
        self.assertEqual(throttled.status_code, 429)
        self.assertGreater(int(throttled.headers["Retry-After"]), 0)

    def test_pwa_metadata_and_privacy_routes(self):
        client = self.client.application.test_client()
        manifest = client.get("/static/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        metadata = json.loads(manifest.data)
        self.assertEqual((metadata["start_url"], metadata["scope"], metadata["display"]),
                         ("/", "/", "standalone"))
        manifest.close()
        for icon in metadata["icons"]:
            response = client.get(icon["src"])
            self.assertEqual(response.mimetype, "image/png")
            self.assertTrue(response.data.startswith(b"\x89PNG"))
            response.close()
        worker = client.get("/service-worker.js")
        self.assertEqual(worker.mimetype, "text/javascript")
        self.assertEqual(worker.headers["Cache-Control"], "no-store")
        self.assertIn(b"fetch(request)", worker.data)
        self.assertNotIn(b"caches.open", worker.data)
        worker.close()
        self.assertEqual(client.get("/station-connection").status_code, 204)
        self.assertEqual(client.get("/cameras/live-frames?camera=private").status_code, 403)
        self.assertEqual(client.get("/incidents/1/clip.mp4").status_code, 403)

    def test_tls_session_cookie_and_trusted_station_name(self):
        app = create_app({"TESTING": True, "DATABASE": self.path,
                          "SECRET_KEY": "synthetic-tls-test-key-only-do-not-use",
                          "SESSION_COOKIE_SECURE": True,
                          "TRUSTED_HOSTS": ["station.example"]})
        client = app.test_client()
        self.assertEqual(client.get("/", base_url="https://untrusted.example").status_code, 400)
        response = client.get("/", base_url="https://station.example")
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_supplier_update_and_pending_report_window(self):
        self.assertEqual(self.post("supplier", name="Before").status_code, 302)
        self.assertEqual(self.post("update-supplier", supplier=1, name="After", contact="Synthetic").status_code, 302)
        self.assertIn(b'name="name" value="After"', self.client.get("/").data)
        self.assertEqual(self.post("movement", sku="missing", warehouse="A", quantity=0, reason="Count").status_code, 400)
        self.assertEqual(self.upload(
            "first,1,2026-01-01T18:00:00Z,Sale,WINDOW-1,1,1.00,,\n"
            "second,1,2026-01-02T18:00:00Z,Sale,WINDOW-2,1,1.00,,\n"
        ).status_code, 302)
        page = self.client.get("/?start=2026-01-01&end=2026-01-01").data
        self.assertIn(b"WINDOW-1", page)
        self.assertNotIn(b"WINDOW-2", page)

    def test_frigate_simulation_and_failed_sync(self):
        with patch.dict(os.environ, NAVIQRA_FRIGATE_URL="http://127.0.0.1:28880"):
            app = create_app({"TESTING": True, "DATABASE": self.path, "SECRET_KEY": "a" * 40})
        self.client = app.test_client()
        self.post("login", username="owner", password="synthetic-password")
        event = {"id": "1731400000.3-abc", "camera": "cashier", "label": "person",
                 "start_time": 1731400000.3, "end_time": 1731400001, "zones": ["counter"],
                 "has_clip": True}
        with patch("cameras.build_opener") as opener:
            for _ in range(2):
                opener.return_value.open.return_value = io.BytesIO(json.dumps([event]).encode())
                self.assertEqual(self.post("sync-cameras").status_code, 302)
            opener.return_value.open.return_value = io.BytesIO(b"invalid upstream")
            self.assertEqual(self.post("sync-cameras").status_code, 400)
        self.assertEqual(self.post("source-camera", source="fixture", camera="cashier").status_code, 302)
        self.assertEqual(self.upload("1,1,2026-01-01T10:00:00Z,No Sale,,0,0,,\n").status_code, 302)
        dashboard = self.client.get("/").data
        self.assertEqual(dashboard.count(b"1731400000.3-abc/clip.mp4"), 1)
        self.assertIn(b"/api/cashier/start/", dashboard)

    def test_camera_outage_incident_and_protected_clip(self):
        with closing(connect(self.path)) as db:
            with patch("monitor.fetch_stats", return_value={"cashier": 0}), patch("monitor.fetch_events", return_value=[]):
                for _ in range(4):
                    poll(db, "http://fixture.invalid", None, {"cashier"})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM incidents WHERE category='Camera offline'").fetchone()[0], 1)
            with patch("monitor.fetch_stats", return_value={"cashier": 5}), patch("monitor.fetch_events", return_value=[]):
                poll(db, "http://fixture.invalid", None, {"cashier"})
            self.assertEqual(db.execute("SELECT state FROM camera_health WHERE camera='cashier'").fetchone()[0], "Online")
            db.execute("INSERT INTO camera_events VALUES ('test-e','cashier','person','2026-01-01T02:00:00Z','counter',1)")
            db.commit()
        with patch.dict(os.environ, NAVIQRA_FRIGATE_URL="http://127.0.0.1:28880"):
            app = create_app({"TESTING": True, "DATABASE": self.path, "SECRET_KEY": "a" * 40})
        self.client = app.test_client()
        self.post("login", username="owner", password="synthetic-password")
        self.assertEqual(self.post("incident", event="test-e", detail="Synthetic incident").status_code, 302)
        clip = b"\x00\x00\x00\x10ftypisom0000"
        with patch("station.fetch_clip", return_value=clip):
            self.assertEqual(self.post("save-clip", incident=2).status_code, 302)
        self.assertEqual(app.test_client().get("/incidents/2/clip.mp4").status_code, 403)
        response = self.client.get("/incidents/2/clip.mp4", headers={"Range": "bytes=0-7"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, clip[:8])
        response.close()
        self.assertEqual(self.post("review-incident", incident=2).status_code, 302)
        self.assertEqual(self.post("delete-clip", incident=2).status_code, 302)
        self.assertEqual(self.client.get("/incidents/2/clip.mp4").status_code, 404)
        event = ("test-e", "cashier", "person", "2026-01-01T02:00:00Z", "counter", 1)
        with closing(connect(self.path)) as db:
            with patch("monitor.fetch_stats", return_value={"cashier": 5}), patch("monitor.fetch_events", return_value=[event]):
                for _ in range(2):
                    poll(db, "http://fixture.invalid", None, {"cashier"}, {"counter"})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM incidents WHERE category='Restricted zone'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
