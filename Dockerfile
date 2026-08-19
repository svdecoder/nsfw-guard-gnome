FROM python:3.11-slim AS base

# System deps:
#  - libgl1/libglib2.0-0: onnxruntime/opencv runtime deps
#  - GI/GObject introspection + dev headers: needed to build PyGObject
#  - gstreamer core + pipewire plugin + dbus: live screen capture path
#  - dbus session bus client libs: talking to xdg-desktop-portal
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgirepository1.0-dev \
    gobject-introspection \
    gcc \
    libcairo2-dev \
    pkg-config \
    python3-dev \
    gstreamer1.0-pipewire \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    libgstreamer1.0-0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    dbus \
    && rm -rf /var/lib/apt/lists/*

# NOTE on the 640m model: we deliberately do NOT auto-download it here.
# GitHub gates this specific release asset behind a login redirect even
# though the repo is public (likely due to the sensitive nature of the
# file), so curl/wget fail in any non-browser context, including CI and
# `docker build`. See README.md "Getting the 640m model" for the manual
# one-time download step. The app falls back to the smaller bundled
# 320n model automatically if /app/models/640m.onnx isn't present
# (mounted in at `docker run` time - see README), so the container
# still works out of the box without it, just less accurately.
RUN mkdir -p /app/models

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Diagnostic (cheap, always run): shows exactly what the nvidia-*-cu11
# pip packages actually installed and where, since guessing at this
# blind has already been wrong twice (cu12-vs-cu11 mismatch, then a
# missing libcudnn.so.8 despite nvidia-cudnn-cu11 being installed).
# Look for this section in the build log if CUDA still doesn't load -
# it'll show either the real path (if LD_LIBRARY_PATH needs
# adjusting) or that the file genuinely isn't there (if pip resolved
# a version that doesn't ship it).
RUN echo "=== nvidia pip package contents ===" && \
    find /usr/local/lib/python3.11/site-packages/nvidia -name "*.so*" 2>/dev/null | sort && \
    echo "=== looking specifically for libcudnn* ===" && \
    find / -xdev -name "libcudnn*" 2>/dev/null && \
    echo "=== end diagnostic ==="

# The nvidia-*-cu12 pip packages install their .so files under
# site-packages/nvidia/<component>/lib - onnxruntime's CUDA execution
# provider needs those on the dynamic linker's search path at process
# start (LD_LIBRARY_PATH is read at exec time, so this must be an ENV,
# not something set from within Python after the process is already
# running). Built for python3.11 site-packages specifically since
# that's what this base image ships.
ARG LD_LIBRARY_PATH=""
ENV LD_LIBRARY_PATH="/usr/local/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cufft/lib:/usr/local/lib/python3.11/site-packages/nvidia/curand/lib:/usr/local/lib/python3.11/site-packages/nvidia/cusolver/lib:/usr/local/lib/python3.11/site-packages/nvidia/cusparse/lib:${LD_LIBRARY_PATH}"

# Non-root user (hardening — carried forward from phase 1 already)
RUN useradd -m -u 1000 nsfwguard
USER nsfwguard

COPY --chown=nsfwguard:nsfwguard app/ .

# Default entrypoint: live monitoring loop (Phase 2).
# For phase-1-style static image testing, override with:
#   docker run ... nsfw-guard python detect.py
ENTRYPOINT ["python", "main.py"]
