# The LiveKit Cloud agent image. Build context is the repository root.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend:/app

# ffmpeg is used by the audio pipeline; build tools are needed by some of
# the pinned native wheels. ca-certificates is required to reach LiveKit.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    gcc \
    g++ \
    python3-dev \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, for layer caching. The manifest pins the LiveKit
# plugins and the provider clients; the lockfile is intentionally the
# repository's pyproject.toml (see docs/DEPLOYMENT.md).
COPY pyproject.toml ./
COPY backend/app ./backend/app
COPY agent ./agent
RUN python -m venv /opt/venv \
  && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
  && /opt/venv/bin/pip install --no-cache-dir -e .
ENV PATH="/opt/venv/bin:$PATH"

# LiveKit Cloud injects LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
# at runtime. Do not bake them (or any provider key) into the image.
RUN adduser --disabled-password --gecos "" --home /app --uid 10001 appuser \
  && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "agent.worker"]
