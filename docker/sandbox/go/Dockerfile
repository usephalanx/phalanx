FROM golang:1.22-alpine

# Install staticcheck and golangci-lint for broader Go lint coverage
RUN go install honnef.co/go/tools/cmd/staticcheck@2024.1.0 && \
    go install github.com/golangci/golangci-lint/cmd/golangci-lint@v1.59.1

# Create non-root user
RUN adduser -D -u 1000 phalanx

COPY ../reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh

RUN mkdir -p /workspace && chown phalanx:phalanx /workspace

WORKDIR /workspace
USER phalanx
