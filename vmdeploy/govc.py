"""Thin subprocess wrapper around the govc CLI.

Connection/placement come from GOVC_* environment variables (same as the bash
toolkit's config.env), inherited by the subprocess — so the container just needs
those vars set.
"""
from __future__ import annotations

import subprocess
import time

from . import config


class GovcError(RuntimeError):
    pass


def _run(args: list[str], timeout: int = 120) -> str:
    try:
        p = subprocess.run(
            ["govc", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            # Effective env = process env overlaid by any runtime settings file,
            # so ⚙-dialog edits take effect without a restart.
            env=config.govc_env(),
        )
    except FileNotFoundError:
        raise GovcError("govc binary not found in PATH")
    except subprocess.TimeoutExpired:
        raise GovcError(f"govc {args[0]} timed out after {timeout}s")
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip() or f"exit {p.returncode}"
        raise GovcError(f"govc {' '.join(args)}: {msg}")
    return p.stdout


def about() -> None:
    """Prove connectivity/credentials. Raises GovcError on failure."""
    _run(["about"])


def find_templates() -> list[str]:
    """Inventory paths of VMs named '*-template' (fast prefilter)."""
    out = _run(["find", "-type", "m", "-name", "*-template"])
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def list_networks() -> list[str]:
    """Names of attachable networks / portgroups (NIC targets), scoped to
    GOVC_DATACENTER. Basenames are what vm.clone -net expects."""
    out = _run(["find", "-type", "n"])
    return sorted({ln.rsplit("/", 1)[-1] for ln in out.splitlines() if ln.strip()})


def list_datastores() -> list[str]:
    """Names of available datastores, scoped to GOVC_DATACENTER. Basenames are
    what vm.clone -ds expects."""
    out = _run(["find", "-type", "s"])
    return sorted({ln.rsplit("/", 1)[-1] for ln in out.splitlines() if ln.strip()})


def get_prop(path: str, prop: str) -> str:
    return _run(["object.collect", "-s", path, prop]).strip()


def vm_exists(name: str) -> bool:
    # govc vm.info exits 0 with empty output when not found; non-zero only on a
    # real API error (which propagates as GovcError).
    return "Name:" in _run(["vm.info", name])


def clone(template: str, name: str, *, datastore: str | None = None,
          network: str | None = None) -> None:
    # Placement defaults to GOVC_DATASTORE / GOVC_NETWORK in the env; an explicit
    # datastore/network overrides just this clone (leaves the container's default
    # intact for the next deploy).
    args = ["vm.clone", "-vm", template, "-on=false"]
    if datastore:
        args += ["-ds", datastore]
    if network:
        args += ["-net", network]
    args.append(name)
    _run(args, timeout=900)


def resize_disk(name: str, size_gb: int) -> None:
    """Grow the VM's primary disk ('Hard disk 1') to size_gb. vSphere can only
    grow a disk — a size smaller than the template's raises a GovcError."""
    _run(["vm.disk.change", "-vm", name, "-disk.label", "Hard disk 1", "-size", f"{size_gb}G"])


def set_guestinfo(name: str, metadata_b64: str, userdata_b64: str) -> None:
    _run([
        "vm.change", "-vm", name,
        "-e", f"guestinfo.metadata={metadata_b64}",
        "-e", "guestinfo.metadata.encoding=gzip+base64",
        "-e", f"guestinfo.userdata={userdata_b64}",
        "-e", "guestinfo.userdata.encoding=gzip+base64",
    ])


def power_on(name: str) -> None:
    _run(["vm.power", "-on", name])


def wait_static_ip(name: str, ip: str, timeout: int = 300) -> str | None:
    """Poll until the guest reports the CONFIGURED static IP (ignoring the
    transient boot-time DHCP lease that govc vm.ip would return first)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = _run(["vm.info", "-json", name])
        except GovcError:
            out = ""
        if f'"{ip}"' in out:
            return ip
        time.sleep(5)
    return None


def read_reported_ip(name: str) -> str | None:
    """Read guestinfo.deploy.ipv4 — the settled IP the guest publishes at the end
    of cloud-init (authoritative; not a transient boot-time DHCP lease)."""
    try:
        out = _run(["vm.info", "-e", name])
    except GovcError:
        return None
    for line in out.splitlines():
        if "guestinfo.deploy.ipv4:" in line:
            val = line.split(":", 1)[1].strip()
            return val or None
    return None


def wait_reported_ip(name: str, timeout: int = 300) -> str | None:
    """Poll until the guest publishes its settled IP via guestinfo."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = read_reported_ip(name)
        if ip:
            return ip
        time.sleep(5)
    return None
