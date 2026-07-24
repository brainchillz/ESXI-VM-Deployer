# Template annotation protocol

Templates built by **VC-Deployer-Builder** are marked with a vCenter VM
*annotation* so the clients (**VC-Deployer-Python**, **VC-Deployer-Shell**) can
discover which templates are deployable and how to configure a VM from them —
without reading any profile files at deploy time.

This annotation is the **only interface** between the builder and the clients.

## Format

Plain `key=value` lines. The first line is a fixed marker:

```
managed-by=vmware-template-toolkit
profile=ubuntu-2604
os_id=ubuntu-2604
default_username=ubuntu
admin_group=sudo
ssh_service=ssh
iface=ens192
built=2025-01-01T00:00:00Z
```

## Keys

| key | meaning | client uses it for |
|---|---|---|
| `managed-by` | fixed marker; its presence means "deployable" | discovery filter |
| `profile` | OS profile id | `--profile` matching |
| `default_username` | login created when `--user` is omitted | user creation |
| `admin_group` | sudo/wheel group | `usermod -aG` |
| `ssh_service` | ssh unit name (`ssh` / `sshd`) | enabling sshd |
| `iface` | guest NIC name | netplan interface |
| `os_family` | `linux` (default when absent) or `windows` | rendering branch: cloud-init vs Cloudbase-Init payloads |
| `os_id`, `built` | informational | display only |

`os_family=windows` templates carry Cloudbase-Init configured for the VMware
guestinfo datasource, so the transport is identical — the client just renders
Cloudbase metadata keys (`admin-username`, `admin-password`, `public-keys-data`)
and PowerShell userdata instead of netplan + `#cloud-config`. Windows deploys
are **DHCP-only** for now. The key is additive: clients that predate it treat
every template as Linux.

## Discovery

Clients enumerate templates by name, then keep those carrying the marker:

```
govc find -type m -name '*-template'      # candidate templates
# for each, read config.annotation; keep those containing `managed-by=...`
# parse the remaining lines as key=value
```

## Compatibility

The marker string and key names are a contract. Changing them requires updating
**VC-Deployer-Builder** and **both** clients, and re-stamping existing templates.
