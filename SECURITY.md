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
**1.1.0 does not.** `SG_IO` is not restricted by that profile, so the same LED is
driven by a SCSI command on a device the container already has, under full
default confinement. Verified on real hardware: the LED lights and clears with
`--cap-drop ALL` and `docker-default` active.

The sysfs path still exists as `KTN_IDENT_METHOD=sysfs` for enclosures where the
SES command is unavailable. Choosing it reintroduces the need for a writable
`/sys`, `CAP_DAC_OVERRIDE` and `apparmor=unconfined`; `auto` (the default)
prefers the SCSI path and only falls back if `sg_ses` is missing.

## Authentication

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
