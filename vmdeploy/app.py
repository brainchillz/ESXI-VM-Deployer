from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, sso
from . import config, core, govc, jobs
from .models import DeploySpec

auth.ensure_bootstrap()


def require_auth(request: Request) -> None:
    """Authenticate every request that is not explicitly public.

    Three ways in, in order: a session cookie (the browser, however it signed
    in), HTTP Basic (scripts, and the way this app has always worked), and
    nothing at all when no accounts exist — which cannot happen, because
    ensure_bootstrap() runs at import.

    Basic is kept deliberately. Something already uses it, and the local way in
    has to keep working when the issuer is unreachable; SSO must never become
    the only door to the thing that deploys the VMs.
    """
    path = request.url.path
    if path in auth.PUBLIC_PATHS or path.startswith("/static/"):
        return
    if auth.read_session(request.cookies.get(auth.SESSION_COOKIE)):
        return
    creds = auth.basic_credentials(request.headers.get("authorization"))
    if creds and auth.check_login(*creds):
        return
    raise HTTPException(
        status_code=401,
        detail="authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


app = FastAPI(title="ESXi VM Deployer", dependencies=[Depends(require_auth)])
_INDEX = (Path(__file__).parent / "static" / "index.html").read_text()


def _current_user(request: Request) -> Optional[str]:
    user = auth.read_session(request.cookies.get(auth.SESSION_COOKIE))
    if user:
        return user
    creds = auth.basic_credentials(request.headers.get("authorization"))
    if creds and auth.check_login(*creds):
        return creds[0]
    return None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX


# ─── sessions ──────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict:
    """Served to unauthenticated callers on purpose: it is what tells the
    login page whether to offer the SSO button. Public values only."""
    user = _current_user(request)
    body = {"authenticated": bool(user), "user": user}
    if sso.enabled():
        body["sso"] = sso.login_hint()
    return body


@app.post("/api/login")
def login(body: dict, response: Response) -> dict:
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not auth.check_login(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    _set_session(response, username)
    return {"success": True, "user": username}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"success": True}


@app.post("/api/account/password")
def change_password(body: dict, request: Request) -> dict:
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    new = str(body.get("new_password") or "")
    if not auth.check_login(user, str(body.get("old_password") or "")):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    if len(new) < auth.MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least %d characters" % auth.MIN_PASSWORD_LEN)
    auth.set_password(user, new)
    return {"success": True}


def _set_session(response: Response, username: str) -> None:
    """Secure is decided by the deployment, not guessed. Caddy terminates TLS
    and proxies plain HTTP, so the app cannot infer it from its own listener —
    the same trap NEXUSSSO_COOKIE_SECURE exists to avoid.
    """
    secure = os.environ.get("VMDEPLOY_COOKIE_SECURE", "1").lower() not in (
        "0", "false", "no", "off")
    response.set_cookie(
        auth.SESSION_COOKIE, auth.make_session(username),
        max_age=auth.SESSION_HOURS * 3600, httponly=True,
        samesite="lax", secure=secure, path="/")


# ─── single sign-on ────────────────────────────────────────────────────

@app.get("/sso/callback")
def sso_callback(request: Request, a: str = "", next: str = "/"):
    """The one place an assertion is ever accepted.

    Exchanged for an ordinary session cookie and nothing more. It is not a
    bearer credential: no other endpoint looks at it, so this adds exactly one
    way in rather than one per route.
    """
    if not sso.enabled():
        raise HTTPException(status_code=404, detail="not found")
    subject = sso.verify(a)
    if not subject:
        return RedirectResponse("/?sso_error=invalid", status_code=302)
    # SSO grants access to accounts that exist; it never creates them. The
    # worst a compromised issuer can do here is sign in as someone who already
    # has an account.
    if not auth.user_exists(subject):
        return RedirectResponse("/?sso_error=unknown_user", status_code=302)
    response = RedirectResponse(sso.safe_next(next), status_code=302)
    _set_session(response, subject)
    return response


@app.get("/api/sso")
def sso_status() -> dict:
    cfg = sso.config() or {}
    return {"enabled": sso.enabled(), "locked": sso.locked(),
            "issuer": cfg.get("issuer", ""), "audience": sso.audience(),
            "kid": cfg.get("kid", ""), "source": cfg.get("source", "")}


@app.post("/api/sso/enroll")
def sso_enroll(body: dict, request: Request) -> dict:
    """Redeem a one-time code minted by the issuer's operator.

    Requires a session here too: neither side can enroll the other
    unilaterally, which is what stops an application registering itself.
    """
    if sso.locked():
        raise HTTPException(
            status_code=409,
            detail="SSO is fixed by the environment (VMDEPLOY_SSO_ISSUER); "
                   "unset it to manage enrollment here.")
    issuer = str(body.get("issuer") or "").strip()
    code = str(body.get("code") or "").strip()
    if not issuer or not code:
        raise HTTPException(status_code=400, detail="issuer and code are required")
    base = str(request.base_url).rstrip("/")
    data, err = sso.redeem(issuer, code, base + "/sso/callback")
    if err:
        raise HTTPException(status_code=400, detail=err)
    sso.save_stored(data["issuer"], data["key"], data.get("kid", ""),
                    data["audience"])
    return {"success": True, "issuer": data["issuer"],
            "audience": data["audience"]}


@app.delete("/api/sso")
def sso_forget() -> dict:
    if sso.locked():
        raise HTTPException(status_code=409, detail="SSO is fixed by the environment")
    return {"success": sso.clear_stored()}



@app.get("/api/config")
def ui_config() -> dict:
    """Non-secret UI prefill: default SSH key + default placement, read from the
    effective config (process env overlaid by the runtime settings file)."""
    return {
        "default_ssh_pubkey": config.get("DEFAULT_SSH_PUBKEY").strip(),
        "default_network": config.get("GOVC_NETWORK").strip(),
        "default_datastore": config.get("GOVC_DATASTORE").strip(),
        "auth_configured": config.auth_configured(),
    }


@app.get("/api/templates")
def templates() -> list[dict]:
    try:
        return core.list_templates()
    except govc.GovcError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/networks")
def networks() -> list[str]:
    try:
        return govc.list_networks()
    except govc.GovcError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/datastores")
def datastores() -> list[str]:
    try:
        return govc.list_datastores()
    except govc.GovcError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/settings")
def get_settings() -> dict:
    """Current editable settings. Connection/credentials are included only when
    app auth is configured; the password is always write-only (never returned)."""
    return config.effective(include_connection=config.auth_configured())


@app.put("/api/settings")
def put_settings(body: dict) -> dict:
    """Persist edited settings to the mounted config file. Editing the ESXi
    connection/credentials requires app auth (VMDEPLOY_PASSWORD)."""
    try:
        config.update(body)
    except OSError as e:
        path = config.effective(include_connection=False).get("_config_path")
        raise HTTPException(
            status_code=500,
            detail=f"Could not persist settings ({e}). Mount a writable volume at "
                   f"{path} (or set VMDEPLOY_CONFIG).",
        )
    return config.effective(include_connection=config.auth_configured())


@app.post("/api/deploy")
async def deploy(spec: DeploySpec) -> dict:
    try:
        spec.validate_request()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    jid = jobs.create(spec.name)
    asyncio.create_task(_run_deploy(jid, spec))
    return {"job": jid}


async def _run_deploy(jid: str, spec: DeploySpec) -> None:
    def progress(step: str) -> None:
        jobs.update(jid, step=step)

    try:
        ip = await asyncio.to_thread(core.deploy, spec, progress)
        jobs.update(jid, status="done", step="done", ip=ip)
    except Exception as e:  # surface any failure to the UI
        jobs.update(jid, status="failed", step="failed", error=str(e))


@app.get("/api/jobs/{jid}")
def job(jid: str) -> dict:
    j = jobs.get(jid)
    if not j:
        raise HTTPException(status_code=404, detail="no such job")
    return j
