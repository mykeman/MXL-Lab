#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
#
# Set how the lab finds NDI sources and announces its outputs.
# The GUI's NDI discovery panel does the same thing.
#
#   scripts/ndi-mode.sh mdns                    normal NDI discovery
#   scripts/ndi-mode.sh local                   NDI Discovery Server on this VM ("server" also works)
#   scripts/ndi-mode.sh external 192.168.1.20   a discovery server you already run (commas for several)
#
# Add --config-only to write the settings without starting, stopping or restarting anything.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() { sed -n '8,12p' "$0"; exit 1; }
mode=${1:-}; shift || true
[ "$mode" = server ] && mode=local
servers=""
case "$mode" in
  mdns|local) ;;
  external) servers=${1:-}; shift || true; [ -n "$servers" ] || usage ;;
  *) usage ;;
esac
only=${1:-}

vm_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}' || true)
[ -n "$vm_ip" ] || vm_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
[ "$mode" = local ] && servers="$vm_ip"

sudo mkdir -p config/ndi
python3 - "$mode" "$servers" <<'PY' | sudo tee config/ndi/ndi-config.v1.json >/dev/null
import json, sys
mode, servers = sys.argv[1], sys.argv[2]
cfg = {"ndi": {}} if mode == "mdns" else {"ndi": {"networks": {"discovery": servers}}}
print(json.dumps(cfg, indent=2))
PY
python3 - "$mode" "$servers" <<'PY' | sudo tee config/ndi/mode.json >/dev/null
import json, sys
mode, servers = sys.argv[1], sys.argv[2]
print(json.dumps({"mode": mode, "servers": servers if mode == "external" else ""}, indent=2))
PY
echo "    NDI discovery set to $mode${servers:+ ($servers)}"

[ "$only" = "--config-only" ] && exit 0

if [ "$mode" = local ]; then
  sudo docker compose up -d --no-deps ndi-discovery
else
  sudo docker stop mxl-ndi-discovery >/dev/null 2>&1 || true
fi
scripts/lab-apply.sh

echo
case "$mode" in
  mdns)     echo "    On each PC, remove any lab entries from NDI Access Manager, then restart vMix and Studio Monitor." ;;
  local)    echo "    On each PC, add $vm_ip in NDI Access Manager under Advanced > Discovery Servers, then restart vMix and Studio Monitor." ;;
  external) echo "    Your PCs should already use $servers. The lab now registers there." ;;
esac
