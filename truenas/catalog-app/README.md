# KTN Enclosure Manager

Physical drive-bay map, chassis telemetry and IDENT LED control for SES disk
shelves attached to TrueNAS SCALE.

TrueNAS gates its built-in enclosure UI behind iX hardware, so *View Enclosure*
reports "Enclosure Unavailable" on a community system with a third-party shelf.
This app fills that gap without patching middleware or spoofing hardware
identity.

## What you get

- A drive map matching the physical shelf, left to right, with each bay's
  serial, device, pool, vdev, ZFS state and SMART temperature
- Chassis telemetry: LCC/controller/expander state, PSU flags, fan RPM, and
  every temperature sensor
- Identify: light a bay's LED for 10s / 30s / 60s / 5min / until cleared, with
  server-side timers so closing the browser cannot strand a lit LED
- An audit log of every write, including automatic clears

## Before you install

- **Create the data storage first** and make it writable by uid 1000. If you
  choose a Host Path: `chown -R 1000:1000 <path>`. The app refuses to start
  otherwise, because accounts and the audit log would not persist.
- **Find your SES device**: `lsscsi -g | grep enclosu`
- **First run has no account and no default password.** Open the Web UI and
  create the administrator immediately.

## Identify (LED) control

Available to any authenticated user, with no extra privileges to enable. The
LED is driven by a single SES SEND DIAGNOSTIC command on the passed-through
`/dev/sg*` node, issued by the privileged helper after it re-validates the
request — the container keeps Docker's default AppArmor profile and mounts no
`/sys`. All telemetry runs on read-only opens; the device's `w` grant exists
solely for that one command, and granting `:r` instead is a supported
monitoring-only deployment where Identify returns a clear permission error.

See SECURITY.md in the project repository for the full reasoning.
