#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
#
# Start the four media functions in order through the GUI's apply job:
# stop all, clear the MXL domain, start ingest, wait for Flow A,
# start autoframe and the outputs, wait for Flow B.
# Uses the settings in config/pipeline.json, falling back to .env.
set -euo pipefail
cd "$(dirname "$0")/.."

port=$(grep -E '^GUI_PORT=' .env 2>/dev/null | cut -d= -f2 || true)
port=${port:-8080}
api="http://127.0.0.1:${port}"

body=$(python3 - <<'PY'
import json
env = {}
for line in open(".env"):
    line = line.rstrip("\n")
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v
try:
    cfg = json.load(open("config/pipeline.json"))
except Exception:
    cfg = {}
def g(key, default):
    value = cfg.get(key)
    if value in (None, ""):
        value = env.get(key) or default
    return value
if "SRC_AUTO" in cfg:
    auto = bool(cfg["SRC_AUTO"])
else:
    auto = (env.get("SRC_AUTO") or "true").strip().lower() in ("1", "true", "yes")
print(json.dumps({
    "NDI_SRC": g("NDI_SRC", ""),
    "NDI_URL": g("NDI_URL", ""),
    "resolution": "auto" if auto else f'{g("SRC_W", 1920)}x{g("SRC_H", 1080)}',
    "output": f'{g("OUT_W", 1080)}x{g("OUT_H", 1920)}',
    "SRC_FPS": 50 if "/" in str(g("SRC_FPS", 50)) else int(g("SRC_FPS", 50)),
    "SCALER": g("SCALER", "lanczos"),
}))
PY
)

if python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1])["NDI_SRC"] else 1)' "$body"; then :; else
  echo "    No NDI source set. Pick one in the GUI and apply it there."
  exit 0
fi

for _ in $(seq 1 30); do
  curl -fs "$api/api/status" >/dev/null && break
  sleep 2
done
curl -fs "$api/api/status" >/dev/null || { echo "    GUI is not answering on port $port"; exit 1; }

curl -fs -X POST -H 'Content-Type: application/json' -d "$body" "$api/api/pipeline/apply" >/dev/null \
  || { echo "    The GUI refused the apply request"; exit 1; }

while true; do
  sleep 2
  job=$(curl -fs "$api/api/pipeline/job") || continue
  running=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["running"])' "$job")
  [ "$running" = "True" ] || break
done

python3 -c 'import json,sys; j=json.loads(sys.argv[1]); print("\n".join("    " + l for l in j["log"]))' "$job"
python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1])["result"] == "ok" else 1)' "$job"
