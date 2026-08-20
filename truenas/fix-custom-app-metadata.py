#!/usr/bin/env python3
"""Give a TrueNAS *custom app* real metadata: title, icon, and version.

Run ON the TrueNAS appliance as root:

    python3 fix-custom-app-metadata.py [app-name]

Why this exists: TrueNAS hardcodes every custom app's metadata to a stub
(title "Custom App", version 1.0.0, app_version "custom", no icon) in
``catalog_reader/custom_app.py``. No compose ``x-`` extension can reach it.
But the WebUI actually reads a collective cache that ``app.metadata.generate``
rebuilds from each app's ``/mnt/.ix-apps/app_configs/<app>/metadata.yaml``,
and ``app.update`` merges only portals over that file - preserving everything
else. Verified on TrueNAS SCALE 25.10.6, including a full app.update cycle
afterwards.

What it changes:
- Display fields: title, description, home, sources, icon, maintainers,
  and ``human_version`` (the Installed-apps card).
- Structural version: the details page reads ``metadata.version`` and
  ``metadata.app_version``. ``metadata.version`` names the ``versions/<dir>``
  the middleware reads config from, so changing it requires renaming that
  directory in the same operation - every consumer derives the path from the
  version value, so they move together or not at all. Both are set to the
  running image's tag. (Custom-app behaviour is keyed on the top-level
  ``custom_app`` flag, not the ``app_version`` string.)

A full tar backup of the app's config dir is taken first and its path
printed. Re-run after a delete-and-reinstall of the app, or after updating
the app to a new image tag (a plain re-run realigns the version); a normal
app update does not revert the title or icon.
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
    "icon": f"{REPO.replace('github.com', 'raw.githubusercontent.com')}/main/frontend/public/icon.svg",
    "maintainers": [{"name": "MechanicalCoderX", "url": REPO, "email": ""}],
}

image = subprocess.check_output(
    ["docker", "inspect", APP, "--format", "{{.Config.Image}}"], text=True
).strip()
tag = image.rsplit(":", 1)[1] if ":" in image else "latest"
print(f"running image tag: {tag}")

stamp = time.strftime("%Y%m%d-%H%M%S")
backup = f"/root/{APP}-app_configs-{stamp}.tar.gz"
subprocess.run(["tar", "czf", backup, "-C", "/mnt/.ix-apps/app_configs", APP], check=True)
print(f"full backup: {backup}   (rollback: extract it back and re-run app.metadata.generate)")

with open(f"{BASE}/metadata.yaml") as fh:
    meta = yaml.safe_load(fh)
old = meta["version"]

# Structural rename first: if it fails nothing else has changed yet.
if old != tag:
    shutil.move(f"{BASE}/versions/{old}", f"{BASE}/versions/{tag}")
    print(f"versions/{old} -> versions/{tag}")

meta["version"] = tag
meta["human_version"] = tag
meta["metadata"].update(DISPLAY)
meta["metadata"]["version"] = tag
meta["metadata"]["app_version"] = tag
with open(f"{BASE}/metadata.yaml", "w") as fh:
    yaml.safe_dump(meta, fh, default_flow_style=False)

app_yaml_path = f"{BASE}/versions/{tag}/app.yaml"
with open(app_yaml_path) as fh:
    av = yaml.safe_load(fh)
av.update(DISPLAY)
av["version"] = tag
av["app_version"] = tag
with open(app_yaml_path, "w") as fh:
    yaml.safe_dump(av, fh, default_flow_style=False)
print("metadata.yaml + app.yaml aligned")

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
print("now showing:", check["metadata"]["title"], "| version:", check["version"],
      "| app_version:", check["metadata"]["app_version"],
      "| icon:", "set" if check["metadata"].get("icon") else "MISSING")
