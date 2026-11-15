import io
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from backup import copy_database
from analytics import pos_exception_signal, register_forecast
from station import FIELDS, ROOT, connect, create_app, migrate
from commander import tlog_csv, import_plu, get_partner_xml


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

    def test_invoice_sale_refund_cost_and_retry(self):
        self.assertEqual(self.post("product", sku="W", name="Water", barcode="",
                                   reorder=1).status_code, 302)
        self.assertEqual(self.post("supplier", name="Synthetic supplier").status_code, 302)
        order = dict(supplier=1, number="PO-1", ordered="2026-01-01", warehouse="A", items="W,10")
        self.assertEqual(self.post("purchase", **order).status_code, 302)
        invoice = dict(supplier=1, number="1", purchased="2026-01-01", warehouse="A",
                       items="W,10,1.25,2026-02-01", order=1)
        self.assertEqual(self.post("invoice", **dict(invoice, items="W,9,1.25,")).status_code, 400)
        self.assertEqual(self.post("invoice", **invoice).status_code, 302)
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
            self.assertEqual(db.execute("SELECT remaining FROM lots").fetchone()[0], 7)
            self.assertEqual(db.execute("SELECT SUM(delta) FROM movements").fetchone()[0], 7)

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
        payload = b"<demo><event><id>1</id><line><id>2</id></line></event></demo>"
        mapping = {"events": "event", "lines": "line", "fields": {
            "transaction_id": {"scope": "event", "path": "id"},
            "line_id": {"scope": "line", "path": "id"},
        }}
        self.assertIn("1,2,", tlog_csv(payload, mapping, FIELDS))
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


if __name__ == "__main__":
    unittest.main()
