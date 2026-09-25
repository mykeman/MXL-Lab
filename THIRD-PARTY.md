# Third-party software

This repository contains only the lab's own scripts, configuration and web GUI.
None of the software below is included in the repository. It is downloaded
when `setup.sh` runs or when the Docker image is built, under its own licence.

| Component | Used for | Licence | Source |
|---|---|---|---|
| MXL SDK and GStreamer plugin | Shared-memory media exchange (`mxlsrc`, `mxlsink`, `mxl-info`) | Apache-2.0 | https://github.com/dmf-mxl/mxl |
| gst-plugins-rs NDI plugin | NDI in and out (`ndisrc`, `ndisink`, device provider) | MPL-2.0 | https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs |
| GStreamer | Media pipelines | LGPL-2.1 or later | https://gstreamer.freedesktop.org |
| vcpkg | Dependencies for the MXL build | MIT | https://github.com/microsoft/vcpkg |
| OpenCV (opencv-python-headless) | Face detection runtime | Apache-2.0 | https://opencv.org |
| YuNet face detection model | Autoframing | MIT, Copyright (c) 2020 Shiqi Yu | https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet |
| FastAPI | GUI backend | MIT | https://github.com/fastapi/fastapi |
| Uvicorn | GUI web server | BSD-3-Clause | https://github.com/encode/uvicorn |
| Docker SDK for Python | GUI container control | Apache-2.0 | https://github.com/docker/docker-py |
| Barlow Semi Condensed | GUI typeface, loaded from Google Fonts | SIL Open Font License 1.1 | https://github.com/jpt/barlow |

YuNet: Wu, W., Peng, H. and Yu, S. (2023). YuNet: A tiny millisecond-level face
detector. Machine Intelligence Research, 20(5), 656-665.

## NDI

The NDI SDK, the NDI runtime (`libndi`) and the NDI Discovery Server are
proprietary software from Vizrt NDI AB. They are not included here and must
not be committed. `setup.sh` downloads the SDK from ndi.video, and whoever
runs it accepts the NDI SDK licence themselves.

NDI® is a registered trademark of Vizrt NDI AB. https://ndi.video
