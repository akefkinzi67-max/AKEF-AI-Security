import csv
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
import math
import os
from pathlib import Path
import secrets
import sqlite3

from flask import Flask, Response, abort, g, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).parent
FIELDS = ("transaction_id", "line_id", "occurred_at", "kind", "sku", "quantity",
          "amount", "fuel_grade", "pump")


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def migrate(path):
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise RuntimeError("Database requires a newer application version.")
        if version == 0:
            sql = (ROOT / "migrations/001.sql").read_text()
            for statement in sql.strip().split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("PRAGMA user_version=1")
        db.commit()


def required(value, limit=120):
    value = value.strip()
    if not value or len(value) > limit:
        raise ValueError("A required field is missing or exceeds its length limit.")
    return value


def import_sales(db, source, content):
    reader = csv.DictReader(StringIO(content, newline=""), strict=True)
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise ValueError("CSV header must match the documented NAVIQRA format.")
    added = duplicates = 0
    for line, row in enumerate(reader, 2):
        try:
            if None in row or None in row.values():
                raise ValueError
            stamp = datetime.fromisoformat(row["occurred_at"])
            if stamp.utcoffset() is None:
                raise ValueError
            kind = row["kind"].strip()
            amount, quantity = Decimal(row["amount"]), Decimal(row["quantity"])
            if kind not in ("Sale", "Refund", "Void", "No Sale"):
                raise ValueError
            if (not amount.is_finite() or not quantity.is_finite()
                or quantity.as_tuple().exponent < -6):
                raise ValueError
            if abs(amount) > 100000000 or abs(quantity) > 100000000:
                raise ValueError
            cents = amount * 100
            if cents != cents.to_integral_value():
                raise ValueError
            if (kind == "Sale" and (cents < 0 or quantity < 0)
                or kind == "Refund" and (cents > 0 or quantity > 0)
                or kind in ("Void", "No Sale") and (cents != 0 or quantity != 0)):
                raise ValueError
            identity = (source, required(row["transaction_id"]), required(row["line_id"]))
            item_fields = tuple(row[key].strip() for key in ("sku", "fuel_grade", "pump"))
            if any(len(value) > 120 for value in item_fields):
                raise ValueError
            payload = (stamp.astimezone(timezone.utc).isoformat(timespec="microseconds"),
                       kind, item_fields[0], format(quantity.normalize(), "f"), int(cents),
                       *item_fields[1:])
        except (ValueError, InvalidOperation, OverflowError):
            raise ValueError(f"Invalid transaction data on CSV record {line}.") from None
        prior = db.execute(
            "SELECT * FROM sales WHERE source=? AND transaction_id=? AND line_id=?", identity
        ).fetchone()
        if prior is not None:
            if tuple(prior)[3:] != payload:
                raise ValueError(f"Transaction identity conflicts on CSV record {line}.")
            duplicates += 1
            continue
        db.execute("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?)", identity + payload)
        added += 1
    if not added and not duplicates:
        raise ValueError("The CSV contains no transaction lines.")
    return added, duplicates


def advise(rows, tz, service_minutes):
    visits = Counter()
    seen = set()
    days = defaultdict(set)
    for row in rows:
        day = datetime.fromisoformat(row["occurred_at"]).astimezone(tz)
        days[day.weekday()].add(day.date())
        key = (row["source"], row["transaction_id"])
        if key not in seen:
            visits[(day.weekday(), day.hour)] += 1
            seen.add(key)
    advice = []
    for (weekday, hour), count in sorted(visits.items()):
        observations = len(days[weekday])
        if observations >= 3:
            rate = count / observations
            registers = min(2, max(1, math.ceil(rate * service_minutes / 48)))
            advice.append((weekday, hour, observations, round(rate, 1), registers))
    return advice


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.getenv("NAVIQRA_DB", str(ROOT / "data" / "station.sqlite3")),
        SECRET_KEY=os.getenv("NAVIQRA_SECRET"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        SESSION_COOKIE_SECURE=os.getenv("NAVIQRA_HTTPS") == "1",
        SESSION_COOKIE_SAMESITE="Strict",
    )
    if config:
        app.config.update(config)
    if len(app.secret_key or "") < 32:
        raise RuntimeError("Set NAVIQRA_SECRET to a random value of at least 32 characters.")
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    migrate(app.config["DATABASE"])

    @app.before_request
    def prepare():
        g.db = connect(app.config["DATABASE"])
        g.user = g.db.execute("SELECT * FROM users WHERE username=?",
                              (session.get("username"),)).fetchone()
        if request.method == "POST":
            csrf = session.get("csrf", "")
            if not csrf or not secrets.compare_digest(csrf, request.form.get("csrf", "")):
                abort(403)
        session.setdefault("csrf", secrets.token_urlsafe(32))

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.teardown_request
    def close(error):
        if "db" in g:
            g.db.close()

    def audit(action, detail, actor=None):
        g.db.execute("INSERT INTO audit(actor,action,detail) VALUES (?,?,?)",
                     (actor or g.user["username"], action, detail))

    def create_account(role):
        username = required(request.form["username"], 64)
        password = request.form["password"]
        if not 12 <= len(password) <= 128:
            raise ValueError("Use a password between 12 and 128 characters.")
        g.db.execute("INSERT INTO users VALUES (?,?,?)",
                     (username, generate_password_hash(password), role))
        return username

    def operate(action):
        db = g.db
        if action == "setup":
            expected = os.getenv("NAVIQRA_SETUP_TOKEN", "")
            if len(expected) < 24 or not secrets.compare_digest(expected, request.form["token"]):
                abort(403)
            if db.execute("SELECT 1 FROM station").fetchone():
                abort(409)
            timezone_name = required(request.form["timezone"])
            ZoneInfo(timezone_name)
            db.execute("INSERT INTO station VALUES (1,?,?)",
                       (required(request.form["name"]), timezone_name))
            db.execute("INSERT INTO warehouses VALUES (?)", (required(request.form["warehouse"]),))
            username = create_account("owner")
            audit(action, "Station initialized", username)
            return "Station initialized. Sign in to continue."
        if g.user is None:
            abort(403)
        if action == "logout":
            session.clear()
            return "Signed out."
        if g.user["role"] == "employee":
            abort(403)
        form = request.form
        if action == "user":
            if g.user["role"] != "owner":
                abort(403)
            role = form["role"]
            if role not in ("manager", "employee"):
                abort(400)
            detail = create_account(role)
        elif action == "warehouse":
            detail = required(form["warehouse"])
            db.execute("INSERT INTO warehouses VALUES (?)", (detail,))
        elif action == "product":
            detail = required(form["sku"])
            db.execute("INSERT INTO products VALUES (?,?,?,?)",
                       (detail, required(form["name"]), form["barcode"].strip() or None,
                        int(form["reorder"])))
        elif action == "movement":
            sku = required(form["sku"])
            warehouse = required(form["warehouse"])
            reason = form["reason"]
            quantity = int(form["quantity"])
            if reason not in ("Receive", "Damage", "Customer return", "Vendor return", "Count", "Transfer"):
                abort(400)
            if not 0 <= quantity <= 100000000 or (quantity == 0 and reason != "Count"):
                raise ValueError("Enter a valid quantity.")
            balance = db.execute(
                "SELECT COALESCE(SUM(delta),0) FROM movements WHERE sku=? AND warehouse=?",
                (sku, warehouse),
            ).fetchone()[0]
            delta = quantity
            if reason in ("Damage", "Vendor return", "Transfer"):
                delta = -quantity
            if reason == "Count":
                delta = quantity - balance
            if balance + delta < 0:
                raise ValueError("Insufficient stock at the source warehouse.")
            actor = g.user["username"]
            if delta:
                db.execute("INSERT INTO movements(sku,warehouse,delta,reason,actor) VALUES (?,?,?,?,?)",
                           (sku, warehouse, delta, reason, actor))
            if reason == "Transfer":
                target = required(form["target"])
                if target == warehouse:
                    raise ValueError("Select a different destination warehouse.")
                db.execute("INSERT INTO movements(sku,warehouse,delta,reason,actor) VALUES (?,?,?,?,?)",
                           (sku, target, quantity, reason, actor))
            detail = f"{sku} / {warehouse} / {reason} / {quantity}"
        elif action == "import":
            source = required(form["source"])
            data = request.files["export"].stream.read().decode("utf-8-sig")
            added, duplicates = import_sales(db, source, data)
            detail = f"{source}: {added} added, {duplicates} duplicates"
        else:
            abort(404)
        audit(action, detail)
        return f"Saved. {detail}"

    @app.post("/<action>")
    def mutate(action):
        if action == "login":
            account = g.db.execute("SELECT * FROM users WHERE username=?",
                                   (request.form["username"],)).fetchone()
            password = request.form["password"]
            if account and len(password) <= 128 and check_password_hash(account["password"], password):
                session.clear()
                session["username"] = account["username"]
                return redirect("/")
            return dashboard("Invalid username or password.", 401)
        try:
            with g.db:
                g.db.execute("BEGIN IMMEDIATE")
                message = operate(action)
            session["message"] = message
            return redirect("/")
        except (ValueError, csv.Error, ZoneInfoNotFoundError) as error:
            return dashboard(str(error), 400)
        except sqlite3.IntegrityError:
            return dashboard("The input conflicts with existing records or references an unknown item.", 409)

    @app.get("/")
    def dashboard(message=None, status=200):
        db = g.db
        station = db.execute("SELECT * FROM station").fetchone()
        context = {"station": station, "message": message or session.pop("message", None)}
        if station and g.user:
            context["stock"] = db.execute("""
                SELECT products.sku,name,reorder,code,COALESCE(SUM(delta),0) AS balance
                FROM products CROSS JOIN warehouses
                LEFT JOIN movements ON movements.sku=products.sku AND movements.warehouse=code
                GROUP BY products.sku,code ORDER BY products.sku,code
            """).fetchall()
            if g.user["role"] != "employee":
                context.update(
                    fuel=db.execute("""SELECT fuel_grade,pump,SUM(cents) AS cents,
                        SUM(CAST(quantity AS REAL)) AS gallons FROM sales
                        WHERE fuel_grade<>'' AND kind IN ('Sale','Refund')
                        GROUP BY fuel_grade,pump""").fetchall(),
                    totals=db.execute("""SELECT COUNT(*) AS lines, SUM(cents) AS cents
                        FROM sales WHERE kind IN ('Sale','Refund')""").fetchone(),
                    exceptions=db.execute("""SELECT kind,occurred_at,transaction_id,source
                        FROM sales WHERE kind<>'Sale' GROUP BY source,transaction_id,kind
                        ORDER BY MAX(occurred_at) DESC LIMIT 30""").fetchall(),
                    advice=advise(db.execute("""SELECT source,transaction_id,
                        MIN(occurred_at) AS occurred_at FROM sales WHERE kind='Sale'
                        GROUP BY source,transaction_id ORDER BY occurred_at"""),
                        ZoneInfo(station["timezone"]), 2),
                    trend=db.execute("""SELECT substr(occurred_at,1,10) AS day,
                        SUM(cents) AS cents FROM sales WHERE kind IN ('Sale','Refund')
                        GROUP BY day ORDER BY day DESC LIMIT 7""").fetchall()[::-1],
                    history=db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 30").fetchall(),
                )
        return render_template("station.html", **context), status

    @app.get("/sales.csv")
    def export_sales():
        if g.user is None or g.user["role"] == "employee":
            abort(403)
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(("source", *FIELDS))
        for row in g.db.execute("SELECT * FROM sales ORDER BY occurred_at"):
            values = list(row)
            values[7] = f"{Decimal(values[7]) / 100:.2f}"
            for index in (0, 1, 2, 5, 8, 9):
                value = values[index]
                if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
                    values[index] = "'" + value
            writer.writerow(values)
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="sales.csv"'})

    return app
