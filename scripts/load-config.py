#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
Print shell export lines for the pipeline settings.

Values in /config/pipeline.json (written by the web GUI) win over
the environment from .env. Used by run-ingest.sh:

    eval "$(python3 /opt/scripts/load-config.py)"
"""
import json
import os
import shlex

KEYS = ["NDI_SRC", "NDI_URL", "SRC_W", "SRC_H", "SRC_FPS", "OUT_W", "OUT_H", "SCALER"]
path = os.environ.get("PIPELINE_CONFIG", "/config/pipeline.json")

cfg = {}
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    pass

for key in KEYS:
    value = cfg.get(key, os.environ.get(key))
    if value is not None and str(value) != "":
        print(f"export {key}={shlex.quote(str(value))}")
