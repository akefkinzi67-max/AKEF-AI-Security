# NAVIQRA Gas Station AI Management

Local review build. The station starts empty and operates independently of the POS. Use synthetic inputs for review. Keep the service on loopback until station security, data retention and deployment acceptance are complete.

## Local installation

Install Python 3.12 and create a virtual environment with `python -m venv .venv`. Activate it using the command appropriate for your shell. Install with `python -m pip install -r requirements.txt`.

Generate two separate values with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Set `NAVIQRA_SECRET` and `NAVIQRA_SETUP_TOKEN` in the process environment. Keep the secret stable across restarts. Start with `waitress-serve --listen=127.0.0.1:8080 --call station:create_app`. Open http://127.0.0.1:8080, initialize the station and warehouse, and sign in. Remove the setup token from the process environment for subsequent launches. Owners can add managers and employees. Managers handle stock and imports. Employees can view stock.

Alternatively put the same settings in an untracked .env file and run `docker compose up --build`. This recipe binds to loopback and keeps SQLite in a named volume. The Docker recipe requires independent verification on a host with Docker.

## Sales import

Convert an authorized report to UTF-8 CSV with this header:

```csv
transaction_id,line_id,occurred_at,kind,sku,quantity,amount,fuel_grade,pump
```

Use ISO 8601 timestamps with explicit offsets, USD amounts with at most two significant fractional digits, and quantities with up to six decimal places. Use Sale or Refund with signed amounts and quantities, or Void or No Sale with zero amount and quantity. Fuel quantity is in US gallons. Leave fuel grade and pump blank for merchandise. Use a stable source identifier and transaction ID unique within that source. Namespace reused register transaction numbers before conversion. Each line ID must be stable within its transaction. Duplicate records are skipped and conflicting records roll back the entire upload. Each upload is limited to 5 MiB. Remove payment credentials, cardholder data and personal identifiers before conversion.

The import format is NAVIQRA's normalized contract. Native Commander T-Log and PLU mappings require sanitized vendor exports and field documentation. Verifone documents C-Site T-Log API availability for Petro Partner accounts. Authorized access, the API contract and a test site are required before implementing and verifying the C-Site adapter. See [Verifone release notes](https://verifone.cloud/sites/default/files/inline-files/%5Bcurrent-date%3Ahtml_month%5D/C_SiteManagementReleaseNotes.Nov2025.pdf).

## Backup, restore and update

Run `python backup.py data/station.sqlite3 /secure/path/station-backup.sqlite3` on a host with access to the database. Use an existing private directory and a new destination filename. The command uses SQLite's backup API and checks integrity. For Compose, make the volume available to a trusted maintenance process before copying. Stop the service before a restore, make a separate copy of the current database, then use the same command to copy a verified backup to a new database path. Set NAVIQRA_DB to the restored path and verify sign-in and reports locally before switching over. Retain the application secret in a separate secure store.

Before updating, verify the backup and test the new version with a restored copy. Roll back using the previous application release and its matching database copy.

## Scope and verification

Run `python -m unittest -v` for synthetic verification of setup and roles, stock transfers, backup integrity, CSV deduplication and rollback, fuel totals, report exports and hourly estimates. The advisor uses observed transaction days, assumes two minutes of service per visit, and displays estimates only after three observed dates for a weekday. Partial imports can bias estimates. Treat the result as a planning input.

Pending: vendor-native file formats, C-Site, ATG, camera integration and event correlation, Business Guardian, suppliers and purchasing, expiry lots, costs and margins, date filters, remote access, installer and browser/device acceptance. Do not use this review build for production station operations.
