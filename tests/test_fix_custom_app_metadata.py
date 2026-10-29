"""truenas/fix-custom-app-metadata.py against a fake appliance.

The script only ever runs as root on TrueNAS, so here the appliance is a
directory tree shaped like /mnt/.ix-apps/app_configs/<app>, the subprocess
calls (docker, tar, midclt) are fakes, and the middleware's portal helper is
a stub module. What is real: the YAML it reads and writes, the versions/
rename, and every line it prints.

The headline case is the one v1.5.7 got wrong: after x-portals switches to
https, the stored portal must be recomputed, not preserved.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "truenas" / "fix-custom-app-metadata.py"
APP = "ktn-enclosure-manager"
OLD_PORTAL = {"Web UI": "http://0.0.0.0:8420/"}
NEW_PORTAL = {"Web UI": "https://truenas.example.test:8420/"}
SECRET = "sentinel-api-key-0123456789"  # must never be printed


def load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("fix_custom_app_metadata", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAppliance:
    """The app_configs tree, plus fakes for docker/tar/midclt."""

    def __init__(self, tmp_path: Path, *, stored_version: str = "1.5.7",
                 image_tag: str = "1.6.0") -> None:
        self.apps_root = tmp_path / "app_configs"
        self.base = self.apps_root / APP
        version_dir = self.base / "versions" / stored_version
        rendered = version_dir / "templates" / "rendered"
        rendered.mkdir(parents=True)
        # The rendered compose carries the app's env - including its API key.
        (rendered / "docker-compose.yaml").write_text(
            f"services:\n  app:\n    environment:\n      KTN_TRUENAS_API_KEY: {SECRET}\n"
        )
        (version_dir / "app.yaml").write_text(
            yaml.safe_dump({"name": APP, "version": stored_version, "app_version": "custom"})
        )
        (self.base / "metadata.yaml").write_text(yaml.safe_dump({
            "custom_app": True,
            "version": stored_version,
            "human_version": stored_version,
            "metadata": {"title": "Custom App", "version": stored_version},
            "portals": dict(OLD_PORTAL),
            "notes": "old notes",
        }))
        self.image_tag = image_tag
        self.calls: list[list[str]] = []

    def check_output(self, argv: list[str], text: bool = True) -> str:
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return f"ghcr.io/mechanicalcoderx/{APP}:{self.image_tag}\n"
        if argv[:3] == ["midclt", "call", "app.metadata.generate"]:
            return "42\n"
        if argv[:3] == ["midclt", "call", "core.get_jobs"]:
            return json.dumps([{"id": 42, "state": "SUCCESS"}])
        if argv[:3] == ["midclt", "call", "app.query"]:
            # Mirrors what app.metadata.generate would publish from the file.
            meta = yaml.safe_load((self.base / "metadata.yaml").read_text())
            return json.dumps([{
                "name": APP, "version": meta["version"],
                "metadata": meta["metadata"], "portals": meta.get("portals"),
            }])
        raise AssertionError(f"unexpected command {argv}")

    def run(self, argv: list[str], check: bool = False) -> None:
        self.calls.append(argv)
        assert argv[0] == "tar"

    def metadata(self) -> dict[str, Any]:
        return yaml.safe_load((self.base / "metadata.yaml").read_text())


def stub_portal_helper(monkeypatch: pytest.MonkeyPatch, appliance: FakeAppliance,
                       seen: list[tuple[str, str]]) -> None:
    """middlewared.plugins.apps.ix_apps.portals, as far as the script uses it."""

    def get_portals_and_app_notes(app: str, version: str) -> dict[str, Any]:
        seen.append((app, version))
        # Like the real helper, it reads the rendered compose of THIS version,
        # so it only works once the versions/ dir has been renamed.
        rendered = appliance.base / "versions" / version / "templates" / "rendered"
        assert (rendered / "docker-compose.yaml").exists(), "called before the rename"
        return {"portals": dict(NEW_PORTAL), "notes": f"notes mentioning {SECRET}"}

    names = ["middlewared", "middlewared.plugins", "middlewared.plugins.apps",
             "middlewared.plugins.apps.ix_apps", "middlewared.plugins.apps.ix_apps.portals"]
    for name in names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules[names[-1]].get_portals_and_app_notes = get_portals_and_app_notes  # type: ignore[attr-defined]


def run_script(monkeypatch: pytest.MonkeyPatch, appliance: FakeAppliance,
               capsys: pytest.CaptureFixture[str]) -> str:
    module = load_script()
    monkeypatch.setattr(module, "APPS_ROOT", str(appliance.apps_root))
    monkeypatch.setattr(module, "BACKUP_DIR", str(appliance.apps_root.parent))
    monkeypatch.setattr(module.subprocess, "check_output", appliance.check_output)
    monkeypatch.setattr(module.subprocess, "run", appliance.run)
    assert module.main(["fix", APP]) == 0
    return capsys.readouterr().out


def test_portals_are_recomputed_after_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    appliance = FakeAppliance(tmp_path)
    seen: list[tuple[str, str]] = []
    stub_portal_helper(monkeypatch, appliance, seen)

    out = run_script(monkeypatch, appliance, capsys)

    assert seen == [(APP, "1.6.0")]
    meta = appliance.metadata()
    assert meta["portals"] == NEW_PORTAL
    assert meta["version"] == "1.6.0" and meta["metadata"]["title"] == "KTN Enclosure Manager"
    assert (appliance.base / "versions" / "1.6.0" / "app.yaml").exists()
    assert not (appliance.base / "versions" / "1.5.7").exists()
    # The URLs are printed: before, and as app.query now reports them.
    assert "Web UI: http://0.0.0.0:8420/" in out
    assert "Web UI: https://truenas.example.test:8420/" in out
    # Never the compose, the notes, or anything else carrying the secret.
    assert SECRET not in out
    assert "KTN_TRUENAS_API_KEY" not in out


def test_without_the_middleware_helper_portals_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The graceful fallback: a run off-appliance, or a renamed module."""
    appliance = FakeAppliance(tmp_path)
    monkeypatch.setitem(sys.modules, "middlewared", None)  # import raises

    out = run_script(monkeypatch, appliance, capsys)

    assert "portal refresh skipped" in out
    meta = appliance.metadata()
    assert meta["portals"] == OLD_PORTAL  # preserved, not dropped
    assert meta["version"] == "1.6.0"     # the rest of the run still happened
    assert SECRET not in out


def test_a_helper_that_raises_keeps_the_stored_portals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    appliance = FakeAppliance(tmp_path)
    stub_portal_helper(monkeypatch, appliance, [])

    def boom(app: str, version: str) -> dict[str, Any]:
        raise FileNotFoundError(f"/secret/path/{SECRET}")

    sys.modules["middlewared.plugins.apps.ix_apps.portals"].get_portals_and_app_notes = boom  # type: ignore[attr-defined]
    out = run_script(monkeypatch, appliance, capsys)

    assert "portal refresh skipped: FileNotFoundError" in out
    assert appliance.metadata()["portals"] == OLD_PORTAL
    assert SECRET not in out  # the exception message is not echoed


def test_same_version_rerun_refreshes_portals_without_renaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After an x-portals-only app.update the tag is unchanged; the portal still moves."""
    appliance = FakeAppliance(tmp_path, stored_version="1.6.0", image_tag="1.6.0")
    stub_portal_helper(monkeypatch, appliance, [])
    out = run_script(monkeypatch, appliance, capsys)
    assert " -> versions/" not in out  # nothing to rename
    assert appliance.metadata()["portals"] == NEW_PORTAL


def test_the_docstring_no_longer_claims_app_update_merges_portals() -> None:
    doc = load_script().__doc__ or ""
    assert "does not touch that file at all" in doc
    assert "app.update`` merges only portals over that file -" not in doc


# ----------------------------------------------- review fixes (M2, L3, L4, L5)


def test_an_existing_target_versions_dir_is_refused_not_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review M2: shutil.move puts the source INSIDE an existing destination.

    versions/<tag> next to versions/<old> is what a tar-extract "rollback"
    leaves behind (extraction never deletes the newer dir). A re-run used to
    produce versions/1.6.0/1.5.7/ and report success while the middleware read
    the stale versions/1.6.0. It must refuse, change nothing, and say why.
    """
    appliance = FakeAppliance(tmp_path, stored_version="1.5.7", image_tag="1.6.0")
    leftover = appliance.base / "versions" / "1.6.0"
    (leftover / "templates" / "rendered").mkdir(parents=True)
    (leftover / "templates" / "rendered" / "docker-compose.yaml").write_text("services: {}\n")
    (leftover / "app.yaml").write_text("name: x\nversion: 1.6.0\n")
    stub_portal_helper(monkeypatch, appliance, [])
    before = (appliance.base / "metadata.yaml").read_bytes()

    module = load_script()
    monkeypatch.setattr(module, "APPS_ROOT", str(appliance.apps_root))
    monkeypatch.setattr(module, "BACKUP_DIR", str(appliance.apps_root.parent))
    monkeypatch.setattr(module.subprocess, "check_output", appliance.check_output)
    monkeypatch.setattr(module.subprocess, "run", appliance.run)
    rc = module.main(["fix", APP])
    out = capsys.readouterr().out

    assert rc != 0
    assert "versions/1.6.0 already exists" in out
    assert not (leftover / "1.5.7").exists()          # never nested
    assert (appliance.base / "versions" / "1.5.7").is_dir()
    assert (appliance.base / "metadata.yaml").read_bytes() == before
    assert not any(call[0] == "midclt" for call in appliance.calls)
    assert SECRET not in out


def test_the_printed_rollback_removes_the_dir_before_extracting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review M2/L5: extracting over the dir leaves versions/<tag> behind."""
    appliance = FakeAppliance(tmp_path)
    stub_portal_helper(monkeypatch, appliance, [])
    out = run_script(monkeypatch, appliance, capsys)
    assert "extract it back and re-run" not in out
    assert f"remove app_configs/{APP}, extract the backup, then app.metadata.generate" in out
    assert "delete after acceptance" in out


def test_the_backup_is_taken_under_a_private_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review L5: the tarball holds the rendered compose (API key, webhook)."""
    import os

    appliance = FakeAppliance(tmp_path)
    stub_portal_helper(monkeypatch, appliance, [])
    seen_umask: list[int] = []
    real_run = appliance.run

    def run(argv: list[str], check: bool = False) -> None:
        current = os.umask(0)
        os.umask(current)
        seen_umask.append(current)
        real_run(argv, check=check)

    monkeypatch.setattr(appliance, "run", run)
    outer = os.umask(0o022)
    try:
        run_script(monkeypatch, appliance, capsys)
        after = os.umask(0o022)
    finally:
        os.umask(outer)
    assert seen_umask == [0o077]
    assert after == 0o022  # restored for everything after the backup


def test_empty_recomputed_portals_keep_the_stored_portals_and_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review L3: the real 25.10.7 helper returns {'portals': {}, 'notes': None}
    on ANY x-portals validation error (a quoted port, say). That used to be
    written over the good stored values."""
    appliance = FakeAppliance(tmp_path)
    stub_portal_helper(monkeypatch, appliance, [])
    sys.modules["middlewared.plugins.apps.ix_apps.portals"].get_portals_and_app_notes = (  # type: ignore[attr-defined]
        lambda app, version: {"portals": {}, "notes": None}
    )
    out = run_script(monkeypatch, appliance, capsys)
    meta = appliance.metadata()
    assert meta["portals"] == OLD_PORTAL
    assert meta["notes"] == "old notes"
    assert "x-portals failed validation" in out
    assert meta["version"] == "1.6.0"  # the rest of the run still happened


def test_empty_portals_are_written_when_none_were_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Control for the above: nothing stored to protect, so nothing to keep."""
    appliance = FakeAppliance(tmp_path)
    meta = appliance.metadata()
    meta["portals"] = {}
    (appliance.base / "metadata.yaml").write_text(yaml.safe_dump(meta))
    stub_portal_helper(monkeypatch, appliance, [])
    sys.modules["middlewared.plugins.apps.ix_apps.portals"].get_portals_and_app_notes = (  # type: ignore[attr-defined]
        lambda app, version: {"portals": {}, "notes": "fresh notes"}
    )
    out = run_script(monkeypatch, appliance, capsys)
    assert appliance.metadata()["notes"] == "fresh notes"
    assert "x-portals failed validation" not in out


def test_yaml_writes_are_atomic_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review L4: written via a temp file + os.replace, mode 0600, no leftovers."""
    import stat

    appliance = FakeAppliance(tmp_path)
    stub_portal_helper(monkeypatch, appliance, [])
    run_script(monkeypatch, appliance, capsys)
    for path in (appliance.base / "metadata.yaml",
                 appliance.base / "versions" / "1.6.0" / "app.yaml"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    leftovers = [p.name for p in appliance.base.rglob("*") if p.name.startswith(".")
                 or p.name.endswith(".tmp")]
    assert leftovers == []


def test_an_interrupted_write_leaves_the_old_metadata_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review L4: open(..., "w") truncated metadata.yaml before the dump; a
    failure mid-dump left a partial file next to an already-renamed dir."""
    appliance = FakeAppliance(tmp_path)
    stub_portal_helper(monkeypatch, appliance, [])
    before = (appliance.base / "metadata.yaml").read_bytes()
    module = load_script()
    monkeypatch.setattr(module, "APPS_ROOT", str(appliance.apps_root))
    monkeypatch.setattr(module, "BACKUP_DIR", str(appliance.apps_root.parent))
    monkeypatch.setattr(module.subprocess, "check_output", appliance.check_output)
    monkeypatch.setattr(module.subprocess, "run", appliance.run)

    def half_dump(data: Any, stream: Any, **_: Any) -> None:
        stream.write("custom_app: tr")  # partial, then the interruption
        raise KeyboardInterrupt

    monkeypatch.setattr(module.yaml, "safe_dump", half_dump)
    with pytest.raises(KeyboardInterrupt):
        module.main(["fix", APP])
    assert (appliance.base / "metadata.yaml").read_bytes() == before
    leftovers = [p.name for p in appliance.base.rglob("*") if p.name.startswith(".")
                 or p.name.endswith(".tmp")]
    assert leftovers == []
