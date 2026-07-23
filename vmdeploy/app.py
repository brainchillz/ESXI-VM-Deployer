from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from . import core, govc, jobs
from .models import DeploySpec

app = FastAPI(title="VM Deployer")
_INDEX = (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX


@app.get("/api/config")
def config() -> dict:
    """Non-secret UI config: prefill SSH key + the container's default placement
    (so the UI can preselect the network/datastore the operator normally uses)."""
    return {
        "default_ssh_pubkey": os.environ.get("DEFAULT_SSH_PUBKEY", "").strip(),
        "default_network": os.environ.get("GOVC_NETWORK", "").strip(),
        "default_datastore": os.environ.get("GOVC_DATASTORE", "").strip(),
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
