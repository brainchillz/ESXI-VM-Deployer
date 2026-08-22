import os
import tempfile

# Every path the app touches must point at a throwaway dir BEFORE the package
# is imported — the modules resolve them at import time.
_TMP = tempfile.mkdtemp(prefix="vmdeploy-test-")
os.environ["VMDEPLOY_USERS"] = os.path.join(_TMP, "users.json")
os.environ["VMDEPLOY_SSO_FILE"] = os.path.join(_TMP, "sso.json")
os.environ["VMDEPLOY_CONFIG"] = os.path.join(_TMP, "settings.json")
os.environ["VMDEPLOY_PASSWORD"] = "test-password-123"
os.environ["VMDEPLOY_USERNAME"] = "admin"
os.environ["VMDEPLOY_COOKIE_SECURE"] = "0"

import pytest                                      # noqa: E402
from fastapi.testclient import TestClient          # noqa: E402

from vmdeploy import auth                          # noqa: E402
from vmdeploy.app import app                       # noqa: E402


@pytest.fixture(autouse=True)
def clean_users():
    """Each test starts with exactly the bootstrap account."""
    auth._save({"secret_key": auth.secret_key(),
                "users": {"admin": {"password": auth.hash_password("test-password-123")}}})
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def signed_in(client):
    r = client.post("/api/login", json={"username": "admin",
                                        "password": "test-password-123"})
    assert r.status_code == 200
    return client
