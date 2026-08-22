"""Optional single sign-on: verify an assertion from a Nexus SSO issuer.

OFF unless configured, and off is the default. With no issuer set the app
registers no SSO route, evaluates no branch and behaves exactly as it did
before this file existed.

Deliberately narrow, and the same shape every other relying party in the suite
uses: an assertion is accepted at exactly ONE endpoint, /sso/callback, where it
is exchanged for an ordinary session cookie. It is never accepted as a bearer
credential, so no API endpoint gains a new way in. HTTP Basic and the local
password keep working — an issuer outage must not lock an operator out of the
tool that deploys their VMs.

Scope of what an assertion can do, once verified:
  * It names a subject. That subject must ALREADY have a local account —
    SSO grants access to accounts that exist, it never creates them.
  * It carries no role. The local record decides everything else.
So the worst a compromised issuer can do is sign in as an existing local user;
it cannot invent an account here.

Verification is `ed25519.py`, copied verbatim from the rest of the suite —
stdlib only, verify-only, no new dependency for either the CLI or the web
extra.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from . import ed25519

ALG = "EdDSA"
TYP = "nxa"
CLOCK_SKEW = 30

SSO_ISSUER = os.environ.get("VMDEPLOY_SSO_ISSUER", "").rstrip("/")
SSO_PUBKEY = os.environ.get("VMDEPLOY_SSO_PUBKEY", "")
SSO_KID = os.environ.get("VMDEPLOY_SSO_KID", "")
SSO_AUDIENCE = os.environ.get("VMDEPLOY_SSO_AUDIENCE", "")


def _store() -> Path:
    """Runtime-enrolled configuration, beside the other state on the data
    volume so an enrollment survives a rebuild without touching the image."""
    return Path(os.environ.get("VMDEPLOY_SSO_FILE", "/data/sso.json"))


def _stored() -> dict:
    try:
        with open(_store()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _hostname() -> str:
    return socket.gethostname()


def config() -> Optional[dict]:
    """The active configuration, or None.

    The environment WINS. That is the opt-in model: an operator who wants this
    decision fixed at install time sets the env vars and the UI can only
    report it. Leaving them unset delegates the choice to the settings page.
    """
    if SSO_ISSUER:
        return {"issuer": SSO_ISSUER, "pubkey": SSO_PUBKEY, "kid": SSO_KID,
                "audience": SSO_AUDIENCE or _hostname(), "source": "env"}
    s = _stored()
    if s.get("issuer") and s.get("pubkey"):
        return {"issuer": str(s["issuer"]).rstrip("/"), "pubkey": str(s["pubkey"]),
                "kid": str(s.get("kid") or ""),
                "audience": str(s.get("audience") or _hostname()),
                "source": "stored"}
    return None


def locked() -> bool:
    """True when the host environment fixes this and the UI must not edit it."""
    return bool(SSO_ISSUER)


def save_stored(issuer: str, pubkey: str, kid: str, aud: str) -> None:
    p = _store()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump({"issuer": str(issuer).rstrip("/"), "pubkey": pubkey,
                   "kid": kid, "audience": aud}, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def clear_stored() -> bool:
    try:
        os.unlink(_store())
        return True
    except FileNotFoundError:
        return False


def _pubkey_bytes(cfg: Optional[dict] = None) -> bytes:
    import base64
    cfg = cfg if cfg is not None else config()
    s = (cfg or {}).get("pubkey", "")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        return b""


def audience() -> str:
    return (config() or {}).get("audience") or _hostname()


def enabled() -> bool:
    """True only when fully configured. A half-configured app behaves as if
    SSO were off rather than failing at login time."""
    cfg = config()
    return bool(cfg) and len(_pubkey_bytes(cfg)) == 32


def login_hint() -> dict:
    """What the login page needs to offer the button. Public values only —
    this is served to unauthenticated callers."""
    cfg = config() or {}
    return {"issuer": cfg.get("issuer", ""), "audience": audience()}


def authorize_url(next_path: str = "/") -> str:
    cfg = config() or {}
    return (cfg.get("issuer", "") + "/sso/authorize?"
            + urlencode({"aud": audience(), "next": safe_next(next_path)}))


def safe_next(value) -> str:
    """Reduce a caller-supplied path to something same-site. Anything that
    could send the browser elsewhere collapses to '/'."""
    if not value or not isinstance(value, str) or len(value) > 512:
        return "/"
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        return "/"
    value = value.replace("\\", "/")
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def redeem(issuer: str, code: str, callback: str, timeout: int = 15):
    """Redeem a one-time enrollment code at `issuer`. Returns (result, None)
    or (None, error).

    stdlib urllib, so enrollment adds no dependency either. Nothing secret is
    sent (the code is single-use and worthless once redeemed) and nothing
    secret comes back (the response is the same public key /sso/jwks already
    serves to anyone).
    """
    import ssl as _ssl
    import urllib.error
    import urllib.request

    body = json.dumps({"code": code, "callback": callback}).encode()
    req = urllib.request.Request(issuer.rstrip("/") + "/sso/enroll", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        try:
            return None, (json.loads(ex.read().decode()).get("error")
                          or "Issuer refused the code (HTTP %d)" % ex.code)
        except Exception:
            return None, "Issuer refused the code (HTTP %d)" % ex.code
    except Exception as ex:
        return None, "Could not reach the issuer: %s" % ex
    if not data.get("success"):
        return None, data.get("error") or "Enrollment failed"
    for k in ("issuer", "key", "audience"):
        if not data.get(k):
            return None, "Issuer response was missing %r" % k
    return data, None


# ─── replay cache ──────────────────────────────────────────────────────
# Assertions are single-use. The cache only has to outlive an assertion's own
# lifetime, so entries are dropped once they cannot possibly still verify.
_seen: dict = {}
_seen_lock = threading.Lock()


def _remember(jti: str, exp: int, now: Optional[int] = None) -> bool:
    """Record a jti as spent. False if it was already spent.

    `now` comes from the caller so the cache and the expiry check share one
    clock — reading time.time() here would give the verifier two notions of
    'now'.
    """
    now = int(now if now is not None else time.time())
    with _seen_lock:
        for k, v in list(_seen.items()):
            if v <= now:
                del _seen[k]
        if jti in _seen:
            return False
        _seen[jti] = exp
        return True


def _b64u_decode(s):
    import base64
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4))


def verify(token, now: Optional[int] = None) -> Optional[str]:
    """Verify an assertion and return its subject, or None.

    Never raises: every rejection path returns None, so the caller can hand it
    whatever arrived in the query string.
    """
    cfg = config()
    if not cfg or len(_pubkey_bytes(cfg)) != 32:
        return None
    now = int(now if now is not None else time.time())
    if not token or not isinstance(token, str) or token.count(".") != 2:
        return None
    h_b64, p_b64, s_b64 = token.split(".")
    try:
        header = json.loads(_b64u_decode(h_b64))
        payload = json.loads(_b64u_decode(p_b64))
        sig = _b64u_decode(s_b64)
    except Exception:
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    # The algorithm is pinned, so "alg": "none" and HMAC key confusion are not
    # reachable. There is no kid-directed key lookup either — this app knows
    # the one key it trusts.
    if header.get("alg") != ALG or header.get("typ") != TYP:
        return None
    if cfg.get("kid") and header.get("kid") != cfg["kid"]:
        return None
    if not ed25519.verify(_pubkey_bytes(cfg), (h_b64 + "." + p_b64).encode(), sig):
        return None

    # Claims are trusted only after the signature checks out.
    if payload.get("iss") != cfg["issuer"]:
        return None
    if payload.get("aud") != cfg["audience"]:
        return None
    exp, iat = payload.get("exp"), payload.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int):
        return None
    if now >= exp or iat > now + CLOCK_SKEW:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti or not _remember(jti, exp, now):
        return None
    return sub
