"""Every place that states the version must state the same one.

The release version appears in several files. Keeping them in step by hand is
exactly the kind of thing that quietly rots: the catalog-app package sat at
1.2.0 for four releases before anyone noticed, and a compose file still
pointing at an older image tag would hand users a stale container while the
release notes described a fix they were not getting.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from ktnmgr import __version__

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/mechanicalcoderx/ktn-enclosure-manager"


def test_version_is_a_release_number() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_pyproject_matches() -> None:
    data = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text())
    assert data["project"]["version"] == __version__


def test_frontend_package_matches() -> None:
    data = json.loads((ROOT / "frontend" / "package.json").read_text())
    assert data["version"] == __version__


@pytest.mark.parametrize(
    "relative",
    ["docker-compose.yml", "truenas/install-via-yaml.yaml"],
)
def test_image_tags_match(relative: str) -> None:
    """A compose file pinning an older tag ships users a stale container."""
    text = (ROOT / relative).read_text()
    tags = re.findall(rf"{re.escape(IMAGE)}:(\S+)", text)
    assert tags, f"no image reference found in {relative}"
    for tag in tags:
        assert tag == __version__, f"{relative} pins {tag}, expected {__version__}"


def test_catalog_app_snapshot_is_consistent() -> None:
    """truenas/catalog-app/ is a snapshot of the package truenas/apps serves.

    Upstream owns that package: its maintainers reworked our submission, its
    `version` is the catalog package's own version (not the app's), and
    truenasbot pins the image as `<app version>@sha256:<digest>` after each
    release. So the snapshot cannot be bumped ahead of a release. It must
    agree with itself, must not claim an app version newer than this one,
    and UPSTREAM.md must name the upstream commit it was copied from.
    """
    pkg = ROOT / "truenas" / "catalog-app"
    app_yaml = (pkg / "app.yaml").read_text()
    ix_values = (pkg / "ix_values.yaml").read_text()

    app_version = re.search(r"^app_version:\s*(\S+)\s*$", app_yaml, re.MULTILINE)
    assert app_version, "app_version not found in catalog-app/app.yaml"
    tag = re.search(r"^\s*tag:\s*['\"]?([0-9][^@'\"\s]*)", ix_values, re.MULTILINE)
    assert tag, "image tag not found in catalog-app/ix_values.yaml"
    assert tag.group(1) == app_version.group(1), (
        f"ix_values tag {tag.group(1)} != app_version {app_version.group(1)}"
    )

    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))

    assert parts(app_version.group(1)) <= parts(__version__), (
        f"catalog snapshot claims {app_version.group(1)}, newer than {__version__}"
    )
    upstream = (pkg / "UPSTREAM.md").read_text()
    assert re.search(r"\b[0-9a-f]{40}\b", upstream), "UPSTREAM.md names no commit"


def test_changelog_has_an_entry_for_this_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{__version__}]" in changelog, (
        f"CHANGELOG.md has no entry for {__version__}"
    )


def test_ci_node_matches_the_frontend_builder() -> None:
    """CI must build the frontend on the same Node the image uses.

    A different Node means a different npm, which resolves a different
    dependency tree. That is not theoretical: a missing CSS type declaration
    passed `npm run typecheck` on one npm and failed the image build on
    another, for the same commit.
    """
    workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    ci_versions = set(re.findall(r'node-version:\s*"(\d+)"', workflow))
    image_versions = set(re.findall(r"FROM node:(\d+)-slim", dockerfile))

    assert ci_versions, "no node-version pins found in verify.yml"
    assert image_versions, "no node base image found in the Dockerfile"
    assert ci_versions == image_versions, (
        f"CI builds on Node {sorted(ci_versions)} but the image uses "
        f"{sorted(image_versions)}"
    )


def test_ci_python_matches_the_shipped_runtime() -> None:
    """The suite must run on the interpreter the image actually ships.

    They drifted when Dependabot bumped the runtime base to 3.14 while CI
    stayed on 3.13: the tests would have kept passing on an interpreter nobody
    receives, and a version-specific failure would have reached users instead
    of the build.
    """
    workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    ci_versions = set(re.findall(r'python-version:\s*"([\d.]+)"', workflow))
    image_versions = set(re.findall(r"FROM python:([\d.]+)-slim", dockerfile))

    assert ci_versions, "no python-version pins found in verify.yml"
    assert image_versions, "no python base image found in the Dockerfile"
    assert ci_versions == image_versions, (
        f"CI tests on {sorted(ci_versions)} but the image ships "
        f"{sorted(image_versions)}"
    )


def test_python_floor_matches_the_shipped_runtime() -> None:
    """requires-python, ruff and mypy all target the image's Python.

    Dependabot chooses each direct dependency's version under the lowest
    Python that requires-python allows. While the floor was 3.11 and the image
    3.15, a release that dropped 3.11 was never proposed, security fixes
    included. ruff and mypy below the floor would also let through code the
    image's interpreter does not need and miss what it allows. When the base
    image moves, this fails until all three move with it.
    """
    pyproject = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text())
    dockerfile = (ROOT / "Dockerfile").read_text()
    (image,) = set(re.findall(r"FROM python:(\d+\.\d+)-slim", dockerfile))

    assert pyproject["project"]["requires-python"] == f">={image}"
    assert pyproject["tool"]["ruff"]["target-version"] == "py" + image.replace(".", "")
    assert pyproject["tool"]["mypy"]["python_version"] == image


def test_readme_never_pins_an_image_tag() -> None:
    """The README must stay version-neutral about the image.

    It sat outside this file's checks and promptly drifted: an uninstall
    example pinned :1.4.0 and survived three releases. Prose that names a
    specific tag goes stale silently; commands in the README must work on
    whatever version the reader runs.
    """
    text = (ROOT / "README.md").read_text()
    tags = re.findall(rf"{re.escape(IMAGE)}:(\S+)", text)
    pinned = [t for t in tags if t != "latest"]
    assert not pinned, f"README pins image tag(s) {pinned}; keep it version-neutral"
