"""Fixtures of the vtherm_quirks tests."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/vtherm_quirks."""
    yield


@pytest.fixture(autouse=True)
def reset_vtherm_api():
    """Start each test with a clean VThermAPI singleton."""
    from vtherm_api import VThermAPI

    VThermAPI.reset_vtherm_api()
    yield
    VThermAPI.reset_vtherm_api()
