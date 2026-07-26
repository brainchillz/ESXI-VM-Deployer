"""Thin subprocess wrapper around the govc CLI, targeting a STANDALONE ESXi host.

Connection/placement come from GOVC_* environment variables (same as the bash
toolkit's config.env), inherited by the subprocess — so the container just needs
those vars set. GOVC_URL points at the ESXi host itself (https://<host>/sdk).

The one deep difference from the vCenter deployer this was forked from:
standalone ESXi has no CloneVM_Task, so clone() re-implements "clone" as
copy-the-template-disk + build-a-VM-shell-around-it (the same pattern the
builder toolkit uses for qcow2/VHD imports). The shell's hardware is read live
from the template VM, so templates built from OVAs (whose profiles carry no
hardware fields) work without any extra annotation stamping.
"""
from __future__ import annotations

import json
import re
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


def api_type() -> str:
    """'HostAgent' (standalone ESXi) or 'VirtualCenter'."""
    out = _run(["about", "-json"])
    try:
        return json.loads(out)["about"]["apiType"]
    except (ValueError, KeyError):
        raise GovcError(f"could not parse `govc about -json` output: {out[:200]}")


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


# vSphere device type -> the controller/adapter names vm.create expects.
_CONTROLLERS = {
    "ParaVirtualSCSIController": "pvscsi",
    "VirtualLsiLogicSASController": "lsilogic-sas",
    "VirtualLsiLogicController": "lsilogic",
    "VirtualBusLogicController": "buslogic",
    "VirtualNVMEController": "nvme",
    "VirtualAHCIController": "sata",
    "VirtualIDEController": "ide",
}
_NICS = {
    "VirtualVmxnet3": "vmxnet3",
    "VirtualE1000e": "e1000e",
    "VirtualE1000": "e1000",
}


def template_shell(template: str) -> dict:
    """Everything needed to rebuild a VM shell around a copy of the template's
    disk, read live from the template VM (not from annotations, so OVA-built
    templates work too): guest id, firmware, CPU/RAM, primary-disk datastore +
    path, disk controller type, NIC adapter type."""
    # object.collect wants an inventory path; templates live in the single
    # ESXi datacenter's vm folder, so a relative 'vm/<name>' resolves.
    tpath = f"vm/{template}"
    if get_prop(tpath, "runtime.powerState") != "poweredOff":
        raise GovcError(
            f"template '{template}' is powered on — power it off first "
            "(its disk can't be copied while it runs)"
        )
    devices = json.loads(_run(["device.info", "-json", "-vm", template]))["devices"]
    by_key = {d["key"]: d for d in devices}
    disk = next((d for d in devices if d.get("type") == "VirtualDisk"), None)
    if not disk:
        raise GovcError(f"template '{template}' has no virtual disk")
    m = re.match(r"\[([^\]]+)\] (.+)", (disk.get("backing") or {}).get("fileName", ""))
    if not m:
        raise GovcError(f"could not parse disk backing path for '{template}'")
    ctrl = by_key.get(disk.get("controllerKey"), {})
    nic = next((d for d in devices if d.get("type") in _NICS), None)
    return {
        "guest_id": get_prop(tpath, "config.guestId"),
        "firmware": get_prop(tpath, "config.firmware"),
        "cpus": get_prop(tpath, "config.hardware.numCPU"),
        "memory_mb": get_prop(tpath, "config.hardware.memoryMB"),
        "src_datastore": m.group(1),
        "src_disk": m.group(2),
        "controller": _CONTROLLERS.get(ctrl.get("type"), "pvscsi"),
        "adapter": _NICS.get(nic.get("type"), "vmxnet3") if nic else "vmxnet3",
    }


def clone(template: str, name: str, *, datastore: str | None = None,
          network: str | None = None, disk_gb: int | None = None,
          cpus: int | None = None, memory_mb: int | None = None) -> None:
    """ESXi 'clone': copy the template's primary disk with the host's
    VirtualDiskManager, then vm.create a shell around the copy with the
    template's own hardware. Slower than a vCenter clone (full copy, no linked
    clones) but functionally equivalent for our single-disk templates.

    Sizing (disk/cpu/memory) is applied HERE, not by reconfiguring afterwards:
    the disk must be grown while still unattached (ExtendVirtualDisk rejects
    attached disks), and create-time CPU/RAM avoids the reconfigure that hosts
    still joined to a vCenter refuse ("resource settings ... restricted").

    Placement defaults to GOVC_DATASTORE / GOVC_NETWORK in the env; an explicit
    datastore/network overrides just this clone."""
    shell = template_shell(template)
    dst_ds = datastore or config.get("GOVC_DATASTORE") or shell["src_datastore"]
    dst_net = network or config.get("GOVC_NETWORK")
    if not dst_net:
        raise GovcError("no network: set GOVC_NETWORK or pass one explicitly")
    dst_disk = f"{name}/{name}.vmdk"

    _run(["datastore.mkdir", "-p", "-ds", dst_ds, name])
    try:
        # Server-side copy of allocated blocks only; timeout generous anyway.
        _run(["datastore.cp", "-ds", shell["src_datastore"], "-ds-target", dst_ds,
              shell["src_disk"], dst_disk], timeout=3600)
        if disk_gb:
            try:
                _run(["datastore.disk.extend", "-ds", dst_ds,
                      "-size", f"{disk_gb}G", dst_disk], timeout=600)
            except GovcError as e:
                raise GovcError(
                    f"could not grow disk to {disk_gb}G (disks can only grow; "
                    f"the template's may already be larger): {e}"
                )
        _run([
            "vm.create", "-on=false", "-force=false",
            "-ds", dst_ds,
            "-g", shell["guest_id"], "-firmware", shell["firmware"],
            "-c", str(cpus) if cpus else shell["cpus"],
            "-m", str(memory_mb) if memory_mb else shell["memory_mb"],
            # -link defaults to TRUE for an existing disk; false = attach the
            # copy directly (a linked clone would chain to it as a parent).
            "-disk", dst_disk, "-disk.controller", shell["controller"], "-link=false",
            "-net", dst_net, "-net.adapter", shell["adapter"],
            name,
        ], timeout=300)
        # Fresh-NVRAM EFI boots try PXE first; a netboot server on the LAN
        # (e.g. netboot.xyz) would park the VM at its menu. Disk first.
        _run(["device.boot", "-vm", name, "-order", "disk,ethernet"])
    except GovcError:
        # Don't leave an orphaned disk copy (or half-built VM) behind.
        try:
            if vm_exists(name):
                _run(["vm.destroy", name])
            else:
                _run(["datastore.rm", "-f", "-ds", dst_ds, name])
        except GovcError:
            pass
        raise


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
