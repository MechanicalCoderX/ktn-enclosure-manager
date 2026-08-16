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

## Container privileges

The shipped `docker-compose.yml` drops **all** capabilities and adds back only:

| Capability | Why |
|---|---|
| `SETUID`, `SETGID` | `setpriv` drops the web process to uid 1000 |

`no-new-privileges` is set, the **default AppArmor profile is left in place**,
`--privileged` is never used, and there is no `/sys` bind mount. The only host
access is the SES device node.

Notably absent: `CAP_DAC_OVERRIDE`. The LED is driven by a SCSI command rather
than a sysfs write, so no capability is needed for it at all.

### `/dev/sg16` is granted `rw`, and that is not what it sounds like

`sg_ses` submits SCSI commands through the `SG_IO` ioctl, which the device
cgroup classifies as a write even for a read-only diagnostic page. Measured:
with `:r` every `sg_ses` call fails `Operation not permitted`. This grants access
to the *enclosure processor*, not to disk data. `CAP_SYS_RAWIO` was tested and is
**not** required, so it is not granted.

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

HSTS is deliberately omitted: this app is normally served over plain HTTP on a
LAN, and a stray HSTS header would pin a hostname the operator cannot serve over
TLS.

## Supply chain

Base images are pinned by digest. CodeQL (`security-extended`) runs on Python
and TypeScript weekly and on every pull request, `pip-audit` and `npm audit`
cover dependencies, Trivy scans the built image for HIGH/CRITICAL issues, and
Dependabot keeps all four current.

## Authentication

Required by default. It can be turned off, and what that does is spelled out
below rather than left implicit.

- Argon2id password hashing; no default password is ever generated or printed.
- First run has **no account**: only the bootstrap endpoint works until an
  administrator is created in the browser. Bootstrap refuses once any account
  exists.
- Session cookies are signed, `HttpOnly`, `SameSite=Strict`, and `Secure` when
  served over HTTPS, with a fixed expiry.
- Login is rate limited per client address.
- Mutating requests require a custom header, which a cross-site form post cannot
  set — combined with `SameSite=Strict`, that is the CSRF defence.
- A missing user and a wrong password cost the same time.
- Changing a password bumps a per-account session epoch carried in the cookie,
  so every existing session for that account stops being accepted. That is the
  action a user takes when they suspect compromise, so it has to be the action
  that ends the attacker's access.
- The account file and the session signing key are created `0600` by `os.open`,
  not written and then `chmod`ed — the latter leaves them world-readable for
  the width of that window.
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

### Known limitation: logout is client-side

`POST /api/auth/logout` clears the cookie in the browser. It does **not**
invalidate the token server-side — sessions are stateless signed cookies, so a
copy of the cookie captured beforehand stays valid until it expires
(`KTN_SESSION_MAX_AGE_SECONDS`, 8 hours by default).

This is a deliberate trade: the app is single-administrator, and the
alternative — bumping the session epoch on logout — would sign the same
account out of every other device, which is surprising behaviour for a normal
logout. If you need every session gone right now, change the password; that
does revoke them all.

## The TrueNAS API key: use a least-privilege one

The app calls exactly five read methods. It does **not** need an
administrator key, and the default path of "create an API key" on TrueNAS
produces one bound to whichever user you were — often `root`, which is
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

**`system.info` is deliberately given up.** It accepts only `READONLY_ADMIN`
or `SHARING_ADMIN` — no narrow role satisfies it — and taking either would let
a leaked key read the entire appliance configuration in order to display a
version string. The drive map, pool and vdev membership, ZFS error counters,
temperatures and over-temperature alerts all work without it; diagnostics
simply shows no TrueNAS version, with the reason alongside it.

Verified on 25.10.5: with those three roles, `pool.query`, `disk.query`,
`disk.temperatures` and `disk.temperature_alerts` all succeed, `system.info`
returns an error, and writes such as `pool.dataset.create` and `user.create`
are refused.

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
