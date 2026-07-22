# VC-Deployer — Python client

Deploy Linux VMs from **vCenter templates** in seconds: clone a template, inject
cloud-init through vSphere `guestinfo`, power on, and report the VM's IP. Ships a
command-line client and an optional web UI that share one deploy engine.

Templates are built separately by **VC-Deployer-Builder**; this repo only lists
templates and deploys VMs from them. See [`PROTOCOL.md`](PROTOCOL.md) for the
template-discovery contract.

## How it works

Cloud-init is baked into the cloud images. On VMware it reads its config from
`guestinfo.*` variables. A deploy is: **clone template → set `guestinfo` → power
on → wait for IP**. Profile details (login user, admin group, ssh unit, NIC) are
read from each template's vCenter annotation, so no per-OS files are needed at
deploy time.

## Requirements

- `python3` (>= 3.9) — the CLI is **stdlib-only**, no pip dependencies
- [`govc`](https://github.com/vmware/govmomi) — the vCenter CLI (the installer can fetch it)
- Network reachability + credentials for your vCenter

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

Copy `config.env.example` to `config.env` and fill in your vCenter connection +
placement, or point the installer at a config file you keep elsewhere with
`--config`. Keys are standard `GOVC_*` variables plus a few deploy defaults.

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
`--pwauth`.

## Use (web UI, optional)

The web UI is a small FastAPI app that calls the same engine.

```bash
cp .env.example .env         # same GOVC_* values as config.env
docker compose up -d --build
# http://localhost:8000
```

Endpoints: `GET /api/templates`, `POST /api/deploy` (returns a job id),
`GET /api/jobs/{id}` (poll progress + IP).

## Layout

```
vmdeploy/
  govc.py     thin wrapper around the govc CLI
  core.py     render cloud-init + clone + inject + wait  (the shared engine)
  cli.py      the deploy-vm command
  app.py      FastAPI web UI      (optional)
  jobs.py     in-memory job store (web)
  models.py   request model       (web)
  static/     web UI page
pyproject.toml     package + `deploy-vm` entry point
client-install.sh  system installer (CLI + wrapper)
Dockerfile / docker-compose.yml   web UI container
PROTOCOL.md        template annotation contract
```
