CREATE TABLE camera_setup(camera TEXT PRIMARY KEY, protocol TEXT NOT NULL CHECK(protocol IN ('RTSP','ONVIF','DVR/NVR')), queue_zone TEXT);
CREATE TABLE queue_tracks(camera TEXT NOT NULL, track TEXT NOT NULL, observed REAL NOT NULL, zone TEXT, PRIMARY KEY(camera,track));
CREATE INDEX queue_tracks_observed ON queue_tracks(observed);
CREATE TABLE alert_delivery(incident INTEGER PRIMARY KEY REFERENCES incidents, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, delivered REAL, error TEXT);
