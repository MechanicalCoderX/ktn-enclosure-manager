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

ENCL = {"id": "0x5006048004a54c3e", "components": "15"}
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

# An empty bay and a decoy non-slot directory, so discovery is proven to key on
# the presence of a 'slot' attribute rather than on directory naming.
w(base / "99" / "slot", "99")
w(base / "99" / "status", "not installed")
w(base / "99" / "locate", "0")
w(base / "99" / "fault", "0")
w(base / "decoy" / "uevent", "DEVTYPE=nonsense")

print(f"wrote {OUT} ({count} real slots + 1 empty bay + 1 decoy dir)")
