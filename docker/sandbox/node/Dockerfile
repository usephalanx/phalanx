FROM node:20-slim

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash phalanx 2>/dev/null || true

# Install common Node tooling used by the CI fixer.
RUN npm install -g \
    eslint@8.57.0 \
    typescript@5.4.5 \
    jest@29.7.0 \
    --no-fund --no-audit

COPY ../reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh

RUN mkdir -p /workspace && chown phalanx:phalanx /workspace

WORKDIR /workspace
USER phalanx
