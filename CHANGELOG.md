# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [1.5.3] — 2026-08-20

Polish release ahead of wider publicity; no behaviour changes.

### Changed
- **The in-app favicon ships at 256px** (was 64px) and the vector source
  `icon.svg` is now served by the app itself, both regenerated from the new
  vector icon introduced alongside 1.5.2. This is the release that actually
  delivers them in the image — they had only existed in the repo.

### Docs and repo (shipped between tags, recorded here for the release notes)
- Full docs accuracy audit; a CI guard now keeps the README version-neutral
  about the image tag.
- Screenshots regenerated to depict the real 15-bay shelf: the fixture's
  synthetic test slot (SES 99) no longer appears in user-facing images.
- Identify demo GIF added to the README; issue form templates (bug +
  hardware compatibility report) and GitHub Discussions enabled.
- README fan section corrected to the measured per-bank speeds.

## [1.5.2] — 2026-08-20

The `/dev/sg*` permission floor, measured properly this time.

### Security
- **Every telemetry read now runs `sg_ses --readonly`.** Reads open the device
  `O_RDONLY`, on which the kernel's per-opcode filter refuses write-class SCSI
  commands outright — so no bug in the read path can ever reach the enclosure
  control page. This is kernel-enforced, not application-promised.

### Added
- **Monitoring-only deployment mode.** Because reads are now genuinely
  read-only, granting the device `:r` instead of `:rw` is a valid deployment:
  every telemetry page keeps working (measured on the live shelf) and
  Identify returns a clear permission error instead of lighting the LED.

### Fixed
- **The `rw` justification was wrong, and is now exact.** Since 1.1.0 the
  docs claimed "`SG_IO` counts as a write even for a read-only diagnostic
  page — with `:r` every call fails EPERM." The measurement was real but
  misread: `sg_ses` opens the device `O_RDWR` *by default*, so `:r` refused
  the open, never the ioctl. Re-measured with `--readonly`: the device cgroup
  gates the open mode; reads pass on `:r`; only SEND DIAGNOSTIC (the IDENT
  LED — the app's single write) needs a write-opened fd. The `w` in `rw` is
  for exactly one command, and SECURITY.md, the compose header, the README
  and the catalog submission texts now say precisely that.

## [1.5.1] — 2026-08-20

Clears the project's parked "can't be done" list: two of the three turned out
to be doable after all, and the third was our own stale documentation.

### Added
- **`truenas_version` now populates on the least-privilege key.**
  ``system.info`` accepts only READONLY_ADMIN/SHARING_ADMIN, so on the
  recommended role-scoped key diagnostics showed ``truenas_version: null``
  forever - the documented cost of not holding a broad key for a version
  string. Reading the middleware source showed ``system.version`` is declared
  with ``authorization_required=False``, so the one field the UI uses is
  available on any key. The client allow-lists it and ``poll_system_info``
  falls back to it when ``system.info`` is denied; a real outage still
  reports the original error rather than a silent empty success.
- **`truenas/fix-custom-app-metadata.py`** - gives the custom app a real card
  title, icon and displayed version. TrueNAS hardcodes every custom app's
  metadata to a stub (title "Custom App", ``1.0.0_custom``, no icon) and no
  compose ``x-`` extension can reach it - but the WebUI reads a collective
  cache rebuilt by ``app.metadata.generate`` from the app's own
  ``metadata.yaml``, and ``app.update`` merges only portals over that file.
  Editing the display fields there is therefore durable across app updates.
  Verified on 25.10.6: title, icon and version all render. The script backs
  both files up and prints the rollback.

### Fixed
- **INSTALL-TRUENAS.md no longer claims the card icon is impossible.** The
  earlier "writing one into the stored metadata is ignored - verified" note
  was wrong: the edit only *looks* ignored until ``app.metadata.generate``
  runs, because the WebUI reads the collective cache, not the per-app file.
- **SECURITY.md's "logout is client-side" section caught up with v1.5.0**: it
  still told operators a password change was the only way to end every
  session, a release after ``revoke-sessions`` shipped exactly that without
  the forced credential rotation.

## [1.5.0] — 2026-08-19

Security-review release: four findings from a full-code review, none critical,
all fixed.

### Security
- **The change-password endpoint is now rate limited.** It verifies a password
  exactly like login does, but the login limiter never saw it because no login
  happens. That mattered because this endpoint is the one a stolen session
  cookie gets pointed at: the cookie expires, the password does not, and
  `current_password` was brute-forceable without limit. The same fixed-window
  limiter now guards both; the refusal is HTTP 429 so the browser can say
  "slow down" rather than "wrong password".

### Added
- **`POST /api/auth/revoke-sessions` — sign out everywhere.** The session-epoch
  mechanism has existed since the change-password work, but nothing exposed
  it, so the only remedy for a suspected stolen cookie was a full password
  change. The new endpoint bumps the epoch (ending every session for the
  account, the caller's included) without touching the password, and the
  change-password dialog now offers it. Logout alone never did this: sessions
  are stateless signed cookies, so deleting the browser's copy leaves a stolen
  copy working until it expires.
- **`KTN_TRUENAS_REST_FALLBACK`** (default `false`) gates the legacy REST
  `/api/v2.0` fallback, which is now **off by default**. Two measured reasons:
  a role-scoped key — the recommended kind — is refused wholesale by REST
  (403 on every read that works over JSON-RPC), so on such a key the fallback
  could only convert a transient WebSocket blip into a false "TrueNAS rejected
  the API key" alarm for the length of the cooldown; and REST is removed in
  TrueNAS 26.04, at which point the path stops existing. Deployments on a
  full-access key can opt back in. When the fallback does run and REST answers
  403, the error now says the key lacks REST roles instead of claiming the key
  was rejected.

### Fixed
- **`/healthz` now probes the privileged helper.** The helper runs as a
  background child with nothing supervising it; if it died, IDENT and all SES
  telemetry silently disappeared while the container stayed Healthy, because
  the healthcheck only ever exercised the web process. The endpoint now sends
  `ses_version` over the helper socket when one is configured and returns 503
  if the helper does not answer, so a dead helper marks the container
  unhealthy instead of hiding.
- **Removed the entrypoint's dead `trap`.** It was installed to kill the
  helper on TERM/INT, but the web process is started with `exec`, which
  replaces the shell — the trap could never fire. Container teardown already
  kills the whole cgroup; the trap was cleanup-shaped code that cleaned up
  nothing.

## [1.4.0] — 2026-08-15

### Changed
- **Dependency and toolchain refresh.** Runtime base python 3.13 → **3.14**,
  frontend builder node 20 → **26**, vite 5 → **8**, TypeScript 5 → **7**,
  react 18 → **19**, and nine GitHub Actions to current majors. All
  digest-pinned; the full suite, typecheck, E2E and Trivy scan pass on the new
  toolchain.
- **Four development-scope advisories resolved** — three in vite (including a
  high-severity `server.fs.deny` bypass) and one in esbuild's dev server. They
  were invisible until Dependabot security alerts were enabled on the
  repository; `npm audit --omit=dev` in CI does not see them by design, since
  they are not shipped to users, but they are real for anyone running the dev
  server.

### Fixed
- **CI now tests on the interpreter and Node the image actually ships.** Both
  had silently drifted: the runtime moved to python 3.14 while the suite ran on
  3.13, and the frontend builder moved to node 26 while CI built on 22.

  That drift is not cosmetic. A different Node means a different npm, which
  resolves a different dependency tree - precisely how a missing CSS type
  declaration passed `npm run typecheck` on one and failed the image build on
  the other, for the same commit. Tests that run on an interpreter nobody
  receives can pass while the released image fails.

  Two tests now assert that `verify.yml` and the `Dockerfile` agree on both
  versions, so they cannot drift again.

## Unreleased

### Security
- **The TrueNAS API key no longer needs to be an administrator.** The app calls
  five read methods; the key it used authenticated as `root`, because that is
  what TrueNAS produces if you create a key as the user you happen to be.

  `SECURITY.md` now documents a dedicated account with exactly three roles -
  `DISK_READ`, `POOL_READ`, `REPORTING_READ` - and the drive map, pool and vdev
  membership, ZFS error counters, temperatures and over-temperature alerts all
  work with them.

  **`system.info` is deliberately given up.** It accepts only `READONLY_ADMIN`
  or `SHARING_ADMIN`, so keeping it would mean granting read access to the
  entire appliance configuration in order to display a version string.
  Diagnostics now reports *why* the version is missing instead of showing a
  bare null, and includes `system_info` in its polling freshness so a denied
  call is visible rather than silent.

  Verified on 25.10.5 with such a key: the four data calls succeed,
  `system.info` errors, and writes (`pool.dataset.create`, `user.create`) are
  refused. Also recorded, because it costs an hour otherwise: role-based access
  is enforced on the JSON-RPC API the app uses, while the legacy REST surface
  returned **403 to every one of those reads** with the same key.



### Security
- **The regression suite for the v1.1.1 arbitrary file read did not test the
  vulnerability.** Mutation-checked by restoring the vulnerable SPA route: of
  the ten traversal vectors it covered, **all ten still passed**. The HTTP
  client normalises `../` and `//` forms before they are ever sent, so those
  requests never reached the server in the shape the test intended. The suite
  written for that bug would not have caught that bug.

  Three `%2f`-encoded vectors are added - including the exact form demonstrated
  to reach the filesystem, a single leading encoded slash making the path
  parameter absolute so `pathlib` discards the bundle root. With the guard
  removed, those three fail; with it present, they pass. The mutation result is
  recorded in the test file so the next person does not quietly drop them.

### Changed
- **A release can no longer be published from a build that failed.** Tags do
  not trigger `ci.yml` - it runs on branch pushes and pull requests - so a tag
  went straight to a registry push with nothing verified on that ref. On
  v1.3.3 that published an image while CI was failing on the same commit: the
  frontend layer came from the build cache, so `npm run build` never re-ran and
  never hit the TypeScript error. The release happened to be correct; a cache
  miss would have shipped a broken one.

  The checks now live in a reusable `verify.yml` called by both `ci.yml` and
  `publish-image.yml`, with the publish job gated on it. One definition, so the
  two cannot drift - which is how this hid in the first place, two workflows
  building the same Dockerfile independently.

## [1.3.4] — 2026-08-14

### Fixed
- **The frontend build broke on any clean checkout.** `npm run typecheck` and
  the Docker image build both failed with
  `TS2882: Cannot find module or type declarations for side-effect import of
  './styles.css'`. Adding `src/vite-env.d.ts` with
  `/// <reference types="vite/client" />` - the declaration Vite's own template
  ships - makes the CSS import legal to TypeScript.

  Worth recording *why* it was not caught: it passes on a machine that has
  built before, and it passed on a pristine local clone too. The developer
  environment runs npm 9, which resolves a larger dependency tree than the npm
  10+ on the runners, and something in that extra tree supplied the
  declaration. The build is now independent of which packages a given npm
  version happens to install.

## [1.3.3] — 2026-08-14

### Fixed
- **1.3.1 broke the helper socket, and the chmod that was meant to fix it did
  the opposite.** Chassis telemetry and the drive map showed
  `helper unreachable: [Errno 13] Permission denied`.

  1.3.1 added `chmod 2770` to the entrypoint to guarantee the setgid bit on
  `/run/ktn`. The container has no `CAP_FSETID`, and when a process without it
  chmods a directory whose group it is not a member of, Linux **strips**
  `S_ISGID` from the mode. The tmpfs is mounted `2770` with gid 1000 and root
  is not in group 1000, so the call returned success and silently turned
  `2770` into `0770`. The helper's socket was then created `root:root` and the
  uid-1000 web process could not connect to it at all.

  The chmod is gone. The helper now sets the socket's group itself: a unix
  socket inherits the creating process's effective gid, so it binds with
  `setegid(gid)` and restores afterwards. That needs `CAP_SETGID`, which this
  container does hold - it is one of the two capabilities kept. Nothing now
  depends on the directory's setgid bit, which also settles the catalog case,
  where the library cannot express a setgid mode at all.

  If the group cannot be assumed the helper still binds and warns, because a
  running app with a warning beats no app.

## [1.3.2] — 2026-08-14

### Security
- **An account could be created by anyone while the app ran open.** With
  `KTN_AUTH_REQUIRED=false`, `/api/auth/bootstrap` was still reachable and
  still worked. The account it created was inert only until the operator
  turned authentication on - at which point a stranger's password was the
  administrator credential and the operator was the one locked out. Bootstrap
  is now refused while authentication is disabled, naming the setting to
  enable first. Introduced by 1.3.0 and found by probing it rather than
  reading it.
- **Usernames no longer accept control characters.** The check was
  `isascii()` and a length bound, which passes `"ad\nmin"`. Usernames are
  written into the application log and into every audit line
  (`audit user=%s ...`), so a newline in one lets an account name forge whole
  log entries - undermining the record that is supposed to establish what
  happened. Now an allow-list: 1-64 of `A-Za-z0-9._@-`. Creation only, so an
  existing account with an unusual name can still sign in.

### Fixed
- **An account named `anonymous` was refused the things it was entitled to.**
  That string is the sentinel the API uses for "no session", and it is compared
  against to gate the IDENT write and the password change, so a real signed-in
  user holding that name got `403` from both. It failed closed - denying a
  legitimate user rather than admitting an illegitimate one - and the name is
  now reserved at account creation.

## [1.3.1] — 2026-08-14

### Fixed
- **The catalog-app package could not have rendered.** It was written in the
  right idiom but against a guessed library API, and had never been run. Fixed
  against the real library (2.3.11) by rendering it: `remove_all_caps` is
  `clear_caps`, `add_tmpfs` is `add_storage` with a `tmpfs_config`, `add_port`
  and `portals.add` take different arguments, `resources.set_profile` should
  not be called at all, and `ix_values.yaml` was missing `consts.app_name`,
  which the template references on its very first line. It now renders to valid
  YAML with the portal and notes intact.
- **The setgid bit on the helper socket directory is applied by the
  entrypoint.** The TrueNAS catalog library validates tmpfs modes against
  `^0[0-7]{3}$` and so cannot express `2770` at all - an app installed from the
  catalog would have arrived without it, the helper socket would have been
  `root:root`, and IDENT would have failed with a permission error for the
  uid-1000 web process. Doing it in the entrypoint also makes the plain compose
  deployment independent of the `mode=` tmpfs option.

### Changed
- The catalog package carries a `basic-values.yaml`, the current library
  version and hash, and a real maintainer contact. `SUBMITTING.md` records the
  API corrections, and warns not to run their CI script on a TrueNAS box: the
  library's `is_truenas_system()` is `"truenas" in os.uname().release`, so on
  the appliance every render fails trying to reach a middleware socket that is
  not mounted into the validation container - an error with nothing to do with
  the app being tested.

## [1.3.0] — 2026-08-14

### Added
- **You can change your password from the application.** The endpoint and its
  API client existed from the first release but nothing ever rendered them, so
  the only way to change a password was to call the API by hand. Anyone who
  suspected their credentials were exposed could not act on it from the app -
  which is the one moment the feature exists for. The dialog says plainly that
  every session is signed out, because the epoch bump means that is what
  happens, and the login screen confirms the change rather than showing a
  confusing 401.

  The server now also refuses a "new" password identical to the current one. A
  change made because the old password leaked has to actually change it, and
  the browser is not a control.

- **Authentication is optional: `KTN_AUTH_REQUIRED`, default `true`.** Setting
  it `false` runs the app as an open, read-only dashboard.

  This follows the category, measured rather than assumed: only **28 of 395**
  TrueNAS community apps configure any app-level login, and scrutiny - the
  closest peer - serves the same class of data this app does (serial, WWN,
  SMART) from an API that answers an anonymous `GET` with no credentials at
  all. Verified live, not inferred. Glances, homepage and speedtest-tracker are
  the same. Requiring an account to look at drive temperatures is the unusual
  choice here.

  Be clear about what it opens: **everything readable**, including
  `/api/diagnostics`, the audit log and raw `sg_ses` output.

- **`KTN_ALLOW_ANONYMOUS_IDENT`, default `false` - and it stays false when
  authentication is off.** Every comparable open dashboard is strictly
  read-only; this one actuates hardware. An anonymous Identify is refused with
  `403` naming the setting, and the UI explains it instead of offering a button
  that fails. The LED is non-destructive - no code path can power a drive off,
  reset a PHY or set a fault LED, and a test asserts the argv cannot express
  one - but a write reachable by anyone on the network should be a deliberate
  decision, never a side effect of opening the dashboard.

  Audit entries for an unauthenticated write record the actor as `anonymous`,
  so the log never implies a named person approved something nobody signed in
  for.

  Both settings are exposed as TrueNAS install questions in the catalog
  package, following the 8 community apps that already offer an auth toggle.

## [1.2.10] — 2026-08-14

### Fixed
- **Every process in the container now resolves the same enclosure lock.**
  The path was only exported by the entrypoint, so anything started later -
  a `docker exec` diagnostic, a future sidecar - fell back to the data
  directory instead, failed to create it there, and carried on believing it
  had synchronised. It is now set as an image `ENV` and its directory is
  created unconditionally, so the value is the same however a process is
  started. Found by a diagnostic script that logged the warning written for
  exactly this case.

## [1.2.9] — 2026-08-14

### Security
- **The authentication gate is now proven for every endpoint, from the route
  list rather than a hand-kept one.** Authentication on a read endpoint is a
  `user: CurrentUser` parameter the body never references - it looks unused,
  and deleting it silently removes the check. The list of paths the tests
  probed had already drifted: `/api/raw/pages` was authenticated but untested.
  The test now enumerates the app's OpenAPI paths, so a new endpoint cannot
  be added without either requiring authentication or being named explicitly
  as public. Verified by mutation: removing the parameter from one endpoint
  fails the test.

## [1.2.8] — 2026-08-14

### Changed
- Documentation accuracy pass. Three comments still explained the enclosure
  lock as preventing the SCSI abort messages - the theory 1.2.2 disproved.
  The lock exists for IDENT write atomicity; `--no-time` is what stopped the
  aborts. `.env.example` also still described the old data-directory default.
  No behaviour change.

## [1.2.7] — 2026-08-14

### Fixed
- **1.2.6 broke enclosure serialisation, and its own warning caught it.**
  That release tightened the lock file to `0600` on the reasoning that root
  bypasses the permission check. It does not: the container drops every
  capability except `SETUID`/`SETGID`, so the root helper has no
  `CAP_DAC_OVERRIDE` and is subject to the mode exactly like uid 1000. The
  helper failed with `EACCES` and the app fell back to unsynchronised access,
  logging the warning that was written for precisely this case.

  The lock is `0666` again - it has to be, since either process may create it
  and neither can open the other's `0600` file - but it now lives on the
  private `/run/ktn` tmpfs beside the helper socket instead of in the user's
  data dataset, which is what the tightening was actually trying to achieve.
  A world-writable file nobody outside the container can see, and which never
  reaches a backup, costs nothing. A test now asserts the mode admits a second
  uid.

## [1.2.6] — 2026-08-14

### Changed
- **The enclosure lock file is no longer world-writable.** ⚠️ **Superseded by
  1.2.7 — this change was wrong and broke serialisation.** It was created
  `0666` so the root helper and the web process could both open it whichever
  started first. That put a world-writable file in the user's data dataset for
  the sake of a lock. The entrypoint now pre-creates it as uid 1000 with mode
  `0600` before the helper starts: the web process owns it, and root bypasses
  the mode check anyway — which it does **not**, because the container drops
  `CAP_DAC_OVERRIDE`. See 1.2.7.
- **The version is asserted to be consistent across all seven files that state
  it.** Keeping `__init__.py`, `pyproject.toml`, `package.json`, both compose
  files and the catalog-app package in step by hand is exactly the kind of
  thing that rots quietly — the catalog-app package sat four releases behind
  before this pass caught it, and a compose file pinning an old tag would ship
  users a stale container while the release notes described a fix they were not
  getting. A test now fails the build instead, and it also requires a CHANGELOG
  entry for the current version.

## [1.2.5] — 2026-08-14

### Security
- **Permissions are repaired on upgrade, not just on creation.** 1.2.4 created
  the account file, session key, audit log and IDENT state `0600` — but
  `O_CREAT`'s mode applies only when the file is created, so a deployment
  upgraded from an earlier version kept its world-readable audit log (and, if
  it predated the fix, its signing key) indefinitely. Verified on the live
  system: `audit.log` was still `0644` after 1.2.4 shipped. Existing files are
  now narrowed at startup.

### Changed
- **The UI stops polling while the tab is hidden.** It asked for the bay map
  every five seconds whether or not anyone was looking — about 17,000 requests
  a day from a tab left open overnight, each composing all fifteen bays
  server-side. Polling now pauses on `visibilitychange` and resumes with an
  immediate refresh, so what you see on returning to the tab is current rather
  than up to one interval stale.
- API path segments are encoded rather than interpolated raw.
- The catalog-app package (for submission to `truenas/apps`) was pinned at
  1.2.0, four releases behind. Its version and image tag now track the release.

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
