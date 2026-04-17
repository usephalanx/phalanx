FROM rust:1.77-slim

# Install clippy and rustfmt (included in toolchain but ensure available)
RUN rustup component add clippy rustfmt

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash phalanx

COPY ../reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh

RUN mkdir -p /workspace && chown phalanx:phalanx /workspace

WORKDIR /workspace
USER phalanx
