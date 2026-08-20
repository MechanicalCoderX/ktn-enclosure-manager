#!/usr/bin/env python3
"""Give a TrueNAS *custom app* real display metadata: title, icon, version.

Run ON the TrueNAS appliance as root:

    python3 fix-custom-app-metadata.py [app-name]

Why this exists: TrueNAS hardcodes every custom app's metadata to a stub
(title "Custom App", human_version "1.0.0_custom", no icon) in
``catalog_reader/custom_app.py``. No compose ``x-`` extension can reach it.
But the WebUI actually reads a collective cache that ``app.metadata.generate``
rebuilds from each app's ``/mnt/.ix-apps/app_configs/<app>/metadata.yaml`` -
and ``app.update`` merges only portals over that file, preserving everything
else. So editing the display fields there is durable across normal app
updates. Verified on TrueNAS SCALE 25.10.6.

What it changes (display only): title, description, home, sources, icon,
maintainers, and top-level ``human_version`` (set to the running image's tag,
so the Apps page shows the truth). What it never touches: the structural
``version`` key and the ``versions/<dir>`` layout, portals, notes.

Both files are backed up with a timestamp suffix first; the rollback is
printed. Re-run after a delete-and-reinstall of the app (a plain app update
does not revert it).
"""

import json
import shutil
import subprocess
import sys
import time

import yaml

APP = sys.argv[1] if len(sys.argv) > 1 else "ktn-enclosure-manager"
BASE = f"/mnt/.ix-apps/app_configs/{APP}"
REPO = "https://github.com/MechanicalCoderX/ktn-enclosure-manager"
DISPLAY = {
    "title": "KTN Enclosure Manager",
    "description": (
        "Web enclosure management for SES disk shelves (EMC KTN-STL3): "
        "live bay map, IDENT LED, chassis telemetry, TrueNAS pool correlation."
    ),
    "home": REPO,
    "sources": [REPO],
    "icon": f"{REPO.replace('github.com', 'raw.githubusercontent.com')}/main/frontend/public/favicon.png",
    "maintainers": [{"name": "MechanicalCoderX", "url": REPO, "email": ""}],
}

image = subprocess.check_output(
    ["docker", "inspect", APP, "--format", "{{.Config.Image}}"], text=True
).strip()
tag = image.rsplit(":", 1)[1] if ":" in image else "latest"
print(f"running image tag: {tag}")

stamp = time.strftime("%Y%m%d-%H%M%S")
# The structural version names the versions/<dir> the middleware reads config
# from; display fields may change, this key must not.
with open(f"{BASE}/metadata.yaml") as fh:
    structural_version = yaml.safe_load(fh)["version"]

for rel in ["metadata.yaml", f"versions/{structural_version}/app.yaml"]:
    path = f"{BASE}/{rel}"
    backup = f"{path}.bak-{stamp}"
    shutil.copy2(path, backup)
    with open(path) as fh:
        data = yaml.safe_load(fh)
    target = data.get("metadata", data)
    target.update(DISPLAY)
    if "human_version" in data:
        data["human_version"] = tag
    with open(path, "w") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False)
    print(f"updated {rel}   (rollback: cp {backup} {path})")

job_id = subprocess.check_output(
    ["midclt", "call", "app.metadata.generate"], text=True
).strip()
state = "RUNNING"
for _ in range(30):
    state = json.loads(subprocess.check_output(
        ["midclt", "call", "core.get_jobs", json.dumps([["id", "=", int(job_id)]])],
        text=True,
    ))[0]["state"]
    if state in ("SUCCESS", "FAILED", "ABORTED"):
        break
    time.sleep(1)
print("app.metadata.generate:", state)

check = json.loads(subprocess.check_output(
    ["midclt", "call", "app.query", json.dumps([["name", "=", APP]])], text=True
))[0]
print("now showing:", check["human_version"], "|", check["metadata"]["title"],
      "| icon:", "set" if check["metadata"].get("icon") else "MISSING")
