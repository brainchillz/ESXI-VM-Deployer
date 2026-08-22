"""Optional SSO: the verifier, and the guarantee that an unconfigured app is
unchanged.

Assertions here are REAL issuer output, committed as a fixture by NexusSSO
tools/gen_rp_fixtures.py. That matters: this app has no crypto dependency and
cannot mint, so testing against locally-constructed tokens would only prove the
verifier agrees with itself. If the two sides ever drift, these fail.

The negative cases are the ones that carry weight — a verifier that returned a
subject for everything would pass every positive test here.
"""
import json
from pathlib import Path

import pytest

from vmdeploy import auth, sso

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "sso_assertions.json").read_text())
NOW = FIXTURES["now"]
A = FIXTURES["assertions"]


@pytest.fixture
def configured(monkeypatch):
    """Point the verifier at the fixture issuer, and freeze its clock.

    Module-level env is read at import, so the attributes are patched
    directly. The clock matters for the route tests: the fixtures are minted
    at a fixed timestamp so they stay reproducible, which means real wall time
    reads every one of them as long expired. Unit tests pass `now=NOW`
    explicitly; the HTTP callback has no such seam, so time is frozen here
    instead of loosening the expiry check to accommodate a test.
    """
    monkeypatch.setattr(sso, "SSO_ISSUER", FIXTURES["issuer"])
    monkeypatch.setattr(sso, "SSO_PUBKEY", FIXTURES["pubkey"])
    monkeypatch.setattr(sso, "SSO_KID", FIXTURES["kid"])
    monkeypatch.setattr(sso, "SSO_AUDIENCE", FIXTURES["audience"])
    monkeypatch.setattr(sso.time, "time", lambda: float(NOW))
    sso._seen.clear()
    yield
    sso._seen.clear()


# ─── off by default ────────────────────────────────────────────────────

def test_disabled_when_unconfigured():
    assert sso.config() is None
    assert sso.enabled() is False
    assert sso.verify(A["valid"], now=NOW) is None


def test_callback_is_absent_when_disabled(client):
    """Not merely refused — there is nothing there to probe."""
    assert client.get("/sso/callback?a=" + A["valid"]).status_code == 404


def test_me_does_not_mention_sso_when_disabled(client):
    assert "sso" not in client.get("/api/me").json()


def test_a_half_configured_app_behaves_as_if_off(monkeypatch):
    """An issuer with no key must not fail open, or fail confusingly at login
    time — it is simply off."""
    monkeypatch.setattr(sso, "SSO_ISSUER", FIXTURES["issuer"])
    monkeypatch.setattr(sso, "SSO_PUBKEY", "")
    assert sso.enabled() is False
    assert sso.verify(A["valid"], now=NOW) is None


# ─── the verifier ──────────────────────────────────────────────────────

def test_a_valid_assertion_names_its_subject(configured):
    assert sso.verify(A["valid"], now=NOW) == "admin"


@pytest.mark.parametrize("case", [
    "expired", "wrong_audience", "wrong_issuer", "issued_in_the_future",
    "signed_by_another_key",
])
def test_bad_assertions_are_refused(configured, case):
    assert sso.verify(A[case], now=NOW) is None


def test_an_assertion_is_single_use(configured):
    """Replay is what a stolen assertion would buy, so the jti cache is the
    thing standing between a leaked URL and a session."""
    assert sso.verify(A["valid"], now=NOW) == "admin"
    assert sso.verify(A["valid"], now=NOW) is None


def test_garbage_never_raises(configured):
    for junk in (None, "", "not-a-token", "a.b", "a.b.c", "..", 12345,
                 "x" * 5000, A["valid"][:-4]):
        assert sso.verify(junk, now=NOW) is None


def test_a_tampered_payload_is_refused(configured):
    head, payload, sig = A["valid"].split(".")
    assert sso.verify(f"{head}.{payload[:-2]}xx.{sig}", now=NOW) is None


def test_the_kid_must_match_when_pinned(configured, monkeypatch):
    monkeypatch.setattr(sso, "SSO_KID", "0000000000000000")
    assert sso.verify(A["valid"], now=NOW) is None


# ─── the callback ──────────────────────────────────────────────────────

def test_callback_signs_in_an_existing_account(configured, client):
    r = client.get("/sso/callback?a=" + A["valid"], follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/me").json()["user"] == "admin"


def test_sso_never_creates_an_account(configured, client):
    """The subject exists at the issuer but not here. The worst a compromised
    issuer can do is sign in as somebody who already has an account."""
    assert not auth.user_exists("nosuchuser")
    r = client.get("/sso/callback?a=" + A["unknown_user"], follow_redirects=False)
    assert r.status_code == 302
    assert "sso_error=unknown_user" in r.headers["location"]
    assert not auth.user_exists("nosuchuser")
    assert client.get("/api/settings").status_code == 401


def test_a_rejected_assertion_grants_nothing(configured, client):
    r = client.get("/sso/callback?a=" + A["expired"], follow_redirects=False)
    assert r.status_code == 302
    assert "sso_error=invalid" in r.headers["location"]
    assert client.get("/api/settings").status_code == 401


def test_the_next_path_cannot_leave_the_site(configured, client):
    r = client.get("/sso/callback?a=" + A["valid"] + "&next=//evil.example.com",
                   follow_redirects=False)
    assert r.headers["location"] == "/"


def test_an_assertion_is_not_a_bearer_credential(configured, client):
    """It is accepted at exactly one endpoint. No API route gains a new way
    in, so this adds one door rather than one per route."""
    assert client.get("/api/settings?a=" + A["valid"]).status_code == 401
    assert client.get("/api/settings",
                      headers={"Authorization": "Bearer " + A["valid"]}).status_code == 401


def test_me_advertises_sso_when_enabled(configured, client):
    body = client.get("/api/me").json()
    assert body["sso"]["issuer"] == FIXTURES["issuer"]
    assert body["sso"]["audience"] == FIXTURES["audience"]


# ─── enrollment ────────────────────────────────────────────────────────

def test_enrollment_requires_a_session(client):
    assert client.post("/api/sso/enroll",
                       json={"issuer": "https://x", "code": "nxe_y"}).status_code == 401


def test_enrollment_is_refused_when_the_environment_fixes_it(configured, signed_in):
    assert sso.locked() is True
    r = signed_in.post("/api/sso/enroll",
                       json={"issuer": "https://x", "code": "nxe_y"})
    assert r.status_code == 409


def test_stored_enrollment_round_trips(monkeypatch, signed_in):
    monkeypatch.setattr(sso, "SSO_ISSUER", "")
    sso.save_stored(FIXTURES["issuer"], FIXTURES["pubkey"], FIXTURES["kid"],
                    FIXTURES["audience"])
    assert sso.enabled() is True
    assert sso.config()["source"] == "stored"
    assert sso.verify(A["valid"], now=NOW) == "admin"
    assert signed_in.delete("/api/sso").json()["success"] is True
    assert sso.enabled() is False
