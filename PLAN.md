# ESXi port plan

Fork of VC-Deployer-Python targeting **standalone ESXi hosts** (no vCenter).
Goal: keep every user-facing feature of the vCenter deployer — web UI, CLI,
template discovery, static/DHCP, Windows, disk/CPU/RAM sizing, settings
dialog, auth, job progress — and change only what vCenter's absence forces.

## Why anything changes at all

Both projects talk to vSphere purely through `govc`, and almost every call
goes through the host agent API that ESXi itself serves. Only two operations
are vCenter-exclusive:

| vCenter call                | ESXi replacement                                      |
|-----------------------------|-------------------------------------------------------|
| `govc vm.clone`             | copy the template's disk + `govc vm.create` a shell   |
| `govc vm.markastemplate`    | none — a "template" is just a powered-off VM          |

Everything else (guestinfo injection, `vm.power`, `vm.change`,
`vm.disk.change`, `object.collect`, `find`, datastore ops, the whole
cloud-init/Cloudbase-Init pipeline, IP-wait) is identical on ESXi.

**License caveat:** the free vSphere Hypervisor license makes the ESXi API
read-only — `vm.create` / `vm.power` / `datastore.cp` fail with a license
error. An eval or paid host license is required.

## 1. `vmdeploy/govc.py` — the real work

- **Add `api_type()`**: `govc about -json` → `.about.apiType`
  (`"HostAgent"` = ESXi, `"VirtualCenter"` = vCenter). Called from
  `core.deploy()`'s `checking` step; refuse `VirtualCenter` with a message
  pointing at the original project.
- **Add template-hardware introspection.** ESXi "clone" must rebuild the VM
  shell, so read the source template's actual config instead of trusting
  annotations (works even for OVA-imported templates whose profile carries no
  GUEST_ID/FIRMWARE):
  - `govc object.collect -s <tpl> config.guestId` / `config.firmware`
  - `govc device.info -json -vm <tpl>` → disk backing path (`.vmdk`),
    disk controller type (pvscsi / lsilogic-sas), NIC adapter type
    (vmxnet3 / e1000e)
  - `config.hardware.numCPU` / `config.hardware.memoryMB` for default sizing
    (vCenter clone inherits these implicitly; we must copy them explicitly).
- **Replace `clone()`** with the copy+create sequence (mirrors what
  VC-Deployer-Builder's qcow2/VHD paths already do):
  1. `govc datastore.mkdir -p <name>` (target datastore = override or
     `GOVC_DATASTORE`)
  2. `govc datastore.cp -d thin <tpl-dir>/<tpl>.vmdk <name>/<name>.vmdk`
     — host VirtualDiskManager copy; `-d thin` keeps it thin-provisioned.
     Long timeout (this is a full copy, not a fast clone — minutes, not
     seconds; no linked clones without vCenter).
  3. `govc vm.create -on=false -g <guestId> -firmware <fw> -c <cpu> -m <mem>
     -disk <name>/<name>.vmdk -disk.controller <ctrl> -link=false
     -net <portgroup> -net.adapter <adapter> <name>`
  4. `govc device.boot -vm <name> -order disk,ethernet` — same fresh-NVRAM
     PXE guard the builder needed (netboot.xyz on the LAN).
- **Keep unchanged:** `about`, `find_templates`, `list_networks`,
  `list_datastores`, `get_prop`, `vm_exists`, `set_resources`, `resize_disk`,
  `set_guestinfo`, `power_on`, all three IP-wait functions.
- **Safety guard:** never operate on a VM whose name ends in `-template`
  except as a clone source (on ESXi the template is a live, power-on-able VM;
  on vCenter the object type protected us).

## 2. `vmdeploy/core.py` — nearly untouched

- `deploy()` flow, both cloud-init renderers, Windows renderers, annotation
  parsing: unchanged.
- `checking` step additionally asserts `api_type() == "HostAgent"`.
- `list_templates()` unchanged — `find -type m -name '*-template'` +
  annotation marker works the same when templates are powered-off VMs.
  Optionally also honor a `role=template` annotation key (see §6).
- `progress("cloning")` label becomes "copying" (full disk copy; UI already
  displays arbitrary step names).

## 3. `vmdeploy/config.py` — placement keys

- `GOVC_URL` now points at the ESXi host (`https://<esxi>/sdk`).
- `GOVC_DATACENTER` → drop from the UI (ESXi presents a synthetic
  `ha-datacenter`; govc defaults correctly when unset).
- `GOVC_RESOURCE_POOL` / `GOVC_FOLDER` → drop from the UI (single implicit
  pool `*/Resources`, no VM folders on a bare host). Keep reading them from
  env for anyone who sets them, but stop surfacing them in the ⚙ dialog.
- `GOVC_DATASTORE` / `GOVC_NETWORK` and all `DEFAULT_*` keys unchanged.

## 4. `vmdeploy/cli.py`, `app.py`, `models.py`, `static/index.html`

- Functionality unchanged: same flags, same routes, same job polling, same
  validation, same optional HTTP Basic auth.
- String-level rebrand: "vCenter" → "ESXi host", app title
  "ESXi VM Deployer", CLI prog stays `deploy-vm` (or `deploy-vm-esxi` if both
  clients will be installed side by side — decide at install-script time).
- `models.DeploySpec`: no field changes. `network`/`datastore` overrides now
  feed `vm.create -net` / the `datastore.cp` target instead of `vm.clone`
  flags — same semantics.

## 5. Packaging / deploy

- `pyproject.toml`: name `esxi-vmdeploy`, keep package dir `vmdeploy/` to
  minimize the diff against upstream (eases future `git pull origin main`).
- `Dockerfile`: unchanged (govc + FastAPI, nothing vCenter-specific).
- `docker-compose.yml`: new service name + host port so it can run next to
  the vCenter deployer on docker.onthenile.net.
- `config.env.example` / `README.md` / `PROTOCOL.md` / `client-install.sh`:
  update wording, drop DATACENTER/POOL/FOLDER examples, document the
  license caveat and the copy-vs-clone speed difference.

## 6. VC-Deployer-Builder — dual-purpose (separate change, not in this repo)

- Probe once after connectivity check:
  `API_TYPE=$(govc about -json | jq -r .about.apiType)` (or grep, to avoid a
  jq dependency).
- If `HostAgent`:
  - skip `govc vm.markastemplate` (leave the prepped VM powered off);
  - add `role=template` to the annotation stamp so intent is explicit;
  - everything else (import.ova, import.vmdk, vm.create, seed ISO, prep boot,
    annotation) already works on ESXi as-is.
- Optionally stamp `guest_id=` / `firmware=` / `disk_controller=` /
  `net_adapter=` into the annotation for transparency; the deployer reads the
  live hardware anyway, so this is informational.

## Findings from the live test pass (2026-07-26, ESXi 8.0.3, homelab host)

1. **vCenter-managed hosts restrict some direct reconfigures.** The test host
   is joined to a vCenter; direct-API `vm.disk.change` fails
   with "Access to resource settings on the host is restricted". CPU/RAM
   reconfigure, extraConfig (guestinfo), power ops, and vm.create all still
   work. Fix adopted: **all sizing happens at clone time** — the disk is grown
   with `datastore.disk.extend` while still unattached (ExtendVirtualDisk
   rejects attached disks with a cryptic "parameter incorrect: capacity"), and
   CPU/RAM are passed to `vm.create`. No post-create reconfigures remain, so
   the deployer works on both truly-standalone and vCenter-joined hosts.
2. **Copies come out thick.** `datastore.cp` (VirtualDiskManager) ignores the
   source's thin format — a thin template disk copies as `preallocated`.
   govc 0.55.1 exposes no format option. Accepted for now (correctness
   unaffected); thin copies would need vmkfstools over SSH.
3. **Copy speed is a non-issue** on this host: ~2s for the rocky disk
   (server-side copy of allocated blocks).
4. **Rocky GenericCloud images boot `net.ifnames=0`** → the NIC is `eth0`, on
   vCenter too; the rocky profiles' `DEFAULT_IFACE="ens192"` was a latent bug
   (static deploys would misconfigure; DHCP masked it via NetworkManager's
   default-dhcp fallback). Fixed in the builder profiles (rocky-9/rocky-10) and
   re-stamped on the ESXi template. Any existing vCenter rocky template
   carries the same wrong annotation and needs re-stamping.

## 7. Test pass (against a licensed ESXi host)

1. `govc about` via the fork's settings dialog — connectivity + HostAgent.
2. Builder: `./build-template.sh rocky-9` pointed at the ESXi host —
   verify it completes and leaves a powered-off, annotated `rocky-9-template`.
3. Fork CLI: `deploy-vm --list-templates` shows it.
4. Deploy Linux DHCP → IP reported via guestinfo; SSH with key.
5. Deploy Linux static → netplan static config lands; SSH.
6. Deploy with `--disk 40 --cpus 4 --memory 8` → verify grow + sizing.
7. Windows template build + DHCP deploy (sysprep specialize; 15-min wait
   path) → RDP + SSH.
8. Web UI end-to-end: template dropdown, network/datastore dropdowns
   (standard vSwitch portgroups only — no DVS on ESXi), job progress, ⚙
   settings edit with auth.
