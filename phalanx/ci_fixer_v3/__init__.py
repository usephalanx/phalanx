"""CI Fixer v3 — multi-agent DAG with on-the-fly SRE-provisioned sandboxes.

Sibling of ci_fixer_v2. v3 modules are kept here (rather than under
phalanx/ci_fixer/) so the two pipelines are fully separable:
  - v2 code lives under phalanx/ci_fixer_v2/
  - v3 code lives under phalanx/ci_fixer_v3/
  - shared primitives (CIFailureEvent, SandboxProvisioner's pool path,
    log_parser, etc.) stay under phalanx/ci_fixer/

Agent modules (Celery tasks) live under phalanx/agents/cifix_*.py because
that's where TaskRouter discovers them. This package contains the
non-agent logic: env detection, on-the-fly provisioning, workflow parsing.
"""
