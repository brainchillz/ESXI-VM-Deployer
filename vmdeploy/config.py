"""Runtime configuration: process env overlaid by a writable JSON file.

The web UI can edit settings at runtime (the ⚙ dialog). Those overrides are
persisted to a JSON file on a mounted volume (VMDEPLOY_CONFIG, default
/data/settings.json) and layered ON TOP of the process environment, so a change
takes effect on the next govc call without restarting the container.

Precedence (highest first): settings file → process env (config.env / .env).

Deliberately stdlib-only so the CLI (which imports govc → config) keeps its
no-dependencies promise. On a CLI host the settings file simply doesn't exist,
so effective config == the GOVC_* the user sourced into their env.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# vCenter connection — sensitive; editing these requires app auth.
CONNECTION_KEYS = ["GOVC_URL", "GOVC_USERNAME", "GOVC_PASSWORD", "GOVC_INSECURE"]
# Placement + deploy defaults — non-secret, editable without auth.
PLACEMENT_KEYS = ["GOVC_DATACENTER", "GOVC_DATASTORE", "GOVC_RESOURCE_POOL",
                  "GOVC_FOLDER", "GOVC_NETWORK"]
DEFAULT_KEYS = ["DEFAULT_PROFILE", "DEFAULT_CIDR", "DEFAULT_DNS", "DEFAULT_SSH_PUBKEY"]

GOVC_KEYS = CONNECTION_KEYS + PLACEMENT_KEYS
ALL_KEYS = GOVC_KEYS + DEFAULT_KEYS
SECRET_KEYS = {"GOVC_PASSWORD"}


def _path() -> Path:
    return Path(os.environ.get("VMDEPLOY_CONFIG", "/data/settings.json"))


def _overrides() -> dict:
    p = _path()
    if p.is_file():
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def get(key: str, default: str = "") -> str:
    """Effective value: non-empty file override wins, else process env."""
    ov = _overrides()
    v = ov.get(key)
    if v not in (None, ""):
        return str(v)
    return os.environ.get(key, default)


def govc_env() -> dict:
    """Full environment dict for the govc subprocess: process env + file
    overrides for the GOVC_* keys (keeps PATH, HOME, session vars, etc.)."""
    env = dict(os.environ)
    for k, v in _overrides().items():
        if k in GOVC_KEYS and v not in (None, ""):
            env[k] = str(v)
    return env


def auth_configured() -> bool:
    return bool(os.environ.get("VMDEPLOY_PASSWORD", ""))


def effective(*, include_connection: bool) -> dict:
    """Current settings for the UI. The password is never returned (write-only);
    connection fields are omitted entirely unless include_connection is set."""
    keys = list(ALL_KEYS) if include_connection else (PLACEMENT_KEYS + DEFAULT_KEYS)
    out: dict = {}
    for k in keys:
        if k in SECRET_KEYS:
            out[k] = ""                       # never expose the secret
            out[k + "_set"] = bool(get(k))    # only whether one exists
        else:
            out[k] = get(k)
    out["_auth_configured"] = auth_configured()
    out["_connection_locked"] = not include_connection
    out["_config_path"] = str(_path())
    return out


def update(new: dict) -> None:
    """Merge provided keys into the settings file. An empty secret is ignored
    (keeps the existing password). Raises OSError if the path isn't writable."""
    ov = _overrides()
    for k, v in new.items():
        if k not in ALL_KEYS:
            continue
        if k in SECRET_KEYS and (v is None or v == ""):
            continue  # write-only: blank means "leave unchanged"
        ov[k] = v
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(ov, indent=2))
    tmp.replace(p)  # atomic
