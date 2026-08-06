# Phalanx sandbox base image — Option β (2026-05-20)
#
# Pre-bakes the four baseline apt deps that the v3 provisioner installs
# on every cifix_sre_setup run:
#   - git              (required by Engineer's commit_and_push + workspace clone)
#   - ca-certificates  (required by HTTPS pip/uv/curl)
#   - curl             (required by various install scripts)
#   - build-essential  (required for C-extension wheel builds)
#
# Eliminates the apt-mirror flake that has been blocking 3 consecutive
# days of pilot dispatches. The provisioner's existing baseline_apt_install
# step still runs but completes in <2s (apt reports "already installed")
# instead of timing out on the mirror.
#
# Build:    docker build -t phalanx-sandbox-base:py312-prebaked-v1 -f docker/sandbox-base/Dockerfile .
# Use:      set env PHALANX_SANDBOX_PYTHON_BASE_IMAGE_OVERRIDE=phalanx-sandbox-base:py312-prebaked-v1
#           on the cifix worker; env_detector.detect_env() reads it at the end of
#           Python-stack detection and overrides spec.base_image accordingly.
#
# Rollback: unset PHALANX_SANDBOX_PYTHON_BASE_IMAGE_OVERRIDE; the default
#           python:3.12-slim path is restored. Image can be deleted with
#           `docker rmi phalanx-sandbox-base:py312-prebaked-v1`.

FROM python:3.12-slim

# noninteractive prevents apt from blocking on tzdata / debconf prompts.
ENV DEBIAN_FRONTEND=noninteractive

# Baseline deps — same set the provisioner installs at runtime today
# (phalanx/ci_fixer_v3/provisioner.py:_BASELINE_APT_DEPS).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        build-essential \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Sanity: verify the deps are reachable on PATH at image-build time so a
# broken layer doesn't ship silently. These echo to stdout during build,
# not at container runtime.
RUN git --version && curl --version | head -1 && gcc --version | head -1

# No CMD/ENTRYPOINT override — the cifix provisioner spawns the container
# with its own `sleep infinity` command via `_docker_run_detached`. The
# image should behave identically to python:3.12-slim except for the
# pre-installed apt deps.

LABEL org.phalanx.sandbox.base="python:3.12-slim"
LABEL org.phalanx.sandbox.purpose="pre-baked baseline apt deps to bypass apt-mirror flakiness"
LABEL org.phalanx.sandbox.version="py312-prebaked-v1"
LABEL org.phalanx.sandbox.created="2026-05-20"
