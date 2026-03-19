# Run test suite

Run the full FORGE test suite and verify coverage meets the 70% threshold.

```bash
cd /Users/rnagulapalle/work/workspace/teamworks
source .venv/bin/activate
pytest --cov=forge --cov-report=term-missing --cov-fail-under=70 -x -q 2>&1
```

Report:
- Total passed / failed / errors
- Coverage % (must be ≥70%)
- Any new failures compared to baseline (342 passed, 0 failed)

If any tests fail, read the failing test and the source it tests, diagnose the root cause, and fix it before declaring success.
