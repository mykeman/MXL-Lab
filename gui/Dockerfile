# Builds on the existing lab image, so GStreamer, the NDI plugin and
# mxl-info are already present. Only the web stack is added.
FROM mxl-lab:latest

RUN pip3 install --break-system-packages --no-cache-dir \
      fastapi "uvicorn[standard]" docker

COPY app.py /opt/gui/app.py
COPY static/ /opt/gui/static/

WORKDIR /opt/gui
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${GUI_PORT:-8080}"]
