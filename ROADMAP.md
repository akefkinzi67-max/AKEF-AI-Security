# Station delivery plan

Baseline: main at 5dda2a23, a static Arabic security demo with simulated controls.

The review branch adds authenticated setup, audited stock and purchasing, repeatable imports, mapped T-Log and PLU adapters, Frigate event and clip references, POS stock reconciliation, and explainable staffing and exception-rate estimates.

Remaining acceptance work:

1. Validate vendor field maps against approved Commander exports. Review the actual C-Site partner API contract and test authorized read-only access. Define an approved ATG adapter.
2. Verify Frigate against station cameras and recorder channels, including clock alignment, retention, clip playback, outages and restricted-zone alerts.
3. Complete partial order fulfillment, cashier queue measurement and role administration recovery. Validate forecast error and coverage against station history.
4. Test backup, restoration, updates and network security on station hardware before release approval.
