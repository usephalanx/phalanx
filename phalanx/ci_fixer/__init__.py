"""
FORGE CI Fixer — autonomous CI failure detection and repair.

Listens to CI webhooks (GitHub Actions, Buildkite, CircleCI, Jenkins),
fetches failure logs, classifies the failure type, and dispatches a
CIFixerAgent to fix the code and commit back to the branch.

Zero changes to existing agents or orchestrator.
"""
