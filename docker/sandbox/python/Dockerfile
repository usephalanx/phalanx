FROM python:3.12-slim

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash phalanx

# Install pinned tool versions used by the CI fixer.
# Versions are chosen to match the most common customer constraints.
# When VersionParityAgent detects a mismatch, it installs the customer's
# pinned version inside the running container before executing.
RUN pip install --no-cache-dir \
    ruff==0.4.4 \
    mypy==1.10.0 \
    pytest==8.2.0 \
    pytest-asyncio==0.23.7

# Copy reset script
COPY ../reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh

# Workspace dir — populated via docker cp by SandboxProvisioner
RUN mkdir -p /workspace && chown phalanx:phalanx /workspace

WORKDIR /workspace
USER phalanx
