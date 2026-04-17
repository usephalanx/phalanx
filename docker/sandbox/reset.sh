#!/bin/bash
# Shared reset script — clears workspace and caches between fix runs.
# Runs inside the container via: docker exec {id} sh /phalanx/reset.sh
set -e
rm -rf /workspace/* 2>/dev/null || true
rm -rf /tmp/pip-* /tmp/npm-* /tmp/.cache /root/.cache 2>/dev/null || true
echo "done"
