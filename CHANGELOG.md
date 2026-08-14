# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [1.2.4] — 2026-08-14

### Corrected
- **1.2.3's portal finding was wrong, and its "fix" made things worse.** That
  release claimed `x-portals` with `host: 0.0.0.0` produced a link that "fails
  in every browser", and turned the host into a `### EDIT ###` line. It does
  not fail. The WebUI rewrites it on click — `openPortalLink()` replaces a
  `0.0.0.0` hostname with `window.location.hostname`, bracketing IPv6 — so
  `0.0.0.0` follows whatever address you browsed to, including a hostname or a
  VPN address, which a hardcoded value cannot do. The stored URL really is
  `http://0.0.0.0:8420/` and the API really does return it that way; only the
  rendered button matters. `host: 0.0.0.0` is restored as the default and
  documented as deliberate. Catalog apps show the same thing for the same
  reason.

### Security
- **The default IDENT path was not covered by the enclosure lock.** 1.2.1 added
  a lock so an IDENT write and its settle read-back could not be interleaved
  with other enclosure traffic — but wrapped only the sysfs writer. The SES
  writer is the *default*, so in practice the guarantee applied to the path
  almost nobody uses, and a concurrent slot sweep could sample a bay mid-write
  and cache a half-applied locate state for the UI.
- **A failed IDENT attempt left no audit record.** Only writes that returned
  were audited, so the cases most worth a trail of — the helper refusing, the
  enclosure gone, a permission failure — were exactly the ones that left none.
- The audit log and the IDENT state file are created `0600`. They carry
  usernames and drive serials.

### Fixed
- **A silent TrueNAS connection could stall all polling permanently.**
  `recv()` has no timeout of its own, so a socket that was open but never
  answered — a half-open connection after a partition, an appliance
  mid-restart — blocked inside the client lock forever. Every TrueNAS poll
  froze for the life of the process, and the UI showed indefinitely stale pool
  data with no error to explain it. The whole request/reply exchange is now
  time-boxed, including the id-mismatch loop.
- **The notifier reported success on a rejected POST.** A 404 from a mistyped
  ntfy topic, or a 401 from a webhook wanting auth, was logged as `notified:`.
  For an alerting path that is the worst possible failure mode: it looks
  healthy while nothing arrives. Status is now checked.
- **Notifications were delivered one at a time.** Losing the TrueNAS
  connection changes every bay at once, so 15 bays against a dead endpoint
  meant 15 × the 10s timeout — two and a half minutes of stalled polling
  caused by the component least entitled to stall it. They are now sent
  concurrently.
- `GET /api/audit`'s `limit` ceiling matches what `AuditLog.tail()` actually
  clamps to (500), instead of advertising 1000 and silently returning fewer.

### Changed
- **`disk.temperature_alerts` is wired up, and its signature was wrong.** It
  was allow-listed and wrapped but never called — and the wrapper omitted the
  `names` argument the appliance requires, so it would have failed with
  `[EINVAL] names: Field required` had anything used it. Same class of bug as
  the `smart.test.results` entry removed earlier: a method that could only ever
  have failed, kept alive by having no caller. TrueNAS' own over-temperature
  alert now shows on the bay and counts as a **warning** — ranked below any ZFS
  fault, so a hot *and* faulted disk still reads as failed.
- **`SmartInfo.overall` and `power_on_hours` are gone.** Both were permanently
  `None`: the 25.10 API exposes neither, so nothing could ever fill them, and
  the UI rendered two blanks that read as missing data rather than as absent
  capability.
- `AuditLog.tail()` reads from the end of the file. It used to load the whole
  log — up to the 5 MB rotation threshold — and split every line on every
  request, to then discard all but the last hundred.
- Documented that a corrected `x-portals` needs `midclt call
  app.metadata.generate` to appear; `app.update` re-renders the compose but
  leaves the cached metadata `app.query` reads.

## [1.2.3] — 2026-08-14

### Security
- **An unreadable account file no longer reopens the bootstrap endpoint.**
  `users.json` failing to read or parse was treated as "no accounts yet",
  identical to a genuine first run. That made `needs_bootstrap` true, reopened
  the unauthenticated `/api/auth/bootstrap` endpoint to anyone on the network,
  and the first write then overwrote the real accounts. A corrupt file, a
  permissions mistake or a half-finished restore was enough to hand the
  application away. An absent file still means first run; a file that exists
  but cannot be read now fails closed, and existing sessions are rejected
  rather than accepted unverified.
- **The account file and session signing key are no longer briefly
  world-readable.** Both were written with `write_text` and `chmod`ed to `0600`
  afterwards, which creates them under the process umask first. They are now
  created `0600` by `os.open`. The signing key is the more serious of the two:
  it forges any session.
- `GET /api/audit` bounds `limit` to 1..1000. It was unbounded, so an
  authenticated caller could ask for the entire log in one response.

### Fixed
- **The Install-via-YAML portal button was a dead link.** `x-portals` shipped
  with `host: 0.0.0.0`, and TrueNAS builds the URL by string concatenation
  without substituting the node address — so the **Web UI** button opened
  `http://0.0.0.0:8420/`, which fails in every browser. It is now a marked
  `### EDIT ###` line. The catalog-app template was never affected: it uses
  `tpl.portals.add_portal()`, which fills the host in.

### Changed
- **Health-change evaluation no longer runs every second.** It ran on every
  poll-loop tick regardless of whether anything had been polled, and each pass
  composes every bay — roughly seven sysfs reads per disk. On a 15-bay shelf
  that was ~100 file reads a second, indefinitely, to re-answer a question
  whose inputs had not changed. It now runs only after a poll actually
  refreshed something. No effect unless alerting is enabled.
- **Disk identity is cached.** Serial, WWN, model, firmware, capacity and
  rotational flag do not change while a disk sits in a bay, but every bay-map
  composition re-read all of them. The cache is keyed on `(device name, wwid)`
  and never on the name alone: a replacement drive on the validation system was
  assigned the same `/dev/sdf` the removed drive had held, so a name-keyed
  cache would have shown the previous drive's serial against the new disk. One
  wwid read confirms the disk is the same one and saves the other six; a disk
  with no wwid is never cached.
- CI builds on Node 22; Node 20 is deprecated on GitHub runners.
- `SECURITY.md` documents that logout is client-side — sessions are stateless
  signed cookies, so a captured cookie stays valid until expiry. Changing the
  password is what revokes them.

## [1.2.2] — 2026-08-14

### Fixed
- **The kernel-log flood from 1.2.1 is actually fixed now, and the 1.2.1
  diagnosis was wrong.** Every `sg_ses` invocation is now passed `--no-time`.

  sg_ses 2.86 issues a `REPORT TIMESTAMP` command before anything else, to
  stamp its output. The KTN-STL3 does not support it, so it comes back
  `DID_SOFT_ERROR` and the HBA logs one abort per invocation — visible in
  sg_ses's own stderr all along:

  ```
  report timestamp: transport: Host_status=0x0b [DID_SOFT_ERROR]
  ```

  Measured on hardware: 20 invocations without the flag produced 21 abort
  messages; 40 invocations with it produced 0.

  1.2.1 blamed concurrent SES access and added a lock. That was wrong — the
  message rate was byte-for-byte identical after it shipped. The aborts have
  nothing to do with concurrency: they are one unsupported command per
  `sg_ses` run. The lock is retained, with a corrected rationale, because it
  does something genuinely useful and unrelated: it keeps an IDENT write and
  its settle read-back from being interleaved with other enclosure traffic,
  so a reader cannot observe a half-applied locate state.

## [1.2.1] — 2026-08-14

### Fixed
- **Attempted fix for an HBA abort flood; superseded by 1.2.2, which
  identified the real cause.** The lock this release added is still present
  and still useful, but it did not fix what this entry claimed it fixed. The
  original text follows for the record.

  On the validation system the kernel log filled with

  ```
  mpt2sas_cm0: log_info(0x31120434): originator(PL), code(0x12), sub_code(0x0434)
  ```

  — `PL_LOGINFO_CODE_ABORT` — at roughly 5,700 messages a day, two every poll
  cycle. Pausing the container stopped it completely (0 events in 95s paused,
  6 in the next 65s running), which is how it was pinned down.

  The cause is concurrent access to the enclosure. Reading a slot attribute
  under `/sys/class/enclosure` is not a passive file read: it makes the kernel
  `ses` driver issue a diagnostic to the shelf. The 5-second slot poll and the
  30-second `sg_ses` poll therefore both talk to the enclosure processor, and
  the KTN-STL3 will not service two SES requests at once — so one gets
  aborted. The read succeeds on retry, which is why nothing ever looked
  broken; the only symptom was log noise, and it kept the kernel ring buffer
  permanently saturated with its own messages, hiding real `mpt3sas` history.

  All enclosure access — sysfs slot sweeps, `sg_ses` page reads, and IDENT
  writes — is now serialised through a single lock. It is an `flock`, not a
  `threading.Lock`, because in the hardened deployment the two readers are in
  *different processes*: the web process reads sysfs as uid 1000 while
  `sg_ses` is executed by the privileged helper, so an in-process lock would
  exclude nothing. Failing to take the lock is never fatal — it degrades to
  unsynchronised access and warns once, since the lock only suppresses log
  noise and must never take enclosure reporting down.

  The IDENT write and its settle poll are now covered by the same lock, so a
  concurrent read can no longer observe a half-applied locate state.

  The lock file is `enclosure.lock` in the data directory (override with
  `KTN_ENCLOSURE_LOCK`). No configuration change is needed to get the fix.

## [1.2.0] — 2026-08-14

### Added
- **Health notifications.** The app already knew the moment a bay degraded but
  only showed it if you happened to be looking. It now posts to an ntfy topic
  or any webhook when a bay's health changes, with a message that names the
  bay, SES slot, serial, device, pool/vdev and error counters - enough to walk
  to the shelf and pull the right drive.

  Fires on *transitions* only, so a permanently degraded pool does not message
  every poll, and the last observed health is persisted so a restart does not
  re-announce what you have already been told. A failure present at startup
  does notify, because that is exactly what an operator needs to hear. A
  broken notification endpoint can never disturb polling.

  Configure with `KTN_ALERT_WEBHOOK_URL`, `KTN_ALERT_STYLE` (`ntfy` or `json`)
  and `KTN_ALERT_ON_RECOVERY`. Empty URL disables it.
- **Security response headers** on every response: a strict `Content-Security-
  Policy` (the bundle is entirely self-hosted, so nothing legitimate needs
  relaxing), `frame-ancestors 'none'` and `X-Frame-Options: DENY` so another
  page cannot frame the UI and trick a click onto Identify, plus `nosniff`,
  `Referrer-Policy` and a restrictive `Permissions-Policy`.
- **CodeQL, dependency auditing and image scanning in CI.** CodeQL runs
  `security-extended` on Python and TypeScript weekly and per PR; it flags
  precisely the `Path / user_input -> FileResponse` shape that produced the
  1.1.1 traversal and survived the whole test suite, ruff and mypy. `pip-audit`
  and `npm audit` cover dependencies, Trivy scans the built image, and
  Dependabot keeps pip, npm, Actions and the base image digests current.
- **The drive's SAS address** is now shown in the bay detail panel, parsed from
  the SES additional element status. It was defined in the model and never
  populated.
- An application **icon**, used by the TrueNAS app card and as the web UI
  favicon.
- `x-notes` on the TrueNAS app, so the first-run warning and the app's
  privileges appear on its page in the Apps UI.

### Changed
- Base images are **pinned by digest**. Floating tags made builds
  irreproducible and an upstream change invisible; Dependabot bumps them.
- The SMART panel now says *why* overall status and power-on hours are absent
  instead of showing a bare "—" that reads as broken.

### Fixed
- `smart.test.results` was in the client's allow-list but **does not exist on
  TrueNAS 25.10.5**, so it could only ever have failed. Removed, and
  `disk.temperature_alerts` - which does exist - added in its place.

### Not done, and why
- Overall SMART status and power-on hours are still unavailable: TrueNAS 25.10
  exposes no SMART attribute endpoint at all, and reading them directly would
  mean passing every disk device into the container. That would undo the
  minimal-privilege model this app is built around, for two cosmetic fields.

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
