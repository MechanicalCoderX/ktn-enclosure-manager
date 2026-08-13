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
| An `/dev/sg*` device, `rw` | `SG_IO` counts as a write even for read-only pages | reasonable, narrow |
| `SETUID`, `SETGID` | drop the web process to uid 1000 | declarable via `capabilities`, and used only to *reduce* privilege |

That is the whole list. No host mounts, no `--privileged`, no AppArmor
relaxation, no `CAP_DAC_OVERRIDE`, and the container keeps Docker's default
profile. Version 1.0.0 needed `apparmor=unconfined` to write the LED through
sysfs; 1.1.0 drives it with a SCSI command instead, which removed the only
genuinely contentious requirement.

If it is rejected, nothing is lost — **Install via YAML works today** and is
documented in [`../../docs/INSTALL-TRUENAS.md`](../../docs/INSTALL-TRUENAS.md).

## Before opening a PR

1. **Replace the remaining placeholders** in `app.yaml`:
   `REPLACE@example.com` (maintainer contact) and
   `REPLACE_WITH_HASH_FROM_TRUENAS_APPS_AT_SUBMISSION_TIME` (library hash).

2. **Publish the image first.** `ix_values.yaml` points at
   `ghcr.io/mechanicalcoderx/ktn-enclosure-manager`. It must be public and pullable
   anonymously, or nothing can install.

3. **Add an icon.** `docs/images/icon.png`, square, at least 256×256.

4. **Vendor the current base library.** Copy
   `ix-dev/community/<any-app>/templates/library/base_vX_Y_Z/` from a fresh
   checkout of truenas/apps into `templates/library/`, then set `lib_version`
   and `lib_version_hash` in `app.yaml` to match. Their CI verifies the hash.

5. **Validate the template against that library version.**
   `templates/docker-compose.yaml` here is written in their idiom but has *not*
   been rendered against a pinned library, because vendoring it into this repo
   would mean shipping thousands of lines of someone else's code. Expect to fix
   API details — particularly the `add_tmpfs` and `devices.add_device`
   signatures, which vary between library versions.

   From a truenas/apps checkout:

   ```bash
   cp -r /path/to/this/truenas/catalog-app ix-dev/community/ktn-enclosure-manager
   make render app=ktn-enclosure-manager train=community
   ```

6. **Add test values.** Their CI renders `templates/test_values/*.yaml`. At
   minimum add `basic-values.yaml`, plus one exercising a Host Path data volume
   rather than an ixVolume.

7. **Run their checks** (`make test`, cspell, etc.) before opening the PR.

## Opening the PR

Fork truenas/apps, add the app under `ix-dev/community/ktn-enclosure-manager/`,
and open the PR. Lead with what a reviewer needs in order to decide:

- what the app does and why TrueNAS cannot already do it
  (`truenas.is_ix_hardware` is False on community hardware, so `enclosure2`
  returns nothing and *View Enclosure* shows "Enclosure Unavailable");
- the exact privileges required and why each is the minimum — link
  [`../../SECURITY.md`](../../SECURITY.md), which documents that
  `CAP_SYS_RAWIO` was tested and deliberately **not** taken, and that
  `--privileged` is never used;
- that the only write the app can perform is the IDENT LED, enforced by an
  allow-list with no mutating `sg_ses` option reachable, and covered by tests.

## If it is rejected

Keep shipping via Install via YAML, which works today and needs no approval
from anyone.
