"""Phalanx v1.7.3 — shadow-mode ledger MVP.

Run Phalanx on a real GitHub Actions workflow failure WITHOUT pushing
any code. Capture the verdict, proposed patch, confidence, root cause,
affected files, cost, and time in `shadow_ledger`. Manual ground-truth
comparison against the maintainer's actual fix for the first 10 entries.

Entry points:
  python -m phalanx.shadow run --repo OWNER/NAME --workflow-run-id N
  python -m phalanx.shadow show <ledger_id>
  python -m phalanx.shadow export out.json
"""
