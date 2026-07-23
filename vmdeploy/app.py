from __future__ import annotations

import asyncio
import os
import secrets as _secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import config, core, govc, jobs
from .models import DeploySpec

_basic = HTTPBasic(auto_error=False)


def require_auth(creds: Optional[HTTPBasicCredentials] = Depends(_basic)) -> None:
    """HTTP Basic auth, enforced only when VMDEPLOY_PASSWORD is set. Until then
    the app is open (backward compatible), but editing the vCenter connection /
    credentials stays locked (see put_settings)."""
    password = os.environ.get("VMDEPLOY_PASSWORD", "")
    if not password:
        return
    username = os.environ.get("VMDEPLOY_USERNAME", "admin")
    ok = (
        creds is not None
        and _secrets.compare_digest(creds.username, username)
        and _secrets.compare_digest(creds.password, password)
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )


app = FastAPI(title="VM Deployer", dependencies=[Depends(require_auth)])
_INDEX = (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX


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
    """Persist edited settings to the mounted config file. Editing the vCenter
    connection/credentials requires app auth (VMDEPLOY_PASSWORD)."""
    if any(k in body for k in config.CONNECTION_KEYS) and not config.auth_configured():
        raise HTTPException(
            status_code=403,
            detail="Set VMDEPLOY_PASSWORD (and sign in) to edit the vCenter "
                   "connection or credentials.",
        )
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
