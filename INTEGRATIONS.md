# Station integrations

## Commander exports

Use mappings built from an approved schema and a sanitized sample for the installed Commander release. NAVIQRA provides no default Commander mapping. The public Verifone release note confirms T-Log API availability for Petro Partner accounts, without publishing the endpoint, authentication exchange or response schema.

- Set NAVIQRA_TLOG_MAP to a read-only JSON file. The top-level keys are events and lines (ElementTree selectors relative to the XML root and to each event), fields (a map keyed by the normalized CSV column names), and optional namespaces (prefix to URI).
- For each field, provide scope (event or line) and path (an ElementTree selector for a text node). Optional values maps documented vendor event codes to NAVIQRA's event labels. Optional normalized columns can be omitted. Stable IDs, a timezone-aware timestamp and signed quantity and amount must already be available in the mapped data. Reports requiring attributes, sign transformations or other conversions need an adapter extension and an approved fixture.
- Set NAVIQRA_PLU_MAP to a JSON file with sku, name and barcode keys whose values are the exact column headers of the approved UTF-8 PLU CSV export. Import updates name and barcode for matching SKUs, preserves reorder thresholds and rolls back on conflicting barcodes.
- The optional owner-only partner transport is disabled until NAVIQRA_PARTNER_XML_URL and NAVIQRA_TLOG_MAP are configured. Use NAVIQRA_PARTNER_AUTH for the authorization header only if the approved partner contract explicitly provides a compatible HTTPS GET endpoint that returns XML. Pagination, token acquisition, and other contracts require vendor documentation and further implementation. Never substitute payment endpoints. Use the same source ID for identical transactions arriving through different import paths.
- The mapped adapters and partner transport have synthetic tests. Compatibility with a live Commander or C-Site account remains unverified.

[Verifone C-Site Management release notes](https://verifone.cloud/sites/default/files/inline-files/%5Bcurrent-date%3Ahtml_month%5D/C_SiteManagementReleaseNotes.Nov2025.pdf)

## Frigate and authorized cameras

Run Frigate separately where its workload can be supported. Add approved cameras or recorder channels in Frigate using ONVIF discovery where supported or the device's documented RTSP stream URL. Configure detection, recording, zones and retention in Frigate. Synchronize POS and camera clocks.

Set NAVIQRA_FRIGATE_URL to the trusted Frigate HTTP(S) origin reachable by the server and the manager's browser. Set NAVIQRA_FRIGATE_TOKEN to a valid least-privilege session token for the configured HTTPS origin if required. Frigate manages recording and retention. Keep its internal unauthenticated endpoints off public networks.

Start automatic polling with `docker compose --profile cameras up --build -d`, or in a separate Windows process run `./install-station.ps1 -Monitor` with the same database and Frigate environment as the web process. Set NAVIQRA_EXPECTED_CAMERAS to a comma-delimited list of active Frigate camera names when missing cameras should be tracked from the first check. The monitor reads camera FPS every 30 seconds plus request time. Three consecutive zero or missing FPS readings create a dashboard incident. Three failed statistics requests create a Frigate service incident. Keep intentionally disabled cameras outside the monitored Frigate instance. A restored stream resets its failure count and leaves the incident available for review. The FPS signal does not verify the separate recording stream. Notifications appear inside the dashboard.

Assign each register-specific POS source to its camera. A POS recording link requests 20 seconds on either side of the timestamp. For an imported Frigate event, a manager can open an incident and save its available MP4 in NAVIQRA for playback or download. Saved footage is included in database backups and limited to 16 MiB per clip and 256 MiB in total. Restrict backups to authorized personnel. Frigate recording availability, live devices, stream-role diagnostics and off-app alert delivery require further verification or integration.

Queue observation accepts a camera or register identifier, a timezone-aware timestamp and a headcount. Recent observations are shown for five minutes. An automated counting adapter must be calibrated for the station camera and queue zone before use.

Set NAVIQRA_RESTRICTED_ZONES to the comma-delimited names of explicitly designated Frigate zones. The monitor opens one review incident per tracked person or vehicle event in these zones. Review and record the outcome before taking action. Owners can remove local footage from reviewed incidents. The audit and incident records remain, and prior backups follow the station's retention policy.

[Frigate camera configuration](https://docs.frigate.video/configuration/cameras/) · [Events API](https://docs.frigate.video/integrations/api/events-events-get/) · [Recording clip API](https://docs.frigate.video/integrations/api/recording-clip-camera-name-start-start-ts-end-end-ts-clip-mp-4-get/)
