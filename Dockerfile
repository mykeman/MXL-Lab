# MXL lab image: MXL SDK and tools, MXL and NDI GStreamer plugins,
# OpenCV and the YuNet face model. One image for all four functions.
#
# UBUNTU is set by setup.sh to match the VM, so the container builds
# on the same release the lab was proven on. Defaults to 24.04.
ARG UBUNTU=24.04
FROM ubuntu:${UBUNTU}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git cmake ninja-build pkg-config \
      curl wget unzip zip tar ca-certificates clang lld bison flex \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      gstreamer1.0-tools gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      python3 python3-pip python3-gi python3-numpy \
      gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      libavahi-client3 libavahi-common3 \
    && rm -rf /var/lib/apt/lists/*

# Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain stable
ENV PATH=/root/.cargo/bin:$PATH

# vcpkg, required by the MXL CMake presets
RUN git clone --depth 1 https://github.com/microsoft/vcpkg.git /opt/vcpkg \
 && /opt/vcpkg/bootstrap-vcpkg.sh -disableMetrics
ENV VCPKG_ROOT=/opt/vcpkg

# NDI runtime, from the build context (not redistributable, keep this image internal)
COPY ndi/libndi.so.6 /usr/local/lib/libndi.so.6
RUN ln -sf /usr/local/lib/libndi.so.6 /usr/local/lib/libndi.so && ldconfig
ENV NDI_RUNTIME_DIR_V6=/usr/local/lib
ENV NDI_RUNTIME_DIR_V5=/usr/local/lib

# MXL SDK and tools
RUN git clone --depth 1 https://github.com/dmf-mxl/mxl.git /src/mxl
# The MXL CMake preset looks for vcpkg under $HOME, which is /root in the build
RUN ln -sfn /opt/vcpkg /root/vcpkg && git -C /opt/vcpkg fetch --unshallow
ENV CMAKE_POLICY_VERSION_MINIMUM=3.5
WORKDIR /src/mxl
RUN cmake --preset Linux-GCC-Release \
    || { tail -n 80 build/Linux-GCC-Release/vcpkg-manifest-install.log; exit 1; } \
 && cmake --build build/Linux-GCC-Release -j"$(nproc)" \
 && cp -a build/Linux-GCC-Release/lib/. /usr/local/lib/ \
 && find build -maxdepth 5 -type f -executable -name 'mxl-*' \
      -exec cp {} /usr/local/bin/ \; \
 && ldconfig

# MXL GStreamer plugin
WORKDIR /src/mxl/rust
RUN cargo build --release

# NDI GStreamer plugin
RUN git clone --depth 1 \
      https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git \
      /src/gst-plugins-rs
WORKDIR /src/gst-plugins-rs
RUN cargo build --release -p gst-plugin-ndi

# Consolidate both plugins
RUN mkdir -p /opt/gst-plugins \
 && cp /src/mxl/rust/target/release/libgstmxl.so /opt/gst-plugins/ \
 && cp /src/gst-plugins-rs/target/release/libgstndi.so /opt/gst-plugins/
ENV GST_PLUGIN_PATH=/opt/gst-plugins
ENV LD_LIBRARY_PATH=/usr/local/lib

# Fail the build here, not at runtime, if either plugin will not load
RUN gst-inspect-1.0 mxlsink >/dev/null && gst-inspect-1.0 ndisrc >/dev/null

# Autoframing deps
RUN pip3 install --break-system-packages --no-cache-dir \
      opencv-python-headless \
 && mkdir -p /opt/models \
 && wget -qO /opt/models/face_detection_yunet_2023mar.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
 && test -s /opt/models/face_detection_yunet_2023mar.onnx

# gst-device-monitor-1.0 and friends, for debugging discovery by hand
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-plugins-base-apps \
    && rm -rf /var/lib/apt/lists/*

# Baked-in copies. docker-compose.yml also bind-mounts these so edits
# on the host apply with a container restart.
COPY autoframe.py /opt/autoframe.py
COPY scripts/ /opt/scripts/
RUN chmod +x /opt/scripts/*

ENV MXL_DOMAIN=/dev/shm/mxl
WORKDIR /opt
