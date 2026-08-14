# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [1.1.2] — 2026-08-14

Closes the two limitations 1.1.1 documented but did not fix.

### Security
- **Changing a password now invalidates every existing session for that user.**
  Previously a stolen session cookie kept working afterwards - which is the one
  action a user takes when they suspect compromise, so it had to be the action
  that ends the attacker's access. Each account carries a `session_epoch` that
  the cookie records and every request checks; changing the password bumps it.
  `revoke_sessions()` does the same without a password change.

  Accounts and cookies created before this field existed are treated as epoch
  0, so upgrading does not sign anyone out.

### Changed
- **The TrueNAS client reuses one authenticated WebSocket** instead of opening
  a new connection and re-running `auth.login_with_api_key` on every call.
  A 20-second poll cycle was producing three connections and three logins,
  roughly 13,000 authentications a day, each an entry in the appliance's auth
  log.

  Measured against a live TrueNAS 25.10.5: the first call costs 0.377 s
  (connect, authenticate, call); each subsequent cycle of three calls now takes
  0.028 s. One login served 16 calls where 16 logins were needed before.

  One request is in flight at a time so replies cannot be mismatched. A dropped
  socket - including the idle timeout that eventually closes any pooled
  connection - reconnects once transparently; an application-level refusal such
  as a rejected key is *not* retried, because that would double the failed
  login attempts recorded on the appliance. The connection is closed on
  shutdown.
- The REST fallback is now time-boxed. A WebSocket transport failure used to
  latch the client onto REST for the lifetime of the process, so a brief blip
  meant permanent degradation; it now retries the preferred transport after
  five minutes.

## [1.1.1] — 2026-08-14

### Security
- **Fixed an unauthenticated arbitrary file read in the SPA fallback route.**
  The handler resolved a request path with `FRONTEND_DIR / path` and served the
  result if it existed. `pathlib` does not confine that: an absolute path
  replaces the base entirely (`Path("/a/b") / "/etc/passwd"` is `/etc/passwd`)
  and `..` segments walk out of it. The route requires no authentication, so
  anyone able to reach the port could read any file the process could.

  Confirmed against a live instance: `/etc/passwd`, application source,
  `/data/users.json` (Argon2 password hashes) and `/data/session-secret` — the
  key that signs session cookies, and therefore a full authentication bypass.

  The handler now resolves the path first and serves it only if the result is
  inside the bundle. Resolution also collapses symlinks, so a link planted in
  the bundle cannot point out of it. 15 regression tests cover absolute paths,
  `..`, percent-encoded and doubled encodings, backslashes, and symlinks.

  **If you ran 1.0.0 or 1.1.0 on a network you do not fully trust, rotate the
  session secret** (delete `/data/session-secret` and restart, which signs out
  everyone) **and change any account password.**

### Fixed
- The app reported version `1.0.0` in `/api/diagnostics` and its OpenAPI
  document regardless of the release. Version is now single-sourced from
  `ktnmgr.__version__`.
- The login rate limiter never pruned its per-address table, growing one entry
  per distinct source address for the lifetime of the process.
- The audit log grew without bound; it now rotates at 5 MB keeping one previous
  generation.

### Changed
- The container image installs dependencies from `pyproject.toml` rather than a
  duplicated list in the Dockerfile that could silently drift out of sync.
- Tests no longer read the developer's `.env` or ambient `KTN_*` environment.
  This had already caused a real divergence where the suite passed locally and
  failed in CI.

### Known limitations (not changed)
- Changing a password does not invalidate existing sessions.
- The TrueNAS client opens a new WebSocket and re-authenticates on every call,
  three times per poll cycle.

## [1.1.0] — 2026-08-13

Removes the AppArmor relaxation entirely. The container now runs under Docker's
default confinement with no host mounts and two capabilities.

### Changed
- **The IDENT LED is driven by a SCSI command instead of a sysfs write.**
  `sg_ses --index=T,E --set=ident`, where `T` is discovered from the enclosure's
  own configuration page rather than assumed, and `E` is the validated slot.
  Verification still reads the sysfs `locate` attribute, which is a read and
  always permitted.
- Container privileges reduced accordingly:

  | | 1.0.0 | 1.1.0 |
  |---|---|---|
  | AppArmor | `unconfined` required for Identify | **default profile** |
  | `/sys` | bind-mounted read-write | **not mounted at all** |
  | Capabilities | `DAC_OVERRIDE`, `SETUID`, `SETGID` | `SETUID`, `SETGID` |
  | Host paths | `/sys` + data | data only |

- The TrueNAS catalog app drops its "Allow lighting drive-bay LEDs" question;
  Identify now works in the default configuration, so there is nothing to opt
  into.

### Added
- `KTN_IDENT_METHOD` (`auto` | `ses` | `sysfs`). `auto` prefers the SCSI path and
  falls back to sysfs only if `sg_ses` is unavailable. `sysfs` reintroduces the
  need for a writable `/sys` and `apparmor=unconfined`, and exists for
  enclosures that do not honour the SES identify bit.
- Security tests covering the new mutating call: hostile indices are rejected,
  the emitted argv is asserted element by element, and `--set=ident` /
  `--clear=ident` are proven to be the only mutating SES arguments reachable.

### Why this was possible
Docker's default AppArmor profile blocks writes under `/sys` but does not
restrict `SG_IO`. Both facts were measured on real hardware: the sysfs write
fails `EACCES` even as root with `CAP_DAC_OVERRIDE`, while the SCSI command
lights the same LED with `--cap-drop ALL` and the default profile active.
Docker's own read-only `/sys` already exposes `/sys/class/enclosure`, so the
bind mount was unnecessary for reads too.

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
