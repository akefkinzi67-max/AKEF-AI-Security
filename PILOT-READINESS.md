# Pilot readiness

## Status

Ready for isolated field acceptance after the station owner approves site connectivity and the acceptance checklist is scheduled. Commercial acceptance requires station-specific evidence. The build starts empty. Automated verification uses explicitly synthetic data.

## Verification matrix

| Area | Automated or isolated evidence | Field acceptance |
| --- | --- | --- |
| Windows | Setup form creation, dependency installation, local Waitress startup, browser-equivalent onboarding, authentication, stock creation and restart against the restored database | Repeat with a clean station PC and its WinGet policy. Check restart, backup location and Windows account recovery. |
| Docker | Build and start, initialize a synthetic station, verify stock, back up, copy to a separate restored database, then start the application against the restored data | Rehearse backup, update and recovery on station storage. Record recovery time and recovery point. |
| Records | Import idempotency, conflict rollback, supplier edits, partial receipts, lot balances, return allocation, date-filtered pending sales and exact quantity accumulation | Reconcile authorized POS exports and opening balances against known station totals. |
| Access | CSRF rejection, trusted request hosts, sign-in limits, revocation and authenticated recording access | Review operator permissions, network isolation and restricted backup storage with the station owner. |
| PWA | Manifest, icons, secure cookie configuration, static offline response and authenticated media denial can be checked independently of station devices. See browser results attached to the review release. | Verify Home Screen installation, touch layout, private TLS trust, playback, sign-out and offline behavior on authorized iPhone, iPad, Android phone and Android tablet devices. |
| Frigate | A documented-path loopback HTTP fixture exercised stats, event ingestion, incident creation, clip retrieval and clip preservation through backup. Fixture media validates transport and storage. | Verify capture and recording separately on authorized hardware, playback of real clips, clock alignment, outage transitions, restricted zones and retention. |
| Alerts and queues | Synthetic tracked-person messages covered duplicates, exits and stale events. Mocked HTTPS failures covered retry and stable idempotency keys. | Test with the authorized Frigate broker and receiver. Measure queue error and coverage under realistic traffic and confirm receiver deduplication. |
| Staffing | Chronological synthetic cases cover stable demand, shifts, sparse hours, and capacity thresholds. Guardian incorporates open purchase quantities. | Compare station forecasts to actual visits and validate service-time and coverage assumptions. |
| Verifone | Synthetic mapped T-Log, PLU and partner transport tests; required normalized output fields validated. | Confirm Commander 55.02.05 with the operator, obtain official site-specific samples and Partner contract, and obtain explicit authorization before any test connection. |

## Go or no-go

Use the [field acceptance checklist](FIELD-ACCEPTANCE.md). Record the station version and evidence for each acceptance item. Review governance for external alerts and footage, and plan a recovery exercise before using the build for live operations. Keep the Pilot running on loopback during initial acceptance.

The workstation wall presents four cameras by default and batches visible frames when expanded. Treat loopback benchmark figures as synthetic estimates. Frigate inference, recording storage, active mobile clients, LAN traffic, and service-time assumptions require separate field evidence. See [phone and tablet installation](PHONE-AND-TABLET.md) for private HTTPS configuration.
