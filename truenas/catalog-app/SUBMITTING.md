# Submitting to the official TrueNAS catalog

This directory holds the app in the format used by
[truenas/apps](https://github.com/truenas/apps), so it can be proposed for the
**community** train.

## Read this first

TrueNAS SCALE 24.10+ has exactly one catalog and **no mechanism for adding a
third-party one** — `catalog.create` does not exist any more. So the official
train is the only route to "appears in Discover Apps like every other app", and
it is gated by iXsystems review.

This app needs a little more than a typical web app, but far less than it once
did:

| Requirement | Why | Likely reviewer reaction |
|---|---|---|
| An `/dev/sg*` device, `rw` | the `w` covers exactly one command — the IDENT LED's SEND DIAGNOSTIC; every telemetry read runs on a read-only open | reasonable, narrow |
| `SETUID`, `SETGID` | drop the web process to uid 1000 | declarable via `capabilities`, and used only to *reduce* privilege |

That is the whole list. No host mounts, no `--privileged`, no AppArmor
relaxation, no `CAP_DAC_OVERRIDE`, and the container keeps Docker's default
profile. Version 1.0.0 needed `apparmor=unconfined` to write the LED through
sysfs; 1.1.0 drives it with a SCSI command instead, which removed the only
genuinely contentious requirement.

If it is rejected, nothing is lost — **Install via YAML works today** and is
documented in [`../../docs/INSTALL-TRUENAS.md`](../../docs/INSTALL-TRUENAS.md).

## Status

**Live status lives in [`SUBMISSION-TEXTS.md`](SUBMISSION-TEXTS.md), not here.**
That file has the status table and the ready-to-paste issue/PR bodies; this file
is the how and why. As of 2026-08-18:

| | |
|---|---|
| Discussion issue | [truenas/apps#5599](https://github.com/truenas/apps/issues/5599) — open since 2026-08-15, no maintainer reply yet |
| PR branch | `MechanicalCoderX/apps:add-ktn-enclosure-manager` — pushed |
| Pull request | not opened, deliberately — see below |

For context on the wait: across the last 30 closed `app-request` issues the
median time to close is **5 days** (min 0, max 59), and every currently-open
app-request is unanswered, the oldest by 8 days. Silence here is the queue's
normal state, not a signal about this app.

### Rendered and validated against library 2.3.11

This package has been rendered against the real catalog library, so the
placeholders are filled in and the template API mismatches this file used to
warn about are fixed.

| Item | Value |
|---|---|
| `lib_version` | `2.3.11` (current at time of writing) |
| `lib_version_hash` | from `library/hashes.yaml` in truenas/apps |
| Render result | valid YAML, one service, portal and notes emitted |

### How to re-validate after changing anything

Their CI script pulls a container image and runs the render inside it:

```bash
./.github/scripts/ci.py --app ktn-enclosure-manager --train community \
    --test-file basic-values.yaml --render-only=true
```

**Do not run that on a TrueNAS box.** The library decides how to reach
middleware with `is_truenas_system()`, which is literally
`"truenas" in os.uname().release`. On the appliance that is true, so it tries
to open the middleware unix socket - which is not mounted into the validation
container - and every render fails with `FileNotFoundError` that has nothing to
do with your app. Their GitHub runners are Ubuntu, where the check is false and
a mock client is used. Run it on any non-TrueNAS Linux host.

### Fixes that were needed, for reference

The template was written in the right idiom but against a guessed API. Against
2.3.11 the real ones are:

| Used | Correct |
|---|---|
| `c.remove_all_caps()` | `c.clear_caps()` |
| `c.security_opt.add_no_new_privileges()` | not needed - the library sets it for every container |
| `c.add_tmpfs(path, {...})` | `c.add_storage(path, {"type": "tmpfs", "tmpfs_config": {...}})` |
| `c.ports.add_port(host, container, {...})` | `c.add_port(values.network.web_port, {"container_port": ...})` |
| `tpl.portals.add_portal({...})` | `tpl.portals.add(values.network.web_port, {"name": "Web UI"})` |
| `c.resources.set_profile(values.resources)` | omit - resources are applied from `values.resources` |

`ix_values.yaml` was also missing `consts.app_name`, which the template
references on its first line.

### One real constraint worth knowing

**The catalog library cannot express a setgid tmpfs.** Its mode validator is
`^0[0-7]{3}$`, so `2770` is rejected outright. The helper's socket must carry
gid 1000 - the web process runs as uid 1000 and cannot open a `root:root`
socket, and chgrp'ing it would need `CAP_CHOWN`, which the container
deliberately drops.

Since 1.3.3 that constraint does not matter: the helper binds the socket
under gid 1000 itself (`os.setegid`, via the retained `CAP_SETGID`), so
nothing depends on the directory's setgid bit at all. An entrypoint `chmod`
was tried first and silently *stripped* the bit (no `CAP_FSETID` - see
`docker-entrypoint.sh`). That is why an app installed from the catalog still
gets a working IDENT path, and it makes the plain compose deployment
independent of the `mode=` tmpfs option too.

## Why the PR is not open yet

**The issue is already filed and is the blocker.** truenas/apps requires an
issue before a PR, and #5599 asks them a direct question: whether a
community-train app may request a `/dev/sg*` device node at `rw`. Opening the
PR before that is answered would be asking and then not waiting.

When a maintainer replies, `SUBMISSION-TEXTS.md` section 2 has the PR body ready
to paste.

## Checks to run before opening the PR

1. **Confirm the image tag in `ix_values.yaml`** matches the release you want
   published, and that it is publicly pullable.

2. **Re-render if you bump `lib_version`.** Their CI recomputes
   `lib_version_hash` and rewrites `templates/library/`, so a stale hash fails.

3. **Expect review discussion about the device.** `/dev/sg*` at `rw` is the
   one unusual ask, and the measured permission floor (v1.5.2) is the
   strongest answer to it - put it in the PR description rather than leaving
   them to wonder. The facts, measured on live hardware: the device cgroup
   gates the `open()` mode, not `SG_IO` itself; every telemetry read runs
   `sg_ses --readonly` on an `O_RDONLY` fd, on which the kernel's per-opcode
   filter refuses write-class commands outright; the `w` exists solely for
   the one IDENT LED SEND DIAGNOSTIC, issued by the gated root helper; and
   `:r` is a supported monitoring-only grant. It addresses the enclosure
   processor, not disk data.

If it is rejected, nothing is lost - **Install via YAML works today** and is
documented in [`../../docs/INSTALL-TRUENAS.md`](../../docs/INSTALL-TRUENAS.md).

## Catalog `version` vs app version

This staged copy keeps `app.yaml`'s `version` equal to the app release so the
repo's version-consistency test can pin every version-bearing file. The copy
in the submission fork deliberately diverges on this one field: upstream's
CONTRIBUTIONS.md starts a new app's catalog `version` at 1.0.0 and bumps it
per package change, decoupled from `app_version`. When syncing this directory
to the fork, carry everything except `version`, which follows the catalog's
own numbering there.
