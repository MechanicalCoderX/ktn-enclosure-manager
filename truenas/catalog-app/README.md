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

Off by default. Enabling it runs the container with AppArmor unconfined,
because Docker's default profile denies all writes under `/sys` regardless of
user or capabilities. Everything except the Identify button works with it off.

See SECURITY.md in the project repository for the full reasoning.
