#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
set -eu
eval "$(python3 /opt/scripts/load-config.py)"
# SRC_FPS is either a whole number (50) or a fraction from auto-detect (60000/1001)
case "$SRC_FPS" in
  */*) FPS_CAPS="$SRC_FPS" ;;
  *)   FPS_CAPS="$SRC_FPS/1" ;;
esac

# NDI_URL (ip:port) connects straight to the sender and skips discovery.
if [ -n "${NDI_URL:-}" ]; then
  set -- url-address="$NDI_URL"
  echo "ndi-ingest: '$NDI_SRC' at $NDI_URL (direct) -> ${SRC_W}x${SRC_H} @ ${FPS_CAPS}"
else
  set --
  echo "ndi-ingest: '$NDI_SRC' (discovery) -> ${SRC_W}x${SRC_H} @ ${FPS_CAPS}"
fi

# videoscale is a passthrough when the source already matches SRC_W x SRC_H.
# It is there so a source with a different size cannot change the geometry
# of Flow A underneath the other three functions.
exec gst-launch-1.0 -e \
  ndisrc ndi-name="$NDI_SRC" "$@" timestamp-mode=receive-time \
  ! ndisrcdemux name=demux \
  demux.video \
    ! queue max-size-buffers=4 leaky=downstream \
    ! videoscale add-borders=true \
    ! video/x-raw,width="$SRC_W",height="$SRC_H",pixel-aspect-ratio=1/1 \
    ! videoconvert ! video/x-raw,format=v210 \
    ! videorate ! video/x-raw,framerate="$FPS_CAPS" \
    ! mxlsink flow-id="$FLOW_A" domain="$MXL_DOMAIN" \
  demux.audio ! queue ! fakesink sync=false
