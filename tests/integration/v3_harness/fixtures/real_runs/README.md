# real_runs — captured prod task outputs for replay

Drop JSON dumps of one CI Fixer v3 run here, named `<scenario>__<run_id>.json`.

Each file is consumed by `test_mini_lint_simulation.py::test_replay_real_run`
which replays the agent chain against the captured outputs and reports
where contracts hold or break.

## Capturing a run from prod

```bash
ssh $DEPLOY_HOST 'docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U forge -d forge -At -F"\t" -c "
    SELECT json_agg(row_to_json(t)) FROM (
      SELECT sequence_num, agent_role, status, output, description
      FROM tasks WHERE run_id = '\''<RUN_UUID>'\''
      ORDER BY sequence_num
    ) t;
  "' > tests/integration/v3_harness/fixtures/real_runs/lint_iter3_turncap__<run_id>.json
```

## Format

```json
[
  {"sequence_num": 1, "agent_role": "cifix_sre_setup", "status": "COMPLETED",
   "description": "{\"sre_mode\":\"setup\",...}", "output": {...}},
  {"sequence_num": 2, "agent_role": "cifix_techlead", "status": "COMPLETED",
   "description": "...", "output": {...}},
  ...
]
```

## Why this exists

Tier-1 tests catch logic bugs inside one agent. Real-run replays catch
**seam bugs** between agents — what bit prod twice this month
(Challenger queue subscription, SRE verify scope). Each piece tested
green; the contract between them silently drifted.

The replay harness asks: given exactly what the upstream agent wrote
in prod, would the downstream agent's loader correctly consume it?
