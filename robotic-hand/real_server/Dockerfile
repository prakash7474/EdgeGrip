# syntax=docker/dockerfile:1
#
# Real inference server image (CPU). Build from the robotic-hand/ directory:
#   docker build -f real_server/Dockerfile -t robotic-hand-real-server:5a .
#
# API_TOKEN is a runtime secret and is never baked into the image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf \
    PORT=8001

# MODEL_ID and DETECTION_THRESHOLD are runtime-overridable; MODEL_ID is also
# used at build time when PRELOAD_MODEL=1 so the container can start offline.
ARG MODEL_ID=PekingU/rtdetr_r18vd
ARG PRELOAD_MODEL=1
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV MODEL_ID=${MODEL_ID}

WORKDIR /app

# libgl/libglib are required by opencv-python (used to decode JPEG bytes).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-real.txt ./
# Install the CPU torch build first; requirements-real.txt then leaves it in place.
RUN python -m pip install --upgrade pip \
    && python -m pip install torch --index-url "${TORCH_INDEX_URL}" \
    && python -m pip install -r requirements-real.txt

COPY real_server ./real_server

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /models \
    && chown -R app:app /app /models

USER app

# Download the model during the build (default). Set PRELOAD_MODEL=0 to defer
# the download to the first container start; /models is writable by `app`.
RUN if [ "${PRELOAD_MODEL}" = "1" ]; then \
        python -c "import os; from transformers import pipeline; pipeline('object-detection', model=os.environ['MODEL_ID'])"; \
    fi

EXPOSE 8001

# /health needs no token and reports only status, model and device.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4)"

CMD ["python", "-m", "real_server", "--host", "0.0.0.0", "--port", "8001"]
