# Invite-only pilot — operator runbook

**Phase:** 3–5 maintainer pilot, shadow-mode only.
**Operator (you):** rnagulapalle.
**Architecture status:** frozen. No new systems for the duration of this pilot.

This runbook is a living document. Update the feedback log inline as each maintainer reacts.

---

## 1. Maintainer profile — who to invite

### What the data tells us about Phalanx's working envelope

Across the last 30 days of dispatches that produced *grounded TL output* (the only kind that produces a maintainer-readable comment):

| Repo | Verdict | Avg confidence | Avg cost | Avg time |
| ---- | ------- | -------------- | -------- | -------- |
| `usephalanx/phalanx-ci-fixer-testbed` | SHIPPED_PROPOSED | 97% | $0.29 | 432s |
| `usephalanx/humanize` | SHIPPED_PROPOSED | 93% | $0.57 | 533s |
| `python/mypy` | SAFE_ESCALATE | 0% (honest refusal) | $0.51 | 576s |
| `pytest-dev/pytest` | SAFE_ESCALATE | 0% (honest refusal) | $0.84 | 365s |

The shape: **Phalanx works best on small-to-medium Python libraries with mechanical failure modes (lint, format, type narrowing, single-file regressions). It refuses honestly on giant repos with multi-platform / multi-Python matrix failures.** That's the slice we should pilot in.

### Pilot maintainer profile

Pick maintainers whose repo + workflow profile lines up with the envelope:

**Strong fit ✅**
- Python library, ≤30k LOC, single language (no Rust/C extensions for the test paths)
- Active CI on every PR (≥5 failing PR-event runs/month)
- Failures often have a clear, localized cause (one file, one rule, one test)
- The maintainer is technical and skeptical — will tell you "this diagnosis is wrong" rather than be polite
- You have direct, low-friction contact (DM, Discord, email)
- The repo accepts AI-assisted contributions in principle (check CONTRIBUTING.md)

**Weak fit ⚠️**
- Repo uses heavy compilation (mypyc, Cython, native ext) — sandbox setup will hit FAILED_SANDBOX_SETUP_* often
- Repo has Windows-only or macOS-only failures — Phalanx can't reproduce
- CI is dominated by flaky/timing tests
- Maintainer is anti-AI categorically (forces unhealthy interaction)
- You don't actually talk to them — feedback loop will be slow or dead

**Bad fit ❌ (do not invite)**
- Repo where Phalanx posting a wrong comment would cause real PR-comment drama (high-visibility, contentious project)
- Repo with strict no-bot rules in CONTRIBUTING.md
- Repos you have a paid commercial relationship with — confuses incentives

### Recommended pilot mix (3–5 maintainers)

The pilot should test trust across distinct shapes:

1. **One pilot you already trust 100%** — your own work (`usephalanx/humanize`, `usephalanx/inflect`) where a wrong diagnosis is no-cost. This is your continuous validation surface, not your retention test.
2. **Two friendly external maintainers, small repos** — these are the retention test. They're people you talk to weekly, who'd take 5 min to give you honest feedback.
3. **One skeptical-but-fair external maintainer** — someone you respect technically who will press hard. If they keep Phalanx installed, that's strong signal.
4. **Optional fifth: an org member** — someone at a company with security review processes. Tests the "would this pass an org review?" question.

Do NOT invite more than 5 simultaneously. Each interaction is operator-attention-intensive. 3 is fine; 5 is the cap.

---

## 2. Outreach templates

Four templates. Pick by relationship distance. **Never paste verbatim** — adapt to the specific person.

### A. Warm (you DM them weekly)

> Hey [name] — quick ask. I've been working on a system called Phalanx that watches failed CI runs on a PR, analyzes the failure in a sandboxed clone of the repo, and posts a single read-only PR comment with what it thinks went wrong. Today on [usephalanx/humanize] it correctly diagnosed an i18n regex regression — full diff in the comment. Zero pushes, zero branches, opt-in per repo, off-switch is a single @-mention.
>
> I'm doing an invite-only pilot (3–5 maintainers I trust). Would you put it on one of your repos for ~2 weeks? Pick something mid-sized. I want unfiltered feedback — if a comment is wrong or annoying, tell me, I iterate.
>
> Two opt-outs: @-mention me on the PR to disable that repo, or revoke the PAT I'd ask for. Takes 10 seconds.

### B. Cool (technical contact, not close)

> Hi [name] — saw your work on [specific thing they did]. I'm running a small invite-only pilot of an AI agent (Phalanx) that analyzes failed CI in shadow mode — reads the failure, posts one PR comment with a proposed diagnosis or honest refusal. No code is ever pushed to the repo.
>
> Looking for 3–5 maintainers who'd put it on a repo for 2 weeks and tell me what's wrong with the experience. Example output from this morning: [paste the inflect or humanize comment URL].
>
> Asking for a fine-grained PAT scoped to one repo, four read permissions, one write (PR comments). Full list in [docs/ops/permissions.md]. Worth your time?

### C. Skeptical (you want them to press hard)

> [Name] — calibrated pessimism request. I'm running an invite-only pilot of Phalanx (CI failure analysis, shadow-mode, posts one PR comment). I want a few maintainers who will hate-read it and tell me exactly where it falls apart.
>
> Bar: if it produces a comment that you find genuinely useful even once across 2 weeks of CI failures, I want to know. If it produces five wrong/annoying ones in a row, I also want to know — that's the signal I'm chasing.
>
> Off-switch is a single @-mention. PAT is fine-grained, scoped to one repo. Worth 20 min over two weeks?

### D. Follow-up after first comment lands

> [Name] — Phalanx posted its first comment on your repo: [link]. Two questions, however brief:
>
> 1. Was the diagnosis right, wrong, or partial?
> 2. Would you have wanted MORE detail, LESS detail, or different framing?
>
> No expectation of a long reply. One word per question is fine.

---

## 3. Onboarding script (when a maintainer says yes)

Run this script per maintainer. Should take 5 minutes of your time, 0–2 minutes of theirs.

```bash
# 1. Confirm the repo + the maintainer's preferred handle
REPO=owner/repo                 # ask them
HANDLE=their-gh-handle          # if different from repo owner

# 2. Ask them to create a fine-grained PAT
#    Pointer: docs/ops/permissions.md §"Fine-grained PAT (recommended for the pilot)"
#    5 scopes for opt-in-to-comments path:
#      Actions: read, Contents: read, Pull requests: read+write, Metadata: read

# 3. They paste the PAT to you (DM, encrypted note — never email)
THEIR_PAT="..."

# 4. Enroll the repo
docker exec -e PAT="$THEIR_PAT" forge-postgres psql -U forge -d forge -c "
INSERT INTO ci_integrations (
  id, repo_full_name, ci_provider, github_token, auto_commit, max_attempts,
  enabled, auto_merge, min_success_count, cifixer_version,
  maintainer_comments_enabled, created_at, updated_at
) VALUES (
  gen_random_uuid(), '$REPO', 'github_actions', '$PAT', false, 1,
  true, false, 1, 'v3', true, NOW(), NOW()
)
ON CONFLICT (repo_full_name) DO UPDATE SET
  github_token = EXCLUDED.github_token,
  maintainer_comments_enabled = true,
  updated_at = NOW();
"

# 5. Verify they're in
docker exec forge-postgres psql -U forge -d forge -c \
  "SELECT repo_full_name, enabled, maintainer_comments_enabled
     FROM ci_integrations WHERE repo_full_name = '$REPO';"

# 6. Add an entry to the pilot feedback log (see §4 below)

# 7. Confirm back to them:
#    "You're in. Phalanx will analyze the next CI failure on a PR. The
#     comment will appear on the PR itself. If you want me out, @-mention
#     me on any PR or revoke the PAT — both work."
```

**Time-box: 2 weeks per maintainer.** At day 14, either they renew explicitly or you disable their integration. Renewal must be opt-in, not opt-out — drift kills pilots.

---

## 4. Feedback tracking — the pilot log

Track one row per maintainer. Update in place as feedback arrives. The categories the user listed map directly to the columns.

### Template (paste into `docs/pilot/feedback-log.md`)

```markdown
# Pilot feedback log

## [maintainer_name] — [owner/repo]

- **Enrolled:** YYYY-MM-DD
- **Relationship:** [warm / cool / skeptical / org]
- **Repo profile:** [size, language, CI shape — 1 line]
- **Status:** [ACTIVE / RENEWED / DISABLED]
- **Renewal at day 14:** [yes / no / pending]

### Comments posted

| Date | PR | Verdict | Conf | Phalanx diagnosis (1-line) | Maintainer reaction |
| ---- | -- | ------- | ---- | -------------------------- | ------------------- |
|      |    |         |      |                            |                     |

### Feedback by category

| Category | Reaction (verbatim or paraphrased) | Action taken |
| -------- | ---------------------------------- | ------------ |
| Trust    |  | |
| Confusion | | |
| Usefulness | | |
| Annoyance | | |
| Wording  |  | |
| Escalation clarity | | |

### Retention signal at day 14

- [ ] Would keep installed: yes / no / not yet sure
- Reason in their own words:
```

### Per-comment audit (operator-side, daily)

```bash
docker exec forge-postgres psql -U forge -d forge -A -F'|' -c "
SELECT
  sl.repo, sl.pr_number, sl.phalanx_verdict, sl.phalanx_confidence,
  LEFT(sl.phalanx_root_cause, 60) AS rc_head, sl.created_at::timestamp(0) AS at
FROM shadow_ledger sl
JOIN ci_integrations ci ON ci.repo_full_name = sl.repo
WHERE ci.maintainer_comments_enabled = true
  AND sl.created_at > NOW() - INTERVAL '24 hours'
ORDER BY sl.created_at DESC;
"
```

Eyeball this once a day. Any verdict with confidence ≥0.7 should be spot-checked against the actual posted comment.

---

## 5. Pattern-recognition framework (pending data)

Once we have 5–10 maintainer-touch comments across the pilot, look for these patterns. Premature today; flag as data-required.

| Pattern to watch for | Hypothesis | Possible small fix |
| -------------------- | ---------- | ------------------ |
| Maintainers consistently mis-read confidence | The percentage feels too precise | Replace `93%` with `high confidence` / `low confidence` labels |
| Multiple maintainers ask "but did you read X?" | "What was examined" line is too generic | Name the failing job + the line count of the log |
| Multiple "this diagnosis is wrong" replies on a specific kind of failure | TL has a systematic blind spot | Track failure shape, escalate to model-level investigation (out of pilot scope) |
| Comment is ignored entirely | Subject line / first 80 chars not compelling | Refine the headline; the AI banner may be too long |
| Maintainer @-mentions to disable | Trust-loss event | Pull the ledger row + the comment, identify the specific trigger, fix in next polish pass |
| "Cool but I don't need this" | Pilot maintainer doesn't have failing CI often enough | Replace with a more active maintainer; don't fight the envelope |

**No fixes get applied without ≥2 maintainers reporting the same pattern.** Single data points are noise.

---

## 6. Pre-mortem + escalation triggers

Before the pilot starts, agree on what would END it.

### What can go wrong

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| Maintainer gets a confidently-wrong diagnosis | Medium | High (trust loss, public) | The PAT is fine-grained → only this repo. Apologize, take the @-mention, disable. |
| Apt-mirror flake during pilot run | High | Medium (suppressed, but rate of useful comments low) | Suppression matrix protects against fake comments. Operator monitors hit-rate. |
| Maintainer thinks `rnagulapalle` wrote the comment, not the AI | Low after polish | Medium | The AI banner is the first line of every comment. If a maintainer reports confusion, it's a real signal. |
| Comment posted then deleted (sentinel breaks) | Low | Low | Idempotency is best-effort; rerun would re-post. Acceptable for pilot. |
| Pilot maintainer gets too few CI failures (<3 in 2 weeks) | Medium | Low | Allow extension; if still <3 at day 21, replace with a more active maintainer. |
| Pilot maintainer disables but doesn't tell you why | Medium | High (no learning) | Send template D before day 14. Direct ask gets direct answers. |

### Hard escalation triggers (immediately disable + investigate)

- **Any maintainer reports a hallucinated diagnosis** — proposes a fix for code that doesn't exist or references symbols that aren't in the repo
- **Any maintainer reports a wrong-confidence event** — Phalanx claimed ≥0.8 on something obviously wrong
- **Any branch gets created in a maintainer repo that Phalanx shouldn't have created** — shadow-mode violation
- **Two maintainers report the same UX complaint** — pattern threshold met; apply a polish pass before continuing

### Soft signals (note but don't act yet)

- One maintainer asks for less verbose comments
- One maintainer says the operator @-mention is awkward
- One maintainer wants to see the cost
- Maintainer notes the comment is correct but doesn't @-mention to thank or react

---

## 7. Definition of success — and of failure

### Pilot success (after 2 weeks per maintainer)

- ≥3 maintainers explicitly say "I'd keep this installed" at day 14
- ≥1 SHIPPED_PROPOSED comment that the maintainer rates as useful
- 0 maintainers report a hallucinated diagnosis
- 0 shadow-mode violations
- ≤1 wrong-confidence event across the pilot

If those hold, we have evidence to start preparing the GitHub App for external beta.

### Pilot failure (any one is sufficient)

- ≥2 maintainers report hallucinated diagnoses → architectural problem, not pilot UX
- ≥3 maintainers explicitly disable in week 1 → wrong fit selection or trust-blocker we missed
- Operator burnout from monitoring → too many maintainers; scale back

### Pilot inconclusive — extend

- Fewer than 3 maintainers got any meaningful comment in 2 weeks → extend duration, don't scale up
- Mixed signals across maintainers (some love, some hate) → pull patterns first, polish, then re-run with same maintainers

---

## 8. Today (2026-05-20) — operator checklist

Right now, before inviting anyone external:

- [ ] Sanity-check the comment that just landed on usephalanx/inflect PR#6 — does it read as trustworthy to you?
- [ ] Pick the first 3 maintainers per the profile above. Write their names + repos in §4's template. Don't invite a 4th until §7's success criteria hold for the first 3.
- [ ] Verify the operator handle in settings (`phalanx_operator_handle="rnagulapalle"`) is the one you want maintainers @-mentioning. If you have a separate "ops" handle, change it.
- [ ] Decide what (if anything) you want to put in `phalanx_about_url`. If empty, the line is omitted — that's fine. If filled, must point to a maintainer-readable explainer (not internal docs).
- [ ] Send template A or C to maintainer #1.

**Nothing else changes today.** No new code. No new endpoints. No new docs after this one.

---

## 9. What this runbook is NOT

- Not architecture. The code freeze applies until the pilot produces signal.
- Not a public document. Internal operator notes.
- Not a substitute for talking to maintainers directly. Templates are starting points; conversations are the data.
