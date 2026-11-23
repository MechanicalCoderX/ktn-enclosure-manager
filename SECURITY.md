# Security

This application runs against a live ZFS storage system, so its security model
is deliberately narrow. The short version: **the only thing it can write is a
drive bay's IDENT LED.**

## Threat model

The app is assumed to be reachable by anyone on the LAN, and the browser is
assumed to be hostile. "It's only on the LAN" is explicitly *not* treated as
authentication.

## What the application can and cannot do

| | |
|---|---|
| **Can** | Read enclosure sysfs, run allow-listed read-only `sg_ses` pages, read the TrueNAS API, turn a bay's IDENT LED on and off with one allow-listed SCSI command |
| **Cannot** | Write any SES page, power a drive off, reset a PHY or expander, touch fault LEDs, change PSU or fan state, flash firmware, run arbitrary commands, read or write any file outside its own data directory |

There is no code path from an HTTP request to a shell. `subprocess` is called
with `shell=False` and an argv assembled entirely from an in-code allow-list
(`backend/ktnmgr/enclosure/ses.py`). The browser cannot supply a device path, a
page name outside the allow-list, or any `sg_ses` argument.

## The one write, and how it is constrained

An IDENT request is expressed semantically as `identify(enclosure_id, slot)`:

1. `enclosure_id` must match `^0x[0-9a-f]{4,32}$`.
2. `slot` must be a genuine `int` (a `bool` is rejected — `True` must not become
   slot 1) in `0..1023`.
3. The enclosure is then **resolved from that id by scanning sysfs**, inside the
   privileged process. A path is never accepted from the caller.
4. The LED is set with a single SCSI command, `sg_ses --index=T,E --set=ident`,
   where `T` is discovered from the enclosure's own configuration page and `E`
   is the validated slot. Both are formatted integers; every other argv element
   is a fixed literal.
5. The result is verified by reading the sysfs `locate` attribute back, polling
   until it settles.

`--set=ident` and `--clear=ident` are the **only** mutating SES arguments the
code can emit. There is no code path to `--set=device_off`, `--clear=fault`, a
PHY reset, or microcode download, and a test asserts it.

Because of (1) and (2), the hostile inputs named in the specification —
`7;rm -rf /`, `../../etc/passwd`, `$(id)`, `7 --set=device_off`,
`0,0 --clear=fault`, `/dev/sg0` — are not *escaped*; they are **unrepresentable**.
`tests/test_security.py` asserts this at both the validator and the HTTP layer,
and additionally proves that a write touches only the `locate` attribute.

## Privilege separation

```
browser ──HTTP──> web process (uid 1000, no capabilities, no device access)
                        │
                        │  unix socket, semantic request only
                        ▼
                  IDENT helper (root)  ──> /sys/.../locate, /dev/sg16
```

The web process holds **no** capabilities and **no** access to `/dev/sg*`. Both
the IDENT write and the read-only `sg_ses` telemetry cross the socket. The
helper re-validates every request independently — it does not trust the web
process to have validated anything — and enforces the enclosure allow-list.

The helper and the web process share one directory, the `/run/ktn` tmpfs
(owned root:1000, mode `0770`/`2770`, no sticky bit), so the helper treats
paths in it as controlled by the web process:

- **Enclosure lock** (opened by root on every poll): opened with
  `O_NOFOLLOW`, then checked through the descriptor - a regular file with a
  single link - before root changes its mode. A symlink, hard link, FIFO,
  socket or device planted there is refused, never followed.
- **Helper socket**: checked once, at helper start, with `lstat` that the
  entry is still the socket the helper just bound before root changes its
  group and mode. The tmpfs is empty at every container start and the web
  process is not yet running when this happens.

## Container privileges

The shipped `docker-compose.yml` drops **all** capabilities and adds back only:

| Capability | Why |
|---|---|
| `SETUID`, `SETGID` | `setpriv` drops the web process to uid 1000; the root helper uses `SETGID` (setegid) to create its socket under gid 1000, so no `CHOWN` is needed |

`no-new-privileges` is set, the **default AppArmor profile is left in place**,
`--privileged` is never used, and there is no `/sys` bind mount. The only host
access is the SES device node.

Both Python processes that run as root start isolated, with `-I -S`: the
image's HEALTHCHECK and the privileged IDENT helper. Neither reads `PYTHON*`
variables or the user site, and Python's `site` start-up does not run, so no
`.pth` file or `sitecustomize` is executed by them: a file planted there by a
lesser process never runs as root. The HEALTHCHECK needs only the standard
library and has no site-packages on its path at all. The helper puts only the
bundled backend and the interpreter's own site-packages on its import path,
appended directly, which makes them importable without running any `.pth`
file in them; none of those directories is writable by the web process's
uid.

Notably absent: `CAP_DAC_OVERRIDE`. The LED is driven by a SCSI command rather
than a sysfs write, so no capability is needed for it at all.

### `/dev/sg16` is granted `rw`, and the `w` is for exactly one command

The `w` exists solely for the IDENT LED. Measured on the live shelf
(2026-08-20): the device cgroup gates the *open mode*, not `SG_IO` itself.
Every telemetry page works through a read-only open — since v1.5.2 all reads
pass `--readonly`, so they run on an `O_RDONLY` fd on which the kernel's
per-opcode filter refuses write-class SCSI commands outright (kernel-enforced:
no bug in the read path can reach the control page). The LED is SEND
DIAGNOSTIC, a write-class command, which requires a write-opened fd — that is
the entire reason for the `w`.

An earlier revision here claimed `SG_IO` "counts as a write even for a
read-only diagnostic page", measured as `:r` failing `EPERM`. That measurement
was real but misread: `sg_ses` opens the device `O_RDWR` by default, so the
failure was the open being refused, not the ioctl. This grants access to the
*enclosure processor*, not to disk data. `CAP_SYS_RAWIO` was tested and is
**not** required, so it is not granted.

**Monitoring-only mode:** grant the device `:r` instead. All telemetry keeps
working; Identify returns a clear permission error instead of lighting the
LED. A deployment that never wants the write can prove it to the kernel.

### Why there is no AppArmor relaxation

Docker's default AppArmor profile (`docker-default`) denies every write under
`/sys`, regardless of uid, capabilities, or mount flags. Measured: with
`docker-default` a `locate` write fails `EACCES` even as root with
`CAP_DAC_OVERRIDE` and `/sys` mounted `rw`.

Version 1.0.0 therefore asked for `apparmor=unconfined` to use the sysfs path.
**1.1.0 onwards do not.** `SG_IO` is not restricted by that profile, so the same
LED is driven by a SCSI command on a device the container already has, under
full default confinement. Verified on real hardware: the LED lights and clears with
`--cap-drop ALL` and `docker-default` active.

The sysfs path still exists as `KTN_IDENT_METHOD=sysfs` for enclosures where the
SES command is unavailable. Choosing it reintroduces the need for a writable
`/sys`, `CAP_DAC_OVERRIDE` and `apparmor=unconfined`; `auto` (the default)
prefers the SCSI path and only falls back if `sg_ses` is missing.

## Response headers

Every response carries a strict `Content-Security-Policy` (the bundle is
entirely self-hosted — no CDN, no inline script, no external fonts — so nothing
legitimate needs relaxing), `frame-ancestors 'none'` and `X-Frame-Options: DENY`
so another page cannot frame the UI and trick a click onto Identify, plus
`nosniff`, `Referrer-Policy: no-referrer` and a restrictive `Permissions-Policy`.

HSTS is deliberately omitted, with or without `KTN_TLS_ENABLED`: this app is
normally served over plain HTTP on a LAN, where a stray HSTS header would pin a
hostname the operator cannot serve over TLS, and an HSTS policy covers every
port of a host - so with TLS on it would be setting policy for whatever else
answers on that name (on TrueNAS, the appliance UI already sends its own).

## Supply chain

Base images and the BuildKit frontend are pinned by digest. Runtime Python
dependencies are pinned with SHA-256 hashes in `backend/requirements.txt`, and
the image build refuses any file that does not match. CI runs the tests and
E2E on that same lock and fails if it no longer matches what
`backend/requirements.in` compiles to. The lock is installed from wheels only
(`--only-binary :all:`), so none of the dependencies is built during the
image build, and a pin without a wheel fails the build rather than running a
build backend nobody hashed. Runtime dependencies are declared by name, never
through an extra, so the image's `pip check` covers every one. The project's
own build backend, setuptools, comes from a second hash-pinned lock,
`backend/build-requirements.txt`, installed the same way; the project is built
with `--no-build-isolation`, so pip fetches nothing unhashed, and setuptools is
uninstalled before the image is finished (since 1.6.5). CI starts the arm64
image under emulation as well as the amd64 one, so both published
architectures are run, not only scanned, before a release (since 1.6.5).

CodeQL (`security-extended`) runs on Python and TypeScript weekly and on every
pull request, `pip-audit` (on the lock and on the test environment) and
`npm audit` cover dependencies, Trivy scans the built image for HIGH/CRITICAL
issues, and Dependabot keeps all four current. For the Python lock it picks
each direct dependency's version under the lowest Python `requires-python`
allows, which is kept equal to the image's Python, so no release the image
can run is held back.

## Authentication

Required by default. It can be turned off, and what that does is spelled out
below rather than left implicit.

- Argon2id password hashing; no default password is ever generated or printed.
- First run has **no account**: only the bootstrap endpoint works until an
  administrator is created in the browser. Bootstrap refuses once any account
  exists.
- Session cookies are signed, `HttpOnly`, `SameSite=Strict`, and `Secure` when
  served over HTTPS, with a fixed expiry. With `KTN_TLS_ENABLED` the app itself
  serves HTTPS and the cookie is `Secure` with nothing else to set (see
  [Serving HTTPS](#serving-https-with-the-truenas-certificate-opt-in)).
  Behind a TLS-terminating reverse proxy, set `KTN_FORWARDED_ALLOW_IPS` to the proxy's address so the app sees
  the HTTPS scheme and marks the cookie `Secure`. Name only the actual proxy:
  a trusted peer also controls `X-Forwarded-For` — the address login rate
  limiting keys on — so `'*'` (or a proxy other clients can reach) lets a
  direct client rotate fabricated addresses past the limiter.
- Login is rate limited per client address.
- Mutating requests require a custom header, which a cross-site form post cannot
  set — combined with `SameSite=Strict`, that is the CSRF defence.
- A missing user and a wrong password cost the same time.
- Changing a password bumps a per-account session epoch carried in the cookie,
  so every existing session for that account stops being accepted. That is the
  action a user takes when they suspect compromise, so it has to be the action
  that ends the attacker's access.
- New accounts start at a random session epoch, not 0, so deleting the
  account file and setting up again cannot revive a cookie that a password
  change or "Sign out everywhere" had ended.
- The account file and the session signing key are `0600` from the moment they
  exist - the key is created by `os.open` with that mode, the account file is
  written to a fresh `mkstemp` temp file pinned `0600` - never written and then
  `chmod`ed, which leaves them world-readable for the width of that window.
- Account changes - first-run bootstrap, password change, session revocation and
  the rehash on sign-in - are serialised in the web process, and each is written
  to its own temp file, fsync'ed and atomically renamed into place. Two
  simultaneous first-run bootstraps cannot both succeed: one creates the
  administrator and the other is refused. The lock is in-process: the app runs
  as a single uvicorn process and is the file's only writer. Running several
  workers (`WEB_CONCURRENCY`) is not supported; it would also multiply the login
  rate limit.
- A temp file left by a crash mid-write (or the fixed-name `users.tmp` that
  earlier releases wrote) is removed at the next startup once it is a minute
  old: exact names only, regular files only, never through a symlink.
- A sign-in's session is bound to the account state it verified, so a password
  change ends every session, including one whose sign-in was still in flight
  when the change landed.
- An account file that exists but cannot be read or parsed **fails closed**. It
  is deliberately not treated as "no accounts": that would reopen the
  unauthenticated bootstrap endpoint and let anyone on the network claim an
  administrator account, then overwrite the real one. A corrupt file, a
  permissions mistake, or a half-finished restore would otherwise hand the
  application away.

### Running it open, and why the LED write is gated separately

`KTN_AUTH_REQUIRED=false` turns the app into an open, unauthenticated
read-only dashboard.

That is the norm for this category on TrueNAS, not a shortcut. Scrutiny,
glances, homepage and speedtest-tracker all serve disk telemetry with no
credentials at all, and scrutiny publishes the same class of data this app
does — serial, WWN, SMART — from an API that answers an anonymous `GET`.
Only 28 of 395 community apps configure any app-level login. Requiring an
account to look at drive temperatures is the unusual choice here, so the
option exists.

Be clear about what it opens: **everything readable**, including
`/api/diagnostics`, the audit log, and raw `sg_ses` page output. Enable it only
on a network you would already let read those.
(Diagnostics show `KTN_TRUENAS_URL` as scheme, host and port only, never its
path, query or any `user:password@`; the host is withheld when the URL contains
`@`.)

One consideration specific to running open: **DNS rebinding**. A malicious page
can repoint its own hostname's DNS at this app and then read API responses as
if they were same-origin — the browser still believes it is talking to the
attacker's site, and the giveaway is the `Host` header, which keeps naming the
attacker's domain. With authentication on this is moot (a request for a foreign
hostname carries no session cookie and gets a 401), which is why the
allowed-hosts setting (`KTN_ALLOWED_HOSTS`, a comma-separated list matched
case- and port-insensitively) defaults to allowing every host rather than
breaking existing bookmark-by-IP deployments. If you disable authentication,
set `KTN_ALLOWED_HOSTS` to the names and addresses you actually browse to.
Loopback Hosts (`127.0.0.1`, `::1`, `localhost`) are always answered even
with the list set: a rebound request's Host names the attacker's domain,
never loopback, so the exemption costs nothing - and the container's own
healthcheck probes over loopback and must keep working.

**It does not open the write.** `KTN_ALLOW_ANONYMOUS_IDENT` is separate and
stays `false` even when authentication is disabled; an anonymous Identify
request is refused with `403` and a message naming the setting. The reason is
that every comparable open dashboard is strictly *read-only*, and this one
actuates hardware. The LED is non-destructive — there is no code path to power
a drive off, reset a PHY, or touch a fault LED, and a test asserts the argv
cannot express one — but a write reachable by anyone on the network should be
a decision someone made deliberately, never a side effect of opening the
dashboard.

Audit entries for an unauthenticated write record the actor as `anonymous`, so
the log never implies a named person approved something nobody signed in for.

### Logout is client-side; revocation is a separate, explicit action

`POST /api/auth/logout` clears the cookie in the browser. It does **not**
invalidate the token server-side — sessions are stateless signed cookies, so a
copy of the cookie captured beforehand stays valid until it expires
(`KTN_SESSION_MAX_AGE_SECONDS`, 8 hours by default).

This is a deliberate trade: the app is single-administrator, and bumping the
session epoch on every logout would sign the same account out of every other
device, which is surprising behaviour for a normal logout.

If you need every session gone right now, you have two remedies (both since
v1.5.0): **Sign out everywhere** in the change-password dialog
(`POST /api/auth/revoke-sessions`) ends every session for the account without
touching the password, and a password change does the same while also
replacing the credential. Before v1.5.0 the password change was the only
option, which forced a credential rotation on anyone who merely suspected a
stolen cookie.

## Serving HTTPS with the TrueNAS certificate (opt-in)

Off by default. With `KTN_TLS_ENABLED` unset or `false` the app serves plain
HTTP exactly as before, and every new setting below is ignored.

When it is on, uvicorn serves TLS itself, using the certificate TrueNAS
already manages for its own UI. The pair reaches the container as two
**read-only, single-file bind mounts** of `/etc/certificates/<name>.crt` and
`.key`, and nothing on the host is changed. Install steps are in
[docs/INSTALL-TRUENAS.md](docs/INSTALL-TRUENAS.md#serving-https-with-the-truenas-certificate).

**Accepted risk R5: the key's blast radius - accept this before turning it
on.** In short, a compromise of KTN exposes the TrueNAS UI's TLS key. The key you
mount is the TrueNAS web UI's own private key. Anyone who gets code execution
in the KTN web process (uid 1000) can read the copy it serves, and with it
impersonate the TrueNAS UI by name to anyone on your LAN they can intercept -
a man-in-the-middle on the appliance's administration login. Issuing KTN a
second certificate for the same name does not help: any valid certificate for
that name impersonates it just as well. The only real isolation is a
different hostname with its own certificate (for example `ktn.<your domain>`),
which this feature deliberately does not do. If that trade is not acceptable,
leave TLS off, or terminate TLS in a reverse proxy that holds a separate
certificate for a separate name.

What limits that exposure:

- **The key copy lives in RAM only.** The entrypoint (root, with no
  capability beyond `SETUID`/`SETGID`) reads the root-only `0400` key through
  its owner bit and pipes it to a uid-1000 writer, which creates
  `/run/ktn/tls/key.pem` as `1000:1000 0600` on the `/run/ktn` tmpfs. It
  refuses to start if that directory is not a tmpfs, so the key never lands on
  the container's writable layer or in a snapshotted dataset. The mounted
  original stays unreadable to uid 1000 (the CI smoke test asserts both).
- **The web process never opens the mounted key**, and never logs or serves
  either key. The renewal watcher hashes only the certificate.
- **Fail closed.** If TLS is on and the files are missing, unreadable,
  unparsable or do not match, the container exits with an `ERROR: TLS ...`
  line naming the paths (never their content) and the recovery,
  `KTN_TLS_ENABLED=false`. It **never falls back to plain HTTP**: a deployment
  that asked for TLS and silently got cleartext is worse than one that visibly
  does not start. An *expired* certificate is the exception - it is served,
  with a warning in the logs and in `/healthz` (`tls.days_left`), because
  refusing it would only turn a browser warning into an outage.
- **Long-syntax binds only** (`type: bind`, `read_only: true`,
  `bind: {create_host_path: false}`). The short `- /etc/certificates/x.key:/tls/...`
  form creates a missing source as a *directory*; if that ever happened at
  `/etc/certificates/<name>.key`, TrueNAS's own certificate writer would fail
  on it and break the appliance UI's TLS.
- **TLS 1.2 is the floor** (Python's default server context; a TLS 1.1 client
  is refused, which the test suite asserts against a control).

Renewal: TrueNAS rewrites the files in place, daily, renewed or not. KTN
compares the mounted certificate's *content* hash with the served copy every
`KTN_TLS_RELOAD_CHECK_SECONDS` (default 3600; `0` disables), and when a change
has stayed unchanged for `KTN_TLS_RELOAD_SETTLE_SECONDS` (default 60) and
parses, it audits `tls.reload` (fingerprints only) and shuts itself down
gracefully. The container's restart policy (`unless-stopped`) brings it back,
and the root entrypoint copies and checks the new pair. That is a few seconds
of outage per renewal. A deployment without a restart policy simply stops -
restart it by hand, or set the check to `0` and restart after each renewal.
The watcher can verify only the renewed *certificate* (the web process cannot
read the key), so a renewal onto a pair that does not load - only the
certificate replaced, or a failed key write on the host - makes the
entrypoint refuse after the restart, and the restart policy loops. That is
the fail-closed choice; the recovery is `KTN_TLS_ENABLED=false` through
`app.update` (see docs/INSTALL-TRUENAS.md), never a tar-extract of a backup.
This restart loop is a **known limitation, deferred (L2)**: KTN neither
detects nor breaks the loop itself. While it lasts nothing is served, not
even `/healthz`, and the only signal is the repeating `ERROR: TLS ...` line
in the container log and the restart count. The
[HTTPS lockouts](README.md#https-lockouts) table lists this and the other
ways to get locked out.

The app and the entrypoint must agree that TLS is on. The app's settings
match variable names case-insensitively, the entrypoint only in exact upper
case, so `ktn_tls_enabled=true` would have meant "on" to the app while uvicorn
served plain HTTP. The entrypoint exports `KTN_TLS_ACTIVE=1` only after it has
built the TLS arguments, and the app refuses to start when that marker and
`KTN_TLS_ENABLED` disagree, in either direction.

No HTTP listener remains when TLS is on, and there is **no http-to-https
redirect**: uvicorn cannot speak both on one port, and a same-port redirect
would mean replacing its listener with custom code on the component that
decides whether the app is reachable at all. Plain HTTP to the TLS port gets
an empty response. A redirect could be added later as its own opt-in
(`KTN_TLS_HTTP_REDIRECT`, not implemented) if real need appears. Consequences
to know:

- Browsers that have opened the TrueNAS UI by name hold its HSTS policy for
  that host, and rewrite `http://<name>:8420` to `https://<name>:8420`
  themselves. A browser that has **never** opened the TrueNAS UI by name, typed
  as `<name>:8420` without `https://`, sends plain HTTP and shows an
  empty-response error. Use the Web UI button or type `https://`.
- If the certificate ever expires, HSTS makes the name **non-bypassable**: the
  browser offers no click-through. Reach the app by IP instead,
  `https://<NAS-IP>:8420`, where the name mismatch is an ordinary warning you
  can click through (HSTS does not apply to IP addresses) - or set
  `KTN_TLS_ENABLED=false` and use `http://<NAS-IP>:8420`.
- Update any `http://` bookmarks, dashboards and monitors to `https://`; they
  stop working the moment TLS is on.

The session cookie is unchanged in name and attributes (`HttpOnly`,
`SameSite=Strict`, now always `Secure`). Cookies are not isolated by port, so
it is also sent to the TrueNAS UI on the same name, and theirs to KTN;
`SameSite` gives no CSRF protection between them. The defence against that
remains the mandatory `X-KTN-Request` header on every mutating request, and
KTN sending no CORS headers.

## Health alerts: the webhook URL is a credential

For ntfy the topic, or an `?auth=` token, *is* the credential, and a generic
webhook URL often carries a token too. So the URL in `KTN_ALERT_WEBHOOK_URL`
is kept out of the logs at every level (since 1.5.7):

- the `httpx` and `httpcore` loggers are held at `WARNING`, so their
  `HTTP Request: POST <url>` line never appears, whatever `KTN_LOG_LEVEL` says;
- the app's own warnings name the endpoint by scheme and host only, and the
  URL is scrubbed from any delivery-error text it quotes. When the URL
  contains `@` the host is withheld too: a username or token holding an
  unencoded `/`, `?` or `#` would otherwise be read as the host (since 1.6.5);
- a malformed URL is logged by error class only, with no part of the URL,
  because the part httpx complains about can be the password or token
  (since 1.6.2).

**Only a 2xx reply is delivery.** The client does not follow redirects, so a
3xx is a failure, like a 4xx, a 5xx, a connection error or a malformed URL.
A health change whose alert was not accepted is not recorded as notified; it
is retried on a 60-second spacing until the endpoint accepts it. A failing
endpoint never disturbs polling.

**What an alert contains.** By default (`KTN_ALERT_DETAIL=full`) a message
names the bay, SES slot, drive serial, `/dev/sdX`, pool/vdev, ZFS state, error
counts and temperature, and the `json` style adds the enclosure's logical id.
On a public ntfy.sh topic, that inventory sits in a third party's cache and
anyone who has the topic name can read it. `KTN_ALERT_DETAIL=minimal` sends
the bay number and condition only. It does not identify the enclosure either,
so on a multi-shelf setup use the dashboard to tell which shelf. A self-hosted
ntfy server avoids the issue entirely.

## TLS: connect by the name on the certificate

**Use `https://` for `KTN_TRUENAS_URL`.** The key is sent in
`auth.login_with_api_key` on every new connection, so with `http://` it
crosses the network in clear. TrueNAS's own API documentation states that it
"automatically revokes any user-linked API keys passed as part of an
authentication attempt via insecure (HTTP) transport", and that HTTPS "is
required for TrueNAS API authentication using API keys"
([TrueNAS 25.10 API documentation](https://www.truenas.com/docs/scale/25.10/api/)).
KTN therefore does not send the key over `http://` or `ws://` (in any letter
case). With such a URL and a key set, it builds no TrueNAS client, logs one
`ERROR` at startup naming the scheme and host - never the key, and the host
is withheld too if the URL contains `@` - and shows the reason in the
dashboard's "TrueNAS unavailable" notice and on the Diagnostics tab. The
drive map, chassis telemetry and Identify keep working. Setting
`KTN_TRUENAS_ALLOW_INSECURE=true` sends the key in clear anyway, with a
`WARNING` at startup. Expect TrueNAS to revoke a key used that way: pool,
vdev and SMART data then fail with an authentication error until you create
a new key and switch to `https://`.

`KTN_TRUENAS_VERIFY_TLS` defaults to `true` and should stay there. If
verification fails, the usual cause is not a missing trust store — it is
connecting by IP.

A certificate is validated against its Subject Alternative Name. TrueNAS
certificates, including the Let's Encrypt ones its ACME integration issues,
typically carry only a DNS name:

```
subject=CN = truenas.example.com
X509v3 Subject Alternative Name: DNS:truenas.example.com
```

`https://10.0.0.5` can never validate against that, no matter what CA bundle
you supply, so setting `KTN_TRUENAS_VERIFY_TLS=false` "fixes" it and quietly
gives up authentication of the endpoint your API key is sent to. Point
`KTN_TRUENAS_URL` at the name instead, and confirm the app can resolve it —
split-horizon DNS pointing that name at a LAN address works fine.

`KTN_TRUENAS_CA_BUNDLE` is for a genuinely private CA. An appliance with a
publicly-issued certificate needs no bundle at all.

One operational consequence worth accepting deliberately: if certificate
renewal ever fails, pool/vdev/SMART data stops rather than silently continuing
over an unverified connection. The drive map, chassis telemetry and Identify
are unaffected — they do not use the API — and the banner names the error.

## The TrueNAS API key: use a least-privilege one

The app calls exactly six read methods (the allow-list in
`backend/ktnmgr/truenas/client.py` is the authoritative set). It does **not**
need an administrator key, and the default path of "create an API key" on
TrueNAS produces one bound to whichever user you were — often `root`, which is
unrestricted.

Create a dedicated account instead. Roles needed:

| Role | Covers |
|---|---|
| `DISK_READ` | `disk.query` |
| `POOL_READ` | `pool.query` |
| `REPORTING_READ` | `disk.temperatures`, `disk.temperature_alerts` |

```
Credentials -> Groups         : add `ktn-readonly`
Credentials -> Users          : add `ktn-readonly`, that group, no shell,
                                password disabled
System -> Advanced -> Privilege: grant the group the three roles above
Credentials -> API Keys       : create one for `ktn-readonly`
```

**`system.info` is deliberately given up — and it costs nothing.** It accepts
only `READONLY_ADMIN` or `SHARING_ADMIN` — no narrow role satisfies it — and
taking either would let a leaked key read the entire appliance configuration
in order to display a version string. Since v1.5.1, when `system.info` is
refused the app falls back to `system.version` (the sixth allow-listed
method), which the middleware declares with `authorization_required=False` —
so the displayed TrueNAS version populates on any key, this role-scoped one
included. The drive map, pool and vdev membership, ZFS error counters,
temperatures and over-temperature alerts all work with the three roles above;
nothing is lost.

Verified on 25.10.5: with those three roles, `pool.query`, `disk.query`,
`disk.temperatures` and `disk.temperature_alerts` all succeed, `system.info`
returns an error (the version arrives via `system.version` instead), and
writes such as `pool.dataset.create` and `user.create` are refused.

One transport note, because it costs an hour otherwise: role-based access is
enforced on the JSON-RPC API at `/api/current`, which is what this app uses.
The legacy REST surface at `/api/v2.0` answered **403 to every one of those
reads** with the same key. If you are testing a narrow key by hand, test it
over the API the app actually speaks.

## Secrets

- The TrueNAS API key is held as a `SecretStr`, never logged, never included in
  an error, and never sent to the browser. `tests/test_api.py` asserts the
  diagnostics payload carries no credential-shaped field.
- Keep `.env` at mode `0600`.
- Audit entries are scrubbed: a `detail` mentioning a credential field name is
  replaced rather than written.

## Audit

Every write is appended to `/data/audit.log` as JSON lines, including automatic
IDENT clearing performed by the timer with no user present
(`user=system:timer`) and by startup reconciliation (`user=system:reconcile`).

## Reporting a vulnerability

Open an issue describing the impact and reproduction. Please do not include
credentials, API keys, or full diagnostic dumps in a public report — the
Diagnostics page has a "Copy diagnostics" button that produces a sanitised
payload suitable for sharing.
