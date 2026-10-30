# One image, three ways to run it: the API (default), the Streamlit page, and the
# weekly scoring job. The base is pinned by digest, refreshed deliberately:
#   docker buildx imagetools inspect python:3.13-slim-bookworm
# The runtime stage has no compiler, no pip cache and no installer: pip and
# setuptools are removed once the install is done, because nothing here needs
# them at run time and their vendored packages are what image scanners flag.

ARG BASE_IMAGE=python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

# ---------- build ----------
FROM ${BASE_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install . \
    && /opt/venv/bin/pip uninstall -y pip setuptools \
    && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /opt/venv -name '*.pyc' -delete

# ---------- runtime ----------
FROM ${BASE_IMAGE} AS runtime
ARG AIQ_VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    AIQ_VERSION=${AIQ_VERSION} \
    AIQ_ARTIFACT_DIR=/srv/artifacts \
    AIQ_TEAMS_PATH=/srv/artifacts/teams.parquet \
    AIQ_PORT=8000 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# curl is for the health checks and nothing else.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip setuptools \
    && rm -rf /usr/local/lib/python3.13/site-packages/_distutils_hack \
              /usr/local/lib/python3.13/site-packages/distutils-precedence.pth \
    && useradd --create-home --uid 10001 aiq

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
COPY config ./config
# The artefact volume is mounted here; the job writes, the API and the page read.
RUN mkdir -p /srv/artifacts /app/data /app/artifacts && chown -R aiq:aiq /srv/artifacts /app
USER aiq

EXPOSE 8000 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["aiq-api"]
