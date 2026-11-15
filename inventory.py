"""Inventory operations run inside the caller's write transaction."""
from datetime import date
from collections import Counter
from decimal import Decimal, InvalidOperation


def units(value, minimum=1):
    value = int(value)
    if not minimum <= value <= 100_000_000:
        raise ValueError("Enter a valid whole-unit quantity.")
    return value


def money(value):
    try:
        cents = Decimal(value) * 100
        if not cents.is_finite() or not 0 <= cents <= 10_000_000_000 or cents != int(cents):
            raise ValueError
        return int(cents)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a nonnegative USD cost to the nearest cent.") from None


def movement(db, sku, warehouse, delta, reason, actor):
    db.execute("INSERT INTO movements(sku,warehouse,delta,reason,actor) VALUES (?,?,?,?,?)",
               (sku, warehouse, delta, reason, actor))


def add_lot(db, sku, warehouse, quantity, cost=None, expires=None, invoice=None):
    db.execute("""INSERT INTO lots(sku,warehouse,remaining,unit_cents,expires,invoice)
                  VALUES (?,?,?,?,?,?)""", (sku, warehouse, quantity, cost, expires, invoice))


def take_stock(db, sku, warehouse, quantity):
    lots = db.execute("""SELECT * FROM lots WHERE sku=? AND warehouse=? AND remaining>0
                         ORDER BY expires IS NULL, expires, id""", (sku, warehouse)).fetchall()
    taken = []
    for lot in lots:
        amount = min(lot["remaining"], quantity)
        db.execute("UPDATE lots SET remaining=remaining-? WHERE id=?", (amount, lot["id"]))
        taken.append((lot, amount))
        quantity -= amount
        if quantity == 0:
            return taken
    raise ValueError("Insufficient stock at the source warehouse.")


def receive_invoice(db, form, actor, required):
    number = required(form["number"])
    warehouse = required(form["warehouse"])
    purchased = date.fromisoformat(form["purchased"]).isoformat()
    order_id = int(form.get("order", "") or 0)
    supplier = int(form["supplier"])
    if order_id:
        order = db.execute("SELECT * FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
        if (not order or order["status"] != "Open" or order["supplier"] != supplier
            or order["warehouse"] != warehouse or order["ordered"] > purchased):
            raise ValueError("The invoice does not match an open purchase order.")
    rows = [row for row in form["items"].splitlines() if row.strip()]
    if not 1 <= len(rows) <= 500:
        raise ValueError("Enter between 1 and 500 invoice lines.")
    invoice = db.execute("INSERT INTO invoices(supplier,number,purchased,warehouse) VALUES (?,?,?,?)",
                         (supplier, number, purchased, warehouse)).lastrowid
    received = Counter()
    for line, row in enumerate(rows, 1):
        fields = row.split(",")
        if len(fields) != 4:
            raise ValueError(f"Invoice line {line} must contain four comma-separated fields.")
        sku, quantity, cost, expires = (value.strip() for value in fields)
        sku = required(sku)
        quantity, cost = units(quantity), money(cost)
        received[sku] += quantity
        expires = date.fromisoformat(expires).isoformat() if expires else None
        db.execute("INSERT INTO invoice_lines VALUES (?,?,?,?,?,?)",
                   (invoice, line, sku, quantity, cost, expires))
        add_lot(db, sku, warehouse, quantity, cost, expires, invoice)
        movement(db, sku, warehouse, quantity, "Invoice receipt", actor)
    if order_id:
        expected = dict(db.execute("SELECT sku,quantity FROM purchase_order_lines WHERE order_id=?", (order_id,)))
        if received != expected:
            raise ValueError("Invoice quantities must match the full purchase order.")
        db.execute("UPDATE purchase_orders SET status='Received',invoice=? WHERE id=?", (invoice, order_id))
    return f"Invoice {number}: {len(rows)} lines received"


def create_order(db, form, required):
    number = required(form["number"])
    ordered = date.fromisoformat(form["ordered"]).isoformat()
    rows = [row for row in form["items"].splitlines() if row.strip()]
    if not 1 <= len(rows) <= 500:
        raise ValueError("Enter between 1 and 500 order lines.")
    order_id = db.execute("""INSERT INTO purchase_orders(supplier,number,warehouse,ordered,status)
        VALUES (?,?,?,?,'Open')""",
        (int(form["supplier"]), number, required(form["warehouse"]), ordered)).lastrowid
    for row in rows:
        fields = row.split(",")
        if len(fields) != 2:
            raise ValueError("Enter each order line as SKU and quantity.")
        sku, quantity = fields
        db.execute("INSERT INTO purchase_order_lines VALUES (?,?,?)",
                   (order_id, required(sku), units(quantity)))
    return f"Purchase order {number} created"


def post_sale(db, form, actor, required):
    identity = tuple(required(form[key]) for key in ("source", "transaction_id", "line_id"))
    row = db.execute("SELECT * FROM sales WHERE (source,transaction_id,line_id)=(?,?,?)",
                     identity).fetchone()
    if row is None:
        raise ValueError("Select an imported transaction line.")
    prior = db.execute("SELECT id FROM stock_postings WHERE (source,transaction_id,line_id)=(?,?,?)",
                       identity).fetchone()
    if prior:
        return "Transaction line was already reviewed"
    mode = form["mode"]
    warehouse = None
    original_id = None
    revenue = row["cents"]
    quantity = Decimal(row["quantity"])
    allocations = []
    if mode == "sell":
        if row["kind"] != "Sale" or row["fuel_grade"] or quantity != int(quantity):
            raise ValueError("Select a merchandise sale with whole-unit quantity.")
        warehouse = required(form["warehouse"])
        count = units(int(quantity))
        allocations = take_stock(db, row["sku"], warehouse, count)
        movement(db, row["sku"], warehouse, -count, "Imported sale", actor)
    elif mode in ("restock", "refund-only", "reverse"):
        expected = "Void" if mode == "reverse" else "Refund"
        if row["kind"] != expected or row["fuel_grade"]:
            raise ValueError("Select a merchandise refund or void matching the review action.")
        original_id = int(form["original"])
        original = db.execute("""
            SELECT p.*, s.sku, s.occurred_at FROM stock_postings p JOIN sales s
            USING(source,transaction_id,line_id) WHERE p.id=? AND p.mode='sell'
        """, (original_id,)).fetchone()
        if (not original or original["source"] != row["source"]
            or (row["sku"] and row["sku"] != original["sku"])
            or original["occurred_at"] > row["occurred_at"]):
            raise ValueError("The original posted sale does not match this transaction.")
        warehouse = original["warehouse"]
        reversals = db.execute("SELECT COUNT(*),COALESCE(SUM(revenue_cents),0) FROM stock_postings WHERE original=?",
                               (original_id,)).fetchone()
        if mode == "reverse":
            if reversals[0]:
                raise ValueError("A void reversal requires a sale without prior refunds or reversals.")
            revenue = -original["revenue_cents"]
        elif -revenue > original["revenue_cents"] + reversals[1]:
            raise ValueError("Refund exceeds the remaining posted sale revenue.")
        if mode in ("restock", "reverse"):
            if mode == "restock" and (-quantity != int(-quantity) or not quantity):
                raise ValueError("A restocked refund requires a whole-unit quantity.")
            available = db.execute("""
                SELECT lots.*, a.quantity - COALESCE((
                    SELECT SUM(r.quantity) FROM allocations r JOIN stock_postings p ON p.id=r.posting
                    WHERE p.original=a.posting AND r.lot=a.lot
                ),0) AS returnable FROM allocations a JOIN lots ON lots.id=a.lot
                WHERE a.posting=? ORDER BY a.lot DESC
            """, (original_id,)).fetchall()
            count = sum(lot["returnable"] for lot in available) if mode == "reverse" else units(int(-quantity))
            if count > sum(lot["returnable"] for lot in available):
                raise ValueError("Return exceeds the remaining sold units.")
            remainder = count
            for lot in available:
                amount = min(lot["returnable"], remainder)
                if amount:
                    db.execute("UPDATE lots SET remaining=remaining+? WHERE id=?", (amount, lot["id"]))
                    allocations.append((lot, amount))
                    remainder -= amount
            movement(db, original["sku"], warehouse, count, "Imported " + row["kind"], actor)
    elif mode == "skip":
        revenue = 0
    else:
        raise ValueError("Select a valid posting action.")
    posting = db.execute("""
        INSERT INTO stock_postings(source,transaction_id,line_id,warehouse,mode,original,revenue_cents)
        VALUES (?,?,?,?,?,?,?)
    """, (*identity, warehouse, mode, original_id, revenue)).lastrowid
    db.executemany("INSERT INTO allocations VALUES (?,?,?)",
                   [(posting, lot["id"], count) for lot, count in allocations])
    return f"Posting {posting}: {mode}"
