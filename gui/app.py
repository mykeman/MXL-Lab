#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
MXL lab control GUI.

- NDI source discovery (GStreamer device monitor, needs host networking)
- Pipeline settings: source, geometry, frame rate, scaler. Applying them
  stops the four functions, clears the MXL domain and starts them again.
- Live autoframe tuning, written to /config/autoframe.json
- Container status, restart and logs through the Docker socket
- MXL flow status through mxl-info
"""
import asyncio
import json
import os
import struct
import re
import shutil
import subprocess
import threading
import time

import docker
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

import sys

CONFIG_DIR   = os.environ.get("CONFIG_DIR", "/config")
PIPELINE_CFG = os.path.join(CONFIG_DIR, "pipeline.json")
TUNING_CFG   = os.path.join(CONFIG_DIR, "autoframe.json")
STATUS_IN    = os.path.join(CONFIG_DIR, "autoframe-status.json")
PREVIEW_IN   = os.path.join(os.environ.get("PREVIEW_DIR", "/preview"), "preview.bin")
DOMAIN       = os.environ.get("MXL_DOMAIN", "/dev/shm/mxl")
FLOW_A       = os.environ.get("FLOW_A", "")
FLOW_B       = os.environ.get("FLOW_B", "")
STATIC_DIR   = os.path.join(os.path.dirname(__file__), "static")
NDI_DIR      = os.path.join(CONFIG_DIR, "ndi")
NDI_CFG      = os.path.join(NDI_DIR, "ndi-config.v1.json")
MODE_FILE    = os.path.join(NDI_DIR, "mode.json")
DISCOVERY_MODES = ("mdns", "local", "external")
SERVER_RE    = re.compile(r"[A-Za-z0-9.\-]+(:\d{1,5})?")

SERVICES = {
    "ndi-discovery":    "mxl-ndi-discovery",
    "ndi-ingest":       "mxl-ndi-ingest",
    "autoframe":        "mxl-autoframe",
    "ndi-out-original": "mxl-ndi-out-original",
    "ndi-out-vertical": "mxl-ndi-out-vertical",
}

RESOLUTIONS = {"1920x1080", "1280x720"}
OUTPUTS     = {"1080x1920", "720x1280"}
FRAME_RATES = {25, 30, 50, 60}
SCALERS     = {"lanczos", "4-tap", "bilinear"}

TUNING_DEFAULTS = {
    "mode": "weighted", "ease": 0.08, "deadband_px": 24, "max_step_px": 6,
    "det_period_s": 0.20, "recentre_s": 3.0, "score_threshold": 0.6,
    "nms_threshold": 0.3, "top_k": 10, "min_face_px": 0, "bias_px": 0,
    "manual_pos": 0.5, "preview": True,
}


# ------------------------------------------------------------------ files
def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else dict(default)


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def vm_ip():
    """This VM's LAN address. The GUI uses host networking, so this is the host's."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def discovery_state():
    saved = load_json(MODE_FILE)
    mode = saved.get("mode")
    if mode not in DISCOVERY_MODES:
        mode = "local" if os.environ.get("NDI_DISCOVERY") == "server" else "mdns"
    return {"mode": mode, "servers": saved.get("servers", ""), "vm_ip": vm_ip()}


def write_discovery(mode, servers):
    """Write the NDI config every lab container reads, plus the lab's own record."""
    os.makedirs(NDI_DIR, exist_ok=True)
    if mode == "mdns":
        cfg = {"ndi": {}}
    else:
        cfg = {"ndi": {"networks": {"discovery": servers}}}
    write_json_atomic(NDI_CFG, cfg)
    write_json_atomic(MODE_FILE, {"mode": mode, "servers": servers if mode == "external" else ""})


def pipeline_settings():
    cfg = load_json(PIPELINE_CFG)
    env = os.environ
    return {
        "NDI_SRC": cfg.get("NDI_SRC", env.get("NDI_SRC", "")),
        "NDI_URL": cfg.get("NDI_URL", env.get("NDI_URL", "")),
        "SRC_W":   int(cfg.get("SRC_W", env.get("SRC_W", 1920))),
        "SRC_H":   int(cfg.get("SRC_H", env.get("SRC_H", 1080))),
        "SRC_FPS": cfg.get("SRC_FPS", env.get("SRC_FPS", 50)),
        # Auto unless turned off, either saved from the GUI or in .env
        "SRC_AUTO": bool(cfg["SRC_AUTO"]) if "SRC_AUTO" in cfg
                    else (env.get("SRC_AUTO") or "true").strip().lower() in ("1", "true", "yes"),
        "OUT_W":   int(cfg.get("OUT_W", env.get("OUT_W", 1080))),
        "OUT_H":   int(cfg.get("OUT_H", env.get("OUT_H", 1920))),
        "SCALER":  cfg.get("SCALER", env.get("SCALER", "lanczos")),
    }


# ------------------------------------------------------------ NDI finder
# Runs in a short-lived child process for every scan. In the lab, only the
# first GStreamer device monitor in a process ever found NDI sources, while
# a fresh process found them every time, so each scan gets a fresh process.
SCAN_CODE = r"""
import json, sys, time, gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
settle = float(sys.argv[1])
mon = Gst.DeviceMonitor.new()
mon.add_filter("Source/Network", Gst.Caps.from_string("application/x-ndi"))
if not mon.start():
    print(json.dumps({"error": "device monitor did not start, is the NDI plugin loaded?"}))
    sys.exit(0)
time.sleep(settle)
out = []
for dev in mon.get_devices() or []:
    entry = {"name": dev.get_display_name()}
    props = dev.get_properties()
    if props is not None:
        for key in ("url-address", "address", "ndi-name"):
            if props.has_field(key):
                entry[key] = str(props.get_value(key))
    out.append(entry)
mon.stop()
print(json.dumps({"sources": out}))
"""


# Connects to one NDI source, waits for the first video frame and reports its
# size and frame rate. Runs in a child process for the same reason as scans.
PROBE_CODE = r"""
import json, sys, gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
name, url, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])

def fail(msg):
    print(json.dumps({"error": msg})); sys.exit(0)

pipe = Gst.Pipeline.new("probe")
src = Gst.ElementFactory.make("ndisrc")
demux = Gst.ElementFactory.make("ndisrcdemux")
sink = Gst.ElementFactory.make("appsink")
if not (src and demux and sink):
    fail("NDI GStreamer plugin not available")
src.set_property("ndi-name", name)
if url:
    src.set_property("url-address", url)
try:
    src.set_property("timeout", int(timeout * 1000))
except TypeError:
    pass
sink.set_property("sync", False)
sink.set_property("max-buffers", 1)
sink.set_property("drop", True)
for e in (src, demux, sink):
    pipe.add(e)
src.link(demux)

def on_pad(_demux, pad):
    if pad.get_name().startswith("video"):
        pad.link(sink.get_static_pad("sink"))
    else:
        fake = Gst.ElementFactory.make("fakesink")
        fake.set_property("sync", False)
        pipe.add(fake)
        fake.sync_state_with_parent()
        pad.link(fake.get_static_pad("sink"))

demux.connect("pad-added", on_pad)
pipe.set_state(Gst.State.PLAYING)
sample = sink.emit("try-pull-sample", int(timeout * Gst.SECOND))
if sample is None:
    pipe.set_state(Gst.State.NULL)
    fail("no video from the source. Check the name or address, and that it is sending.")
st = sample.get_caps().get_structure(0)
ok_w, width = st.get_int("width")
ok_h, height = st.get_int("height")
ok_f, num, den = st.get_fraction("framerate")
fmt = st.get_string("format") or ""
pipe.set_state(Gst.State.NULL)
if not (ok_w and ok_h):
    fail("could not read the frame size from the source")
print(json.dumps({"width": width, "height": height,
                  "fps_n": num if ok_f else 0, "fps_d": den if ok_f and den else 1,
                  "format": fmt}))
"""


def probe_source(name, url="", timeout=10):
    """Return the source's video format, or raise RuntimeError with a readable reason."""
    res = subprocess.run([sys.executable, "-c", PROBE_CODE, name, url, str(timeout)],
                         capture_output=True, text=True, timeout=timeout + 15)
    lines = [l for l in res.stdout.splitlines() if l.startswith("{")]
    if not lines:
        tail = (res.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("probe produced no result: " + " | ".join(tail))
    data = json.loads(lines[-1])
    if "error" in data:
        raise RuntimeError(data["error"])
    w, h, n, d = data["width"], data["height"], data["fps_n"], data["fps_d"]
    if w % 2 or h % 2:
        raise RuntimeError(f"source is {w}x{h}; width and height must be even")
    if w <= h:
        raise RuntimeError(f"source is {w}x{h}; the 9:16 crop needs a landscape source")
    if w > 4096 or h > 2160:
        raise RuntimeError(f"source is {w}x{h}; larger than 4096x2160 is not supported")
    if n <= 0:
        raise RuntimeError("source did not report a frame rate")
    data["fps"] = str(n) if d == 1 else f"{n}/{d}"
    data["label"] = f"{w}x{h} at {round(n / d, 2):g} fps"
    return data


class NdiDiscovery:
    """Scans for NDI sources in a background thread, one child process per scan."""

    SETTLE_S = 6      # time the NDI finder gets to collect sources per scan
    INTERVAL_S = 4    # pause between scans
    MIN_GAP_S = 2     # never start a scan sooner than this after the last one
    EXPIRE_S = 30     # drop a source only after it has been missing this long

    def __init__(self):
        self.lock = threading.Lock()
        self.sources = []
        self.updated = 0.0
        self.error = None
        self.scanning = False
        self.seen = {}    # name -> (entry, last time seen)
        self.restart = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _scan(self):
        res = subprocess.run([sys.executable, "-c", SCAN_CODE, str(self.SETTLE_S)],
                             capture_output=True, text=True, timeout=self.SETTLE_S + 15)
        lines = [l for l in res.stdout.splitlines() if l.startswith("{")]
        if not lines:
            tail = (res.stderr or "").strip().splitlines()[-3:]
            raise RuntimeError("scan produced no result: " + " | ".join(tail))
        data = json.loads(lines[-1])
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["sources"]

    def _run(self):
        while True:
            self.restart.clear()
            with self.lock:
                self.scanning = True
            try:
                found = self._scan()
                now = time.time()
                with self.lock:
                    for entry in found:
                        self.seen[entry["name"]] = (entry, now)
                    # One scan can come back short, so a source is kept until it
                    # has been missing from every scan for EXPIRE_S.
                    self.seen = {n: v for n, v in self.seen.items() if now - v[1] < self.EXPIRE_S}
                    self.sources = sorted((v[0] for v in self.seen.values()), key=lambda e: e["name"].lower())
                    self.updated, self.error = now, None
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
            finally:
                with self.lock:
                    self.scanning = False
            time.sleep(self.MIN_GAP_S)
            self.restart.wait(max(0, self.INTERVAL_S - self.MIN_GAP_S))

    def reset(self):
        """Forget cached sources, for when the discovery method changes."""
        with self.lock:
            self.seen, self.sources = {}, []
        self.restart.set()

    def snapshot(self):
        with self.lock:
            return {"sources": list(self.sources), "updated": self.updated,
                    "error": self.error, "scanning": self.scanning}


# ------------------------------------------------------------ containers
_docker = None


def dock():
    """Docker client over the mounted socket. Raises if the socket is missing."""
    global _docker
    if _docker is None:
        client = docker.from_env()
        client.ping()
        _docker = client
    return _docker


def container(service):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown function {service}")
    try:
        dock()
    except Exception as exc:
        raise HTTPException(503, f"cannot reach Docker: {exc}")
    try:
        return dock().containers.get(SERVICES[service])
    except docker.errors.NotFound:
        raise HTTPException(404, f"container {SERVICES[service]} not found, run docker compose up -d first")


def container_status():
    out = {}
    for service, name in SERVICES.items():
        try:
            c = dock().containers.get(name)
            state = c.attrs.get("State", {})
            out[service] = {
                "status": c.status,
                "started": state.get("StartedAt"),
                "restarts": c.attrs.get("RestartCount", 0),
                "exit_code": state.get("ExitCode"),
            }
        except docker.errors.NotFound:
            out[service] = {"status": "missing"}
        except Exception as exc:
            out[service] = {"status": "unknown", "error": str(exc)}
    disc = out.get("ndi-discovery", {})
    if discovery_state()["mode"] != "local" and disc.get("status") in ("exited", "created", "missing"):
        out["ndi-discovery"] = {"status": "unused"}
    return out


# ------------------------------------------------------------------ flows
class FlowWatch:
    """Runs mxl-info against each flow and works out the grain rate."""

    HEAD = re.compile(r"head\s*index\D*(\d+)", re.I)

    def __init__(self):
        self.lock = threading.Lock()
        self.prev = {}
        self.cache = {}
        self.cache_ts = 0.0

    def one(self, flow_id):
        exists = os.path.isdir(os.path.join(DOMAIN, f"{flow_id}.mxl-flow"))
        info = {"id": flow_id, "exists": exists, "head": None, "rate": None, "raw": ""}
        if not exists:
            return info
        try:
            res = subprocess.run(["mxl-info", "-d", DOMAIN, "-f", flow_id],
                                 capture_output=True, text=True, timeout=2)
            raw = (res.stdout or res.stderr).strip()
        except Exception as exc:
            raw = f"mxl-info failed: {exc}"
        info["raw"] = raw[:2000]
        m = self.HEAD.search(raw)
        if m:
            head, now = int(m.group(1)), time.time()
            info["head"] = head
            prev = self.prev.get(flow_id)
            if prev and now > prev[1] and head >= prev[0]:
                info["rate"] = round((head - prev[0]) / (now - prev[1]), 1)
            self.prev[flow_id] = (head, now)
        return info

    def status(self):
        with self.lock:
            if time.time() - self.cache_ts > 1.0:
                self.cache = {"A": self.one(FLOW_A), "B": self.one(FLOW_B)}
                self.cache_ts = time.time()
            return self.cache


# ------------------------------------------------------------------ apply
class ApplyJob:
    """Stop all functions, clear the domain, start them again in order."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log = []
        self.result = None
        self.kind = "pipeline"

    def _say(self, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _wait_flow(self, flow_id, timeout=15):
        path = os.path.join(DOMAIN, f"{flow_id}.mxl-flow")
        end = time.time() + timeout
        while time.time() < end:
            if os.path.isdir(path):
                return True
            time.sleep(0.5)
        return False

    def start(self, settings, first=None, kind="pipeline"):
        """settings None means run only `first` and leave the functions alone."""
        with self.lock:
            if self.running:
                raise HTTPException(409, "a change is already being applied")
            self.running, self.log, self.result, self.kind = True, [], None, kind
        threading.Thread(target=self._run, args=(settings, first), daemon=True).start()

    def _run(self, settings, first=None):
        try:
            if first is not None:
                first(self._say)
            if settings is None:
                self.result = "ok"
                return
            write_json_atomic(PIPELINE_CFG, settings)
            self._say("Saved pipeline settings")
            for svc in ("ndi-out-vertical", "ndi-out-original", "autoframe", "ndi-ingest"):
                container(svc).stop(timeout=5)
                self._say(f"Stopped {svc}")

            removed = 0
            for entry in os.listdir(DOMAIN):
                path = os.path.join(DOMAIN, entry)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
                removed += 1
            self._say(f"Cleared MXL domain ({removed} entries)")

            container("ndi-ingest").start()
            self._say("Started ndi-ingest, waiting for Flow A")
            if not self._wait_flow(FLOW_A):
                raise RuntimeError("Flow A did not appear. Check the ndi-ingest log and the source name.")
            self._say("Flow A is up")

            container("ndi-out-original").start()
            self._say("Started ndi-out-original")
            container("autoframe").start()
            self._say("Started autoframe, waiting for Flow B")
            if not self._wait_flow(FLOW_B):
                raise RuntimeError("Flow B did not appear. Check the autoframe log.")
            self._say("Flow B is up")

            container("ndi-out-vertical").start()
            self._say("Started ndi-out-vertical")
            self.result = "ok"
        except HTTPException as exc:
            self._say(f"Failed: {exc.detail}")
            self.result = "failed"
        except Exception as exc:
            self._say(f"Failed: {exc}")
            self.result = "failed"
        finally:
            with self.lock:
                self.running = False

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "result": self.result,
                    "log": list(self.log), "kind": self.kind}


# -------------------------------------------------------------------- api
app = FastAPI(title="MXL lab control")
ndi = NdiDiscovery()
flows = FlowWatch()
job = ApplyJob()


class PipelineIn(BaseModel):
    NDI_SRC: str = Field(min_length=1, max_length=256)
    NDI_URL: str = ""
    resolution: str
    output: str
    SRC_FPS: int
    SCALER: str


class TuningIn(BaseModel):
    mode: str = "weighted"
    ease: float = Field(0.08, ge=0.005, le=0.5)
    deadband_px: float = Field(24, ge=0, le=300)
    max_step_px: float = Field(6, ge=0.5, le=80)
    det_period_s: float = Field(0.2, ge=0.04, le=2.0)
    recentre_s: float = Field(3.0, ge=0.2, le=60)
    score_threshold: float = Field(0.6, ge=0.1, le=0.99)
    nms_threshold: float = Field(0.3, ge=0.05, le=0.9)
    top_k: int = Field(10, ge=1, le=50)
    min_face_px: float = Field(0, ge=0, le=1000)
    bias_px: float = Field(0, ge=-600, le=600)
    manual_pos: float = Field(0.5, ge=0, le=1)
    preview: bool = True


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/ndi/sources")
def ndi_sources():
    return ndi.snapshot()


@app.post("/api/ndi/refresh")
def ndi_refresh():
    ndi.restart.set()
    return {"ok": True}


@app.get("/api/pipeline")
def get_pipeline():
    return {"settings": pipeline_settings(), "job": job.snapshot(),
            "choices": {"resolutions": ["auto"] + sorted(RESOLUTIONS, reverse=True),
                        "outputs": sorted(OUTPUTS, reverse=True),
                        "frame_rates": sorted(FRAME_RATES),
                        "scalers": ["lanczos", "4-tap", "bilinear"]}}


@app.post("/api/pipeline/apply")
def apply_pipeline(body: PipelineIn):
    auto = body.resolution == "auto"
    if not auto and body.resolution not in RESOLUTIONS:
        raise HTTPException(400, "unsupported source resolution")
    if body.output not in OUTPUTS:
        raise HTTPException(400, "unsupported output size")
    if not auto and body.SRC_FPS not in FRAME_RATES:
        raise HTTPException(400, "unsupported frame rate")
    if body.SCALER not in SCALERS:
        raise HTTPException(400, "unsupported scaler")
    url = body.NDI_URL.strip()
    if url and not re.fullmatch(r"[A-Za-z0-9.\-]+:\d{1,5}", url):
        raise HTTPException(400, "address must look like 192.168.1.169:5961")
    ow, oh = (int(v) for v in body.output.split("x"))
    name = body.NDI_SRC.strip()
    if auto:
        prev = pipeline_settings()
        sw, sh, fps = prev["SRC_W"], prev["SRC_H"], prev["SRC_FPS"]
    else:
        sw, sh = (int(v) for v in body.resolution.split("x"))
        fps = body.SRC_FPS
    settings = {"NDI_SRC": name, "NDI_URL": url, "SRC_W": sw, "SRC_H": sh,
                "SRC_FPS": fps, "SRC_AUTO": auto, "OUT_W": ow, "OUT_H": oh,
                "SCALER": body.SCALER}

    def detect(say):
        say(f"Reading the format of {name}")
        fmt = probe_source(name, url)
        settings.update({"SRC_W": fmt["width"], "SRC_H": fmt["height"], "SRC_FPS": fmt["fps"]})
        say(f"Source is {fmt['label']}")

    job.start(settings, detect if auto else None)
    return {"ok": True}


class ProbeIn(BaseModel):
    NDI_SRC: str = Field(min_length=1, max_length=256)
    NDI_URL: str = ""


@app.post("/api/ndi/probe")
def ndi_probe(body: ProbeIn):
    url = body.NDI_URL.strip()
    if url and not re.fullmatch(r"[A-Za-z0-9.\-]+:\d{1,5}", url):
        raise HTTPException(400, "address must look like 192.168.1.169:5961")
    try:
        return probe_source(body.NDI_SRC.strip(), url)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc))
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "the source did not answer in time")


@app.get("/api/pipeline/job")
def get_job():
    return job.snapshot()


class DiscoveryIn(BaseModel):
    mode: str
    servers: str = ""


@app.get("/api/discovery")
def get_discovery():
    return discovery_state()


@app.post("/api/discovery")
def set_discovery(body: DiscoveryIn):
    mode = body.mode
    if mode not in DISCOVERY_MODES:
        raise HTTPException(400, "mode must be mdns, local or external")
    servers = ""
    if mode == "local":
        servers = vm_ip()
        if not servers:
            raise HTTPException(500, "could not work out this VM's IP address")
    elif mode == "external":
        parts = [p.strip() for p in body.servers.split(",") if p.strip()]
        if not parts:
            raise HTTPException(400, "enter the discovery server address")
        bad = [p for p in parts if not SERVER_RE.fullmatch(p)]
        if bad:
            raise HTTPException(400, f"not a valid address: {', '.join(bad)}")
        servers = ",".join(parts)

    def first(say):
        write_discovery(mode, servers)
        label = {"mdns": "mDNS", "local": f"internal discovery server ({servers})",
                 "external": f"external discovery server {servers}"}[mode]
        say(f"NDI discovery set to {label}")
        ndi.reset()
        try:
            disc = container("ndi-discovery")
        except HTTPException:
            if mode == "local":
                raise HTTPException(404, "the discovery server container does not exist. "
                                         "Run setup.sh again to create it.")
            return
        if mode == "local":
            if disc.status != "running":
                disc.start()
                time.sleep(1)
            say("Internal discovery server is running")
        elif disc.status == "running":
            disc.stop(timeout=5)
            say("Stopped the internal discovery server")
        say("Scanning with the new setting, the source list refreshes within about 15 s")

    settings = pipeline_settings()
    job.start(settings if settings["NDI_SRC"] else None, first, kind="discovery")
    return {"ok": True}


@app.get("/api/tuning")
def get_tuning():
    values = dict(TUNING_DEFAULTS)
    values.update(load_json(TUNING_CFG))
    return {"values": values, "defaults": TUNING_DEFAULTS}


@app.put("/api/tuning")
def put_tuning(body: TuningIn):
    if body.mode not in ("weighted", "largest", "manual", "hold"):
        raise HTTPException(400, "mode must be weighted, largest, manual or hold")
    data = body.model_dump()
    data["top_k"] = int(data["top_k"])
    write_json_atomic(TUNING_CFG, data)
    return {"ok": True, "values": data}


@app.post("/api/tuning/reset")
def reset_tuning():
    write_json_atomic(TUNING_CFG, TUNING_DEFAULTS)
    return {"ok": True, "values": TUNING_DEFAULTS}


@app.get("/api/status")
def status():
    af = load_json(STATUS_IN)
    if af.get("ts"):
        af["age"] = round(time.time() - af["ts"], 1)
    try:
        dock()
        containers, docker_error = container_status(), None
    except Exception as exc:
        containers = {svc: {"status": "unknown"} for svc in SERVICES}
        docker_error = f"{exc}. Is /var/run/docker.sock mounted into the GUI container?"
    return {"containers": containers, "docker_error": docker_error,
            "autoframe": af, "flows": flows.status(), "job": job.snapshot()}


_preview_cache = {"mtime": None, "meta": None, "jpg": None}


def read_preview():
    """Latest preview frame as (meta, jpeg bytes), or None. Re-read only when the file changes."""
    try:
        mtime = os.stat(PREVIEW_IN).st_mtime_ns
    except OSError:
        return None
    if mtime != _preview_cache["mtime"]:
        try:
            with open(PREVIEW_IN, "rb") as f:
                data = f.read()
            n = struct.unpack(">I", data[:4])[0]
            _preview_cache.update(mtime=mtime, meta=json.loads(data[4:4 + n]), jpg=data[4 + n:])
        except (OSError, ValueError, struct.error):
            return None
    return _preview_cache["meta"], _preview_cache["jpg"]


@app.get("/api/preview")
async def preview(after: int = -1):
    """
    Next preview frame. Waits up to a second for a frame other than `after`,
    so the page receives each frame as soon as it exists. The crop position
    for that exact frame is in the X-Preview header.
    """
    deadline = time.time() + 1.0
    while True:
        latest = read_preview()
        if latest is not None:
            meta, jpg = latest
            if meta.get("seq") != after and time.time() - meta.get("ts", 0) < 5:
                return Response(jpg, media_type="image/jpeg",
                                headers={"Cache-Control": "no-store",
                                         "X-Preview": json.dumps(meta)})
        if time.time() > deadline:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        await asyncio.sleep(0.02)


@app.post("/api/containers/{service}/restart")
def restart(service: str):
    container(service).restart(timeout=5)
    return {"ok": True}


@app.get("/api/containers/{service}/logs")
def logs(service: str, tail: int = 150):
    tail = max(10, min(tail, 2000))
    text = container(service).logs(tail=tail, timestamps=False).decode("utf-8", "replace")
    return JSONResponse({"service": service, "text": text})
