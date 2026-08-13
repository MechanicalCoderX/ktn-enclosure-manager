# Architecture

## Design principle

Do not reproduce TrueNAS Enterprise. Combine three mechanisms that already work
on a community system, and keep each one's failure contained.

```
                     KTN Enclosure Manager
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  Linux enclosure        TrueNAS API             sg_ses
      sysfs                                   (read-only)
        │                     │                     │
   slot mapping         disk / ZFS            chassis telemetry
   slot health          pool / vdev           PSU, fans, temps
   IDENT write          SMART, errors         LCC, expanders
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              ▼
                        EMC KTN-STL3
```

Each source has its own poll interval, its own cache, and its own last-error.
If TrueNAS is down the drive map keeps updating from sysfs; if `sg_ses` times
out only the chassis tab degrades. Nothing takes the application down with it.

## Layers

```
backend/ktnmgr/
  models.py              domain types; identity rules encoded here
  config.py              env-driven settings, secrets as SecretStr
  enclosure/
    sysfs.py             discovery, slot enumeration, locate read/write
    disks.py             local disk identity straight from sysfs
    locate.py            semantic validation + the two write paths
    helper_client.py     unix-socket client for the privileged helper
    ses.py               allow-listed sg_ses execution (local and via helper)
    ses_parser.py        configuration + join page parsers
  truenas/
    client.py            JSON-RPC over WebSocket, REST v2.0 fallback
    correlate.py         pool/vdev/SMART attribution onto block devices
  services/
    state.py             polling, caching, composition of the Bay view
    ident.py             timer engine, persistence, startup reconciliation
    auth.py              Argon2id accounts, signed sessions, rate limiting
    audit.py             append-only JSON-lines audit log
  api/routes.py          semantic HTTP surface
  main.py                wiring
helper/ktn_ident_helper.py   the only privileged process
frontend/                    React + TypeScript + Vite
```

## Identity model

`/dev/sdX` is transient and is never persisted as identity.

```
persistent :  (enclosure logical id, SES slot)     ← what a bay IS
disk       :  serial + WWN                          ← what is IN the bay
runtime    :  /dev/sdX                              ← this boot only
```

The enclosure logical id comes from `/sys/class/enclosure/<x>/id`, so persistent
identity requires no `sg_ses` round trip. Every operation re-resolves the
enclosure by that id before acting, which is what makes a changed `/dev/sgX`,
an HBA reset, or a renumbered SCSI address a non-event.

`/dev/sdX` is used for exactly one purpose: joining this poll cycle's TrueNAS
records to a bay, via `<slot>/device/block`. It is never stored.

## Why the slot mapping comes from sysfs

Two measured facts rule out the obvious alternatives:

- **Alphabetical order is wrong.** Slots 11–14 on this shelf are
  `sdn, sdp, sdo, sdm`. A sort-based implementation is correct for the first
  eleven bays and silently wrong for the last four — the worst possible failure
  mode when the point of the tool is to tell you which drive to pull.
- **WWN matching is wrong.** SES reports a drive's SAS *port* address
  (`…7d86`); the block layer reports its *node* WWN (`…7d84`). They differ by 2,
  so equality matching yields nothing.

The kernel already resolves this correctly through `<slot>/device/block`, so
that is the single source of truth.

## SES parsing

`sg_ses --join` emits `[T,E]` where **T is a type-descriptor index, not a
subenclosure id**. On this shelf T runs 0–25 across five subenclosures. Mapping
an element to its subenclosure therefore requires the configuration page, which
lists type descriptors in the same global order:

```
config page  ->  index 1  = (sub 0, Temperature sensor, "Temp. Sensor B")   LCC B
                 index 21 = (sub 3, Temperature sensor, "Temp. Sensor B")   PSU B
join page    ->  [1,0] and [21,0] are DIFFERENT sensors with the SAME label
```

Elements are therefore keyed on `(type_index, element_index)` and labelled from
the device's own configuration page — the app hardcodes no element names, so
another shelf's labels come through unchanged.

## IDENT lifecycle

```
request ─► validate (hex id, bounded int slot)
        ─► acquire per-enclosure lock
        ─► read current locate
        ─► write locate
        ─► POLL read-back until it settles (measured 0.17–0.22 s)
        ─► audit with previous, result and verification
        ─► persist record + expiry
        ─► timer clears it server-side when it expires
```

The read-back **polls**. The sysfs attribute does not update synchronously with
the write: the kernel dispatches a SES control command and refreshes the cached
value only when the enclosure processor answers. A single immediate read returns
the previous value, which made every successful IDENT report as a failed
verification until it was caught on hardware.

### Startup reconciliation

Records are persisted, so a restart mid-timer is recoverable. On startup:

| Observed | Recorded | Action |
|---|---|---|
| lit | ours, expired | clear it — we can prove we created it |
| lit | ours, not expired | keep the timer running |
| lit | none | **leave alone**, report *external/unknown origin* |
| dark | ours | drop the record; something else cleared it |

The application never blindly clears every lit LED at startup.

## Privilege boundary

```
web process (uid 1000, no caps, no /dev access)
      │  {"op":"identify_on","enclosure_id":"0x…","slot":0}
      │  {"op":"ses_read","enclosure_id":"0x…","page":"join"}
      ▼
IDENT helper (root)  ── re-validates independently ──► sysfs / sg_ses
```

The socket protocol carries only semantics. There is no field for a path, a
command, or an `sg_ses` argument, so a fully compromised web process still
cannot express one. The helper does not trust the caller's validation and
repeats it, and enforces the enclosure allow-list itself.

Routing SES reads through the helper as well means the web process needs no
access to `/dev/sg*` at all.

## Polling and caching

| Source | Interval | Cost |
|---|---|---|
| sysfs slots | 5 s | negligible, pure file reads |
| TrueNAS disks/pools | 20 s | one WebSocket round trip |
| `sg_ses` chassis | 30 s | two page reads, the expensive one |
| SMART temperatures | 120 s | one API call |

One backend poll serves every connected browser. After a verified IDENT write
the slot cache is refreshed immediately rather than waiting for the next tick,
so the UI and its countdown reflect the write at once.

## Failure handling

| Failure | Behaviour |
|---|---|
| `/dev/sgX` changed | re-resolved by logical id on every operation |
| sysfs path changed | same |
| enclosure disconnected | discovery returns empty; UI states it plainly |
| TrueNAS offline | banner names the error; bay map keeps working from sysfs |
| `sg_ses` missing or timing out | chassis tab only; last-good marked stale |
| disk replaced in a slot | new serial in the same bay; audit history preserved |
| `/dev` renumbering | tolerated; identity is serial + WWN |
| container restart mid-IDENT | timers restored from disk and reconciled |
| locate write fails | reported as failed verification, never as success |
| `/data` not writable | container refuses to start and prints the fix |

## Frontend

React + TypeScript + Vite, no component library — the built bundle is ~51 kB
gzipped. The shelf is a single-row CSS grid inside a horizontally scrolling
container: on a narrow viewport it scrolls rather than wrapping, because
wrapping would misrepresent the physical layout of the shelf.

Health is conveyed by glyph + text + colour together, and every bay tile carries
an accessible name (`"Bay 8, SES slot 7, OK, serial K1A00008"`).

## Deliberate non-goals

- No writes other than IDENT.
- No dependency on TrueNAS private Python modules, patched files, private WebUI
  routes, or spoofed hardware identity.
- No modification of the TrueNAS host: no `apt`, no systemd units, no
  `/etc` or `/usr` changes, no middleware edits.
