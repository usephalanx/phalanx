FROM golang:1.22-alpine

RUN apk add --no-cache git

RUN go install honnef.co/go/tools/cmd/staticcheck@v0.5.1

RUN adduser -D -u 1000 phalanx

COPY reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh && \
    mkdir -p /workspace && \
    chown phalanx /workspace

WORKDIR /workspace
USER phalanx
