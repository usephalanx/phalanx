# Postgres backups (P0-1)

Operational durability for the shadow ledger and all Phalanx postgres state.
Implements item P0-1 of the [Beta Readiness Stabilization Plan](../beta-readiness-stabilization-plan-2026-05-11.md).

## What's running

- **Local dumps** every 6 hours into `backups/postgres/forge-<UTC-timestamp>.dump`
- **14-dump retention** — oldest pruned automatically
- **Self-check on every dump** — `pg_restore --list` runs against the dump before it's kept
- **Optional off-host sync** via rclone to any cloud provider you configure
- **Throwaway-container verification** via `make backup-verify`
- **macOS LaunchAgent** schedules dumps; non-Mac systems use `scripts/backup_postgres.crontab`

## Operator commands

```bash
make backup                  # take a dump now (with self-check + retention)
make backup-list             # ls -lh on backups/postgres/
make backup-verify           # restore latest dump to a throwaway container, compare row counts
make backup-restore dump=backups/postgres/forge-...dump
                             # restore <dump> into PHALANX_PG_TARGET (default: forge-postgres-restore-test)
make backup-offhost          # sync dumps to PHALANX_BACKUP_REMOTE via rclone
make backup-install          # install macOS LaunchAgent (runs at 00, 06, 12, 18 local time)
make backup-uninstall        # remove the LaunchAgent
```

## First-time setup

### Local dumps only (no off-host)

```bash
make backup                  # one-shot test
make backup-list             # confirm a .dump file appeared
make backup-verify           # confirm round-trip works
make backup-install          # schedule it (macOS); see crontab template for Linux
launchctl list | grep com.phalanx.backup    # confirm it loaded
tail -f backups/postgres/launchagent.log    # watch it fire
```

### Add off-host sync

Off-host upload is your disaster-recovery copy. Without it, a disk failure
or accidental `rm -rf` loses everything.

```bash
brew install rclone
rclone config              # configure a remote (R2 / S3 / Backblaze / etc.)
export PHALANX_BACKUP_REMOTE='r2:phalanx-backups/postgres/'   # or your remote
make backup-offhost        # one-shot upload — should print "OK N local dump(s) synced"
```

Add the `PHALANX_BACKUP_REMOTE` export to your shell rc file (`~/.zshrc`)
so the LaunchAgent picks it up.

To verify the LaunchAgent is uploading:

```bash
rclone ls "$PHALANX_BACKUP_REMOTE"          # list remote dumps
rclone size "$PHALANX_BACKUP_REMOTE"        # total bytes off-host
```

### Linux fallback (non-Mac)

```bash
crontab -l > /tmp/current-crontab           # save current
sed "s|PHALANX_REPO_ROOT|$(pwd)|g" scripts/backup_postgres.crontab >> /tmp/current-crontab
crontab /tmp/current-crontab
crontab -l | grep backup_postgres           # confirm
```

## Restore drill

You must run a real restore drill at least once before relying on these
backups — backups you've never restored are not backups.

```bash
# 1. Bring up a throwaway target
docker run -d --rm --name forge-postgres-restore-test \
  -e POSTGRES_USER=forge \
  -e POSTGRES_PASSWORD=restore_drill \
  -e POSTGRES_DB=forge \
  pgvector/pgvector:pg16
sleep 5

# 2. Pick a dump and restore
DUMP=$(ls -1t backups/postgres/forge-*.dump | head -1)
make backup-restore dump="$DUMP"

# 3. Confirm row counts on the target
docker exec forge-postgres-restore-test psql -U forge -d forge \
  -c "SELECT 'shadow_ledger' AS t, count(*) FROM shadow_ledger
      UNION ALL SELECT 'ci_integrations', count(*) FROM ci_integrations
      UNION ALL SELECT 'runs', count(*) FROM runs
      UNION ALL SELECT 'tasks', count(*) FROM tasks;"

# 4. Tear down
docker rm -f forge-postgres-restore-test
```

The `make backup-verify` target automates steps 1–4 for the *latest* dump
and compares row counts to the live container.

## Restoring after real data loss

If you've lost the live database and need to restore from the latest backup
back into the same container:

```bash
# 1. Bring up an empty forge-postgres (compose handles this on first up)
make up

# 2. Restore — note the safety flag, because we're overwriting "prod"
PHALANX_PG_TARGET=forge-postgres \
PHALANX_RESTORE_ALLOW_PROD=1 \
make backup-restore dump=backups/postgres/forge-<latest>.dump
```

The restore script refuses to overwrite `forge-postgres` unless
`PHALANX_RESTORE_ALLOW_PROD=1` is set explicitly. This is your only
guardrail against typo'ing a dump file into your live DB.

## Verifying the LaunchAgent

```bash
launchctl list | grep com.phalanx.backup    # should show the label + last exit code
launchctl print gui/$(id -u)/com.phalanx.backup   # full state (macOS 11+)
tail -100 backups/postgres/launchagent.log  # last few runs
```

If `last exit code` is non-zero, read `launchagent.log` to triage. Common causes:
- Docker Desktop not running → `forge-postgres` doesn't exist
- `PHALANX_BACKUP_REMOTE` unset → off-host step exits 1 (non-fatal, the local dump still landed)
- Disk full → `pg_dump` write fails

## Environment variables

| Variable                     | Default                         | Purpose                                           |
| ---------------------------- | ------------------------------- | ------------------------------------------------- |
| `PHALANX_BACKUP_DIR`         | `<repo>/backups/postgres`       | Where local dumps live                            |
| `PHALANX_BACKUP_RETENTION`   | `14`                            | Number of dumps to keep locally                   |
| `PHALANX_PG_CONTAINER`       | `forge-postgres`                | Source container for dumps                        |
| `PHALANX_PG_USER`            | `forge`                         | DB user                                           |
| `PHALANX_PG_NAME`            | `forge`                         | DB name                                           |
| `PHALANX_PG_TARGET`          | `forge-postgres-restore-test`   | Where `make backup-restore` writes                |
| `PHALANX_RESTORE_ALLOW_PROD` | (unset)                         | Required to allow restoring into `forge-postgres` |
| `PHALANX_BACKUP_REMOTE`      | (unset)                         | rclone remote spec, e.g. `r2:bucket/path/`        |
| `PHALANX_REMOTE_MIN_AGE`     | `30d`                           | Remote retention — older dumps deleted            |
| `PHALANX_VERIFY_DUMP`        | latest in dir                   | Override which dump `backup-verify` uses          |

## Tests

Static checks live in [`tests/unit/ops/test_backup_scripts.py`](../../tests/unit/ops/test_backup_scripts.py):
syntax, executability, plist parsing, schedule shape, Makefile target presence.

End-to-end verification is `make backup-verify` — requires Docker + a live
`forge-postgres` (or any postgres container the live container's volume is bound to).

## Operational risks still present

The following gaps remain after P0-1 and will be closed by other backlog items:

- **No ledger-level write durability between dump windows** → P0-2 (ledger.jsonl auto-export) closes the 6-hour gap by writing every dispatch's evidence to an append-only file at commit time.
- **No "wrong volume mounted" guard at `make up` time** → P0-3 (bootstrap pre-flight) closes the silent-volume-replacement failure mode that caused the 2026-05-11 data loss.
- **No alerting if a scheduled backup fails** → out of scope for P0-1; we rely on `launchctl list`'s exit code being checked manually until P2-11 (status endpoint).
- **rclone install is manual** → operator must install + configure rclone before off-host works. Scripted setup is intentionally not part of this item.
