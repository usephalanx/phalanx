# Check pipeline status

Query the prod DB for the current state of all active and recent runs.

```bash
ssh -i ~/work/LightsailDefaultKey-us-west-2.pem ubuntu@44.233.157.41 "
docker exec forge-prod-postgres-1 psql -U forge -d forge -c \"
SELECT
  r.id,
  r.status,
  r.created_at,
  wo.title
FROM runs r
JOIN work_orders wo ON r.work_order_id = wo.id
ORDER BY r.created_at DESC
LIMIT 10;
\"

echo ''
echo '--- Tasks for most recent run ---'
docker exec forge-prod-postgres-1 psql -U forge -d forge -c \"
SELECT t.sequence_num, t.agent_role, t.status, t.assigned_agent_id, t.error
FROM tasks t
JOIN runs r ON t.run_id = r.id
WHERE r.id = (SELECT id FROM runs ORDER BY created_at DESC LIMIT 1)
ORDER BY t.sequence_num;
\"
"
```

Summarize:
- Most recent run: ID, status, title
- Which task is currently executing / last completed / failed
- Any task errors
