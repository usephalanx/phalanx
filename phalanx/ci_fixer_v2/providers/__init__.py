"""Provider adapters for CI Fixer v2.

Each adapter translates between the agent loop's provider-neutral
message format (Anthropic-style content blocks) and the LLM SDK's wire
format, and normalizes the SDK's response into `LLMResponse`.

Entry points (built here, used by the run bootstrap in Week 1.7b):
  - build_gpt_reasoning_callable   → main agent
  - build_sonnet_coder_callable    → coder subagent
"""

from phalanx.ci_fixer_v2.providers.anthropic_sonnet import (  # noqa: F401
    build_sonnet_coder_callable,
)
from phalanx.ci_fixer_v2.providers.openai_gpt import (  # noqa: F401
    build_gpt_reasoning_callable,
)
