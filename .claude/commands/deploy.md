# Deploy to production

Before deploying, always run tests first to prevent regressions.

## Step 1 — Run tests
```bash
cd /Users/rnagulapalle/work/workspace/teamworks
source .venv/bin/activate
pytest --cov=forge --cov-fail-under=70 -x -q 2>&1
```
Stop and report if tests fail. Do not deploy broken code.

## Step 2 — Deploy
```bash
cd /Users/rnagulapalle/work/workspace/teamworks
./deploy.sh $ARGUMENTS
```

`$ARGUMENTS` is the version tag passed by the user (e.g. `v1.2.0`), or omit for auto-bump.

The deploy script:
1. Builds `forge-api` and `forge-worker` images locally for `linux/amd64`
2. Saves + scps tarballs to `ubuntu@44.233.157.41`
3. Loads images, runs DB migrations, restarts all containers
4. Verifies API health at `http://44.233.157.41:8000/health`

## Step 3 — Post-deploy checks
```bash
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 \
  "curl -s http://localhost:8000/health && docker logs forge-prod-forge-gateway-1 --tail 5"
```

Confirm:
- API returns `{"status":"ok","db":"ok","redis":"ok"}`
- Gateway shows `A new session (s_...) has been established`
- Fix forge-repos permissions: `docker exec -u root forge-prod-forge-worker-1 chmod 777 /tmp/forge-repos`
