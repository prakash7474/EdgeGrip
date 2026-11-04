"""Phase 3 conftest."""
from __future__ import annotations

import pytest
import requests


@pytest.fixture(autouse=True)
def _reset_mocks(mock_providers):
    try:
        requests.get(f"{mock_providers['ca']}/control/reset", timeout=2)
        requests.get(f"{mock_providers['cb']}/control/reset", timeout=2)
    except Exception:
        pass
    yield
    try:
        requests.get(f"{mock_providers['ca']}/control/reset", timeout=2)
        requests.get(f"{mock_providers['cb']}/control/reset", timeout=2)
    except Exception:
        pass
