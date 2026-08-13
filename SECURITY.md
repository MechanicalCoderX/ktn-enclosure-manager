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
| **Can** | Read enclosure sysfs, run allow-listed read-only `sg_ses` pages, read the TrueNAS API, turn a bay's IDENT LED on and off |
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
4. The write targets `<enclosure>/<slot>/locate` and nothing else.
5. The result is verified by reading the value back.

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

## Container privileges, and the one relaxation

The shipped `docker-compose.yml` drops **all** capabilities and adds back only:

| Capability | Why |
|---|---|
| `DAC_OVERRIDE` | write the root-owned `locate` attribute |
| `SETUID`, `SETGID` | `setpriv` drops the web process to uid 1000 |

`no-new-privileges` is set. The only device exposed is the SES node. No host
path other than `/sys` is mounted. `--privileged` is **not** used.

### `/dev/sg16` is granted `rw`, and that is not what it sounds like

`sg_ses` submits SCSI commands through the `SG_IO` ioctl, which the device
cgroup classifies as a write even for a read-only diagnostic page. Measured: with
`:r` every `sg_ses` call fails `Operation not permitted`. This grants access to
the *enclosure processor*, not to disk data, and the application only ever issues
allow-listed read-only pages. `CAP_SYS_RAWIO` was tested and is **not** required,
so it is not granted.

### AppArmor and the Identify button

Docker's default AppArmor profile (`docker-default`) **denies every write under
`/sys`**, regardless of uid, capabilities, or mount flags. This was measured, not
assumed: with `docker-default` the locate write fails `EACCES` even as root with
`CAP_DAC_OVERRIDE` and `/sys` mounted `rw`; with the profile removed and an
otherwise identical capability set, it succeeds.

So there are exactly two supported deployment modes:

**Read-only mode (shipped default).** Everything works — the drive map, chassis
telemetry, pool/vdev/ZFS state, SMART, diagnostics, audit — except the Identify
button, which returns a permission error. Nothing about the host is relaxed.

**Identify-enabled mode (opt-in).** Add to the service in `docker-compose.yml`:

```yaml
    security_opt:
      - no-new-privileges:true
      - apparmor=unconfined      # required for the locate write; see above
```

This is a real relaxation and should be a deliberate choice. It remains far
narrower than `--privileged`: capabilities stay dropped to three, the only
device is the SES node, no other host path is mounted, and the web process still
runs unprivileged with no device access.

**The stricter alternative**, if you want Identify without unconfining: install a
custom AppArmor profile on the host permitting writes only to
`/sys/devices/**/enclosure/**/locate`. That is genuinely tighter. It is not the
default because installing it modifies the TrueNAS host (`/etc/apparmor.d`),
which this project deliberately does not do.

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
