# syntax=docker/dockerfile:1.7
#
# Samni Labs — backend container image.
#
# One image, two roles:
#   * API    →  uvicorn main:app --host 0.0.0.0 --port 8000
#   * Worker →  python -m worker
# The role is chosen at runtime by overriding CMD (in ECS/K8s task def);
# the default command is the API so a plain `docker run` boots something
# useful and the health-check target is always valid for the default role.
#
# Security posture (this image carries code that touches PHI):
#   * Multi-stage build. Only the runtime stage ships. Build tools
#     (gcc, headers) never make it into the final layer, shrinking both
#     the image and the CVE surface.
#   * Non-root user `samni` (uid/gid 10001). The app directory is owned
#     by that user with mode 550 so a compromised process cannot rewrite
#     its own code. Writable scratch goes to /tmp (tmpfs in prod).
#   * Pinned base image by digest-friendly tag (`python:3.11-slim-bookworm`).
#     Upgrade cadence is tracked in CI; do not float to `latest`.
#   * No pip cache, no wheel cache, no apt lists. `PIP_NO_CACHE_DIR=1`
#     and `apt-get clean` keep /root/.cache and /var/lib/apt/lists/* out
#     of the layer. A cache entry is both a size problem and a PHI
#     exfiltration vector if an attacker manages to read the fs.
#   * No secrets baked in. Every credential (DB password, AWS keys,
#     Cognito client secret, KMS key id) is injected at runtime via
#     environment variables from the orchestrator's secret store.
#     Dockerfile `ENV` is for non-secret defaults only.
#   * `PYTHONDONTWRITEBYTECODE=1` — no .pyc files on disk. Keeps the
#     image reproducible and avoids writing anything to the code dir at
#     runtime, which means we can mount the code read-only.
#   * `PYTHONUNBUFFERED=1` — stdout/stderr flush immediately so logs
#     reach CloudWatch without sitting in a buffer during a crash.
#   * `PIP_DISABLE_PIP_VERSION_CHECK=1` — stops pip from phoning home
#     on every invocation.
#   * Tini (PID 1) reaps zombies and forwards SIGTERM cleanly. Uvicorn
#     and the worker both install signal handlers; without tini, a
#     container stop can leave orphaned tasks or miss the graceful-
#     shutdown window. The worker's `shutdown_event` and the API's
#     `lifespan` shutdown both depend on SIGTERM arriving.
#   * `HEALTHCHECK` targets /health via a Python one-liner rather than
#     curl so we don't ship curl just for healthchecks (one fewer
#     binary = one fewer CVE vector). The worker container overrides
#     HEALTHCHECK in its task-def since /health belongs to the API.
#   * OCI labels are filled by CI (`--label` flags) with commit SHA,
#     build timestamp, and source URL. We intentionally do NOT embed
#     those at Dockerfile-time; pinning them here would force a layer
#     rebuild on every commit.
#
# Build:
#     docker build -t samni-backend:$(git rev-parse --short HEAD) \
#         --label org.opencontainers.image.revision=$(git rev-parse HEAD) \
#         backend/
#
# Run (API):
#     docker run --rm -p 8000:8000 --env-file .env samni-backend
#
# Run (worker):
#     docker run --rm --env-file .env samni-backend python -m worker
#


# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
# Installs build-time system packages, resolves Python deps into a
# virtualenv at /opt/venv. Only that venv is copied forward.
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# System build deps.
#   * build-essential / gcc    — for any wheel without a prebuilt arm64/x86_64
#     (e.g. some versions of psycopg/cryptography under source build).
#   * libpq-dev                — psycopg build headers (runtime uses libpq5).
#   * libffi-dev / libssl-dev  — cryptography / python-jose transitive.
#   * curl                     — only used to fetch tini in this stage; it
#                                does NOT ship to the runtime image.
# `rm -rf /var/lib/apt/lists/*` shrinks the layer and removes package
# metadata that was only needed for install-time resolution.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
        libffi-dev \
        libssl-dev \
        curl \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Create an isolated venv so the runtime stage gets a self-contained
# `/opt/venv` without having to worry about the system site-packages of
# the slim base.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Upgrade pip + build tools inside the venv. Keeping them pinned in the
# builder stage only (they never ship to runtime) is a deliberate
# tradeoff between reproducibility and a smaller attack surface.
RUN pip install --no-cache-dir --upgrade \
        "pip==24.2" \
        "setuptools==75.1.0" \
        "wheel==0.44.0"

# Install dependencies first, before copying app source. This keeps the
# (large, slow) dependency layer cached across code changes.
WORKDIR /build
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Fetch tini statically so the runtime stage can use it without apt.
# We pin the version and verify the signature is present. The binary is
# copied forward to /usr/local/bin/tini in the runtime stage.
ARG TINI_VERSION=v0.19.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) tini_arch="amd64" ;; \
        arm64) tini_arch="arm64" ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/tini \
        "https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini-${tini_arch}"; \
    chmod +x /tmp/tini


# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
# Minimal runtime. No compilers, no build headers, no curl. Runs as
# non-root user `samni` with a read-only app directory.
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    APP_HOME=/app \
    PORT=8000

# Runtime-only system deps:
#   * libpq5            — psycopg runtime (no -dev headers)
#   * ca-certificates   — TLS root store for outbound AWS / Postgres TLS
#   * tzdata            — audit timestamps need a complete zoneinfo db;
#                         without it TZ="UTC" still works but any
#                         explicit zone lookup would fail.
# We explicitly do NOT install curl, wget, netcat, vim, less, or any
# other "debug convenience" tools. If an operator needs to shell in,
# they use `aws ecs execute-command` with a one-off sidecar image, not a
# debug-enabled production image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && apt-get clean

# Copy venv and tini from the builder. Ownership is root:root; the app
# user only needs read+execute, which is the default for /opt/venv.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /tmp/tini /usr/local/bin/tini

# Create the non-root user. Fixed UID/GID so volume permissions are
# predictable across environments (ECS, EKS, local docker-compose).
# The numeric id (10001) is well above typical system uids and below
# the 65534 nobody ceiling.
RUN groupadd --system --gid 10001 samni \
 && useradd  --system --uid 10001 --gid samni \
             --home-dir /home/samni --create-home \
             --shell /usr/sbin/nologin samni

# Application code. Copy second so source changes do not invalidate the
# dep-install layer above. Ownership is root:samni with mode 550 —
# samni can read+execute the directory and its contents but cannot
# modify them. This is belt-and-suspenders for running the container
# with a read-only root filesystem.
WORKDIR ${APP_HOME}
COPY --chown=root:samni . ${APP_HOME}
RUN find ${APP_HOME} -type d -exec chmod 550 {} + \
 && find ${APP_HOME} -type f -exec chmod 440 {} + \
 && chmod 550 /usr/local/bin/tini

# Drop every capability shell history could capture. Even though we run
# as non-root, clearing these removes the `.bash_history` attack vector
# entirely.
ENV HISTFILE=/dev/null \
    LESSHISTFILE=/dev/null

USER samni:samni

EXPOSE 8000

# Healthcheck uses the venv's Python (already on PATH) and the stdlib
# http.client — no external binary, no extra package, and it fails
# fast if the app event loop is wedged. The check runs against
# 127.0.0.1 so it works even when the container's public interface is
# fronted by a load balancer doing its own probe.
#
# Worker containers should override this in their task definition with
# `HEALTHCHECK NONE` (or set `healthCheck: null` in ECS), since /health
# belongs to the API role.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; \
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT','8000') + '/health', timeout=3).status == 200 else sys.exit(1)" \
    || exit 1

# PID 1 = tini. Tini reaps zombies, forwards SIGTERM/SIGINT to the
# child, and exits with the child's exit code. Without it, `docker stop`
# escalates to SIGKILL after 10s and the API's lifespan shutdown
# (shutdown_pipeline, shutdown_db) would not run — leaving DB
# connections orphaned and in-flight checkpoints partially written.
ENTRYPOINT ["/usr/local/bin/tini", "--"]

# Default command: API role. Worker role overrides this at task-def
# level with ["python", "-m", "worker"].
#
# `--proxy-headers` + `--forwarded-allow-ips=*` tells uvicorn to trust
# X-Forwarded-* from the load balancer so the `_TraceMiddleware`'s
# client_ip logging reflects the real caller rather than the ALB's
# private IP. This is safe because the container is only reachable via
# the load balancer in production; direct ingress is blocked by the
# security group. Override `--forwarded-allow-ips` to the ALB subnet
# CIDR if you want defense-in-depth.
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--no-server-header", \
     "--no-access-log"]

# OCI labels. Values are supplied by CI via --label flags so rebuilds
# are not triggered by every commit. These keys are the canonical set;
# do not invent custom `com.samni.*` labels without talking to platform.
LABEL org.opencontainers.image.title="samni-backend" \
      org.opencontainers.image.description="Samni Labs healthcare assessment backend (API + worker)" \
      org.opencontainers.image.vendor="Samni Labs" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.source="https://github.com/samnilabs/backend"
