import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

import server  # noqa: E402


@pytest.fixture
def client():
    server.reset_for_tests()
    with TestClient(server.app) as c:
        yield c
    server.reset_for_tests()
