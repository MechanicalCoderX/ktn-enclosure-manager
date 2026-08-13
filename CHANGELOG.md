# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-08-13

First release. Validated end to end against a live EMC KTN-STL3 on
TrueNAS SCALE 25.10.5.

### Added
- Generic SES enclosure discovery over the Linux enclosure sysfs ABI, with no
  hardcoded paths and no dependency on TrueNAS private modules.
- Persistent identity by enclosure logical id + SES slot; disks identified by
  serial and WWN, never by `/dev/sdX`.
- Drive map UI: single horizontal row, Bay 1 = SES slot 0, health conveyed by
  glyph + text + colour, search by bay, slot, serial, device, WWN, pool or vdev.
- Per-bay detail: physical, disk, TrueNAS/ZFS and SMART sections.
- Chassis dashboard: LCC A/B, controllers, SAS expanders, PSUs, cooling with
  RPM, and all temperature sensors, with labels read from the device itself.
- IDENT control with 10s/30s/60s/5min/until-cleared durations, server-side
  timers, read-back verification, persistence across restarts, and startup
  reconciliation that never clears an externally-lit LED.
- TrueNAS integration over JSON-RPC WebSocket with REST v2.0 fallback.
- Local authentication (Argon2id), signed HttpOnly SameSite=Strict sessions,
  login rate limiting, CSRF header requirement, first-run bootstrap.
- Append-only JSON-lines audit log covering automatic clears.
- Privilege separation: unprivileged web process, tiny root helper reachable
  only through a semantic unix-socket protocol.
- Container packaging with `sg3-utils` bundled; no host modification.
- 155 backend tests and 28 Playwright E2E tests, all runnable without hardware.

### Hardware findings recorded in code and tests
- `sg_ses --join` brackets are `[type_descriptor_index, element_index]`, not
  `[subenclosure_id, ...]`; subenclosure attribution requires the configuration
  page. Getting this wrong mislabels LCC A/B and PSU A/B.
- Element labels are not unique: type descriptors 1 (LCC B) and 21 (PSU B) are
  both `Temp. Sensor B`, so elements are keyed on `(type_index, element_index)`.
- Slot-to-device mapping is not alphabetical: slots 11–14 are `sdn, sdp, sdo,
  sdm`.
- SES reports a drive's SAS *port* address, the block layer its *node* WWN;
  they differ by 2, so the two must not be correlated by equality.
- The sysfs `locate` attribute does not settle synchronously with the write
  (measured 0.17–0.22 s). A single immediate read-back returns the previous
  value and reports every successful IDENT as a failed verification.
- `SG_IO` requires the device cgroup `rw` permission even for read-only pages;
  `CAP_SYS_RAWIO` is not required.
- Docker's `docker-default` AppArmor profile denies all `/sys` writes
  regardless of uid, capabilities or mount flags.
