# Phalanx — GitHub permissions (Path B)

This document is for **maintainers** evaluating whether to enroll their
repo in Phalanx's shadow-mode pilot. It explains exactly what Phalanx
does to your repo and the minimum permissions required.

If you are an operator looking for the enrollment SQL or PAT-vending
machinery, that's [docs/ops/maintainer-comments.md](maintainer-comments.md).

## TL;DR

In shadow mode, Phalanx **reads** your CI failures and the failing PR's
diff in a sandboxed environment to produce a diagnosis. The diagnosis
lands in Phalanx's internal ledger. Nothing in your repository is
modified.

If you opt in to maintainer comments (Path B, 2026-05-20), Phalanx will
**add one PR comment per terminal verdict** so you can see what it
concluded. That comment is the only thing Phalanx ever writes to your
repository.

## What Phalanx actually does

When a CI run fails on your repo and that failure matches a PR you have
opted into shadow analysis for:

1. **Read** the failed workflow's job logs via the GitHub Actions API.
2. **Read** the PR's diff and metadata.
3. **Clone** the repo at the failing commit into Phalanx's sandbox.
4. **Run** a multi-agent analysis pipeline that produces one of:
   - `SHIPPED_PROPOSED` — a proposed patch with ≥70% confidence
   - `SAFE_ESCALATE` — the system refused to ship because evidence was insufficient
   - `FAILED` — the system attempted but could not produce a verifiable fix
5. **Optionally** (if you have opted in) post a single PR comment per terminal verdict.

What Phalanx **does not** do:

- Push commits to any branch in your repo
- Open PRs
- Create labels, statuses, or check runs
- Modify repository settings, webhooks, or collaborators
- Read private organization data outside the specific repo you enrolled
- Train on your code

## Minimum required token scopes

You can grant Phalanx access via either a **fine-grained PAT**
(recommended) or a **GitHub App** (in development, not yet available).

### Fine-grained PAT (recommended for the pilot)

Create at <https://github.com/settings/personal-access-tokens/new>.

| Scope (Repository permissions) | Access level | Why Phalanx needs it |
| ------------------------------ | ------------ | -------------------- |
| **Actions**                    | Read-only    | Read failed workflow run logs (`fetch_ci_log`) |
| **Contents**                   | Read-only    | Clone the repo at the failing commit into Phalanx's sandbox |
| **Pull requests**              | Read-only    | Read PR metadata + the diff under analysis |
| **Metadata**                   | Read-only    | (Implicit — GitHub requires this on every fine-grained PAT) |

If you also want **Phalanx to post PR comments** (Path B opt-in, recommended for
the pilot so you can see what Phalanx concluded without operator interpretation):

| Scope (Repository permissions) | Access level | Why Phalanx needs it |
| ------------------------------ | ------------ | -------------------- |
| **Pull requests**              | **Read and write** | Post one comment per terminal verdict |

That's it. Five permissions, all scoped to the single repo you enroll.

### What you should NOT use

**A classic PAT** is over-scoped. Classic PATs grant `repo` (which
includes write to code, issues, PRs, webhooks, *and* admin operations on
your private repositories). Phalanx does not need any of that. If the
operator asks you for a classic PAT, refuse.

If you need to enroll multiple repos in your org, create one fine-grained
PAT per repo (the GitHub UI makes this easy). One token per repo limits
blast radius if a token ever leaks.

## Off-switch

To stop Phalanx from analyzing your repo entirely, choose any one:

1. **Revoke the PAT** at <https://github.com/settings/personal-access-tokens>.
   Phalanx will lose access on the next call; ongoing analysis runs may
   complete but no new ones can start.
2. **Set the integration to disabled** — tell the operator. They run:
   `UPDATE ci_integrations SET enabled=false WHERE repo_full_name='<owner/name>';`
3. **Disable maintainer comments only** (keep analysis going internally):
   tell the operator. They run:
   `UPDATE ci_integrations SET maintainer_comments_enabled=false WHERE repo_full_name='<owner/name>';`

A native off-switch UX (a button in a dashboard, or a slash-command
in the PR comment) is a planned beta-launch feature; for the pilot,
contact the operator.

## What the maintainer-facing comment looks like

Three shapes, one per verdict. All carry the same shadow-mode footer
naming the off-switch.

### When Phalanx proposes a fix

> 🛠 **Phalanx — Proposed fix (shadow mode, read-only)**
>
> [one-paragraph diagnosis explaining what was wrong + why this fix works]
>
> <details open><summary><b>Proposed change</b></summary>
>
> ```diff
> [the actual diff]
> ```
>
> **Affected files:** `path/to/file.py`
> **Confidence:** 85%
> **What was examined:** the failing CI job, the PR diff, and the full CI log.
>
> </details>
>
> _Phalanx ran in shadow mode. Nothing in this repo was modified beyond this comment. To disable Phalanx on this repo, contact the operator who enrolled it._

### When Phalanx refuses to ship

> 🔍 **Phalanx — Refused to ship (shadow mode, read-only)**
>
> [the grounded diagnosis from the TL agent]
>
> <details><summary><b>Why escalated</b></summary>
>
> Phalanx's confidence (40%) was below the threshold required to propose a fix, so it escalated for human review instead of guessing.
>
> **What was examined:** the failing CI job, the PR diff, and the CI log.
> **Outcome:** No code change was generated. This PR is unchanged.
>
> </details>
>
> _Phalanx ran in shadow mode..._

### When Phalanx tried but couldn't verify a fix

> ⚠️ **Phalanx — Could not produce a verifiable fix (shadow mode, read-only)**
>
> [the grounded reasoning]
>
> <details><summary><b>Details</b></summary>
>
> A proposed change was attempted but did not pass verification in Phalanx's sandbox. Out of caution, no patch is being suggested.
>
> </details>
>
> _Phalanx ran in shadow mode..._

### When you will NOT see a comment

Phalanx **suppresses** the comment in these cases — the goal is to keep
your PR free of noise that isn't useful to you:

- Phalanx's sandbox failed to bootstrap (e.g. an `apt-get install` flake inside the sandbox). You shouldn't have to read about Phalanx's internal infra issues.
- The diagnosis was synthesized rather than grounded (i.e. the TL agent never produced a real analysis).
- The dispatch never reached a true terminal state (was timed out by a watchdog).
- No PR number was associated with the failing workflow.

## Frequently asked

**Q: Can Phalanx read my private repos that aren't enrolled?**
A: No — the PAT is scoped to a single repo. Even if your account has
access to others, Phalanx's token cannot read them.

**Q: Can Phalanx see other PRs on my repo, not just the failing one?**
A: Yes — the `Pull requests: read` permission is repo-scoped, not
PR-scoped, so technically the token CAN list other PRs. In practice
Phalanx only reads the PR associated with a failing workflow run.

**Q: What if Phalanx posts a wrong diagnosis?**
A: For the pilot, reply on the PR comment naming the issue and
@-mentioning the operator. A formal feedback mechanism is on the
beta-launch roadmap.

**Q: Will Phalanx ever push code to my repo?**
A: Not in shadow mode. Shadow mode is the only mode currently in
production. A future "write mode" (Phalanx opens an actual PR with the
proposed change) is a separate milestone with separate consent.

**Q: How do I know Phalanx actually ran shadow-mode and didn't push?**
A: Two ways:
1. **Side-effect audit**: `git log --all | head` on your repo should show no commits authored by `rnagulapalle` or any `phalanx/*` branches.
2. **Audit endpoint**: ask the operator for the row in Phalanx's ledger for your repo. Every row has `phalanx_proposed_patch` (a text blob — never a real branch) and a full provenance trail.

**Q: How do I rotate the PAT?**
A: Create a new fine-grained PAT, tell the operator, they update the
integration's token via SQL. Old PAT can then be revoked from your
GitHub settings.
