"""Control-plane core: list deployable templates + deploy a VM from one.

cloud-init is rendered here in Python (mirrors the bash toolkit's templates,
including the cross-distro fixes: `to: 0.0.0.0/0` and `usermod -aG`).
"""
from __future__ import annotations

import base64
import gzip
import secrets

from . import govc

ANNOTATION_MARKER = "managed-by=vmware-template-toolkit"


def _parse_annotation(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def list_templates() -> list[dict]:
    """Toolkit-managed templates (those stamped with our annotation), each with
    the profile metadata the UI needs (default user, admin group, ssh unit, NIC)."""
    templates = []
    for path in govc.find_templates():
        try:
            ann = govc.get_prop(path, "config.annotation")
        except govc.GovcError:
            continue
        if ANNOTATION_MARKER not in ann:
            continue
        meta = _parse_annotation(ann)
        meta["name"] = path.rsplit("/", 1)[-1]
        meta["path"] = path
        templates.append(meta)
    templates.sort(key=lambda t: t["name"])
    return templates


def _gzb64(text: str) -> str:
    return base64.b64encode(gzip.compress(text.encode())).decode()


def render_metadata(*, hostname, iface, dhcp, ip, cidr, gateway, dns, instance_id) -> str:
    if dhcp:
        eth = "      dhcp4: true\n      dhcp6: false"
    else:
        dns_list = ", ".join(s.strip() for s in dns.split(","))
        eth = (
            "      dhcp4: false\n      dhcp6: false\n"
            f"      addresses:\n        - {ip}/{cidr}\n"
            f"      nameservers:\n        addresses: [{dns_list}]\n"
            # 0.0.0.0/0 (not the netplan keyword "default") so EL/NetworkManager
            # rendering doesn't choke — see toolkit deploy-vm.sh.
            f"      routes:\n        - to: 0.0.0.0/0\n          via: {gateway}"
        )
    return (
        f"instance-id: {instance_id}\n"
        f"local-hostname: {hostname}\n"
        "network:\n  version: 2\n  ethernets:\n"
        f"    {iface}:\n{eth}\n"
    )


def render_userdata(*, hostname, username, admin_group, ssh_service, iface, pwauth, password, ssh_key) -> str:
    lines = [
        "#cloud-config",
        f"hostname: {hostname}",
        f"fqdn: {hostname}",
        "manage_etc_hosts: true",
        f"ssh_pwauth: {'true' if pwauth else 'false'}",
        "users:",
        f"  - name: {username}",
        f"    gecos: {username}",
        f"    groups: [{admin_group}]",
        "    shell: /bin/bash",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        f"    lock_passwd: {'false' if password else 'true'}",
    ]
    if password:
        lines.append(f'    plain_text_passwd: "{password}"')
    if ssh_key:
        lines.append("    ssh_authorized_keys:")
        lines.append(f'      - "{ssh_key.strip()}"')
    # usermod ensures admin-group membership even for the pre-existing default
    # user (cloud-init's `groups:` is skipped for existing users).
    lines += [
        "runcmd:",
        f"  - usermod -aG {admin_group} {username}",
        f"  - systemctl enable --now {ssh_service}",
    ]
    # Publish the guest's settled primary IPv4 to guestinfo (runs last), so the
    # deployer reads the final address, not a transient boot-time DHCP lease.
    inner = (
        "command -v vmware-rpctool >/dev/null && "
        'vmware-rpctool "info-set guestinfo.deploy.ipv4 '
        f"$(ip -4 -o addr show {iface} scope global | awk '{{print $4}}' | head -1 | cut -d/ -f1)\" "
        "|| true"
    )
    lines.append(f"  - '{inner.replace(chr(39), chr(39) * 2)}'")  # YAML-escape single quotes
    return "\n".join(lines) + "\n"


def deploy(spec, progress=lambda step: None) -> str:
    """Clone a template + inject cloud-init + power on + wait for IP.
    Returns the VM's IP. `progress` is called with step names for the UI."""
    name = spec.name
    progress("checking")
    govc.about()
    if govc.vm_exists(name):
        raise ValueError(f"A VM named '{name}' already exists")

    instance_id = f"iid-{name}-{secrets.token_hex(4)}"
    hostname = spec.hostname or name
    md = render_metadata(
        hostname=hostname, iface=spec.iface, dhcp=spec.dhcp, ip=spec.ip,
        cidr=spec.cidr, gateway=spec.gateway, dns=spec.dns, instance_id=instance_id,
    )
    ud = render_userdata(
        hostname=hostname, username=spec.username, admin_group=spec.admin_group,
        ssh_service=spec.ssh_service, iface=spec.iface, pwauth=spec.pwauth,
        password=spec.password, ssh_key=spec.ssh_key,
    )

    progress("cloning")
    govc.clone(spec.template, name)
    progress("injecting")
    govc.set_guestinfo(name, _gzb64(md), _gzb64(ud))
    progress("powering-on")
    govc.power_on(name)

    progress("waiting-for-ip")
    # DHCP: read the settled IP the guest publishes to guestinfo (avoids the
    # transient boot-time lease). Static: poll for the known configured IP.
    ip = govc.wait_reported_ip(name) if spec.dhcp else govc.wait_static_ip(name, spec.ip)
    if not ip:
        raise TimeoutError(
            "VM powered on but no IP was reported within 5 min "
            "(cloud-init may still be running — check the console)."
        )
    progress("done")
    return ip
