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

    # Identity
    name: str
    hostname: Optional[str] = None

    # Network
    dhcp: bool = False
    ip: Optional[str] = None
    cidr: str = "24"
    gateway: Optional[str] = None
    dns: str = "1.1.1.1, 8.8.8.8"

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
        if not self.dhcp:
            if not self.ip or not self.gateway:
                raise ValueError("Static mode requires both an IP and a gateway (or choose DHCP)")
        if not self.password and not self.ssh_key:
            raise ValueError("Provide a password or an SSH public key (else the VM is unreachable)")
