# syntax=docker/dockerfile:1.7

# ---------- Builder stage ----------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Build deps for any wheels that need compiling (most are wheels, but keep gcc as a safety net)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first (cached layer) using the lockfile
COPY pyproject.toml uv.lock README.md ./
COPY src/__init__.py ./src/__init__.py
RUN uv sync --frozen --no-dev --no-install-project

# Copy the rest of the project and install it
COPY . .
RUN uv sync --frozen --no-dev

# ---------- Runtime stage ----------
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv \
    MCP_TRANSPORT=http \
    MCP_SERVER_HOST=0.0.0.0 \
    MCP_SERVER_PORT=9000 \
    OUTPUT_DIR=/data

# Runtime libs:
# - libpq5: psycopg2-binary runtime
# - chromium + fonts: required by kaleido>=1.0 for Plotly static image export
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        chromium \
        fonts-liberation \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV KALEIDO_CHROMIUM_PATH=/usr/bin/chromium

# Bring in the pre-built virtualenv
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY . .

# Non-root user; owns /app and /data
RUN useradd --create-home --uid 1000 oba \
    && mkdir -p /data \
    && chown -R oba:oba /app /data

USER oba

VOLUME ["/data"]
EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,socket,sys; s=socket.socket(); s.settimeout(3); \
s.connect(('127.0.0.1', int(os.environ.get('MCP_SERVER_PORT','9000')))); s.close()" \
        || exit 1

CMD ["python", "server.py"]
