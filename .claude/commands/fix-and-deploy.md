# Fix regression then deploy

Use this when a code change broke tests and needs to be fixed before shipping.

## Step 1 — Run tests and capture failures
```bash
cd /Users/rnagulapalle/work/workspace/teamworks
source .venv/bin/activate
pytest --cov=forge --cov-fail-under=70 -x -q 2>&1
```

## Step 2 — Fix every failing test
For each failure:
1. Read the failing test file
2. Read the source file it tests
3. Determine if the test is stale (implementation changed correctly) or the code has a bug
4. Fix the root cause — never delete tests or skip assertions to make tests pass
5. Re-run just the failing test to confirm it's fixed: `pytest path/to/test.py::TestClass::test_name -xvs`

## Step 3 — Run full suite to confirm no regressions
```bash
pytest --cov=forge --cov-fail-under=70 -q 2>&1
```
Must show 0 failed and ≥70% coverage before proceeding.

## Step 4 — Commit fixes
```bash
git add -p   # stage only relevant changes
git commit -m "fix: <short description of what was broken and why>"
git push origin main
```

## Step 5 — Deploy
```bash
./deploy.sh
```
