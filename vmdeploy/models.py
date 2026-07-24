from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class DeploySpec(BaseModel):
    # Target template + profile metadata (the UI copies these from the selected
    # template's annotation so the core doesn't need the profiles/ files).
    template: str
    admin_group: str
    ssh_service: str
    iface: str
    username: str
    os_family: str = "linux"           # from the template annotation; 'windows' switches rendering

    # Identity
    name: str
    hostname: Optional[str] = None

    # Network
    dhcp: bool = False
    ip: Optional[str] = None
    cidr: str = "24"
    gateway: Optional[str] = None
    dns: str = "1.1.1.1, 8.8.8.8"

    # Placement overrides (each defaults to the container's GOVC_* env when None)
    network: Optional[str] = None      # NIC portgroup   -> vm.clone -net
    datastore: Optional[str] = None    # target datastore -> vm.clone -ds
    disk_gb: Optional[int] = None      # grow primary disk to N GB (None = template size)

    # Compute sizing (None = keep the template's sizing)
    cpus: Optional[int] = None         # vCPU count      -> vm.change -c
    memory_gb: Optional[int] = None    # memory in GB    -> vm.change -m

    # Auth (at least one of password / ssh_key required)
    password: Optional[str] = None
    ssh_key: Optional[str] = None
    pwauth: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v):
            raise ValueError("VM name must be non-empty and contain no spaces")
        return v

    def validate_request(self) -> None:
        """Cross-field checks the route surfaces as 400s."""
        if self.os_family == "windows" and not self.dhcp:
            raise ValueError("Windows deploys are DHCP-only for now (choose DHCP)")
        if not self.dhcp:
            if not self.ip or not self.gateway:
                raise ValueError("Static mode requires both an IP and a gateway (or choose DHCP)")
        if not self.password and not self.ssh_key:
            raise ValueError("Provide a password or an SSH public key (else the VM is unreachable)")
        if self.disk_gb is not None and self.disk_gb <= 0:
            raise ValueError("Disk size must be a positive number of GB (or leave blank for the template default)")
        if self.cpus is not None and not 1 <= self.cpus <= 128:
            raise ValueError("vCPU count must be between 1 and 128 (or leave blank for the template default)")
        if self.memory_gb is not None and not 1 <= self.memory_gb <= 1024:
            raise ValueError("Memory must be between 1 and 1024 GB (or leave blank for the template default)")
