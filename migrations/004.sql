CREATE TABLE order_receipts(order_id INTEGER NOT NULL REFERENCES purchase_orders, invoice INTEGER PRIMARY KEY REFERENCES invoices);
INSERT INTO order_receipts SELECT id,invoice FROM purchase_orders WHERE invoice IS NOT NULL;
CREATE TABLE camera_health(camera TEXT PRIMARY KEY, failures INTEGER NOT NULL, state TEXT NOT NULL, checked TEXT NOT NULL);
CREATE TABLE incidents(id INTEGER PRIMARY KEY, camera TEXT NOT NULL, category TEXT NOT NULL, detail TEXT NOT NULL, event_id TEXT REFERENCES camera_events, opened TEXT NOT NULL, reviewed TEXT, reviewer TEXT REFERENCES users);
CREATE UNIQUE INDEX one_open_camera_outage ON incidents(camera,category) WHERE reviewed IS NULL AND category IN ('Camera offline','Frigate unavailable');
CREATE TABLE incident_clips(incident INTEGER PRIMARY KEY REFERENCES incidents, content BLOB NOT NULL);
CREATE TABLE queue_observations(camera TEXT NOT NULL, observed TEXT NOT NULL, waiting INTEGER NOT NULL CHECK(waiting BETWEEN 0 AND 100), PRIMARY KEY(camera,observed));
