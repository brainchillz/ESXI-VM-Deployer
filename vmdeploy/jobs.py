"""In-memory job store for async deploys (home-lab scale; not persistent)."""
from __future__ import annotations

import threading
import uuid

_jobs: dict[str, dict] = {}
_lock = threading.Lock()

# Ordered steps the UI renders as a progress trail.
STEPS = ["checking", "cloning", "injecting", "powering-on", "waiting-for-ip", "done"]


def create(vm: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[jid] = {
            "id": jid, "vm": vm, "status": "running",
            "step": "queued", "ip": None, "error": None,
        }
    return jid


def update(jid: str, **kw) -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def get(jid: str) -> dict | None:
    with _lock:
        return dict(_jobs[jid]) if jid in _jobs else None
