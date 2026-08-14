"""Test isolation.

Without this the suite reads the developer's real `.env` and `KTN_*`
environment. That is not hypothetical: it already caused a divergence where
tests passed locally and failed in CI, because settings differed between the
two machines. Tests must describe their own world completely.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_ktn_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every KTN_* variable so ambient config cannot reach a test."""
    for key in list(os.environ):
        if key.startswith("KTN_"):
            monkeypatch.delenv(key, raising=False)
