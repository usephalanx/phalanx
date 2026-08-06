# FetchSandbox ↔ Phalanx — behavioral proof contract

**Status:** v1 documents what is ALREADY live in production. v1.1 is proposed and unimplemented.
**Owners:** FetchSandbox (`~/sandbox`) owns the oracle. Phalanx (`~/forge`) owns the runtime.
**Rule:** this contract is the only thing that crosses the boundary. Neither side may
assume anything about the other that is not written here.

---

## 0. The boundary, stated once

> FetchSandbox is the senior staff engineer. Phalanx is his MacBook.

The laptop does not know what the engineer is testing. It boots things, runs things,
captures what happened, and reports it honestly. That is the whole design constraint,
and it produces a hard rule:

**Phalanx MUST NOT interpret scenario semantics.**

| Concern | Owner | Why |
| --- | --- | --- |
| Which failure class this bug is | FetchSandbox | It's the behavioral oracle |
| What the invariant is ("fulfillment happens exactly once") | FetchSandbox | Domain knowledge lives in the brain/spec |
| Event ordering, duplicates, retries, timing | FetchSandbox | It simulates the provider |
| What "proven" means (the honest-green gate) | FetchSandbox | `app/flows/proof_gate.py` |
| The receipt shown to the user | FetchSandbox | It owns the user relationship |
| Isolation, materialization, provisioning | Phalanx | It owns the Docker socket |
| Executing the scenario and capturing evidence | Phalanx | It's the runtime |
| Reporting "I could not run this" | Phalanx | Honest failure is a runtime duty |
| Minting a verdict | **Neither — FetchSandbox alone** | Phalanx returns measurements, never judgements |

The corollary that matters: **there is no `IntegrationFixSpec` in Phalanx, no verdict model,
no failure taxonomy, no provenance table.** The moment Phalanx understands what a
"duplicate webhook" is, the oracle is split across two repos and they must ship together.
The wire payload stays flat and dumb on purpose.

---

## 1. Transport (live today)

```
POST https://<phalanx>/v1/run_probe
Header: X-Probe-Token: <PHALANX_PROBE_TOKEN>
```

Additionally IP-whitelisted at nginx to the FetchSandbox origin. Both gates are
independent; either one closes the door.

FetchSandbox client: `backend/app/flows/phalanx_probe.py`
Phalanx route: `phalanx/api/routes/run_probe.py` → Celery `cifix_sre` queue →
`phalanx/ci_fixer_v3/run_probe_task.py` (the socket-having worker; the API container
never touches Docker).

Sibling endpoints on the same contract: `/v1/find_bugs`, `/v1/fix_bug`, `/v1/synthesize_probe`.

### Request

| Field | Type | Notes |
| --- | --- | --- |
| `workspace_tar_b64` | string? | base64 `.tar.gz` of the app. Local/uncommitted/private code. Max 64 MB unpacked. |
| `git_url` + `git_ref` | string? | Alternative to the tar; shallow clone. |
| `probe_cmd` | string | The one command that runs the scenario. Opaque to Phalanx. |
| `setup_cmds` | string[]? | Overrides detected install commands. Opaque to Phalanx. |
| `timeout_s` | int | 1–600. Wall clock for `probe_cmd` only, not setup. |

Exactly one of `workspace_tar_b64` / `git_url` is required.

### Response

| Field | Type | Notes |
| --- | --- | --- |
| `available` | bool | **`false` means the run never happened.** Never conflate with a failing probe. |
| `exit_code` | int | The probe's real exit code, never synthesized. `-1` = could not run. |
| `stdout` / `stderr` | string? | Tail-truncated (see §4, Gap 1). |
| `setup_log` | object[] | Per-step provisioning record: step, cmd, exit_code, stderr tail. |
| `proof_events` | array? | The probe's own evidence (§3). `null` if absent or unparseable. |
| `error` | string? | Populated iff `available=false`. |

### Exit-code semantics — load-bearing

This is the single most important line of the contract, and today it exists only as a
docstring in `proof_gate.py` and in each probe file. It is normative:

| Probe exit | Meaning | Constant |
| --- | --- | --- |
| `0` | The invariant **HELD** | `HELD` |
| `1` | The invariant was **VIOLATED** — the bug reproduced | `VIOLATED` |
| `2` | The harness itself failed (boot, signature, dependency) | `INCONCLUSIVE` |
| anything else, or `available=false` | Could not measure | `INCONCLUSIVE` |

**A crashed probe is never a reproduction.** Exit 2 exists precisely so that a broken
harness cannot masquerade as a caught bug, which would let a meaningless fix mint a green.

Phalanx guarantees it passes the child's exit code through unmodified, and that `-1`
is reserved for "we could not run it".

---

## 2. The proof shape (live today)

`prove_fix` (FetchSandbox) is the whole loop:

```
buggy tar ──┬─> materialize ──> inject scenario probe ──> POST /v1/run_probe ──> exit 1 (VIOLATED)
            │
            └─> apply fix diff ─> inject SAME probe ────> POST /v1/run_probe ──> exit 0 (HELD)
                                                                    │
                                            honest-green gate ──────┘
                                            green ⟺ VIOLATED → HELD
```

Both legs run the **identical** probe file. Green is allowed only on a measured flip.
Any other combination — no reproduction, no fix, either leg inconclusive — is not green.
`app/flows/proof_gate.py` is pure logic with no I/O, so this rule is exhaustively testable.

---

## 3. Evidence envelope (`FS_PROOF_JSON`)

The probe prints a final line `FS_PROOF_JSON=<json-array>`; Phalanx parses the LAST such
marker and returns it as `proof_events`. The array's *shape is owned by FetchSandbox* —
Phalanx does a `json.loads` and a `isinstance(list)` check and nothing else.

### v1 (live)

```json
[{"label": "...", "request": {...}, "response": {"status": 200, "body": "..."}}]
```

### v1.1 (proposed) — needed for stateful scenarios

Request/response pairs cannot express "fulfillment happened twice". Add typed steps:

```json
[
  {"seq": 1, "kind": "request",  "label": "transaction.completed (first delivery)",
   "request": {...}, "response": {...}},
  {"seq": 2, "kind": "request",  "label": "transaction.completed (duplicate, same event_id)",
   "request": {...}, "response": {...}},
  {"seq": 3, "kind": "state",    "label": "fulfillments for txn_probe_1",
   "state": {"fulfillment_count": 2, "subscription_count": 1}},
  {"seq": 4, "kind": "assert",   "label": "exactly-once fulfillment",
   "invariant": {"name": "fulfillment_exactly_once", "expected": 1, "actual": 2, "held": false}}
]
```

`kind ∈ {request, state, assert}`. Backward compatible: v1 events have no `kind` and are
treated as `request`. This is a **FetchSandbox-side change only** — Phalanx already passes
the array through opaquely.

---

## 4. Gaps that block stateful scenarios

Three, found by reading the code. Only Gap 1 blocks the first slice.

### Gap 1 — evidence dies silently at 24 KB *(blocking)*

`run_probe_task.py` calls `_exec_in_container(..., stdout_cap=24000)`, and
`_exec_in_container` does `stdout.decode()[-stdout_cap:]` — a **tail** slice. The
`FS_PROOF_JSON=` marker is the last line, so if the JSON array itself exceeds the cap,
the slice cuts the *front* of the JSON. `json.loads` then fails, `_extract_proof` returns
`None`, and the receipt silently degrades to the exit-code fallback.

The verdict is unaffected (it comes from the exit code) — but the *proof the user reads*
quietly loses its detail, with no error anywhere. A five-step matrix with request bodies,
response bodies and state snapshots will cross 24 KB.

**Fix (additive, ~20 lines):** the probe also writes `/workspace/.fs_proof.json`; Phalanx
reads that file after the exec and prefers it, falling back to the stdout marker. Bounded
read (cap 1 MB), never raises, no behavior change when the file is absent.

### Gap 2 — no service lifecycle *(not blocking slice 1)*

`run_probe` runs exactly one command. There is no "boot the app, wait for ready, then
drive it". Today's probes work around this by loading the app **in-process** — the Paddle
probe `require()`s the real route module and mounts it on an express app.

That technique is Node-specific and won't carry to a Python/FastAPI app. When it's needed:
add optional `boot: {cmd, ready_cmd, ready_timeout_s}`, run `cmd` detached, poll `ready_cmd`
until exit 0, then run `probe_cmd`. Still one container; still opaque to Phalanx.

### Gap 3 — no dependent services *(deliberately deferred)*

No postgres/redis sidecar. Probes substitute a recorder in place of the DB module
(`require.cache` injection), which doubles as the state oracle. That's elegant for Node
and fine for slice 1, but doesn't generalize to apps whose bug lives in a real transaction
boundary — and "was the row inserted twice?" is exactly a transaction-boundary question.

This is the one that eventually needs real work (compose-style multi-container provisioning).
Do not build it until a scenario actually fails without it.

---

## 5. What the Paddle duplicate-delivery slice actually needs

Scenario: `transaction.completed` delivered more than once → duplicate fulfillment.
Matrix: normal · duplicate · delayed retry · out-of-order related event · handler timeout then retry.

| Work | Side | Status |
| --- | --- | --- |
| Deliver N events in order, with delays and duplicate `event_id`s | FetchSandbox | Probe-internal. **Zero Phalanx changes** — it's just JS in one process. |
| Record fulfillment/subscription writes | FetchSandbox | Existing `require.cache` recorder pattern. |
| Assert exactly-once + emit typed steps | FetchSandbox | Evidence envelope v1.1 (§3). |
| Register the scenario + keyword match | FetchSandbox | `corpus/scenarios/__init__.py`. |
| Carry >24 KB of evidence | **Phalanx** | Gap 1. The only Phalanx work in this slice. |

That is the headline: **the vertical slice is ~90% a FetchSandbox scenario file.** The
runtime is already capable of it.

---

## 6. Isolation from the CI Fixer — guaranteed structurally, not by discipline

The proof surface and the CI-fix pipeline share a package directory and nothing else.
Verified by import graph:

- `run_probe_task`, `find_bugs_task`, `fix_bug_task`, `synthesize_probe_task` are imported
  **only** by the `/v1` routes and by each other. **No `cifix_*` agent imports them; they
  import no agent.**
- They share exactly two things with the CI fixer: `provisioner.py` (provision / exec / stop)
  and `env_detector.py` — both pure, stateless, already shared, already covered by the v3
  test suite.
- The proof path writes **nothing** to postgres. No `runs`, no `tasks`, no `shadow_ledger`,
  no migrations. The receipt is FetchSandbox state. This is why no new models are needed —
  and why adding them would be a regression.
- Queue: reuses `cifix_sre` on the existing socket-having worker. No new worker, no new queue,
  no compose change.

Therefore the slice's Phalanx change is: **one new helper module + a ~3-line call site in
`run_probe_task.py`.** No agent, no DAG, no schema, no shadow code, no deployment topology.

Kill switches, both independent and already live: `FETCHSANDBOX_PHALANX_PROBE=0` on the
FetchSandbox side, and token + IP allowlist on the Phalanx side.

---

## 7. Why not a separate repository

A parallel `phalanx-integration-fixture/` with its own run/task/verdict/provenance models
was considered and rejected:

1. **It duplicates a pipeline we already run in production.** The probe surface is live,
   IP-whitelisted, and serving FetchSandbox today. A second one is a second thing to
   deploy, secure, and keep honest.
2. **It puts oracle semantics in the runtime.** New verdict + failure-taxonomy models in
   Phalanx move behavioral knowledge out of FetchSandbox — the exact coupling §0 forbids.
3. **The isolation it buys, we already have** (§6), enforced by the import graph rather
   than by a repo boundary.
4. **Statefulness is a downgrade here.** Phalanx's proof path is deliberately stateless;
   the receipt belongs to FetchSandbox. Adding provenance tables would make the runtime
   the system of record for something it doesn't own.

The legitimate concern behind that proposal — *don't destabilize the beta-ready CI Fixer* —
is real and is answered by §6, at far lower cost.

---

## 8. Implementation plan

### Stage 1 — Gap 1 only (Phalanx)

| File | Change |
| --- | --- |
| `phalanx/ci_fixer_v3/proof_evidence.py` | **new.** `read_proof_file(ws)` + `extract_proof(stdout)`; bounded, never raises. |
| `phalanx/ci_fixer_v3/run_probe_task.py` | ~3 lines: prefer file evidence, fall back to stdout. |
| `tests/unit/fsproof/test_proof_evidence.py` | **new.** file-present, file-absent, oversized, malformed, >24 KB regression. |

No route change, no schema, no compose, no CI-fixer file touched.

### Stage 2 — the scenario (FetchSandbox)

| File | Change |
| --- | --- |
| `backend/app/corpus/scenarios/paddle_duplicate_webhook.probe.js` | **new.** Five-case matrix, recorder DB, typed evidence, exit 0/1/2. |
| `backend/app/corpus/scenarios/__init__.py` | **new registry entry** + match keywords (`paddle` × `duplicate/idempot/replay/retry`). |
| `backend/tests/test_scenarios_paddle_duplicate.py` | **new.** Asserts VIOLATED on the buggy fixture, HELD on the fixed one. |

### Stage 3 — acceptance

Run `prove_fix` end-to-end against a deliberately non-idempotent Paddle handler:

- buggy leg exits `1`, evidence shows two fulfillments for one `event_id`
- fixed leg exits `0`, evidence shows one
- gate returns `proven`, `green_allowed=true`
- receipt renders the five-step transcript with before/after state
- Phalanx wrote nothing to postgres; container destroyed; no upstream push

Any leg inconclusive ⇒ **not green**. That is the whole product.

---

## 9. Versioning

This contract is versioned as a whole. Breaking changes require a new endpoint
(`/v1/run_probe` → `/v2/...`), never a silent field-meaning change — FetchSandbox and
Phalanx deploy independently, so at any moment one side is older than the other. Additive
optional fields with safe defaults are the only in-place change permitted.
