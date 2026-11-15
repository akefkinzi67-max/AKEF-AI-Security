import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from backup import copy_database
from station import FIELDS, connect, create_app


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

    def test_register_advice_uses_distinct_transactions(self):
        rows = "".join(
            f"{day},{line},2026-01-{day}T09:00:00-08:00,Sale,A,1,1.00,,\n"
            for day in ("01", "08", "15") for line in (1, 2)
        )
        self.assertEqual(self.upload(rows).status_code, 302)
        self.assertIn(b"1.0 per hour", self.client.get("/").data)


if __name__ == "__main__":
    unittest.main()
