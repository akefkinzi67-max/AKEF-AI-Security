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

As a manager, refresh the latest 100 events and assign each register-specific POS source to its camera. A POS recording link requests 20 seconds on either side of the timestamp. Confirm camera identity, clock alignment, existence and usefulness of footage before acting. Opening clips uses the manager's separate Frigate session. Real equipment verification, independent video retention/export, outage alarms and automated alert delivery remain pending.

[Frigate camera configuration](https://docs.frigate.video/configuration/cameras/) · [Events API](https://docs.frigate.video/integrations/api/events-events-get/) · [Recording clip API](https://docs.frigate.video/integrations/api/recording-clip-camera-name-start-start-ts-end-end-ts-clip-mp-4-get/)
