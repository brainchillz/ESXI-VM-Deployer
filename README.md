# ESXI-VM-Deployer — Python client

Deploy Linux and Windows VMs from templates on a **standalone ESXi host** — no
vCenter required: copy a template's disk, rebuild the VM around it, inject
cloud-init through vSphere `guestinfo`, power on, and report the VM's IP. Ships
a command-line client and an optional web UI that share one deploy engine.

This is a fork of **VC-Deployer-Python** (the vCenter deployer) with the
vCenter-only operations replaced; everything user-facing is unchanged. Templates
are built separately by **VC-Deployer-Builder** (which auto-detects ESXi vs
vCenter); this repo only lists templates and deploys VMs from them. See
[`PROTOCOL.md`](PROTOCOL.md) for the template-discovery contract.

> **License requirement:** the host needs a real (eval or paid) ESXi license.
> The free vSphere Hypervisor license makes the API **read-only** and every
> deploy fails with a license error.

## How it works

Cloud-init is baked into the cloud images. On VMware it reads its config from
`guestinfo.*` variables. Standalone ESXi has no `CloneVM` API and no template
object type, so a "template" is a powered-off, annotated VM and a deploy is:
**copy the template's disk (VirtualDiskManager) → `vm.create` a shell around
the copy with the template's own hardware (read live: guest id, firmware,
disk controller, NIC type, CPU/RAM) → set `guestinfo` → power on → wait for
IP**. Profile details (login user, admin group, ssh unit, NIC) are read from
each template's annotation, so no per-OS files are needed at deploy time.

Because the disk is fully copied (no linked clones without vCenter), the
"clone" step takes seconds-to-minutes proportional to the template's disk
usage. Note the host's disk manager writes the copy **preallocated (thick)**
regardless of the template's thin format — plan datastore space for the full
virtual disk size per VM. Disk growing (`--disk`) happens on the unattached
copy and CPU/RAM are set at create time, so deploys also work on hosts that
are joined to a vCenter (whose direct API refuses post-create resource
reconfigures).

## Requirements

- `python3` (>= 3.9) — the CLI is **stdlib-only**, no pip dependencies
- [`govc`](https://github.com/vmware/govmomi) — the vSphere CLI (the installer can fetch it)
- Network reachability + root (or equivalent) credentials for the ESXi host
- A licensed (not free-tier) ESXi host — see above

## Install (CLI)

```bash
sudo ./client-install.sh --config /path/to/your/config.env
```

This installs the `vmdeploy` package to `/opt/vmdeploy`, installs `govc` if
missing, and puts a `deploy-vm` command on your `PATH`. Options: `--prefix`,
`--bin`, `--owner`, `--group` (gate `config.env` to a group), `--config`,
`--govc auto|yes|no`. Run `./client-install.sh --help`.

Or install as a normal Python package:

```bash
pip install .            # provides the `deploy-vm` entry point
```

## Configure

Copy `config.env.example` to `config.env` and fill in your ESXi connection +
placement, or point the installer at a config file you keep elsewhere with
`--config`. Keys are standard `GOVC_*` variables plus a few deploy defaults —
just datastore + network for placement (a standalone host has one synthetic
datacenter, one resource pool, and no VM folders).

## Use (CLI)

```bash
# list deployable templates
deploy-vm --list-templates

# static IP
deploy-vm --profile ubuntu-2604 --name web01 --ip 10.0.0.50 --gateway 10.0.0.1

# DHCP + a login password
deploy-vm --profile ubuntu-2604 --name lab01 --dhcp --password 'S0mePass!' --pwauth

# machine-readable template list
deploy-vm --list-templates --json
```

Key flags: `--profile` / `--template`, `--name`, `--ip` / `--gateway` / `--dhcp`,
`--cidr`, `--dns`, `--hostname`, `--iface`, `--user`, `--ssh-key`, `--password`,
`--pwauth`, `--network`, `--datastore`, `--disk` (GB, grow-only), `--cpus`,
`--memory` (GB).

### Windows templates

Templates whose annotation carries `os_family=windows` (built by
VC-Deployer-Builder's `windows-2025` profile) deploy through the same engine:
the client renders **Cloudbase-Init** metadata (`admin-username`,
`admin-password`, `public-keys-data`) and PowerShell userdata over the same
`guestinfo.*` transport, and the new VM comes up with your user in
Administrators, SSH (OpenSSH Server) and RDP enabled, and its settled IP
published to `guestinfo.deploy.ipv4`. Windows deploys are **DHCP-only** for
now, and the first boot takes noticeably longer than Linux (sysprep
specialize + reboot before Cloudbase-Init runs).

## Use (web UI, optional)

The web UI is a small FastAPI app that calls the same engine. Pick a template,
target network and datastore, optionally grow the disk and set vCPUs / memory,
choose static IP or DHCP, and deploy — live host inventory populates the
dropdowns.

```bash
cp .env.example .env         # same GOVC_* values as config.env
docker compose up -d --build
# http://localhost:8001
```

### Settings (⚙) and optional auth

A ⚙ Settings dialog edits configuration from the browser — no shell needed.
Changes are written to `/data/settings.json` (a Docker volume, so they persist
across container recreation) and layer on top of the environment, taking effect
on the next call.

By default the app runs **open**, and Settings can edit only the non-secret
placement / deploy defaults — the ESXi **connection + credentials stay
locked**. Set `VMDEPLOY_PASSWORD` (and optional `VMDEPLOY_USERNAME`, default
`admin`) in `.env` to require HTTP Basic auth on the whole UI and unlock
connection/credential editing. The password is **write-only** — the API never
returns it.

Endpoints: `GET /api/templates`, `GET /api/networks`, `GET /api/datastores`,
`GET`/`PUT /api/settings`, `POST /api/deploy` (returns a job id),
`GET /api/jobs/{id}` (poll progress + IP).

## Layout

```
vmdeploy/
  govc.py     thin wrapper around the govc CLI (ESXi copy+create "clone")
  core.py     render cloud-init + copy + inject + wait  (the shared engine)
  config.py   effective config: env overlaid by the runtime settings file
  cli.py      the deploy-vm command
  app.py      FastAPI web UI + auth + settings  (optional)
  jobs.py     in-memory job store (web)
  models.py   request model       (web)
  static/     web UI page
pyproject.toml     package + `deploy-vm` entry point
client-install.sh  system installer (CLI + wrapper)
Dockerfile / docker-compose.yml   web UI container
PROTOCOL.md        template annotation contract
```
