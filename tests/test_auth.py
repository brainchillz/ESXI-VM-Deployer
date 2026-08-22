"""Authentication: accounts, sessions, and the fact that nothing is open.

The app used to be wide open unless VMDEPLOY_PASSWORD happened to be set, and
the vCenter connection — including its credentials — sat behind that same
optional flag. These pin down that there is no longer a way in without proving
who you are.
"""
import base64

from vmdeploy import auth


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(
        f"{user}:{pw}".encode()).decode()}


# ─── nothing is reachable unauthenticated ──────────────────────────────

def test_api_requires_authentication(client):
    for path in ("/api/settings", "/api/templates", "/api/config",
                 "/api/networks", "/api/sso"):
        assert client.get(path).status_code == 401, path


def test_the_page_itself_loads_so_you_can_sign_in(client):
    """It renders the sign-in form rather than 401-ing. A browser that cannot
    fetch the page has no way in — but it carries no data of its own."""
    r = client.get("/")
    assert r.status_code == 200
    assert "loginScreen" in r.text


def test_deploy_requires_authentication(client):
    assert client.post("/api/deploy", json={}).status_code == 401


def test_the_public_paths_really_are_public(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/me").status_code == 200
    assert client.get("/api/me").json()["authenticated"] is False


# ─── ways in ───────────────────────────────────────────────────────────

def test_session_login(client):
    assert client.post("/api/login", json={"username": "admin",
                                           "password": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"username": "admin",
                                        "password": "test-password-123"})
    assert r.status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/me").json()["user"] == "admin"


def test_basic_auth_still_works(client):
    """Scripts already use it, and the local way in must keep working when the
    issuer is unreachable."""
    assert client.get("/api/settings",
                      headers=_basic("admin", "test-password-123")).status_code == 200
    assert client.get("/api/settings",
                      headers=_basic("admin", "nope")).status_code == 401


def test_logout_ends_the_session(signed_in):
    assert signed_in.post("/api/logout").status_code == 200
    assert signed_in.get("/api/settings").status_code == 401


def test_a_tampered_cookie_is_refused(client):
    client.post("/api/login", json={"username": "admin",
                                    "password": "test-password-123"})
    client.cookies.set(auth.SESSION_COOKIE, "forged.value")
    assert client.get("/api/settings").status_code == 401


def test_a_session_for_a_deleted_account_is_refused(signed_in):
    auth._save({"secret_key": auth.secret_key(),
                "users": {"someone-else": {"password": auth.hash_password("x" * 10)}}})
    assert signed_in.get("/api/settings").status_code == 401


# ─── passwords ─────────────────────────────────────────────────────────

def test_password_hashing_roundtrip():
    h = auth.hash_password("correct horse battery")
    assert auth.verify_password(h, "correct horse battery")
    assert not auth.verify_password(h, "wrong")


def test_malformed_stored_hash_is_refused_not_crashed():
    for bad in ("", "garbage", "pbkdf2_sha256$notanint$aa$bb", "a$b$c$d", None):
        assert auth.verify_password(bad, "anything") is False


def test_change_password(signed_in):
    assert signed_in.post("/api/account/password",
                          json={"old_password": "wrong",
                                "new_password": "a-new-password"}).status_code == 403
    assert signed_in.post("/api/account/password",
                          json={"old_password": "test-password-123",
                                "new_password": "short"}).status_code == 400
    assert signed_in.post("/api/account/password",
                          json={"old_password": "test-password-123",
                                "new_password": "a-new-password"}).status_code == 200
    assert auth.check_login("admin", "a-new-password")


def test_the_connection_is_no_longer_gated_on_an_env_var(signed_in):
    """It used to be readable only when VMDEPLOY_PASSWORD was set, which meant
    an unconfigured deployment exposed it to anyone. Now reaching the endpoint
    at all requires authentication."""
    r = signed_in.get("/api/settings")
    assert r.status_code == 200
    # The password itself is still write-only.
    assert "GOVC_PASSWORD" not in r.json() or r.json().get("GOVC_PASSWORD") in ("", None)
