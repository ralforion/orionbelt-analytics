# syntax=docker/dockerfile:1.7

# ---------- Builder stage ----------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Pin the venv to the base image interpreter, making the FROM tag the single
# source of truth for the image's Python. Without this, a base image whose
# Python differs from .python-version makes uv fetch its own CPython; the venv
# then symlinks outside /opt/venv, and the runtime stage — which copies only
# /opt/venv — ships a dangling python that breaks every import at startup.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3

# Build deps for any wheels that need compiling (most are wheels, but keep gcc as a safety net)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first (cached layer) using the lockfile.
#
# Only the three files this step actually reads are copied. src/__init__.py
# used to be copied too, for the version [tool.hatch.version] reads -- but
# --no-install-project does not build the project, so it was never needed, and
# it made every version bump invalidate this layer and pay a full cold install
# of chromadb, onnxruntime and pandas. The project's own version is now
# dynamic, so none of these three change on a bump either, and the layer
# survives a release. The project (and its version) is installed below, once
# the source is present.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy the rest of the project and install it
COPY . .
RUN uv sync --frozen --no-dev

# Bake in the GraphRAG embedding model (all-MiniLM-L6-v2, ONNX). chromadb
# downloads it on first use from its own S3 bucket into ~/.cache/chroma; in a
# container that meant every fresh container fetched ~80 MB at runtime, and in
# a network without internet access the download failed and the server fell
# back to keyword-only TF-IDF search -- working, but much weaker, and easy to
# miss. Fetching it here uses chromadb's own loader, so the archive is checked
# against the SHA-256 chromadb pins, and unpacked exactly as at runtime. Once
# the unpacked files exist chromadb never reads the archive again, so it is
# removed. HOME is pointed at a staging directory the runtime stage copies
# from.
RUN HOME=/opt/model-home /opt/venv/bin/python -c \
        "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as F; F()(['probe'])" \
    && rm -f /opt/model-home/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx.tar.gz \
    && test -f /opt/model-home/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx

# The multilingual model (GRAPHRAG_EMBEDDING_MODEL=multilingual), for the same
# reason: a deployment that chooses it must not need huggingface.co at
# runtime. Fetched by the server's own loader, which pins the revision and
# checks each file's SHA-256 (src/graphrag/multilingual.py). 118 MB.
RUN HOME=/opt/model-home /opt/venv/bin/python -c \
        "from src.graphrag.multilingual import model_files; model_files()"

# Collect the verbatim licence text of every bundled dependency into a single
# file. The image redistributes the whole production closure, so MIT/BSD/Apache
# attribution clauses apply to it in a way they do not to the PyPI wheel, which
# only declares its dependencies. The texts already exist under each package's
# dist-info; gathering them here means that stays true by construction rather
# than by accident, and survives any future slimming of the runtime layer.
RUN /opt/venv/bin/python scripts/gen-third-party-notices.py \
        --dump-texts /licenses/THIRD_PARTY_LICENSES.txt

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

# Third-party licence texts, next to the THIRD_PARTY_NOTICES.md index that
# COPY . . brings in.
COPY --from=builder /licenses /app/licenses

# Non-root user; owns /app and /data
RUN useradd --create-home --uid 1000 oba \
    && mkdir -p /data \
    && chown -R oba:oba /app /data

# The embedding model, where chromadb looks for it: the runtime user's
# ~/.cache/chroma. With it present the server never contacts the download
# location, so the image works without outbound internet access.
COPY --from=builder --chown=oba:oba /opt/model-home/.cache/chroma /home/oba/.cache/chroma
COPY --from=builder --chown=oba:oba /opt/model-home/.cache/huggingface /home/oba/.cache/huggingface

USER oba

VOLUME ["/data"]
EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,socket,sys; s=socket.socket(); s.settimeout(3); \
s.connect(('127.0.0.1', int(os.environ.get('MCP_SERVER_PORT','9000')))); s.close()" \
        || exit 1

CMD ["python", "server.py"]
