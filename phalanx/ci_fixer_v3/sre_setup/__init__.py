"""CI Fixer v3 — Agentic SRE setup tools.

See [docs/ci-fixer-v3-agentic-sre.md](../../../docs/ci-fixer-v3-agentic-sre.md)
for the full design (v2). This package implements Phase 0 — tools + strict
input validation + evidence checking + tests. Phase 1+ (LLM loop, hybrid
integration) lands in subsequent commits.

Public surface:
  - SREToolContext     — runtime context (container_id, workspace, exec callable)
  - Capability         — installed-tool record
  - BlockedReason      — enum of escalation reasons
  - SRE_SETUP_TOOLS    — list of (ToolSchema, handler) pairs ready for the
                         LLM provider's tool-use API
"""

from __future__ import annotations

from phalanx.ci_fixer_v3.sre_setup.schemas import (
    BlockedReason,
    Capability,
    ObservedTokenStatus,
    SREToolContext,
)
from phalanx.ci_fixer_v3.sre_setup.tools import SRE_SETUP_TOOLS

__all__ = [
    "BlockedReason",
    "Capability",
    "ObservedTokenStatus",
    "SREToolContext",
    "SRE_SETUP_TOOLS",
]
