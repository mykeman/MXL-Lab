#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
#
# MXL lab setup for a fresh Ubuntu VM.
#
# Run from the unzipped mxl-lab directory as your normal user (not root):
#
#   ./setup.sh                       interactive, asks about the NDI EULA and source
#   ./setup.sh --source "PC (vMix - Output 1)"
#   ./setup.sh --accept-ndi-eula     skip the EULA prompt (you have read it)
#   ./setup.sh --skip-build          everything except the image build and start
#
# Safe to run again. Each step checks whether it is already done.
# A full log is written to setup.log next to this script.

set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$LAB_DIR/setup.log"
NDI_SDK_URL="https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz"
NDI_WORK="$HOME/src/ndi-sdk"

SOURCE=""
ACCEPT_EULA=no
SKIP_BUILD=no

while [ $# -gt 0 ]; do
  case "$1" in
    --source)          SOURCE="${2:-}"; shift 2 ;;
    --accept-ndi-eula) ACCEPT_EULA=yes; shift ;;
    --skip-build)      SKIP_BUILD=yes; shift ;;
    -h|--help)         sed -n '5,16p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

exec > >(tee -a "$LOG") 2>&1

step() { printf '\n=== %s\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*"; }
die()  { printf '\n    FAILED: %s\n    Full log: %s\n' "$*" "$LOG"; exit 1; }

echo "MXL lab setup started $(date)"
cd "$LAB_DIR"

# ------------------------------------------------------------------ checks
step "Checking the VM"

[ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not root. It uses sudo where needed."
[ -f /etc/os-release ] && . /etc/os-release
[ "${ID:-}" = "ubuntu" ] || die "This script expects Ubuntu. Found: ${PRETTY_NAME:-unknown}"
ok "$PRETTY_NAME"

[ "$(uname -m)" = "x86_64" ] || die "The NDI runtime used here is x86_64 only."

for f in Dockerfile docker-compose.yml autoframe.py env.example gui/Dockerfile gui/app.py scripts/run-ingest.sh scripts/ndi-mode.sh scripts/lab-apply.sh; do
  [ -f "$f" ] || die "Missing $f. Run setup.sh from inside the unzipped mxl-lab folder."
done
ok "lab files present"

if grep -q -m1 avx2 /proc/cpuinfo; then
  ok "AVX2 available"
else
  warn "No AVX2. In Proxmox set this VM's CPU type to 'host', then reboot and run again."
  warn "The build may fail or be very slow without it."
fi

mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
[ "$mem_gb" -ge 12 ] && ok "${mem_gb} GB RAM" || warn "${mem_gb} GB RAM. 16 GB is recommended."

disk_gb=$(df -BG --output=avail "$LAB_DIR" | tail -1 | tr -dc '0-9')
[ "$disk_gb" -ge 30 ] && ok "${disk_gb} GB free disk" || warn "${disk_gb} GB free. The image build needs about 30 GB."

sudo -v || die "sudo is required."
# keep sudo alive for the long build
( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &

# ----------------------------------------------------------- base packages
step "Installing host packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ca-certificates curl wget tar unzip python3 avahi-daemon avahi-utils >/dev/null
ok "packages installed"

# ------------------------------------------------------------------- avahi
step "Enabling mDNS (avahi) for NDI discovery"
sudo systemctl enable --now avahi-daemon >/dev/null 2>&1
systemctl is-active --quiet avahi-daemon && ok "avahi-daemon running" || die "avahi-daemon did not start"

# ---------------------------------------------------------------- firewall
step "Firewall"
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  gui_port=$(grep -E '^GUI_PORT=' .env 2>/dev/null | cut -d= -f2 || true)
  gui_port=${gui_port:-8080}
  for rule in 5353/udp 5959:5969/tcp 5959:5969/udp 6960:6970/tcp 6960:6970/udp \
              7960:7970/tcp 7960:7970/udp "${gui_port}/tcp"; do
    sudo ufw allow "$rule" >/dev/null
  done
  ok "ufw rules added for NDI and the GUI on ${gui_port}"
else
  ok "ufw not active, nothing to open"
fi

# -------------------------------------------------------------- MXL domain
step "MXL domain at /dev/shm/mxl"
echo "d /dev/shm/mxl 0777 root root -" | sudo tee /etc/tmpfiles.d/mxl.conf >/dev/null
sudo systemd-tmpfiles --create /etc/tmpfiles.d/mxl.conf
[ -d /dev/shm/mxl ] && ok "created, and recreated at every boot" || die "could not create /dev/shm/mxl"
shm_gb=$(df -BG --output=size /dev/shm | tail -1 | tr -dc '0-9')
ok "/dev/shm is ${shm_gb} GB"

# ------------------------------------------------------------------ docker
step "Docker"
if ! command -v docker >/dev/null; then
  echo "    installing Docker CE from get.docker.com"
  if ! curl -fsSL https://get.docker.com | sudo sh >/dev/null; then
    warn "get.docker.com failed on this release, falling back to Ubuntu's docker.io"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null
  fi
fi
sudo systemctl enable --now docker >/dev/null 2>&1
sudo docker compose version >/dev/null 2>&1 || die "Docker is installed but 'docker compose' is missing."
ok "$(sudo docker --version)"
ok "$(sudo docker compose version)"

if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  ok "added $USER to the docker group (applies from your next login)"
fi
# this script uses sudo for docker so it works before you log in again
DOCKER="sudo docker"

# --------------------------------------------------------------------- NDI
step "NDI runtime"
mkdir -p ndi
if [ -s ndi/libndi.so.6 ] && [ ! -L ndi/libndi.so.6 ]; then
  ok "ndi/libndi.so.6 already present"
else
  mkdir -p "$NDI_WORK"
  cd "$NDI_WORK"
  if [ ! -f Install_NDI_SDK_v6_Linux.sh ]; then
    echo "    downloading the NDI SDK"
    wget -q -O ndi.tar.gz "$NDI_SDK_URL" || die "Could not download $NDI_SDK_URL"
    tar -xzf ndi.tar.gz
  fi
  if ! find "$NDI_WORK" -name 'libndi.so.6*' -path '*x86_64-linux-gnu*' | grep -q .; then
    echo
    echo "    The NDI SDK installer shows its licence and asks you to accept it."
    # the licence check below is what decides success, not the installer's exit code
    if [ "$ACCEPT_EULA" = yes ]; then
      yes y | PAGER="cat" ./Install_NDI_SDK_v6_Linux.sh >/dev/null || true
    else
      PAGER="cat" ./Install_NDI_SDK_v6_Linux.sh || true
    fi
  fi
  lib=$(find "$NDI_WORK" -name 'libndi.so.6*' -path '*x86_64-linux-gnu*' | sort | head -1)
  [ -n "$lib" ] || die "libndi.so.6 not found after the NDI installer ran. Was the licence accepted?"
  cd "$LAB_DIR"
  # -L copies the real file. A symlink in the build context breaks the Docker COPY.
  cp -L "$lib" ndi/libndi.so.6
  ok "copied $(basename "$lib") to ndi/libndi.so.6"
fi

# ------------------------------------------------------ discovery server
step "NDI Discovery Server"
if [ -x ndi/ndi-discovery-server ]; then
  ok "ndi/ndi-discovery-server already present"
else
  [ -d "$NDI_WORK" ] || die "NDI SDK folder $NDI_WORK is missing. Delete ndi/libndi.so.6 and run setup again."
  ds=$(find "$NDI_WORK" -type f -iname '*discovery*server*' -path '*x86_64*' 2>/dev/null | sort | head -1)
  [ -n "$ds" ] || ds=$(find "$NDI_WORK" -type f -iname '*discovery*' -perm -u+x 2>/dev/null | sort | head -1)
  [ -n "$ds" ] || die "Could not find the NDI Discovery Server in the SDK under $NDI_WORK"
  cp -L "$ds" ndi/ndi-discovery-server
  chmod +x ndi/ndi-discovery-server
  ok "copied $(basename "$ds") to ndi/ndi-discovery-server"
fi


# --------------------------------------------------------------------- env
step "Lab settings (.env)"
if [ ! -f .env ]; then
  cp env.example .env
  ok "created .env from env.example"
else
  ok "keeping existing .env"
fi

set_env() {   # set_env KEY VALUE, handles spaces and brackets safely
  python3 - "$1" "$2" <<'PY'
import sys
key, value = sys.argv[1], sys.argv[2]
lines = open(".env").read().splitlines()
out, done = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={value}"); done = True
    else:
        out.append(line)
if not done:
    out.append(f"{key}={value}")
open(".env", "w").write("\n".join(out) + "\n")
PY
}

set_env UBUNTU_VERSION "$VERSION_ID"
ok "container base set to ubuntu:${VERSION_ID}, matching this VM"

chmod +x scripts/*.sh scripts/load-config.py
# Keep the discovery method chosen in the GUI if there is one, otherwise take it from .env.
ndi_mode=$(python3 -c 'import json; print(json.load(open("config/ndi/mode.json")).get("mode", ""))' 2>/dev/null || true)
ndi_servers=$(python3 -c 'import json; print(json.load(open("config/ndi/mode.json")).get("servers", ""))' 2>/dev/null || true)
if [ -z "$ndi_mode" ]; then
  env_mode=$(grep -E '^NDI_DISCOVERY=' .env | cut -d= -f2 || true)
  [ "$env_mode" = server ] && ndi_mode=local || ndi_mode=mdns
fi
if [ "$ndi_mode" = external ] && [ -n "$ndi_servers" ]; then
  scripts/ndi-mode.sh external "$ndi_servers" --config-only >/dev/null
else
  [ "$ndi_mode" = external ] && ndi_mode=mdns
  scripts/ndi-mode.sh "$ndi_mode" --config-only >/dev/null
fi
ok "NDI discovery: $ndi_mode${ndi_servers:+ ($ndi_servers)}. Change it in the GUI."

current_src=$(grep -E '^NDI_SRC=' .env | cut -d= -f2- || true)
if [ -z "$current_src" ] && [ -f config/pipeline.json ]; then
  current_src=$(python3 -c 'import json; print(json.load(open("config/pipeline.json")).get("NDI_SRC", ""))' 2>/dev/null || true)
fi
if [ -z "$SOURCE" ] && [ -z "$current_src" ] && [ -t 0 ]; then
  echo "    Looking for NDI sources for 5 seconds"
  mapfile -t found < <(timeout 6 avahi-browse -tpr _ndi._tcp 2>/dev/null | python3 -c '
import re, sys
names = set()
for line in sys.stdin:
    f = line.rstrip("\n").split(";")
    if len(f) > 3 and f[0] == "=":
        names.add(re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), f[3]))
print("\n".join(sorted(names)))
' | sed '/^$/d')
  if [ "${#found[@]}" -gt 0 ]; then
    i=1
    for n in "${found[@]}"; do echo "      $i) $n"; i=$((i + 1)); done
    read -r -p "    Pick a source number, or press Enter to choose later in the GUI: " pick
    if [[ "$pick" =~ ^[0-9]+$ ]] && [ "$pick" -ge 1 ] && [ "$pick" -le "${#found[@]}" ]; then
      SOURCE="${found[$((pick - 1))]}"
    fi
  else
    echo "    None seen from the host. You can pick one in the GUI later."
  fi
fi
if [ -n "$SOURCE" ]; then
  set_env NDI_SRC "$SOURCE"
  current_src="$SOURCE"
fi
[ -n "$current_src" ] && ok "NDI source: $current_src" || ok "NDI source not set, choose it in the GUI"

mkdir -p config

if [ "$SKIP_BUILD" = yes ]; then
  step "Skipping build and start (--skip-build)"
  echo "Done. Run ./setup.sh again without --skip-build to build and start."
  exit 0
fi

# ------------------------------------------------------------------- build
step "Building the lab image (30 to 60 minutes the first time)"
if ! $DOCKER pull -q "ubuntu:${VERSION_ID}" >/dev/null 2>&1; then
  warn "ubuntu:${VERSION_ID} is not on Docker Hub, building on ubuntu:24.04 instead"
  set_env UBUNTU_VERSION 24.04
fi
echo "    Progress is in $LOG"
$DOCKER compose build ndi-ingest || die "Lab image build failed. The last lines of the log show which step."
ok "mxl-lab:latest built"

step "Building the GUI image"
$DOCKER compose build gui || die "GUI image build failed."
ok "mxl-lab-gui:latest built"

# ------------------------------------------------------------------- start
step "Starting"
$DOCKER compose create >/dev/null
# The discovery server container always exists so the GUI can start and stop it.
$DOCKER compose up -d --no-deps --force-recreate gui ndi-discovery
[ "$ndi_mode" = local ] || $DOCKER stop mxl-ndi-discovery >/dev/null
if [ -n "$current_src" ]; then
  # Start the four functions in order with a clean MXL domain. Starting them
  # all at once can leave an output attached to a flow that is being replaced.
  $DOCKER compose create --force-recreate ndi-ingest autoframe ndi-out-original ndi-out-vertical >/dev/null
  scripts/lab-apply.sh && ok "all functions started in order" \
    || warn "Ordered start failed. Check the ingest source name or address in the GUI."
else
  ok "GUI started. The four functions are created but stopped until you pick a source."
fi

# ------------------------------------------------------------------ verify
step "Checking"
gui_port=$(grep -E '^GUI_PORT=' .env | cut -d= -f2)
gui_port=${gui_port:-8080}
for _ in $(seq 1 30); do
  curl -fs "http://127.0.0.1:${gui_port}/api/status" >/dev/null && break
  sleep 2
done
curl -fs "http://127.0.0.1:${gui_port}/api/status" >/dev/null \
  && ok "GUI answering on port ${gui_port}" \
  || warn "GUI not answering yet. Check: docker compose logs gui"

$DOCKER compose run --rm --no-deps --entrypoint sh autoframe -c \
  'gst-inspect-1.0 mxlsink >/dev/null && gst-inspect-1.0 ndisrc >/dev/null' \
  && ok "MXL and NDI GStreamer plugins load in the image" \
  || warn "A GStreamer plugin failed to load in the image"

if [ -n "$current_src" ]; then
  flow_a=$(grep -E '^FLOW_A=' .env | cut -d= -f2)
  for _ in $(seq 1 20); do
    [ -d "/dev/shm/mxl/${flow_a}.mxl-flow" ] && break
    sleep 1
  done
  if [ -d "/dev/shm/mxl/${flow_a}.mxl-flow" ]; then
    ok "Flow A is being written"
  else
    warn "Flow A has not appeared. Check the source name and: docker compose logs ndi-ingest"
  fi
fi

$DOCKER compose ps

ip=$(hostname -I | awk '{print $1}')
cat <<EOF

Setup complete.

  GUI:            http://${ip}:${gui_port}
  NDI discovery:  ${ndi_mode}. Change it in the GUI under NDI discovery.
                  With mdns nothing needs setting on other PCs. With the server
                  on this VM, add ${ip} in NDI Access Manager on each PC.
  NDI outputs:    "MXL Lab Original" and "MXL Lab Vertical" under this VM's hostname
  Logs:           docker compose logs -f <service>
  Log out and back in to use docker without sudo.
EOF
