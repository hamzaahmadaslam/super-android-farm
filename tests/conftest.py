"""Shared test setup.

Unit tests (tests/unit) need nothing but Python: no Docker, no network, no devices.
Live tests (tests/live) drive real containers and are skipped unless SF_LIVE=1.
Every live test creates its own objects under a `sftest` prefix in a throwaway farm
home and removes them again, so it can run on a machine that hosts a real farm.
"""
import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("SF_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set SF_LIVE=1 (needs Docker and an Android image)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def farm_home(tmp_path, monkeypatch):
    """An empty farm home, so a test can never read or write the operator's real farm."""
    monkeypatch.setenv("SUPERFARM_HOME", str(tmp_path / "farm"))
    from superfarm.paths import get_paths
    return get_paths().ensure()
