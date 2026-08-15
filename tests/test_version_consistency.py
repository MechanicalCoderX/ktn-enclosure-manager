"""Every place that states the version must state the same one.

The release version appears in seven files. Keeping them in step by hand is
exactly the kind of thing that quietly rots: the catalog-app package sat at
1.2.0 for four releases before anyone noticed, and a compose file still
pointing at an older image tag would hand users a stale container while the
release notes described a fix they were not getting.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import tomllib
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


def test_catalog_app_matches() -> None:
    """The package submitted to truenas/apps carries its own version pair."""
    app_yaml = (ROOT / "truenas" / "catalog-app" / "app.yaml").read_text()
    ix_values = (ROOT / "truenas" / "catalog-app" / "ix_values.yaml").read_text()

    for field in ("app_version", "version"):
        match = re.search(rf"^{field}:\s*(\S+)\s*$", app_yaml, re.MULTILINE)
        assert match, f"{field} not found in catalog-app/app.yaml"
        assert match.group(1) == __version__, f"app.yaml {field} is {match.group(1)}"

    tag = re.search(r"^\s*tag:\s*(\S+)\s*$", ix_values, re.MULTILINE)
    assert tag, "tag not found in catalog-app/ix_values.yaml"
    assert tag.group(1) == __version__


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
