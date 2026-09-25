#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
set -eu
exec gst-launch-1.0 -e \
  mxlsrc video-flow-id="$FLOW_B" domain="$MXL_DOMAIN" \
  ! videoconvert ! video/x-raw,format=UYVY \
  ! queue max-size-buffers=4 \
  ! ndisink ndi-name="MXL Lab Vertical"
