# NAVIQRA Gas Station AI Management

Local review build. The station starts empty and operates independently of the POS. Use synthetic inputs for review. Keep the service on loopback until station security, data retention and deployment acceptance are complete.

## Local installation

Install Python 3.12 and create a virtual environment with `python -m venv .venv`. Activate it using the command appropriate for your shell. Install with `python -m pip install -r requirements.txt`.

Generate two separate values with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Set `NAVIQRA_SECRET` and `NAVIQRA_SETUP_TOKEN` in the process environment. Keep the secret stable across restarts. Start with `waitress-serve --listen=127.0.0.1:8080 --call station:create_app`. Open http://127.0.0.1:8080, initialize the station and warehouse, and sign in. Remove the setup token from the process environment for subsequent launches. Owners can add managers and employees. Managers handle stock and imports. Employees can view stock.

On Windows, `./install-station.ps1` creates the virtual environment and installs dependencies. With the environment values set, `./install-station.ps1 -Start` also starts the service.

Alternatively put the same settings in an untracked .env file and run `docker compose up --build`. This recipe binds to loopback and keeps SQLite in a named volume. Keep backups outside that volume.

## Purchasing and stock reconciliation

Create suppliers and receive invoices with existing SKUs, whole-unit quantities, unit costs in USD, and optional expiration dates. Each supplier invoice number can be received once. Downward adjustments and sales draw from soonest-expiring lots first, then older undated lots. Transfers preserve lot cost and expiration. Opening stock and manual receipts have unknown cost unless received through an invoice.

Create a purchase order with the supplier, destination and SKU quantities. An invoice can close an open order when the supplier, destination and total quantities per SKU match. Standalone invoices are supported. Open orders can be canceled. Partial fulfillment remains pending.

Review normalized POS lines before posting them to inventory. For a sale, select the warehouse from which the goods physically left. A restocked refund restores the original cost layers, a refund-only posting changes revenue, and a void can reverse an unreversed sale with no prior refunds. Use Skip for already-accounted opening history and events with no inventory effect. The same imported line can be reviewed once. Posted merchandise gross profit includes only reviewed sales and adjustments; it displays as unavailable if any participating cost is unknown. Date filters use the station time zone and inclusive calendar dates.

## Sales import

Convert an authorized report to UTF-8 CSV with this header:

```csv
transaction_id,line_id,occurred_at,kind,sku,quantity,amount,fuel_grade,pump
```

Use ISO 8601 timestamps with explicit offsets, USD amounts with at most two significant fractional digits, and quantities with up to six decimal places. Use Sale or Refund with signed amounts and quantities, or Void or No Sale with zero amount and quantity. Fuel quantity is in US gallons. Leave fuel grade and pump blank for merchandise. Use a stable source identifier and transaction ID unique within that source. Namespace reused register transaction numbers before conversion. Each line ID must be stable within its transaction. Duplicate records are skipped and conflicting records roll back the entire upload. Each upload is limited to 5 MiB. Remove payment credentials, cardholder data and personal identifiers before conversion.

The import format is NAVIQRA's normalized contract. See [station integrations](INTEGRATIONS.md) to configure the mapping-based Commander adapters, optional partner transport and Frigate.

## Backup, restore and update

Run `python backup.py data/station.sqlite3 /secure/path/station-backup.sqlite3` on a host with access to the database. Use an existing private directory and a new destination filename. The command uses SQLite's backup API and checks integrity. For Compose, make the volume available to a trusted maintenance process before copying. Stop the service before a restore, make a separate copy of the current database, then use the same command to copy a verified backup to a new database path. Set NAVIQRA_DB to the restored path and verify sign-in and reports locally before switching over. Retain the application secret in a separate secure store.

Before updating, verify the backup and test the new version with a restored copy. Roll back using the previous application release and its matching database copy.

## Scope and verification

Run `python -m unittest -v` for synthetic integration verification. The advisor uses observed transaction days and a configurable service time (two minutes by default) and displays estimates only after three observed dates for a weekday. Partial imports can bias estimates. Treat the result as a planning input.

Pending: authorized field maps and vendor acceptance, ATG, camera device and retention acceptance, purchase-order approval workflow, queue measurement, advanced anomaly modeling, secure remote deployment and full station acceptance. Do not use this review build for production station operations.
