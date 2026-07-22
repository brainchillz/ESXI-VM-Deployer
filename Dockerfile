FROM python:3.12-slim

# govc (the vСenter CLI the core wraps) — single static binary.
ARG GOVC_VERSION=0.55.1
ARG TARGETARCH=amd64
RUN set -eux; \
    apt-get update; apt-get install -y --no-install-recommends curl ca-certificates; \
    case "$TARGETARCH" in amd64) A=x86_64;; arm64) A=arm64;; *) A=x86_64;; esac; \
    curl -fsSL "https://github.com/vmware/govmomi/releases/download/v${GOVC_VERSION}/govc_Linux_${A}.tar.gz" \
      | tar -xz -C /usr/local/bin govc; \
    chmod +x /usr/local/bin/govc; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vmdeploy ./vmdeploy

EXPOSE 8000
CMD ["uvicorn", "vmdeploy.app:app", "--host", "0.0.0.0", "--port", "8000"]
