#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
MXL autoframing media function.

Reads an MXL video flow, produces a 9:16 crop that tracks faces,
and writes the result to a second MXL video flow.

All pixel work stays in GStreamer. Python only sets the horizontal
offset of the videocrop element.

Live control (written by the web GUI, all optional):
  /config/pipeline.json      geometry, frame rate, scaler (read at start)
  /config/autoframe.json     tuning, re-read whenever it changes
Status out:
  /config/autoframe-status.json   position, faces, detection rate
  /preview/preview.bin            live preview: 480x270 JPEG plus crop position
"""
import json
import os
import struct
import sys
import threading
import time

import cv2
import numpy as np

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)

CONFIG_DIR   = os.environ.get("CONFIG_DIR", "/config")
PIPELINE_CFG = os.path.join(CONFIG_DIR, "pipeline.json")
TUNING_CFG   = os.path.join(CONFIG_DIR, "autoframe.json")
STATUS_OUT   = os.path.join(CONFIG_DIR, "autoframe-status.json")
PREVIEW_DIR  = os.environ.get("PREVIEW_DIR", "/preview")
PREVIEW_OUT  = os.path.join(PREVIEW_DIR, "preview.bin")
PREVIEW_FPS  = int(os.environ.get("PREVIEW_FPS", 12))
PV_W, PV_H   = 480, 270


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------- config
_pcfg = load_json(PIPELINE_CFG)


def setting(key, default):
    return _pcfg.get(key, os.environ.get(key, default))


DOMAIN   = os.environ.get("MXL_DOMAIN", "/dev/shm/mxl")
FLOW_IN  = os.environ["FLOW_A"]
FLOW_OUT = os.environ["FLOW_B"]

SRC_W   = int(setting("SRC_W", 1920))
SRC_H   = int(setting("SRC_H", 1080))
OUT_W   = int(setting("OUT_W", 1080))
OUT_H   = int(setting("OUT_H", 1920))
# A whole number (50) or a fraction from auto-detect (60000/1001)
_fps = str(setting("SRC_FPS", 50)).strip()
FPS_CAPS = _fps if "/" in _fps else f"{int(float(_fps))}/1"
FPS_VALUE = int(FPS_CAPS.split("/")[0]) / int(FPS_CAPS.split("/")[1])
SCALER  = str(setting("SCALER", "lanczos"))

DET_W, DET_H = 320, 180
MODEL = os.environ.get(
    "YUNET_MODEL", "/opt/models/face_detection_yunet_2023mar.onnx")

# Crop window: fixed width, only the horizontal offset moves.
# left + right is always CROP_SLACK, so output caps never change.
CROP_W     = int(SRC_H * 9 / 16 / 2 + 0.5) * 2   # nearest even: 608 at 1080, 406 at 720
CROP_SLACK = SRC_W - CROP_W

TICK_S = 0.02   # motion thread period, 50 Hz

# Defaults for everything the GUI can change live.
DEFAULT_TUNING = {
    "mode":            "weighted",  # weighted | largest | manual | hold
    "ease":            0.08,        # fraction of remaining distance per tick
    "deadband_px":     24,          # ignore target moves smaller than this
    "max_step_px":     6,           # speed limit, source pixels per tick
    "det_period_s":    0.20,        # detection period
    "recentre_s":      3.0,         # drift to centre after this long with no face
    "score_threshold": 0.6,         # YuNet confidence
    "nms_threshold":   0.3,
    "top_k":           10,
    "min_face_px":     0,           # ignore faces narrower than this (source px)
    "bias_px":         0,           # shift the frame left (-) or right (+)
    "manual_pos":      0.5,         # 0 = far left, 1 = far right (manual mode)
    "preview":         True,
}


class Tuning:
    """Tuning values, reloaded when the GUI rewrites the file."""

    def __init__(self):
        self.lock = threading.Lock()
        self.values = dict(DEFAULT_TUNING)
        self.mtime = None
        self.reload()

    def reload(self):
        try:
            mtime = os.stat(TUNING_CFG).st_mtime
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        data = load_json(TUNING_CFG)
        with self.lock:
            merged = dict(DEFAULT_TUNING)
            merged.update({k: v for k, v in data.items() if k in DEFAULT_TUNING})
            self.values = merged
            self.mtime = mtime
        print(f"autoframe: tuning loaded {merged}", flush=True)
        return True

    def get(self):
        with self.lock:
            return dict(self.values)


# --------------------------------------------------------------- framing
class Framer:
    """Holds the target and eases the crop toward it."""

    def __init__(self, crop_element, tuning):
        self.crop = crop_element
        self.tuning = tuning
        self.target = SRC_W / 2.0
        self.current = SRC_W / 2.0
        self.left = CROP_SLACK // 2
        self.lock = threading.Lock()

    def set_target(self, centre_x):
        with self.lock:
            self.target = centre_x

    def get_target(self):
        with self.lock:
            return self.target

    def tick(self):
        t = self.tuning.get()
        if t["mode"] == "hold":
            return
        if t["mode"] == "manual":
            pos = min(1.0, max(0.0, float(t["manual_pos"])))
            self.set_target(CROP_W / 2.0 + pos * CROP_SLACK)
            deadband = 2
        else:
            deadband = float(t["deadband_px"])

        delta = self.get_target() - self.current
        if abs(delta) < deadband:
            return
        step = delta * float(t["ease"])
        limit = float(t["max_step_px"])
        step = max(-limit, min(limit, step))
        self.current += step
        self.apply()

    def apply(self):
        left = int(round(self.current - CROP_W / 2.0))
        left = max(0, min(CROP_SLACK, left)) & ~1   # keep even for I420 chroma
        if left == self.left:
            return
        self.left = left
        self.crop.set_property("left", left)
        self.crop.set_property("right", CROP_SLACK - left)


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.faces = 0
        self.boxes = []
        self.last_seen = 0.0
        self.det_count = 0
        self.det_fps = 0.0
        self._window_start = time.time()

    def set_boxes(self, boxes):
        with self.lock:
            self.boxes = boxes

    def get_boxes(self):
        with self.lock:
            return list(self.boxes)

    def detection(self, faces, seen):
        with self.lock:
            self.faces = faces
            if seen:
                self.last_seen = time.time()
            self.det_count += 1
            now = time.time()
            if now - self._window_start >= 1.0:
                self.det_fps = self.det_count / (now - self._window_start)
                self.det_count = 0
                self._window_start = now

    def snapshot(self):
        with self.lock:
            return self.faces, self.last_seen, self.det_fps


# ------------------------------------------------------------- pipeline
PIPELINE = f"""
mxlsrc name=src video-flow-id={FLOW_IN} domain={DOMAIN}
  ! videoconvert ! video/x-raw,format=I420
  ! tee name=t
t. ! queue max-size-buffers=4
   ! videocrop name=crop left={CROP_SLACK // 2 & ~1} right={CROP_SLACK - (CROP_SLACK // 2 & ~1)} top=0 bottom=0
   ! videoscale method={SCALER}
   ! video/x-raw,width={OUT_W},height={OUT_H}
   ! videoconvert ! video/x-raw,format=v210
   ! videorate ! video/x-raw,framerate={FPS_CAPS}
   ! queue max-size-buffers=4
   ! mxlsink flow-id={FLOW_OUT} domain={DOMAIN}
t. ! queue max-size-buffers=1 leaky=downstream
   ! videoscale method=nearest-neighbour
   ! video/x-raw,width={DET_W},height={DET_H}
   ! videoconvert ! video/x-raw,format=BGR
   ! appsink name=det sync=false max-buffers=1 drop=true
t. ! queue max-size-buffers=1 leaky=downstream
   ! valve name=pvalve drop=false
   ! videorate drop-only=true max-rate={PREVIEW_FPS}
   ! videoscale method=bilinear
   ! video/x-raw,width={PV_W},height={PV_H}
   ! videoconvert ! video/x-raw,format=BGR
   ! appsink name=pv sync=false max-buffers=1 drop=true
"""


# ------------------------------------------------------------- detection
def preview_loop(appsink, valve, framer, stats, tuning, stop):
    """
    Live preview at up to PREVIEW_FPS. Each frame is written with the crop
    position at that moment, so the GUI can draw the window in step with it.
    File layout: 4-byte big-endian header length, JSON header, JPEG.
    """
    try:
        os.makedirs(PREVIEW_DIR, exist_ok=True)
    except OSError:
        pass
    scale = PV_W / DET_W
    seq = 0
    dropping = False
    while not stop.is_set():
        on = bool(tuning.get()["preview"])
        if dropping == on:
            valve.set_property("drop", not on)
            dropping = not on
        if not on:
            stop.wait(0.5)
            continue
        sample = appsink.emit("try-pull-sample", int(0.5 * Gst.SECOND))
        if sample is None:
            continue
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            continue
        try:
            img = np.frombuffer(info.data, dtype=np.uint8).reshape(PV_H, PV_W, 3).copy()
        finally:
            buf.unmap(info)
        for x, y, w, h in stats.get_boxes():
            cv2.rectangle(img, (int(x * scale), int(y * scale)),
                          (int((x + w) * scale), int((y + h) * scale)), (80, 200, 120), 1)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            continue
        seq += 1
        meta = json.dumps({
            "seq": seq, "ts": time.time(), "src_w": SRC_W, "crop_w": CROP_W,
            "crop_left": framer.left, "target": framer.get_target(),
        }).encode()
        tmp = PREVIEW_OUT + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(struct.pack(">I", len(meta)) + meta + jpg.tobytes())
            os.replace(tmp, PREVIEW_OUT)
        except OSError:
            pass


def detector_loop(appsink, framer, tuning, stats, stop):
    t = tuning.get()
    detector = cv2.FaceDetectorYN_create(
        MODEL, "", (DET_W, DET_H),
        score_threshold=float(t["score_threshold"]),
        nms_threshold=float(t["nms_threshold"]),
        top_k=int(t["top_k"]))
    applied = (t["score_threshold"], t["nms_threshold"], t["top_k"])
    scale_x = SRC_W / DET_W

    while not stop.is_set():
        t = tuning.get()
        wanted = (t["score_threshold"], t["nms_threshold"], t["top_k"])
        if wanted != applied:
            detector.setScoreThreshold(float(t["score_threshold"]))
            detector.setNMSThreshold(float(t["nms_threshold"]))
            detector.setTopK(int(t["top_k"]))
            applied = wanted

        period = max(0.02, float(t["det_period_s"]))
        loop_start = time.time()
        auto = t["mode"] in ("weighted", "largest")

        sample = appsink.emit("try-pull-sample", int(period * Gst.SECOND))
        if sample is None:
            _, last_seen, _ = stats.snapshot()
            if auto and time.time() - last_seen > float(t["recentre_s"]):
                framer.set_target(SRC_W / 2.0)
            continue

        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            continue
        try:
            frame = np.frombuffer(
                info.data, dtype=np.uint8).reshape(DET_H, DET_W, 3).copy()
        finally:
            buf.unmap(info)

        _, faces = detector.detect(frame)

        kept = []
        if faces is not None:
            min_w = float(t["min_face_px"]) / scale_x
            kept = [f for f in faces if float(f[2]) >= min_w]

        chosen = None
        if kept:
            if t["mode"] == "largest":
                f = max(kept, key=lambda f: float(f[2]) * float(f[3]))
                chosen = (float(f[0]) + float(f[2]) / 2.0) * scale_x
            else:
                # Weight by box area so the nearest subject wins.
                num = den = 0.0
                for f in kept:
                    weight = float(f[2]) * float(f[3])
                    num += (float(f[0]) + float(f[2]) / 2.0) * weight
                    den += weight
                if den > 0:
                    chosen = (num / den) * scale_x

        stats.set_boxes([tuple(float(v) for v in f[0:4]) for f in kept])
        stats.detection(len(kept), chosen is not None)

        if auto:
            if chosen is not None:
                framer.set_target(chosen + float(t["bias_px"]))
            else:
                _, last_seen, _ = stats.snapshot()
                if time.time() - last_seen > float(t["recentre_s"]):
                    framer.set_target(SRC_W / 2.0)

        # Pace detection to the configured period.
        remaining = period - (time.time() - loop_start)
        if remaining > 0:
            stop.wait(remaining)


def motion_loop(framer, tuning, stats, stop):
    n = 0
    while not stop.is_set():
        framer.tick()
        n += 1
        if n % 25 == 0:   # every 0.5 s
            tuning.reload()
            t = tuning.get()
            faces, last_seen, det_fps = stats.snapshot()
            write_json_atomic(STATUS_OUT, {
                "ts": time.time(),
                "mode": t["mode"],
                "src_w": SRC_W, "src_h": SRC_H,
                "out_w": OUT_W, "out_h": OUT_H,
                "fps": round(FPS_VALUE, 2), "scaler": SCALER,
                "crop_w": CROP_W, "crop_left": framer.left,
                "current": framer.current, "target": framer.get_target(),
                "faces": faces,
                "last_seen_age": (time.time() - last_seen) if last_seen else None,
                "det_fps": round(det_fps, 1),
            })
        time.sleep(TICK_S)


# ------------------------------------------------------------------ main
def main():
    print(f"autoframe: crop {CROP_W}x{SRC_H} from {SRC_W}x{SRC_H} "
          f"-> {OUT_W}x{OUT_H} @ {FPS_CAPS}, scaler {SCALER}", flush=True)

    tuning = Tuning()
    stats = Stats()

    pipeline = Gst.parse_launch(PIPELINE)
    crop = pipeline.get_by_name("crop")
    appsink = pipeline.get_by_name("det")
    pv_sink = pipeline.get_by_name("pv")
    pv_valve = pipeline.get_by_name("pvalve")

    framer = Framer(crop, tuning)

    stop = threading.Event()
    threads = [
        threading.Thread(target=detector_loop,
                         args=(appsink, framer, tuning, stats, stop), daemon=True),
        threading.Thread(target=motion_loop,
                         args=(framer, tuning, stats, stop), daemon=True),
        threading.Thread(target=preview_loop,
                         args=(pv_sink, pv_valve, framer, stats, tuning, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"ERROR: {err} ({dbg})", file=sys.stderr, flush=True)
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            print("EOS", file=sys.stderr, flush=True)
            loop.quit()

    bus.connect("message", on_message)
    pipeline.set_state(Gst.State.PLAYING)

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        pipeline.set_state(Gst.State.NULL)
        # Non-zero exit so Docker's restart policy brings it back after an error.
        sys.exit(1)


if __name__ == "__main__":
    main()
