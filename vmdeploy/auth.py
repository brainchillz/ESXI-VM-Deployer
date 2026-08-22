"""Local accounts, password hashing and browser sessions.

Replaces the single shared password with named accounts, because an assertion
from the SSO issuer names a *subject* and that subject has to map onto
something. A shared password has no identity to map onto, so proper accounts
are the precondition for single sign-on rather than a separate nicety.

Stdlib only, deliberately. pyproject promises the CLI has no runtime
dependencies and the web extra is three packages; neither budget moves to gain
authentication. PBKDF2-SHA256 comes from hashlib and the session cookie is
signed with hmac — the same primitives a library would use.

HTTP Basic still works when VMDEPLOY_PASSWORD is set. Scripts already use it,
and the local way in must keep working even when the issuer is unreachable —
the same rule the rest of the suite follows about never letting SSO become the
only door.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

SESSION_COOKIE = "vmdeploy_session"
SESSION_HOURS = int(os.environ.get("VMDEPLOY_SESSION_HOURS", "12"))
PBKDF2_ROUNDS = 600_000
MIN_PASSWORD_LEN = 8

# Reachable without a session: the login endpoint itself, the SSO landing
# point (whoever arrives there has no session yet — the assertion is the
# credential, checked inside the handler), and the two things the login page
# needs in order to draw itself.
# "/" is here because it renders the sign-in page itself rather than 401-ing:
# a browser that cannot fetch the page has no way to sign in. It is a static
# form with no data in it — every endpoint behind it stays protected.
PUBLIC_PATHS = {"/", "/api/login", "/api/logout", "/api/me", "/sso/callback",
                "/healthz"}


def _path() -> Path:
    return Path(os.environ.get("VMDEPLOY_USERS", "/data/users.json"))


def _load() -> dict:
    try:
        with open(_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(d: dict) -> None:
    """Write via a temp file in the same directory, then replace, so a crash
    mid-write leaves the original intact rather than a truncated file."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# ─── passwords ─────────────────────────────────────────────────────────

def hash_password(password: str, rounds: int = PBKDF2_ROUNDS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return "pbkdf2_sha256$%d$%s$%s" % (rounds, salt.hex(), dk.hex())


def verify_password(stored: str, password: str) -> bool:
    """Constant-time, and never raises on a malformed stored value."""
    try:
        scheme, rounds, salt_hex, want_hex = str(stored).split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(dk.hex(), want_hex)


# ─── accounts ──────────────────────────────────────────────────────────

def users() -> dict:
    u = _load().get("users")
    return u if isinstance(u, dict) else {}


def user_exists(username: str) -> bool:
    return username in users()


def secret_key() -> str:
    """The key session cookies are signed with. Generated on first use and
    kept with the accounts, so restarting does not sign everyone out."""
    d = _load()
    if not d.get("secret_key"):
        d["secret_key"] = secrets.token_hex(32)
        d.setdefault("users", {})
        _save(d)
    return d["secret_key"]


def set_password(username: str, password: str) -> None:
    d = _load()
    rec = d.setdefault("users", {}).get(username)
    rec = dict(rec) if isinstance(rec, dict) else {}
    rec["password"] = hash_password(password)
    d["users"][username] = rec
    _save(d)


def delete_user(username: str) -> bool:
    d = _load()
    if username not in d.get("users", {}):
        return False
    if len(d["users"]) == 1:
        return False
    del d["users"][username]
    _save(d)
    return True


def check_login(username: str, password: str) -> bool:
    rec = users().get(username)
    if not isinstance(rec, dict):
        # Equalise timing so a wrong username and a wrong password cost the
        # same, rather than the absence of a hash being measurably faster.
        hashlib.pbkdf2_hmac("sha256", password.encode(), b"dummy-salt",
                            PBKDF2_ROUNDS)
        return False
    return verify_password(rec.get("password", ""), password)


def ensure_bootstrap() -> None:
    """Make sure an account exists.

    Adopts VMDEPLOY_USERNAME / VMDEPLOY_PASSWORD on first run, so an existing
    deployment keeps the credentials its operator already set rather than
    printing a new one they have to go and find.
    """
    d = _load()
    if d.get("users"):
        return
    pw = os.environ.get("VMDEPLOY_PASSWORD", "")
    name = os.environ.get("VMDEPLOY_USERNAME", "admin")
    generated = False
    if not pw:
        pw = secrets.token_urlsafe(12)
        generated = True
    d.setdefault("users", {})[name] = {"password": hash_password(pw)}
    d.setdefault("secret_key", secrets.token_hex(32))
    _save(d)
    if generated:
        print("=" * 64, flush=True)
        print("VC-Deployer: created initial account", flush=True)
        print("  username: %s" % name, flush=True)
        print("  password: %s" % pw, flush=True)
        print("=" * 64, flush=True)


# ─── sessions ──────────────────────────────────────────────────────────

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session(username: str, now: Optional[int] = None) -> str:
    """A signed `username|expiry` cookie. Nothing secret is inside it; the
    signature is what makes it unforgeable."""
    now = int(now if now is not None else time.time())
    payload = "%s|%d" % (username, now + SESSION_HOURS * 3600)
    sig = hmac.new(secret_key().encode(), payload.encode(), hashlib.sha256).digest()
    return _b64u(payload.encode()) + "." + _b64u(sig)


def read_session(cookie: Optional[str], now: Optional[int] = None) -> Optional[str]:
    """The username a cookie names, or None. Never raises."""
    if not cookie or "." not in cookie:
        return None
    now = int(now if now is not None else time.time())
    try:
        p_b64, s_b64 = cookie.split(".", 1)
        payload = _unb64u(p_b64).decode()
        sig = _unb64u(s_b64)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    want = hmac.new(secret_key().encode(), payload.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, want):
        return None
    try:
        username, expires = payload.rsplit("|", 1)
        if int(expires) <= now:
            return None
    except ValueError:
        return None
    # A cookie naming an account that no longer exists is not a valid session.
    return username if user_exists(username) else None


def basic_credentials(header: Optional[str]) -> Optional[tuple]:
    """Decode an Authorization: Basic header, or None."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(None, 1)[1]).decode()
    except (ValueError, IndexError, UnicodeDecodeError, binascii.Error):
        return None
    if ":" not in raw:
        return None
    username, password = raw.split(":", 1)
    return username, password
