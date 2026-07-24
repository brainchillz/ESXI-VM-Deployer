"""deploy-vm CLI — a thin argparse front-end over the shared control-plane core.

Mirrors the bash deploy-vm.sh UX (same flags) but reuses vmdeploy.core /
vmdeploy.govc, so the renderer, govc calls, IP-wait strategy, and every
hard-won gotcha live in ONE place shared with the web UI.

Deliberately stdlib-only: core.py and govc.py have no third-party deps, and the
CLI builds its own duck-typed spec instead of importing the pydantic model
(that model is the web's JSON-validation layer). So this client needs only
Python 3 + govc — nothing to pip install.

Profile metadata (user / admin group / ssh unit / NIC) is discovered from the
template's vCenter annotation via core.list_templates() — so, unlike the bash
client, there are no profiles/*.env or cloud-init/*.tpl.yaml files to ship.

Connection/placement come from GOVC_* env vars (source config.env first), the
same as the bash toolkit and the web app.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional

from . import core, govc

BLUE, GREEN, YELLOW, RED, RESET = "\033[1;34m", "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"


def log(msg: str) -> None:
    # Diagnostics/progress -> stderr, so stdout stays clean (e.g. --json).
    print(f"{BLUE}==>{RESET} {msg}", file=sys.stderr)


def die(msg: str) -> "None":
    print(f"{RED}ERROR:{RESET} {msg}", file=sys.stderr)
    raise SystemExit(1)


# Duck-typed spec: core.deploy() only does attribute access, so a dataclass is
# all it needs — no pydantic. Field names match what core.deploy reads.
@dataclass
class Spec:
    template: str
    name: str
    hostname: Optional[str]
    iface: str
    username: str
    admin_group: str
    ssh_service: str
    dhcp: bool
    ip: Optional[str]
    cidr: str
    gateway: Optional[str]
    dns: str
    password: Optional[str]
    ssh_key: Optional[str]
    pwauth: bool
    network: Optional[str] = None
    datastore: Optional[str] = None
    disk_gb: Optional[int] = None
    cpus: Optional[int] = None
    memory_gb: Optional[int] = None
    os_family: str = "linux"


def _templates_or_die() -> list[dict]:
    try:
        return core.list_templates()
    except govc.GovcError as e:
        die(f"vCenter/govc error: {e}")


def cmd_list_templates(as_json: bool) -> int:
    log("Checking vCenter connectivity")
    try:
        govc.about()
    except govc.GovcError as e:
        die(f"Cannot reach vCenter (check config.env / GOVC_* env): {e}")
    tpls = _templates_or_die()
    if as_json:
        import json
        print(json.dumps(tpls, indent=2))
        return 0
    if not tpls:
        print(f"{YELLOW}WARN:{RESET} No toolkit-managed templates found "
              "(need VMs named '*-template' with the managed-by annotation).")
        return 0
    print()
    print(f"{'TEMPLATE':<24} {'PROFILE':<12} {'USER':<14} {'GROUP':<8} BUILT")
    for t in tpls:
        print(f"{t.get('name','?'):<24} {t.get('profile','?'):<12} "
              f"{t.get('default_username','?'):<14} {t.get('admin_group','?'):<8} "
              f"{t.get('built','?')}")
    print()
    return 0


def _resolve_template(args, tpls: list[dict]) -> dict:
    """Pick the template dict (with its annotation-derived profile metadata)."""
    if not tpls:
        die("No deployable templates found. Build one first (build-template.sh).")
    if args.template:
        for t in tpls:
            if t.get("name") == args.template or t.get("path", "").rsplit("/", 1)[-1] == args.template:
                return t
        die(f"Template '{args.template}' not found. Available: "
            + ", ".join(t.get("name", "?") for t in tpls))
    profile = args.profile or os.environ.get("DEFAULT_PROFILE")
    if profile:
        for t in tpls:
            if t.get("profile") == profile:
                return t
        die(f"No template for profile '{profile}'. Available profiles: "
            + ", ".join(t.get("profile", "?") for t in tpls))
    if len(tpls) == 1:
        return tpls[0]
    die("Multiple templates exist; pick one with --profile NAME or --template NAME:\n  "
        + "\n  ".join(f"{t.get('profile','?'):<14} {t.get('name','?')}" for t in tpls))


def _resolve_ssh_key(path: Optional[str]) -> Optional[str]:
    """Return SSH public-key text from --ssh-key FILE, a default file, or
    DEFAULT_SSH_PUBKEY env (mirrors the web app's prefill)."""
    candidate = path or os.environ.get("DEFAULT_SSH_KEY") or "~/.ssh/id_ed25519.pub"
    expanded = os.path.expanduser(candidate)
    if os.path.isfile(expanded):
        return open(expanded).read().strip()
    if path:  # user explicitly named a file that doesn't exist
        die(f"SSH key file not found: {path}")
    env_key = os.environ.get("DEFAULT_SSH_PUBKEY", "").strip()
    return env_key or None


def cmd_deploy(args) -> int:
    log("Checking vCenter connectivity")
    try:
        govc.about()
    except govc.GovcError as e:
        die(f"Cannot reach vCenter (check config.env / GOVC_* env): {e}")

    meta = _resolve_template(args, _templates_or_die())

    ssh_key = _resolve_ssh_key(args.ssh_key)
    os_family = meta.get("os_family", "linux")
    # Validation (CLI-side; the web path uses models.DeploySpec.validate_request).
    if os_family == "windows" and not args.dhcp:
        die("Windows deploys are DHCP-only for now (pass --dhcp).")
    if not args.dhcp and (not args.ip or not args.gateway):
        die("Static mode needs --ip and --gateway (or pass --dhcp).")
    if not args.password and not ssh_key:
        die("No SSH key and no --password: the VM would be unreachable. "
            "Pass --ssh-key FILE or --password.")

    spec = Spec(
        template=meta["name"],
        name=args.name,
        hostname=args.hostname or args.name,
        iface=args.iface or meta.get("iface", "ens192"),
        username=args.user or meta.get("default_username", "ubuntu"),
        admin_group=meta.get("admin_group", "sudo"),
        ssh_service=meta.get("ssh_service", "ssh"),
        dhcp=args.dhcp,
        ip=args.ip,
        cidr=args.cidr,
        gateway=args.gateway,
        dns=args.dns,
        password=args.password,
        ssh_key=ssh_key,
        pwauth=args.pwauth,
        network=args.network,
        datastore=args.datastore,
        disk_gb=args.disk,
        cpus=args.cpus,
        memory_gb=args.memory,
        os_family=os_family,
    )

    steps = {
        "checking": "Checking vCenter",
        "cloning": f"Cloning template '{spec.template}' -> '{spec.name}'",
        "injecting": f"Injecting cloud-init (user '{spec.username}', SSH)",
        "powering-on": f"Powering on '{spec.name}'",
        "waiting-for-ip": "Waiting for the guest to report its IP (up to 5 min)...",
        "done": "cloud-init reported settled",
    }
    try:
        ip = core.deploy(spec, progress=lambda s: log(steps.get(s, s)))
    except (govc.GovcError, ValueError, TimeoutError) as e:
        die(str(e))

    login_host = ip or (spec.ip if not spec.dhcp else "<see vCenter>")
    if spec.dhcp:
        addr = f"DHCP  ->  {ip}" if ip else "DHCP"
    else:
        addr = f"{spec.ip}/{spec.cidr}  (gw {spec.gateway}, dns {spec.dns})"
    print()
    print(f"{GREEN}✓ Deployed {spec.name}{RESET}")
    print(f"  Address : {addr}")
    print(f"  Login   : ssh {spec.username}@{login_host}")
    if spec.os_family == "windows":
        print(f"  RDP     : {login_host} (user {spec.username})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy-vm",
        description="Deploy a VM from a vCenter template (Python client on the shared core).",
    )
    p.add_argument("--list-templates", action="store_true",
                   help="List deployable (toolkit-managed) templates, then exit")
    p.add_argument("--json", action="store_true", help="Machine-readable output (with --list-templates)")
    p.add_argument("--profile", help="OS profile to deploy (matched against template annotations)")
    p.add_argument("--template", help="Template name (overrides --profile selection)")
    p.add_argument("--name", help="New VM name (also default hostname)")
    p.add_argument("--hostname", help="Guest hostname (default: --name)")
    p.add_argument("--dhcp", action="store_true", help="Use DHCP instead of a static IP")
    p.add_argument("--ip", help="Static IPv4 (omit if --dhcp)")
    p.add_argument("--gateway", help="Default gateway (omit if --dhcp)")
    p.add_argument("--cidr", default=os.environ.get("DEFAULT_CIDR", "24"), help="Subnet prefix length")
    p.add_argument("--dns", default=os.environ.get("DEFAULT_DNS", "1.1.1.1, 8.8.8.8"),
                   help="DNS servers, comma-separated")
    p.add_argument("--network", help="vCenter network/portgroup for the NIC (default: GOVC_NETWORK)")
    p.add_argument("--datastore", help="Datastore to place the VM on (default: GOVC_DATASTORE)")
    p.add_argument("--disk", type=int, metavar="GB",
                   help="Grow the primary disk to GB (grow-only; default: template size)")
    p.add_argument("--cpus", type=int, metavar="N",
                   help="vCPU count for the VM (default: template sizing)")
    p.add_argument("--memory", type=int, metavar="GB",
                   help="Memory in GB for the VM (default: template sizing)")
    p.add_argument("--iface", help="Guest NIC name (default: from template annotation)")
    p.add_argument("--user", help="Username to create (default: from template annotation)")
    p.add_argument("--ssh-key", help="Public key FILE to authorize (default: ~/.ssh/id_ed25519.pub)")
    p.add_argument("--password", help="Plaintext password; cloud-init hashes it in-guest")
    p.add_argument("--pwauth", action="store_true", help="Enable SSH password auth (default: key-only)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    # govc caches its session under $HOME; a home-less account otherwise dies with
    # "mkdir <home>: permission denied". Match the bash client's guard.
    home = os.path.expanduser("~")
    if not os.access(home, os.W_OK):
        os.environ.setdefault("GOVC_PERSIST_SESSION", "false")

    args = build_parser().parse_args(argv)
    if args.list_templates:
        return cmd_list_templates(args.json)
    if not args.name:
        die("--name is required (or use --list-templates). See --help.")
    return cmd_deploy(args)


if __name__ == "__main__":
    raise SystemExit(main())
