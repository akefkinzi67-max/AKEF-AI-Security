import csv
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from flask import Flask, Response, abort, g, redirect, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from analytics import pos_exception_signal, register_forecast
from cameras import fetch_clip, fetch_events, fetch_stats, origin, recording_url
from monitor import save_events, timestamp
from queue_tracking import estimates
from commander import get_partner_xml, import_plu, tlog_csv
from inventory import add_lot, create_order, movement, post_sale, receive_invoice, take_stock

ROOT = Path(__file__).parent
FIELDS = ("transaction_id", "line_id", "occurred_at", "kind", "sku", "quantity",
          "amount", "fuel_grade", "pump")
SCHEMA_VERSION = 6


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
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_REFRESH_EACH_REQUEST=False,
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
        g.user = g.db.execute("SELECT * FROM users WHERE username=? AND active=1 AND session_version=?",
                              (session.get("username"), session.get("version", 0))).fetchone()
        if request.method == "POST":
            csrf = session.get("csrf", "")
            if not csrf or not secrets.compare_digest(csrf, request.form.get("csrf", "")):
                abort(403)
        session.setdefault("csrf", secrets.token_urlsafe(32))

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
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
        g.db.execute("INSERT INTO users(username,password,role) VALUES (?,?,?)",
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
        elif action == "update-user":
            if g.user["role"] != "owner":
                abort(403)
            username = required(form["username"], 64)
            role, active = form["role"], form["active"]
            if role not in ("manager", "employee") or active not in ("0", "1"):
                raise ValueError("Select a valid account status and role.")
            changed = db.execute("""UPDATE users SET role=?,active=?,session_version=session_version+1
                WHERE username=? AND role<>'owner'""", (role, int(active), username)).rowcount
            if not changed:
                raise ValueError("Select a manager or employee account.")
            detail = f"{username} / {role} / {'active' if active == '1' else 'disabled'}"
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
        elif action == "incident":
            event_id = form.get("event", "").strip() or None
            camera = required(form["camera"]) if not event_id else ""
            if event_id:
                event = db.execute("SELECT camera FROM camera_events WHERE id=?", (event_id,)).fetchone()
                if event is None:
                    raise ValueError("Select an existing camera event.")
                camera = event["camera"]
            incident = db.execute("""INSERT INTO incidents(camera,category,detail,event_id,opened)
                VALUES (?,'Security review',?,?,?)""",
                (camera, required(form["detail"], 1000), event_id, timestamp())).lastrowid
            detail = f"Incident {incident} recorded"
        elif action == "review-incident":
            incident = int(form["incident"])
            changed = db.execute("UPDATE incidents SET reviewed=?,reviewer=? WHERE id=? AND reviewed IS NULL",
                                 (timestamp(), g.user["username"], incident)).rowcount
            if not changed:
                raise ValueError("Select an open incident.")
            detail = f"Incident {incident} reviewed"
        elif action == "delete-clip":
            if g.user["role"] != "owner":
                abort(403)
            incident = int(form["incident"])
            changed = db.execute("""DELETE FROM incident_clips WHERE incident=?
                AND EXISTS (SELECT 1 FROM incidents WHERE id=? AND reviewed IS NOT NULL)""",
                (incident, incident)).rowcount
            if not changed:
                raise ValueError("Select a reviewed incident with a saved clip.")
            detail = f"Saved clip removed for incident {incident}"
        elif action == "queue":
            camera = required(form["camera"])
            stamp = datetime.fromisoformat(form["observed"])
            if stamp.utcoffset() is None:
                raise ValueError("Use a timestamp with an explicit UTC offset.")
            db.execute("INSERT INTO queue_observations VALUES (?,?,?)",
                       (camera, stamp.astimezone(timezone.utc).isoformat(timespec="microseconds"), int(form["waiting"])))
            detail = f"Queue observation for {camera}"
        elif action == "post-sale":
            detail = post_sale(db, form, g.user["username"], required)
        elif action == "source-camera":
            source, camera = required(form["source"]), required(form["camera"])
            if not re.fullmatch(r"[A-Za-z0-9_-]+", camera):
                raise ValueError("Enter a valid Frigate camera name.")
            db.execute("INSERT INTO source_cameras VALUES (?,?) ON CONFLICT(source) DO UPDATE SET camera=excluded.camera",
                       (source, camera))
            detail = source
        elif action == "camera-setup":
            camera = required(form["camera"])
            protocol = form["protocol"]
            queue_zone = form.get("queue_zone", "").strip() or None
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", camera) or (
                queue_zone and not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", queue_zone)
            ):
                raise ValueError("Use Frigate configuration IDs for the camera and zone.")
            db.execute("""INSERT INTO camera_setup VALUES (?,?,?) ON CONFLICT(camera)
                DO UPDATE SET protocol=excluded.protocol,queue_zone=excluded.queue_zone""",
                (camera, protocol, queue_zone))
            db.execute("DELETE FROM queue_tracks WHERE camera=?", (camera,))
            detail = f"Camera setup for {camera}"
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
            account = g.db.execute("SELECT * FROM users WHERE username=? AND active=1",
                                   (request.form["username"],)).fetchone()
            password = request.form["password"]
            if account and len(password) <= 128 and check_password_hash(account["password"], password):
                session.clear()
                session.permanent = True
                session["username"] = account["username"]
                session["version"] = account["session_version"]
                return redirect("/")
            return dashboard("Invalid username or password.", 401)
        try:
            events = None
            clip = None
            partner_payload = None
            if action == "camera-check":
                if g.user is None or g.user["role"] == "employee":
                    abort(403)
                camera = required(request.form["camera"])
                if not frigate:
                    raise ValueError("Configure the Frigate service endpoint first.")
                if not g.db.execute("SELECT 1 FROM camera_setup WHERE camera=?", (camera,)).fetchone():
                    raise ValueError("Select a configured camera.")
                fps = fetch_stats(frigate, os.getenv("NAVIQRA_FRIGATE_TOKEN")).get(camera)
                if fps is None:
                    session["message"] = f"Frigate is reachable. Camera {camera} is missing from its statistics."
                else:
                    session["message"] = f"Frigate is reachable. {camera} currently reports {fps:g} captured frames per second."
                return redirect("/#cameras")
            if action == "save-clip":
                if g.user is None or g.user["role"] == "employee":
                    abort(403)
                incident = int(request.form["incident"])
                row = g.db.execute("""SELECT e.id FROM incidents i JOIN camera_events e
                    ON e.id=i.event_id WHERE i.id=? AND e.has_clip=1""", (incident,)).fetchone()
                if not frigate or not row:
                    raise ValueError("The incident has no available Frigate recording.")
                clip = fetch_clip(frigate, os.getenv("NAVIQRA_FRIGATE_TOKEN"), row["id"])
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
                if clip is not None:
                    held = g.db.execute("SELECT COALESCE(SUM(length(content)),0) FROM incident_clips WHERE incident<>?",
                                        (incident,)).fetchone()[0]
                    if held + len(clip) > 256 * 1024 * 1024:
                        raise ValueError("The station clip storage limit has been reached.")
                    g.db.execute("INSERT INTO incident_clips VALUES (?,?) ON CONFLICT(incident) DO NOTHING",
                                 (incident, clip))
                    message = f"Clip saved for incident {incident}."
                    audit(action, message)
                elif events is None:
                    message = operate(action, partner_payload)
                else:
                    save_events(g.db, events)
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
                forecast, error, validation_hours, schedule = register_forecast(db.execute("""
                    SELECT MIN(occurred_at) AS occurred_at FROM sales WHERE kind='Sale'
                    GROUP BY source,transaction_id
                """), tz, minutes, include_profile=True)
                latest_sale = db.execute("SELECT MAX(occurred_at) FROM sales").fetchone()[0]
                signal = None
                if latest_sale:
                    since = datetime.fromisoformat(latest_sale) - timedelta(days=30)
                    signal = pos_exception_signal(db.execute("""
                        SELECT MIN(occurred_at) AS occurred_at, MAX(kind<>'Sale') AS exception
                        FROM sales WHERE occurred_at>=? GROUP BY source,transaction_id
                    """, (since.isoformat(timespec="microseconds"),)), tz)
                context.update(
                    fuel=db.execute("""SELECT fuel_grade,pump,SUM(cents) AS cents,
                        SUM(CAST(quantity AS REAL)) AS gallons FROM sales
                        WHERE fuel_grade<>'' AND kind IN ('Sale','Refund') AND occurred_at>=? AND occurred_at<?
                        GROUP BY fuel_grade,pump""", bounds).fetchall(),
                    totals=db.execute("""SELECT COUNT(*) AS lines, SUM(cents) AS cents,
                        SUM(CASE WHEN fuel_grade<>'' THEN cents ELSE 0 END) AS fuel_cents,
                        SUM(CASE WHEN fuel_grade='' THEN cents ELSE 0 END) AS store_cents
                        FROM sales WHERE kind IN ('Sale','Refund') AND occurred_at>=? AND occurred_at<?""", bounds).fetchone(),
                    exceptions=[dict(row, clip=recording_url(frigate, row["camera"], row["occurred_at"])
                                     if frigate and row["camera"] else None) for row in db.execute("""
                        SELECT kind,MAX(occurred_at) AS occurred_at,transaction_id,source,camera
                        FROM sales LEFT JOIN source_cameras USING(source)
                        WHERE kind<>'Sale' AND occurred_at>=? AND occurred_at<?
                        GROUP BY source,transaction_id,kind ORDER BY MAX(occurred_at) DESC LIMIT 30""", bounds)],
                    advice=forecast,
                    schedule=schedule,
                    evaluated_days=validation_hours // 24,
                    forecast_error=error,
                    validation_hours=validation_hours,
                    exception_signal=signal,
                    trend=db.execute("""SELECT substr(occurred_at,1,10) AS day,
                        SUM(cents) AS cents FROM sales WHERE kind IN ('Sale','Refund') AND occurred_at>=? AND occurred_at<?
                        GROUP BY day ORDER BY day DESC LIMIT 7""", bounds).fetchall()[::-1],
                    history=db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 30").fetchall(),
                    suppliers=db.execute("SELECT * FROM suppliers ORDER BY name").fetchall(),
                    orders=db.execute("""SELECT o.*,s.name,
                        (SELECT COUNT(*) FROM order_receipts r WHERE r.order_id=o.id) AS receipts,
                        (SELECT SUM(quantity) FROM purchase_order_lines p WHERE p.order_id=o.id) AS ordered_units,
                        (SELECT COALESCE(SUM(quantity),0) FROM order_receipts r
                         JOIN invoice_lines l ON l.invoice=r.invoice WHERE r.order_id=o.id) AS received_units
                        FROM purchase_orders o
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
                    camera_setup=db.execute("SELECT * FROM camera_setup ORDER BY camera").fetchall(),
                    queue_estimates=estimates(db, time.time()),
                    health=db.execute("""SELECT camera,checked,CASE WHEN checked<? THEN 'Stale' ELSE state END AS state
                        FROM camera_health ORDER BY camera""", ((datetime.now(timezone.utc) -
                        timedelta(minutes=2)).isoformat(timespec="microseconds"),)).fetchall(),
                    incidents=db.execute("""SELECT i.*,e.has_clip,c.incident IS NOT NULL AS saved FROM incidents i
                        LEFT JOIN incident_clips c ON c.incident=i.id
                        LEFT JOIN camera_events e ON e.id=i.event_id ORDER BY i.reviewed IS NOT NULL,i.id DESC LIMIT 50""").fetchall(),
                    delivery=db.execute("""SELECT incident,attempts,delivered,error FROM alert_delivery
                        ORDER BY incident DESC LIMIT 15""").fetchall(),
                    queue=db.execute("""SELECT * FROM queue_observations WHERE observed<=? AND observed>=?
                        ORDER BY observed DESC LIMIT 1""", (timestamp(), (datetime.now(timezone.utc) -
                        timedelta(minutes=5)).isoformat(timespec="microseconds"))).fetchone(),
                    frigate=frigate,
                    vendor_maps=maps.keys(),
                    partner=bool(partner),
                    minutes=minutes,
                )
                if g.user["role"] == "owner":
                    context["accounts"] = db.execute("""SELECT username,role,active FROM users
                        WHERE role<>'owner' ORDER BY username""").fetchall()
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

    @app.get("/incidents/<int:incident>/clip.mp4")
    def incident_clip(incident):
        if g.user is None or g.user["role"] == "employee":
            abort(403)
        row = g.db.execute("SELECT content FROM incident_clips WHERE incident=?", (incident,)).fetchone()
        if row is None:
            abort(404)
        return send_file(BytesIO(row["content"]), mimetype="video/mp4", conditional=True)

    return app
