#!/usr/bin/env bash
#
# client-install.sh — Install the VC-Deployer Python client (deploy-vm).
#
# Installs only the runtime the CLI needs:
#   - vmdeploy/{__init__,govc,core,cli}.py   the stdlib-only package
#   - config.env                             GOVC_* creds + placement + defaults (secret)
#   + python3 (>=3.9) and the govc binary
#   + a `deploy-vm` wrapper on PATH that sources config.env and runs vmdeploy.cli
#
# The web UI (models/app/jobs/static, FastAPI) is NOT installed by this script —
# run it separately with Docker (see README).
#
# Usage:
#   sudo ./client-install.sh [options]
#
# Options:
#   --prefix DIR        Install the client here            (default: /opt/vmdeploy)
#   --bin DIR           Install the `deploy-vm` wrapper here(default: /usr/local/bin)
#   --owner USER        Own the installed files as USER    (default: $SUDO_USER or root)
#   --group GROUP       Gate config.env to GROUP at 0640; created if missing.
#   --config FILE       Install FILE as config.env (e.g. from your private infra repo)
#   --govc auto|yes|no  Install govc: auto=only if missing, yes=always, no=never
#   --govc-version V    govc release to install             (default: 0.55.1)
#   -h, --help          Show this help
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_SRC="$SRC_DIR/vmdeploy"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

PREFIX="/opt/vmdeploy"
BIN_DIR="/usr/local/bin"
OWNER="${SUDO_USER:-root}"
GROUP=""
CONFIG_SRC=""
INSTALL_GOVC="auto"
GOVC_VERSION="0.55.1"

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)       PREFIX="$2"; shift 2 ;;
    --bin)          BIN_DIR="$2"; shift 2 ;;
    --owner)        OWNER="$2"; shift 2 ;;
    --group)        GROUP="$2"; shift 2 ;;
    --config)       CONFIG_SRC="$2"; shift 2 ;;
    --govc)         INSTALL_GOVC="$2"; shift 2 ;;
    --govc-version) GOVC_VERSION="$2"; shift 2 ;;
    -h|--help)      sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)              die "Unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo) — installs to $PREFIX and $BIN_DIR."

PKG_FILES=( "__init__.py" "govc.py" "core.py" "cli.py" )
for f in "${PKG_FILES[@]}"; do
  [ -f "$PKG_SRC/$f" ] || die "Missing required package file: $PKG_SRC/$f"
done
command -v python3 >/dev/null 2>&1 || die "python3 not found — the client needs Python 3."
log "Using python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

CONFIG_SRC="${CONFIG_SRC:-$SRC_DIR/config.env}"
CONFIG_FALLBACK=""
if [ ! -f "$CONFIG_SRC" ]; then
  if [ -f "$SRC_DIR/config.env.example" ]; then
    CONFIG_SRC="$SRC_DIR/config.env.example"; CONFIG_FALLBACK="yes"
  else
    die "No config.env (or config.env.example). Pass one with --config FILE."
  fi
fi
id "$OWNER" >/dev/null 2>&1 || die "Owner user '$OWNER' does not exist."

install_govc() {
  local ver="$1" arch tmp
  case "$(uname -m)" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) die "Unsupported arch for govc auto-install: $(uname -m). Install govc manually." ;;
  esac
  local url="https://github.com/vmware/govmomi/releases/download/v${ver}/govc_Linux_${arch}.tar.gz"
  log "Installing govc ${ver} ($arch) -> ${BIN_DIR}/govc"
  tmp="$(mktemp -d)"
  curl -fsSL "$url" | tar xz -C "$tmp" govc || die "govc download/extract failed ($url)"
  install -m 0755 "$tmp/govc" "${BIN_DIR}/govc"
  rm -rf "$tmp"
}
case "$INSTALL_GOVC" in
  no)  command -v govc >/dev/null 2>&1 || warn "govc not found and --govc no; deploy-vm will fail until it's installed." ;;
  yes) install_govc "$GOVC_VERSION" ;;
  auto)
    if command -v govc >/dev/null 2>&1; then
      log "govc already present: $(command -v govc) ($(govc version 2>/dev/null | awk '{print $2}'))"
    else install_govc "$GOVC_VERSION"; fi ;;
  *) die "--govc must be one of: auto|yes|no" ;;
esac

if [ -n "$GROUP" ]; then
  getent group "$GROUP" >/dev/null 2>&1 || { log "Creating group '$GROUP'"; groupadd "$GROUP"; }
fi

log "Installing client -> $PREFIX"
install -d -o "$OWNER" -g "$OWNER" -m 0755 "$PREFIX" "$PREFIX/vmdeploy"
for f in "${PKG_FILES[@]}"; do
  install -o "$OWNER" -g "$OWNER" -m 0644 "$PKG_SRC/$f" "$PREFIX/vmdeploy/$f"
done

CFG_GRP="${GROUP:-$OWNER}"
install -o "$OWNER" -g "$CFG_GRP" -m 0640 "$CONFIG_SRC" "$PREFIX/config.env"

log "Installing wrapper -> $BIN_DIR/deploy-vm"
install -d -m 0755 "$BIN_DIR"
cat > "$BIN_DIR/deploy-vm" <<EOF
#!/usr/bin/env bash
# Wrapper: source config.env (GOVC_* + defaults) and run the Python deploy client.
set -a; source "$PREFIX/config.env"; set +a
export PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m vmdeploy.cli "\$@"
EOF
chmod 0755 "$BIN_DIR/deploy-vm"

echo
log "Client installed."
printf '  files:   %s (%s)\n' "$PREFIX" "$(du -sh "$PREFIX" | awk '{print $1}')"
printf '  run:     %s/deploy-vm  (in PATH)\n' "$BIN_DIR"
printf '  owner:   %s\n' "$OWNER"
if [ -n "$GROUP" ]; then
  printf '  access:  members of group "%s" can read config.env / run deploys\n' "$GROUP"
else
  printf '  access:  owner-only config.env (0640)\n'
fi
[ -n "$CONFIG_FALLBACK" ] && warn "config.env seeded from config.env.example — EDIT $PREFIX/config.env (or reinstall with --config)."
echo
echo "Installed files:"
find "$PREFIX" -type f | sort | sed 's/^/  /'
