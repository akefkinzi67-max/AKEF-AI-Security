# Station field acceptance checklist

Complete acceptance on an authorized test station or during an approved maintenance window. Record the date, operator, station time zone, application version, Commander version, Frigate version and evidence for each item. Use screened samples without cardholder data and redact recordings and credentials in shared evidence.

## Installation, user access and recovery

- [ ] Download the release ZIP and compare its SHA-256 sidecar. Extract to a private Windows user folder or prepare the approved Docker host.
- [ ] On Windows, double-click Setup NAVIQRA.cmd, create the station in the browser wizard, and verify restart and sign-in. On Docker, confirm loopback binding, persistent volume, restart and optional camera monitor.
- [ ] Verify owner, manager and employee access. Confirm an employee cannot open the reports or incident footage. Disable a test account and verify its existing session ends.
- [ ] Back up to restricted storage and restore to an isolated directory. Compare sales totals, lot balances, operational logs and a playable saved clip. Measure backup time, restore time and recoverable data loss. Record the tested rollback procedure.

## Commander data and time

- [ ] Confirm the actual Commander release and configuration; record whether it is 55.02.05. Confirm the site's partner authorization and Tlog feature assignment if C-Site is used.
- [ ] Obtain approved, sanitized T-Log and PLU examples and file layout documentation. Verify stable site/register and transaction/line identities, timestamps and offset, tender exclusions, event codes, quantities, fuel grades, pump IDs and price units.
- [ ] Reconcile a closed period by store and fuel totals, then by refunds, voids, No Sale and line counts. Try reimporting the identical file and a conflicting revision. Verify the approved replay policy.
- [ ] Compare local display, Commander export and camera timestamps against a trusted clock, including a midnight or daylight-saving boundary. Verify that a POS exception opens the correct camera footage.

## Cameras, queues and alerts

- [ ] Confirm RTSP/ONVIF/recorder channel assignments and capture rates in Frigate. Verify configured detection and recording streams independently.
- [ ] Review an authorized clip in Frigate and in NAVIQRA. Verify playback, sound policy, local retention and saved-clip access controls.
- [ ] Simulate a camera interruption and Frigate interruption; verify outage incident, stale state and recovery. Confirm restricted-zone detection with known entries.
- [ ] Verify the external receiver authenticates and deduplicates Idempotency-Key. Test temporary receiver failure, retry and a timeout after receiver acceptance. Verify one external notification per incident.
- [ ] Define a single-camera queue zone per register. Compare manual headcounts with recent track counts at different light and traffic levels. Include occlusion, stationary people, exits, camera restart and duplicate detection. Record coverage and errors before using counts operationally.

## Inventory and register staffing

- [ ] Reconcile opening stock and costing by warehouse. Verify invoice, partial receipt, expiry, sale, refund, void and duplicate import against expected balance and gross profit.
- [ ] Compare historical staffing estimates with actual counts for stable periods, promotions and partial data. Verify the station's service-time assumption and document staffing overrides. Record weekday coverage and forecast error before using the recommendations.
- [ ] Sign off by operations and the station owner after resolving exceptions. Keep production credentials and recordings out of the release package.
