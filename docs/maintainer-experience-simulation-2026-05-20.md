# Maintainer-experience simulation — internal beta findings

**Date:** 2026-05-20
**Scope:** simulate end-to-end maintainer journey using existing ledger rows + actual surface inventory. No new UX built; this document IDs the gaps so we can decide what to build next.

## Method

For each of the nine evaluation axes the user listed, I walked through what a maintainer would actually do today, surface by surface, using:

- The actual ledger rows in postgres (8 rows from the trailing week — mix of SAFE_ESCALATE with real TL, FAILED_SANDBOX_SETUP_APT, and reconciled FAILED_INFRA_WORKER_HANG).
- The actual HTTP endpoints + GitHub-side surface (`/webhook/github`, `_post_closed_loop_comment`).
- The actual operator-side CLI (`phalanx shadow show`, `make ledger-tail`, `make beat-health`).
- The actual install path (DB INSERT + PAT seed).

Findings are organized by flow. Each flow is rated **Friction**: 🟢 acceptable / 🟡 confusing / 🔴 blocker for beta.

---

## Flow 1 — Install / setup

| Step the maintainer would take | What actually happens | Verdict |
| ------------------------------ | -------------------- | ------- |
| 1. Read landing page or repo README | (no doc exists for "how do I add Phalanx to my repo") | 🔴 |
| 2. Click "Install" button | (no GitHub App; no install URL) | 🔴 |
| 3. Authorize permissions | (no app manifest; permissions ad-hoc from PAT) | 🔴 |
| 4. Verify install on repo settings | (would not appear in the repo's "Apps" page because no app) | 🔴 |
| 5. First webhook fires on next failed CI | Endpoint exists at `/webhook/github` but no app dispatches to it; would require the maintainer to manually configure a webhook URL in repo settings + a shared secret | 🔴 |

**Verdict: 🔴 blocker.** The install/setup flow does not exist. Today's enrollment path is `INSERT INTO ci_integrations` with a PAT — operator-only, not maintainer-feasible. Internal sim CANNOT begin without first manually enrolling via SQL; that itself is a confidence-killer.

**Smallest fix that doesn't expand architecture:** write a CLI tool `phalanx ci-integration add --repo <owner/name> --token <pat>` so the operator can enroll a repo without typing SQL. Internal beta only — still not maintainer-self-serve. Real beta needs a GitHub App.

---

## Flow 2 — Permission clarity

The PAT-based enrollment uses a classic PAT with `admin:enterprise, admin:gpg_key, admin:org, admin:org_hook, admin:public_key, admin:repo_hook, admin:ssh_signing_key, audit_log, codespace, copilot, delete:packages, delete_repo, gist, notifications, project, repo, user, workflow, write:discussion, write:network_configurations, write:packages` (from `gh auth status` earlier).

A maintainer reading "we'll use your PAT to do shadow-mode CI analysis" would correctly object:
- Why do you need `delete_repo`?
- Why do you need `admin:org`?
- Why do you need `write:packages`?

Shadow mode requires only `contents:read`, `actions:read`, `pull_requests:read`, `checks:read`. The token used today is wildly over-scoped.

| Concern | Today | Beta-acceptable |
| ------- | ----- | --------------- |
| Token scope minimum | classic PAT with full admin | fine-grained PAT or App with 4 read scopes |
| Permission rationale doc | none | required (one paragraph per scope) |
| Off-switch | `UPDATE ci_integrations SET enabled=false` | revoke installation in GitHub UI |

**Verdict: 🔴 blocker.** Today's permission surface would not pass a security review at any maintainer org. Even friendly maintainers will balk at "give me a token with delete_repo to look at CI logs."

**Smallest fix:** document the **minimum** scopes a maintainer must grant, and accept fine-grained PATs (not classic). The code already only uses read APIs; it just demands a too-broad token.

---

## Flow 3 — Evidence readability (reviewing SAFE_ESCALATE)

Concrete simulation: maintainer of `python/mypy` learns Phalanx looked at PR #21316 (workflow 25767602205). They want to see what Phalanx concluded.

**What they'd want to see:**
- One screen with the verdict, the diagnosis, the file/line implicated, the cost, the time, and an audit trail of how the conclusion was reached.

**What exists today:**
- `phalanx shadow show <ledger_id>` — operator CLI, dumps the full row as JSON.
- The row's `phalanx_root_cause` field is genuinely good: "The new reachable-branch logic in `mypy/semanal_namedtuple.py` still fails to collect `y` from nested `if` blocks inside a `NamedTuple`..." — maintainer-quality.
- BUT the maintainer doesn't have CLI access, doesn't have a DB connection, doesn't get an email.

The diagnosis exists; the **delivery channel** doesn't.

| Concern | Today | Beta-acceptable |
| ------- | ----- | --------------- |
| Where the diagnosis lives | `shadow_ledger.phalanx_root_cause` (postgres) | a maintainer-visible artifact (PR comment, dashboard URL, or email) |
| Format | one-paragraph string | one-paragraph + the file:line + the exact failing test name |
| Confidence display | numeric `phalanx_confidence` 0.0-1.0 + classification reason | needs human-readable label ("Refused to ship — would have hedged at 40%, below ship threshold") |
| Provenance display | `phalanx_provenance` JSONB with tl_task_id + sequence_num | a "Verify this audit trail" link or query the maintainer can run |

**Verdict: 🟡 confusing.** The evidence itself is excellent. Maintainers can't see it.

**Smallest fix:** add a `_post_shadow_safe_escalate_comment(ci_run, ledger_row)` analogue to `_post_closed_loop_comment` — posts a comment on the PR with the diagnosis. Body templated from `phalanx_root_cause` + a short "Phalanx examined this CI failure in shadow mode. No code was pushed. Verdict: SAFE_ESCALATE (confidence X). Reasoning: ..."

---

## Flow 4 — Escalation readability (the empty-diagnosis edge cases)

Real ledger evidence: most `SAFE_ESCALATE` rows from today's batches have **synthesized** `phalanx_root_cause`:

> "Phalanx escalated: TL emitted confidence=0.0 without a root_cause (canonical low-confidence escalate)."

A maintainer reading that gets ZERO actionable info. They learn that Phalanx didn't ship and that's it.

The architecture is correct (synthesized > NULL), but the synthesized text is operator-jargon, not maintainer-friendly.

| Concern | Today | Beta-acceptable |
| ------- | ----- | --------------- |
| Synthesized fallback text | "tl_zero_confidence", "calibration_failed", etc. (jargon) | plain English: "Phalanx examined this but couldn't ground a fix because the CI log was truncated before the actual traceback. Manual review needed." |
| Distinction between "TL refused after grounded analysis" and "TL couldn't run" | only visible via `provenance.tl_task_status` field | needs to be the FIRST thing the maintainer sees |
| FAILED_SANDBOX_SETUP_APT diagnostic | grounded + actionable but in operator terms | maintainer doesn't care about apt failures inside Phalanx's sandbox; they need: "Phalanx couldn't reproduce your CI failure in its sandbox — skipping analysis for this PR" |

**Verdict: 🟡 confusing.** SAFE_ESCALATE shape varies: real grounded refusal vs. synthesized fallback. Maintainer-facing text needs to plainly distinguish. Internally we already have the info (provenance + classification reason); just needs presentation.

---

## Flow 5 — Operational trust signals

The maintainer's mental model: "Is this thing still alive? Is it producing wrong results silently?"

What today's stack provides:

| Signal | Operator-visible | Maintainer-visible |
| ------ | ---------------- | ------------------ |
| Beat scheduler firing | ✅ `make beat-health` | ❌ |
| Reconciler healing stale rows | ✅ ledger query | ❌ |
| Per-run cost cap | ✅ `phalanx_cost_usd` | ❌ |
| Side-effect audit | ✅ branch/PR diff | ❌ |
| Worker queue depth | ✅ redis-cli | ❌ |
| Run history on a repo | ✅ ledger SQL | ❌ |
| Provenance audit | ✅ JSONB query | ❌ |

**Verdict: 🔴 blocker.** Maintainer has no way to know if Phalanx is even running. If we onboard 3-5 maintainers today, each one will need to message the operator (you) to ask "did Phalanx see PR #X?" — and the operator runs SQL to find out. That doesn't scale beyond ~2 maintainers.

**Smallest fix that doesn't expand architecture:** a single read-only HTTP endpoint `GET /api/shadow/runs?repo=<owner/name>` that returns the last N ledger rows for a repo. Maintainer hits the URL, sees the runs Phalanx attempted. Doesn't need a UI — JSON is fine for internal beta.

---

## Flow 6 — Reviewing SHIPPED_PROPOSED

No SHIPPED_PROPOSED has landed on a third-party repo. Cannot simulate from real data. From the schema:

- `phalanx_proposed_patch` (text) — a unified diff string.
- `phalanx_affected_files` (JSONB array) — list of files touched.
- `phalanx_confidence` (≥0.7 for SHIPPED).
- `phalanx_provenance.tl_task_*` — full audit trail of how TL produced the fix_spec.

What a maintainer reviewing a SHIPPED would want:
1. The diff in GitHub's standard PR diff view (not a text blob).
2. The test that previously failed + the test result post-patch.
3. The TL's reasoning verbatim.
4. The engineer's verification log (what command was run, what output, did the test go green).
5. A clear "this was generated by Phalanx in shadow mode; no code was pushed; here's the proposed change for your review" framing.

**Today none of those are presented as an artifact.** The maintainer would need to read JSON.

**Verdict: 🔴 blocker** when SHIPPED_PROPOSED arrives. The fix recipe: a draft PR (or a PR comment with the patch as a suggestion block) that contains the diff + the verification log + the reasoning. Same pattern as `_post_closed_loop_comment`, just rendered for shadow mode.

---

## Flow 7 — Stuck/reconciled runs

Real simulation possible: the four 2026-05-20 ledger rows where the same `(repo, workflow_run_id)` got two ledger entries — first a stale CLI-snapshot (`PENDING` → `previous_verdict=PENDING`), later a reconciler-healed `FAILED_SANDBOX_SETUP_APT` and then a `FAILED_INFRA_WORKER_HANG`.

Maintainer pulling the ledger for that workflow would see TWO rows for the same (repo, wfid). Today the schema correctly uses `attempt_number` to disambiguate — but the maintainer-facing presentation must:

- Show only the latest attempt by default.
- Surface "this was originally PENDING and was healed by the reconciler" as a tooltip / audit trail, not as the headline.
- Make `reconciled_reason` and `previous_verdict` invisible by default; available on click-through.

**Verdict: 🟡 confusing.** The data is honest; presenting raw rows would confuse a maintainer ("why did Phalanx say two different things?"). Easy fix: any maintainer-facing view defaults to `latest_per_workflow` (helper already exists in `phalanx/shadow/ledger.py`).

---

## Flow 8 — Runtime observability

What can the maintainer see without operator help?

- Their repo's CI fails as usual — that's GitHub-native.
- Phalanx receives the webhook (if installed) — invisible.
- Phalanx dispatches a shadow run — invisible.
- Sandbox spawns, TL runs, engineer runs — invisible.
- Verdict written to ledger — invisible.

**The maintainer cannot tell Phalanx ran at all.** This is partly by design (shadow mode = no side effects) but goes too far for trust: a maintainer who can't see ANY signal that the system engaged with their repo has no basis to trust or evaluate it.

**Verdict: 🔴 blocker.** "Did Phalanx look at my PR?" must have an answer the maintainer can find.

**Smallest fix:** the SAFE_ESCALATE PR comment from Flow 3 doubles as this signal. Adding a single low-volume PR comment per dispatch ("Phalanx examined this CI failure. Verdict: ..., no code was pushed") solves Flows 3, 4, and 8 simultaneously.

---

## Flow 9 — "Would I trust this system?"

Hypothetical maintainer post-onboarding, three days in:

| What they observe | Trust trajectory |
| ----------------- | --------------- |
| "Phalanx didn't push any code. Their docs said it wouldn't." | + |
| "They diagnosed my PR's failure correctly. I checked." | + (large) |
| "When their sandbox couldn't reproduce my CI failure, they said so honestly instead of guessing." | + |
| "I don't know when they looked at my PRs or how often." | − |
| "I asked them to disable Phalanx on my repo. The answer was 'I'll run an SQL update'." | − (large) |
| "Their reasoning text is jargon — 'tl_zero_confidence', 'calibration_failed'. Not English." | − |
| "I have no way to flag a wrong diagnosis back to them." | − |

Net: a thoughtful maintainer trusts the **architecture** today (honest refusals, no side effects, real diagnosis when grounded) but cannot trust the **operations** because there's no maintainer-side visibility, no off-switch UX, no feedback channel.

**Verdict: 🟡 — they'd give us a chance because they like that we don't push, but they'd quietly stop paying attention after the first week unless we close the visibility gap.**

---

## Summary — what blocks internal beta vs. external beta

**Blocking internal sim (i.e. operator drives, maintainer just watches):**
- Nothing blocks. We CAN run today's stack against rnagulapalle-controlled repos right now, dispatch shadow runs, and email maintainers screenshots of ledger rows. That's the absolute minimum.

**Blocking real internal beta (maintainers actively participating):**
- Flow 1 (install/setup — no UX): add `phalanx ci-integration add` CLI as a stopgap.
- Flow 2 (permission scope): document minimum scopes, accept fine-grained PATs.
- Flow 5 (no observability): add a single read-only HTTP endpoint.

**Blocking external beta (3-5 trusted maintainers):**
- Real GitHub App with proper scopes (Flow 1, Flow 2).
- PR comments for SAFE_ESCALATE + SHIPPED_PROPOSED (Flows 3, 4, 6, 8 — one comment-poster solves four flows).
- Off-switch UX (Flow 9): web-form or a "disable" command the maintainer can run themselves.
- Feedback loop (Flow 9): a way for maintainers to flag wrong diagnoses back.

**The architecture is ready. The maintainer UX is not.**

Per your "do not expand architecture" rule, I am proposing — not building — these. Each one is a small, additive feature that fits inside the existing surface (extending `_post_closed_loop_comment` pattern, adding a CLI subcommand, adding a single HTTP endpoint). None of them touch shadow-mode safety, the ledger, the reconciler, the provenance, or any agent logic.

---

## Concrete next-step recommendations (in priority order)

1. **PR-comment for shadow-mode verdicts** — single feature, closes Flows 3/4/6/8. Mirror `_post_closed_loop_comment` for shadow runs. Smallest unit of trust delivery.
2. **`phalanx ci-integration add/remove/list` CLI** — closes Flow 1 for internal beta. ~50 lines of code.
3. **Document minimum PAT scopes** — closes Flow 2. ~30 minutes of writing.
4. **Read-only `/api/shadow/runs?repo=...` endpoint** — closes Flow 5 + part of Flow 9.
5. **GitHub App** — closes everything else for external beta. Largest item; bounded by GitHub's app manifest scaffolding.

Holding here. Internal sim using rnagulapalle-controlled repos is feasible *today* against the existing stack — just understand that the experience is "operator-driven, maintainer-passive" with no UX layer between them.
