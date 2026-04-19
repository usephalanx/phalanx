FROM node:20-slim

RUN usermod -l phalanx -d /home/phalanx -m node && \
    groupmod -n phalanx node 2>/dev/null || true

RUN npm install -g \
    eslint@8.57.0 \
    typescript@5.4.5 \
    jest@29.7.0 \
    --no-fund --no-audit

COPY reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh && \
    mkdir -p /workspace && \
    chown phalanx:phalanx /workspace 2>/dev/null || chown phalanx /workspace

WORKDIR /workspace
USER phalanx
