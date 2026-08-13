#!/usr/bin/env python3
"""Generate a fake /sys tree from the captured KTN-STL3 data.

Lets the whole sysfs layer be tested with no hardware attached (spec §42).
Regenerate with: python3 tests/build_sysfs_fixture.py
"""
import re, shutil
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "fixtures" / "ktn-stl3" / "sysfs_slots.txt"
OUT = HERE / "fixtures" / "sysfs_root"
ADDR = "1:0:15:0"

ENCL = {"id": "0x50060480aabbcc00", "components": "15"}
DEVICE = {"vendor": "EMC     ", "model": "ESES Enclosure  ", "rev": "0001", "type": "13"}

def w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text + "\n")

if OUT.exists():
    shutil.rmtree(OUT)

base = OUT / "class" / "enclosure" / ADDR
for k, v in ENCL.items():
    w(base / k, v)
for k, v in DEVICE.items():
    w(base / "device" / k, v)
(base / "device" / "scsi_generic" / "sg16").mkdir(parents=True, exist_ok=True)

slot = None
count = 0
for line in SRC.read_text().splitlines():
    m = re.match(r"### slot (\d+)", line)
    if m:
        slot = m.group(1)
        count += 1
        continue
    if slot is None or "=" not in line:
        continue
    key, _, val = line.partition("=")
    if key == "block":
        if val:
            (base / slot / "device" / "block" / val).mkdir(parents=True, exist_ok=True)
    else:
        w(base / slot / key, val)

# ---------------------------------------------------------------- /sys/block
# vpd_pg80 is written as real bytes with its 4-byte header, so the serial
# parser is exercised on the byte layout the kernel actually produces rather
# than on a convenient pre-stripped string. Getting the header offset wrong
# silently truncates the 'P9' prefix off every serial on this hardware.
BLOCKS = HERE / "fixtures" / "ktn-stl3" / "sysfs_blocks.txt"
dev = None
for line in BLOCKS.read_text().splitlines():
    m = re.match(r"### (\w+)", line)
    if m:
        dev = m.group(1)
        continue
    if dev is None or "=" not in line:
        continue
    key, _, val = line.partition("=")
    bdir = OUT / "block" / dev
    if key == "serial":
        serial = val.rstrip()
        payload = bytes([0x00, 0x80, 0x00, len(serial)]) + serial.encode("ascii")
        (bdir / "device").mkdir(parents=True, exist_ok=True)
        (bdir / "device" / "vpd_pg80").write_bytes(payload)
    elif key == "rotational":
        w(bdir / "queue" / "rotational", val)
    elif key.startswith("device/"):
        w(bdir / "device" / key.split("/", 1)[1], val)
    else:
        w(bdir / key, val)

# An empty bay and a decoy non-slot directory, so discovery is proven to key on
# the presence of a 'slot' attribute rather than on directory naming.
w(base / "99" / "slot", "99")
w(base / "99" / "status", "not installed")
w(base / "99" / "locate", "0")
w(base / "99" / "fault", "0")
w(base / "decoy" / "uevent", "DEVTYPE=nonsense")

print(f"wrote {OUT} ({count} real slots + 1 empty bay + 1 decoy dir)")
