#!/bin/bash
# Recreate oo-worker to render on the host's REAL display (:0) with the GPU,
# instead of headless Xvfb (which forced software rendering).
podman rm -f oo-worker 2>/dev/null || true
podman run -d --name oo-worker --restart=always \
  --net=host \
  --security-opt label=disable \
  --device /dev/dri/renderD128 \
  --device /dev/dri/renderD129 \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -e PYTHONUNBUFFERED=1 \
  -e FASTEMBED_CACHE_DIR=/app/data/.cache/fastembed \
  -e DISPLAY=:0 \
  -v /home/linkedinautomation/OpenOutreach:/app:z \
  -v openoutreach-data:/app/data \
  ghcr.io/eracle/openoutreach:latest \
  bash -lc 'python manage.py run_worker --interval 90'
echo "--- recreated (no Xvfb, DISPLAY=:0, GPU) ---"
sleep 4
podman ps --format '{{.Names}} {{.Status}}' | grep oo-worker
