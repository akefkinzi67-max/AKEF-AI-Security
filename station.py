import csv
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3

from flask import Flask, Response, abort, g, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from cameras import fetch_events, origin, recording_url
from commander import get_partner_xml, import_plu, tlog_csv
from inventory import add_lot, create_order, movement, post_sale, receive_invoice, take_stock

ROOT = Path(__file__).parent
FIELDS = ("transaction_id", "line_id", "occurred_at", "kind", "sku", "quantity",
          "amount", "fuel_grade", "pump")
SCHEMA_VERSION = 2


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def migrate(path):
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database requires a newer application version.")
        for number in range(version + 1, SCHEMA_VERSION + 1):
            sql = (ROOT / f"migrations/{number:03d}.sql").read_text()
            for statement in sql.strip().split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(f"PRAGMA user_version={number}")
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


def report_window(args, tz):
    start, end = args.get("start", ""), args.get("end", "")
    first = date.fromisoformat(start) if start else None
    last = date.fromisoformat(end) if end else None
    if first and last and first > last:
        raise ValueError("Start date must be on or before the end date.")

    def timestamp(day):
        return datetime.combine(day, datetime.min.time(), tz).astimezone(timezone.utc).isoformat(timespec="microseconds")

    return (timestamp(first) if first else "", timestamp(last + timedelta(days=1)) if last else "9999")


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
    frigate = os.getenv("NAVIQRA_FRIGATE_URL")
    if frigate:
        frigate = origin(frigate)
    maps = {}
    for kind in ("TLOG", "PLU"):
        path = os.getenv(f"NAVIQRA_{kind}_MAP")
        if path:
            maps[kind.lower()] = json.loads(Path(path).read_text(encoding="utf-8"))
    partner = os.getenv("NAVIQRA_PARTNER_XML_URL")
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

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

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

    def operate(action, partner_payload=None):
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
            taken = take_stock(db, sku, warehouse, -delta) if delta < 0 else []
            if delta > 0:
                add_lot(db, sku, warehouse, delta)
            if delta:
                movement(db, sku, warehouse, delta, reason, actor)
            if reason == "Transfer":
                target = required(form["target"])
                if target == warehouse:
                    raise ValueError("Select a different destination warehouse.")
                movement(db, sku, target, quantity, reason, actor)
                for lot, count in taken:
                    add_lot(db, sku, target, count, lot["unit_cents"], lot["expires"], lot["invoice"])
            detail = f"{sku} / {warehouse} / {reason} / {quantity}"
        elif action == "import":
            source = required(form["source"])
            data = request.files["export"].stream.read().decode("utf-8-sig")
            added, duplicates = import_sales(db, source, data)
            detail = f"{source}: {added} added, {duplicates} duplicates"
        elif action in ("tlog", "plu", "partner"):
            kind = "tlog" if action == "partner" else action
            if kind not in maps:
                raise ValueError("Configure the verified vendor field map before importing.")
            payload = partner_payload if action == "partner" else request.files["export"].stream.read()
            if kind == "plu":
                detail = f"{import_plu(db, payload, maps[kind], required)} catalog products processed"
            else:
                source = required(form["source"])
                added, duplicates = import_sales(db, source, tlog_csv(payload, maps[kind], FIELDS))
                detail = f"{source}: {added} added, {duplicates} duplicates"
        elif action == "supplier":
            detail = required(form["name"])
            db.execute("INSERT INTO suppliers(name,contact) VALUES (?,?)",
                       (detail, form.get("contact", "").strip()[:250]))
        elif action == "invoice":
            detail = receive_invoice(db, form, g.user["username"], required)
        elif action == "purchase":
            detail = create_order(db, form, required)
        elif action == "cancel-purchase":
            order_id = int(form["order"])
            changed = db.execute("UPDATE purchase_orders SET status='Canceled' WHERE id=? AND status='Open'",
                                 (order_id,)).rowcount
            if not changed:
                raise ValueError("Select an open purchase order.")
            detail = f"Purchase order {order_id} canceled"
        elif action == "post-sale":
            detail = post_sale(db, form, g.user["username"], required)
        elif action == "source-camera":
            source, camera = required(form["source"]), required(form["camera"])
            if not re.fullmatch(r"[A-Za-z0-9_-]+", camera):
                raise ValueError("Enter a valid Frigate camera name.")
            db.execute("INSERT INTO source_cameras VALUES (?,?) ON CONFLICT(source) DO UPDATE SET camera=excluded.camera",
                       (source, camera))
            detail = source
        elif action == "service-time":
            minutes = int(form["minutes"])
            db.execute("INSERT INTO service_settings VALUES (1,?) ON CONFLICT(id) DO UPDATE SET minutes=excluded.minutes",
                       (minutes,))
            detail = f"{minutes} minutes"
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
            events = None
            partner_payload = None
            if action == "partner":
                if g.user is None or g.user["role"] != "owner":
                    abort(403)
                if "tlog" not in maps or not partner:
                    raise ValueError("Configure a verified partner export contract first.")
                partner_payload = get_partner_xml(partner, os.getenv("NAVIQRA_PARTNER_AUTH", ""))
            if action == "sync-cameras":
                if g.user is None or g.user["role"] == "employee":
                    abort(403)
                if not frigate:
                    raise ValueError("Configure the Frigate server before syncing.")
                events = fetch_events(frigate, os.getenv("NAVIQRA_FRIGATE_TOKEN"))
            with g.db:
                g.db.execute("BEGIN IMMEDIATE")
                if events is None:
                    message = operate(action, partner_payload)
                else:
                    g.db.executemany("""INSERT INTO camera_events VALUES (?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET zones=excluded.zones, has_clip=excluded.has_clip""", events)
                    message = f"Refreshed {len(events)} Frigate events."
                    audit(action, message)
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
                tz = ZoneInfo(station["timezone"])
                today = datetime.now(tz).date()
                try:
                    bounds = report_window(request.args, tz)
                except (ValueError, OverflowError):
                    abort(400, "Enter a valid date range.")
                minutes = db.execute("SELECT minutes FROM service_settings").fetchone()
                minutes = minutes[0] if minutes else 2
                context.update(
                    fuel=db.execute("""SELECT fuel_grade,pump,SUM(cents) AS cents,
                        SUM(CAST(quantity AS REAL)) AS gallons FROM sales
                        WHERE fuel_grade<>'' AND kind IN ('Sale','Refund') AND occurred_at>=? AND occurred_at<?
                        GROUP BY fuel_grade,pump""", bounds).fetchall(),
                    totals=db.execute("""SELECT COUNT(*) AS lines, SUM(cents) AS cents
                        FROM sales WHERE kind IN ('Sale','Refund') AND occurred_at>=? AND occurred_at<?""", bounds).fetchone(),
                    exceptions=[dict(row, clip=recording_url(frigate, row["camera"], row["occurred_at"])
                                     if frigate and row["camera"] else None) for row in db.execute("""
                        SELECT kind,MAX(occurred_at) AS occurred_at,transaction_id,source,camera
                        FROM sales LEFT JOIN source_cameras USING(source)
                        WHERE kind<>'Sale' AND occurred_at>=? AND occurred_at<?
                        GROUP BY source,transaction_id,kind ORDER BY MAX(occurred_at) DESC LIMIT 30""", bounds)],
                    advice=advise(db.execute("""SELECT source,transaction_id,
                        MIN(occurred_at) AS occurred_at FROM sales WHERE kind='Sale'
                        GROUP BY source,transaction_id ORDER BY occurred_at"""),
                        tz, minutes),
                    trend=db.execute("""SELECT substr(occurred_at,1,10) AS day,
                        SUM(cents) AS cents FROM sales WHERE kind IN ('Sale','Refund') AND occurred_at>=? AND occurred_at<?
                        GROUP BY day ORDER BY day DESC LIMIT 7""", bounds).fetchall()[::-1],
                    history=db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 30").fetchall(),
                    suppliers=db.execute("SELECT * FROM suppliers ORDER BY name").fetchall(),
                    orders=db.execute("""SELECT o.*,s.name FROM purchase_orders o
                        JOIN suppliers s ON s.id=o.supplier ORDER BY o.id DESC LIMIT 50""").fetchall(),
                    invoices=db.execute("""SELECT i.*,s.name,SUM(l.quantity*l.unit_cents) AS cents
                        FROM invoices i JOIN suppliers s ON s.id=i.supplier JOIN invoice_lines l ON l.invoice=i.id
                        GROUP BY i.id ORDER BY i.purchased DESC,i.id DESC LIMIT 30""").fetchall(),
                    expiring=db.execute("""SELECT * FROM lots WHERE remaining>0 AND expires<=?
                        ORDER BY expires,id LIMIT 50""", ((today + timedelta(days=30)).isoformat(),)).fetchall(),
                    pending=db.execute("""SELECT s.* FROM sales s LEFT JOIN stock_postings p
                        USING(source,transaction_id,line_id) WHERE p.id IS NULL AND s.fuel_grade=''
                        ORDER BY s.occurred_at DESC LIMIT 50""").fetchall(),
                    postings=db.execute("""SELECT p.id,p.mode,p.source,p.transaction_id,p.line_id,s.sku
                        FROM stock_postings p JOIN sales s USING(source,transaction_id,line_id)
                        ORDER BY p.id DESC LIMIT 30""").fetchall(),
                    margin=db.execute("""WITH cost AS (
                        SELECT a.posting,SUM(a.quantity*l.unit_cents) AS cents,
                        COUNT(*)-COUNT(l.unit_cents) AS unknown FROM allocations a JOIN lots l ON l.id=a.lot
                        GROUP BY a.posting)
                        SELECT COUNT(*) AS lines,COALESCE(SUM(p.revenue_cents),0) AS revenue,
                        COALESCE(SUM(CASE p.mode WHEN 'sell' THEN cost.cents ELSE -cost.cents END),0) AS cogs,
                        COALESCE(SUM(cost.unknown),0) AS unknown
                        FROM stock_postings p JOIN sales s USING(source,transaction_id,line_id)
                        LEFT JOIN cost ON cost.posting=p.id
                        WHERE p.mode<>'skip' AND s.occurred_at>=? AND s.occurred_at<?""", bounds).fetchone(),
                    camera_events=db.execute("SELECT * FROM camera_events ORDER BY occurred_at DESC LIMIT 30").fetchall(),
                    frigate=frigate,
                    vendor_maps=maps.keys(),
                    partner=bool(partner),
                    minutes=minutes,
                )
        return render_template("station.html", **context), status

    @app.get("/sales.csv")
    def export_sales():
        if g.user is None or g.user["role"] == "employee":
            abort(403)
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(("source", *FIELDS))
        tz = ZoneInfo(g.db.execute("SELECT timezone FROM station").fetchone()[0])
        try:
            bounds = report_window(request.args, tz)
        except (ValueError, OverflowError):
            abort(400, "Enter a valid date range.")
        for row in g.db.execute("SELECT * FROM sales WHERE occurred_at>=? AND occurred_at<? ORDER BY occurred_at", bounds):
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
