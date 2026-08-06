# External maintainer candidate research

**Date:** 2026-05-20
**Goal:** identify ONE external maintainer to pilot Phalanx with. Prepare outreach context, do NOT execute outreach.

You make the final call — I'm proposing candidates grounded in the proven envelope, and noting where my information ends and your social context begins.

---

## The proven envelope, restated

Phalanx today reliably analyzes:

| Property | Requirement |
| -------- | ----------- |
| Language | Python (3.10+) |
| Native extensions | **None** in the test path (no Cython speedups, no DataFusion, no mypyc compile in CI) |
| Install command | `pip install -e .` or `uv pip install -e .` that finishes in ≤ ~5 min from a clean Debian-slim |
| Workflow shape | Has a dedicated lint/format/type-check job that runs on every PR (ruff, black, mypy, pre-commit, etc.) |
| Repo size | Roughly 1k–30k Python LOC. Doesn't matter that much; what matters is the install footprint, not the line count |
| Activity | ≥ 3 failing PR-event runs/month, so the pilot generates signal during a 2-week window |

This is **deliberately narrow**. It's the slice where Phalanx's diagnoses are grounded and confident. Don't widen it for the pilot.

---

## Maintainer criteria (in priority order)

1. **You have direct, low-friction contact.** This trumps everything else. A perfectly fitting repo whose maintainer takes a week to reply is worse than an OK fit whose maintainer DMs you back in 10 minutes.
2. **They've publicly engaged with AI dev tooling — positively or critically.** Either is fine; what you want to avoid is "no opinion." A maintainer who has written one thoughtful blog post about Copilot or Cursor will read the comment, form a view, and tell you what they think. Someone who's never engaged with AI tooling will either ignore or panic.
3. **Repo fits the envelope.** Filter their repos through the table above. If their primary repo doesn't fit, sometimes they have a smaller side-project that does.
4. **Technically thoughtful, not just famous.** Conference fame is a noise signal. What you want is "this person writes clear issue replies, distinguishes hypotheses from conclusions, and changes their mind in public when shown evidence."
5. **Would actively appreciate honest escalation.** A maintainer whose first instinct on a "this is uncertain" signal is to dig deeper — not to demand certainty.

The criterion that is NOT on this list: stars, reach, or commercial value. The pilot is about whether the *trust contract* survives one specific person's scrutiny, not about reach.

---

## Candidate shapes (not specific names — you map these to your network)

Three archetypes. Pick ONE shape; pick ONE person inside that shape from your network.

### Shape 1 — "the small-lib polymath"

**Repo:** a single Python utility library, 2k–10k LOC, that this maintainer wrote most of and still maintains alone or with one collaborator.

**Workflow shape:** typically `tox` + `ruff` + `mypy` + `pre-commit`. Failures are usually mechanical (lint rules, type narrowing).

**Maintainer disposition:** They've seen every PR for years. They know exactly what a good vs. wrong-shape diagnosis looks like. They'll be quick to spot a hallucination.

**Why this is the strongest pilot fit:** maximum signal density. Every CI failure has a clear cause; if Phalanx is right, the maintainer can verify in 30 seconds; if Phalanx is wrong, they'll know immediately.

**Examples of repos that fit (none of these are recommendations — they're typology examples):** `jaraco/inflect`, `mahmoud/boltons`, `dabeaz/sly`, `pyca/cryptography` (too big actually), `python-attrs/attrs` (would fit but we've already used it as a non-maintainer-facing target).

**What to ask yourself:** in your DM history, who maintains a small Python library you've actually used and you've talked to about a bug or PR? That's your candidate.

### Shape 2 — "the type-system reviewer"

**Repo:** a Python library with serious type discipline — uses mypy strict mode, py.typed, has tests for type behavior.

**Maintainer disposition:** Will appreciate `SAFE_ESCALATE` honesty (they know type-error fixes are often non-obvious). Will press on whether a `SHIPPED_PROPOSED` patch is semantically right, not just lint-clean.

**Why this is the strongest "skeptical-but-fair" fit:** they have built-in skepticism about mechanical fixes. If Phalanx's wrong, they'll say so politely with the specific reason.

**Examples of repos that fit:** typeshed/typeshed adjacent, `python/mypy` itself (we already use as a target), `agronholm/anyio` (Alex Grönholm — we touched in W1), `Pylons/pyramid`.

**What to ask yourself:** who do you respect for their type-system thinking AND who has answered your DMs in the past week?

### Shape 3 — "the unhurried OSS person"

**Repo:** a niche but well-maintained Python tool. Hundred-ish stars. Maintainer does this for love, not for a job.

**Maintainer disposition:** Reads things carefully. Replies thoughtfully. Doesn't have a corporate review process. The lowest-friction trust experiment available.

**Why this is the strongest "would they actually keep it installed" fit:** they don't have organizational concerns. Their decision will be purely about whether Phalanx is useful to them as one human reading one comment.

**Examples of repos that fit:** smaller `jaraco/*` projects, individual-author tools on PyPI with under 1k downloads/week but active maintenance.

**What to ask yourself:** in OSS Slack/Discord, who responds to your bug reports within 48h with a thoughtful "interesting, can you say more"? That's your candidate.

---

## Recommended next step

Pick **one** maintainer from **one** of the three shapes above, from people you have direct DM contact with. Write their name in the table below before sending any outreach. Then use the template I'll draft once you've named them.

### When you've identified them, fill this in:

```
Name (GitHub handle):
Their primary Python repo (owner/name):
LOC (approx):
CI workflow (lint/format/type — what specifically):
Relationship age:
Last conversation date:
Last conversation topic:
Their public AI/tooling stance (if known):
Why they'd find this interesting (one sentence):
```

I'll use the answers to draft outreach that is **specific to them** — not the generic templates from the runbook. A maintainer can tell the difference between "Phalanx is interesting" (generic) and "you mentioned ruff strict-mode last month and I think Phalanx would have something to say about that PR you closed" (specific).

---

## Pre-outreach checklist (do these before sending anything)

- [ ] Verified their repo fits the envelope (Python, ≤30k LOC, no native test-path deps, has a lint or type CI job that fires on every PR)
- [ ] Confirmed the repo has ≥3 failing PR-event runs in the last 30 days (signal density check)
- [ ] Looked at their CONTRIBUTING.md — confirms AI-assisted contributions aren't prohibited
- [ ] Confirmed your relationship is current (recent DM exchange, not "we tweeted in 2023")
- [ ] Prepared mentally for "no" or no-reply — both are fine outcomes; pilot isn't urgent

---

## Anti-pattern: do NOT pick this person

- A maintainer of a highly visible/political project (a wrong Phalanx comment becomes a Twitter thread)
- Someone you've never talked to but admire from afar (cold outreach has low conversion AND wastes pilot slot)
- A maintainer whose project includes Rust/C extensions in the test path (envelope mismatch — Phalanx will produce mostly `FAILED_SANDBOX_SETUP_*` comments which we correctly suppress, so no signal)
- A maintainer at a company where you have an active commercial relationship (incentive confusion)
- A maintainer whose CONTRIBUTING.md says "no AI-assisted PRs" (respect the boundary; their repo isn't in the pilot envelope by their own definition)

---

## What I'm waiting on from you

Just the name. Once you write it into the table above (with the relationship context), I'll:

1. Draft a *specific* outreach DM tailored to them — not a template
2. Pre-write your day-0 onboarding script with their repo's quirks (workflow shape, expected CI runtime, anything special)
3. Open their slot in [docs/pilot/feedback-log.md](feedback-log.md) so we're ready to record reactions

I won't send anything. The send is your move.
