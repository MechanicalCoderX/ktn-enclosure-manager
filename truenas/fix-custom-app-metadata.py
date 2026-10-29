#!/usr/bin/env python3
"""Give a TrueNAS *custom app* real metadata: title, icon, version, portal.

Run ON the TrueNAS appliance as root:

    python3 fix-custom-app-metadata.py [app-name]

Why this exists: TrueNAS hardcodes every custom app's metadata to a stub
(title "Custom App", version 1.0.0, app_version "custom", no icon) in
``catalog_reader/custom_app.py``. No compose ``x-`` extension can reach it.
But the WebUI actually reads a collective cache that ``app.metadata.generate``
rebuilds from each app's ``/mnt/.ix-apps/app_configs/<app>/metadata.yaml``,
so editing that file and regenerating the cache is what sticks.

For a custom app, ``app.update`` does not touch that file at all: the
middleware's portal refresh (``update_app_metadata_for_portals``) runs on
update only for catalog apps, and a custom app's portals are computed from
the compose ``x-portals`` once, at create. (An earlier version of this
docstring said ``app.update`` "merges only portals over that file"; for a
custom app it merges nothing.) That is why the title and icon set here
survive an update - verified on TrueNAS SCALE 25.10.6, including a full
app.update cycle - and also why a changed ``x-portals`` (say, switching the
Web UI button to https) never reaches the button by itself:
``app.metadata.generate`` only copies the per-app file into the cache, it
does not recompute it. (Both read from the 25.10.7 middleware source.)

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
- Portals and notes: recomputed from the rendered compose with the
  middleware's own ``get_portals_and_app_notes`` - the function it uses for
  catalog apps - after the rename, so the Web UI button follows the current
  ``x-portals``. If that import is unavailable the stored portals are kept
  and the script says so.

It prints portal URLs only. It never prints the rendered compose, which
carries the app's environment (the TrueNAS API key, the alert webhook).

A full tar backup of the app's config dir is taken first and its path
printed. Re-run after a delete-and-reinstall of the app, after updating the
app to a new image tag (a plain re-run realigns the version), or after
changing ``x-portals``; a normal app update does not revert the title or icon.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from typing import Any

import yaml

APPS_ROOT = "/mnt/.ix-apps/app_configs"
BACKUP_DIR = "/root"
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


def print_portals(label: str, portals: Any) -> None:
    """Names and URLs only - nothing else from the app's config."""
    print(f"{label}:")
    if not isinstance(portals, dict) or not portals:
        print("  (none)")
        return
    for name, url in portals.items():
        print(f"  {name}: {url}")


def computed_portals(app: str, version: str) -> dict[str, Any] | None:
    """Portals and notes as the middleware computes them, or None.

    Imported lazily: it exists only on the appliance, and a missing or
    changed module must not abort a run that has already renamed the
    versions directory. The function reads the rendered compose files; its
    result is a small dict ({"portals": {...}, "notes": ...}) and the compose
    itself never passes through this script.
    """
    try:
        from middlewared.plugins.apps.ix_apps.portals import (  # type: ignore[import-not-found]
            get_portals_and_app_notes,
        )
    except Exception as exc:  # noqa: BLE001 - any import failure means "skip"
        print(f"portal refresh skipped: middleware helper unavailable ({type(exc).__name__}); "
              "the stored portals are kept")
        return None
    try:
        result = get_portals_and_app_notes(app, version)
    except Exception as exc:  # noqa: BLE001
        print(f"portal refresh skipped: {type(exc).__name__}; the stored portals are kept")
        return None
    if not isinstance(result, dict) or not isinstance(result.get("portals"), dict):
        print("portal refresh skipped: unexpected result shape; the stored portals are kept")
        return None
    return result


def wait_for_job(job_id: int) -> str:
    state = "RUNNING"
    for _ in range(30):
        state = json.loads(subprocess.check_output(
            ["midclt", "call", "core.get_jobs", json.dumps([["id", "=", job_id]])],
            text=True,
        ))[0]["state"]
        if state in ("SUCCESS", "FAILED", "ABORTED"):
            break
        time.sleep(1)
    return state


def main(argv: list[str]) -> int:
    app = argv[1] if len(argv) > 1 else "ktn-enclosure-manager"
    base = f"{APPS_ROOT}/{app}"

    image = subprocess.check_output(
        ["docker", "inspect", app, "--format", "{{.Config.Image}}"], text=True
    ).strip()
    tag = image.rsplit(":", 1)[1] if ":" in image else "latest"
    print(f"running image tag: {tag}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{BACKUP_DIR}/{app}-app_configs-{stamp}.tar.gz"
    subprocess.run(["tar", "czf", backup, "-C", APPS_ROOT, app], check=True)
    print(f"full backup: {backup}   (rollback: extract it back and re-run app.metadata.generate)")

    with open(f"{base}/metadata.yaml") as fh:
        meta = yaml.safe_load(fh)
    old = meta["version"]
    print_portals("portals before", meta.get("portals"))

    # Structural rename first: if it fails nothing else has changed yet.
    if old != tag:
        shutil.move(f"{base}/versions/{old}", f"{base}/versions/{tag}")
        print(f"versions/{old} -> versions/{tag}")

    meta["version"] = tag
    meta["human_version"] = tag
    meta["metadata"].update(DISPLAY)
    meta["metadata"]["version"] = tag
    meta["metadata"]["app_version"] = tag
    # After the rename: the helper reads versions/<tag>/ for the rendered compose.
    refreshed = computed_portals(app, tag)
    if refreshed is not None:
        meta.update(refreshed)
    with open(f"{base}/metadata.yaml", "w") as fh:
        yaml.safe_dump(meta, fh, default_flow_style=False)

    app_yaml_path = f"{base}/versions/{tag}/app.yaml"
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
    print("app.metadata.generate:", wait_for_job(int(job_id)))

    check = json.loads(subprocess.check_output(
        ["midclt", "call", "app.query", json.dumps([["name", "=", app]])], text=True
    ))[0]
    print("now showing:", check["metadata"]["title"], "| version:", check["version"],
          "| app_version:", check["metadata"]["app_version"],
          "| icon:", "set" if check["metadata"].get("icon") else "MISSING")
    print_portals("portals now (app.query)", check.get("portals"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
