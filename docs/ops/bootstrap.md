# Bootstrap safety (P0-3)

`make up` now refuses to start the stack if any of seven safety conditions
fail. This prevents the 2026-05-11 class of incident, where `docker compose up`
silently created a fresh empty postgres volume and the Week 1 ledger evaporated.

## What gets checked

Run on every `make up`, in order:

### Pre-up — `scripts/preflight_check.sh`

| #  | Check                                          | Refusal type | Override                         |
| -- | ---------------------------------------------- | ------------ | -------------------------------- |
| 1  | Docker daemon is running                       | hard (exit 1)| start Docker Desktop             |
| 2  | docker-compose.yml has `name: phalanx-dev`     | hard (exit 1)| restore the line                 |
| 3  | `.env` exists                                  | hard (exit 1)| `cp .env.example .env`           |
| 4  | DATABASE_URL points to `@postgres:5432/forge`  | soft (exit 2)| `PHALANX_ALLOW_EXTERNAL_DB=1`    |
| 5  | Postgres volume exists                         | soft (exit 2)| `PHALANX_ALLOW_FRESH_BOOT=1`     |
| 6  | `backups/postgres/` exists and is writable     | hard (exit 1)| fix filesystem perms             |
| 7  | `ledger.jsonl` path is writable                | hard (exit 1)| fix filesystem perms             |

### Post-up — `scripts/beat_health.sh` (added 2026-05-20)

After `docker compose up -d`, `make up` verifies that `forge-beat` is
**actually dispatching** scheduled tasks. Catches the silent-failure
mode discovered 2026-05-20: a beat container that boots, prints its
banner, then crashes inside the scheduler init — leaving the
reconciler, stuck-task detector, and blocked-run watchdog dormant
indefinitely while every other surface looks healthy.

| #  | Check                                          | Refusal type | Override                       |
| -- | ---------------------------------------------- | ------------ | ------------------------------ |
| 8  | `forge-beat` container is running              | hard (exit 1)| start the container            |
| 9  | `forge-beat` has logged `Sending due task`     | soft (exit 2)| `PHALANX_SKIP_BEAT_HEALTH=1`   |
|    | within `PHALANX_BEAT_MAX_WAIT_S` (default 180s)|              |                                |

Run standalone any time with `make beat-health`.

Exit codes:
- **0** — all passed, `docker compose up` runs.
- **1** — hard refusal; operator must fix.
- **2** — soft refusal; overridable with the listed env flag.

Warnings (don't refuse, do print):
- Postgres volume created < 5 min ago — suspicious; you may have lost the previous volume without realizing.
- `PHALANX_ALLOW_EXTERNAL_DB=1` is set — your services are talking to an external DB.

## Normal startup

```bash
make up
```

Output on a healthy machine:

```
┌──────────────────────────────────────────────────────────┐
│  Phalanx preflight check                                 │
└──────────────────────────────────────────────────────────┘

  ✓ docker daemon is running
  ✓ compose project name is phalanx-dev
  ✓ .env exists
  ✓ DATABASE_URL points to compose-internal `postgres:5432/forge`
  ✓ postgres volume found: phalanx-dev_forge-postgres-data
      created : 2026-05-04T12:00:00Z  (7 d ago)
      mount   : /var/lib/docker/volumes/phalanx-dev_forge-postgres-data/_data
  ✓ backups/postgres/ is writable
  ✓ ledger.jsonl path is writable: /Users/raj/forge/ledger.jsonl

✅ All preflight checks passed.
```

Followed by `docker compose up -d`. Nothing fancy.

## First-time setup (a genuinely fresh environment)

On a brand-new machine the postgres volume does not exist yet. The preflight
will refuse with exit code 2. That's intentional — the operator must
acknowledge that there is no prior data to preserve.

```bash
# First time only — this is the only case where it's safe.
PHALANX_ALLOW_FRESH_BOOT=1 make up
```

The script prints a warning and tells you what to do once a dispatch has
landed real data:

```
⚠  Preflight passed with 1 warning(s).

Fresh boot mode: after first dispatch lands, run:
    make backup
    make ledger-verify
```

Take that seriously — those two commands are what makes the new environment
recoverable from the next incident.

## Data-loss prevention: what happens when the preflight saves you

Scenario: you just upgraded Docker Desktop and the compose project name
default shifted. You run `make up`. The preflight notices:

```
  ✗ postgres volume not found: phalanx-dev_forge-postgres-data
      DATA LOSS RISK: `docker compose up` will create a fresh empty volume.
      the previous volume (if any) is now orphaned.

❌ Preflight REFUSED — 1 safety check(s) tripped

To proceed:
  • If this is a clean first-time setup: PHALANX_ALLOW_FRESH_BOOT=1 make up
  • If data should still exist, DO NOT continue — investigate first:
      docker volume ls | grep postgres
      make backup-list                  # available restores
      cat docs/ops/backups.md           # restore drill

docker compose up will NOT run.
```

**Do not set `PHALANX_ALLOW_FRESH_BOOT=1`** until you've checked
`docker volume ls | grep postgres` for orphaned volumes. The previous
volume may still be on disk under a different project-name prefix —
that's recoverable; a fresh boot on top of it is not.

## Recovery: preflight refused, what do I do?

### Exit 1 (hard refusal)

| Cause                            | Fix                                                                       |
| -------------------------------- | ------------------------------------------------------------------------- |
| Docker daemon not running         | Start Docker Desktop; wait for the whale icon to settle; re-run `make up`. |
| Compose project name mismatch     | Restore `name: phalanx-dev` on line 7 of `docker-compose.yml`.            |
| `.env` missing                    | `cp .env.example .env`, fill in keys.                                     |
| `backups/postgres/` not writable  | Check perms on the repo dir. Usually fixable with `chmod u+w backups/postgres/`. |
| `ledger.jsonl` path not writable  | Same fix; or `unset PHALANX_LEDGER_JSONL_PATH` if you'd overridden it.    |

### Exit 2 (soft refusal — overridable)

| Cause                                      | Override                                  | When safe                                                |
| ------------------------------------------ | ----------------------------------------- | -------------------------------------------------------- |
| Postgres volume missing                    | `PHALANX_ALLOW_FRESH_BOOT=1`              | only on a genuinely fresh setup with no data to preserve  |
| DATABASE_URL points outside compose         | `PHALANX_ALLOW_EXTERNAL_DB=1`             | only if you intentionally configured an external DB       |

## When `PHALANX_ALLOW_FRESH_BOOT=1` is the right call

- Brand-new machine, never run Phalanx before.
- After deliberate `docker compose down -v` (you wanted everything wiped).
- After a deliberate `docker volume rm phalanx-dev_forge-postgres-data`.
- In CI / ephemeral environments that never have prior data.

It is **never** the right call when:
- The previous stack was working yesterday and now isn't.
- You just upgraded Docker / docker-compose / OS.
- A teammate gave you a checkout and you're running it for the first time on a machine where prior data *might* exist.
- You see "very new" warnings in the preflight output for the existing volume.

In any of those cases, run `docker volume ls | grep postgres` first.

## Bypass (emergency only)

```bash
PHALANX_SKIP_PREFLIGHT=1 make up
```

This bypasses all checks. Print a yellow warning, do not run the preflight.
Reserved for cases where the preflight itself is broken — for example a bug
in the script — and you need to start the stack without it.

If you find yourself reaching for this, file an issue against the preflight
script and document what tripped you up.

## Test seams (for the harness)

These are documented in [scripts/preflight_check.sh](../../scripts/preflight_check.sh) but
should never be set in normal operation:

- `PHALANX_TEST_VOLUMES=name1,name2` — claim these volumes exist
- `PHALANX_TEST_VOLUME_CREATED_AT=ISO` — override CreatedAt for stubbed volumes
- `PHALANX_TEST_DOCKER_INFO_OK=1|0` — claim docker daemon is/isn't running
- `PHALANX_TEST_REPO_ROOT=/path` — anchor preflight to a temp repo

Tests in [tests/unit/ops/test_preflight_check.py](../../tests/unit/ops/test_preflight_check.py)
exercise every refusal path through these seams.

## Operational risks still remaining

P0-3 prevents the data-loss class. It does not solve:

1. **Detection of "different volume mounted under the same name".** If someone
   `docker volume rm`'d the real volume and then created an empty one with the
   same name, the preflight will see it exists and pass. Mitigation: P0-1
   backups are the recovery path. P2 candidate: pin a volume-identity fingerprint
   (e.g. a known sentinel row in `shadow_ledger`) and verify it.
2. **No protection during `docker compose down -v`.** That command intentionally
   destroys volumes; the operator must read what they typed. P0-3 only runs
   on `up`. Future: add a `preflight-down` check on `make down` that warns
   if `-v` is in the args.
3. **No protection on direct `docker compose up`.** Operators bypassing
   `make up` skip the preflight entirely. The `Makefile` is the enforcement
   point. Consider a `pre-commit` hook or shell alias for paranoid setups.
4. **Compose project name is checked statically against a hardcoded value
   (`phalanx-dev`).** If we ever rename, both the docker-compose.yml and the
   preflight script must change in lockstep. Documented; not enforced.
5. **macOS-specific date parsing in `humanize_age`.** Linux date binary uses
   different flags. The age display falls back to empty silently. Functional
   on Linux — just less informative. Not a safety risk.
6. **Test seams could be left set in a real shell.** If `PHALANX_TEST_VOLUMES=foo`
   is exported in your `.zshrc` by accident, the preflight will trust it. This
   is acceptable risk; the variable name is explicit, and forgetting it is a
   pebble compared to the boulder it replaces.
