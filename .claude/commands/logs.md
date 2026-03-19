# Tail prod logs

Show recent logs from all FORGE production containers on `44.233.157.41`.

```bash
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 "
echo '=== GATEWAY (last 20) ==='
docker logs forge-prod-forge-gateway-1 --tail 20 2>&1

echo ''
echo '=== WORKER (last 40) ==='
docker logs forge-prod-forge-worker-1 --tail 40 2>&1

echo ''
echo '=== API (last 10) ==='
docker logs forge-prod-forge-api-1 --tail 10 2>&1
"
```

After printing logs:
- Identify any ERROR or WARNING lines
- Flag any `SoftTimeLimitExceeded`, `PermissionError`, `RuntimeError: Future attached to a different loop`, or `MissingGreenlet` errors
- Summarize the current pipeline state (what run_id is active, which agent is running, any failures)

If `$ARGUMENTS` is provided, use it as a `--since` timestamp or `--tail N` override.
