"""Tier-2 v3 harness — real Postgres + mocked LLM.

Catches the bug classes Tier-1 can't reach because they require either
the real BaseAgent + DB integration or actual provider request shapes:

  - Bug #2 (canary):  _audit signature mismatch when overriding base.
  - Bug #5 (canary):  tool_result message shape rejected by the OpenAI
                      Responses API. Caught here via a schema validator
                      that mimics the API's input contract — no real API
                      call needed.
  - Bug #6 (canary):  cifix_engineer forgot to pass llm_call= to
                      run_coder_subagent, hitting the test-only stub.

Tier-2 still does NOT exercise:
  - Real Docker provisioning (Path 1 territory; needs a Docker daemon)
  - Real OpenAI/Anthropic responses (Tier-3 / canary)
  - Real GitHub commits

Run requires Postgres at $DATABASE_URL (default
postgresql+asyncpg://forge:forge@localhost:5432/forge_test). Tests skip
cleanly if Postgres is unreachable so dev workflow doesn't get stuck.
"""
