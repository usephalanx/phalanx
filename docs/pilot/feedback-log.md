# Pilot feedback log

This is a working log. Update inline as feedback arrives. One section per maintainer.
Use the template from §4 of `invite-only-pilot-operator-runbook-2026-05-20.md`.

---

## Maintainer slot 1 — (operator) — usephalanx/xorq (fork of xorq-labs/xorq)

- **Enrolled:** 2026-05-20 (validation surface, not a retention test)
- **Relationship:** operator-owned fork; path C from the runbook
- **Repo profile:** 44 MB Python data-engine repo (DataFusion / pyarrow / heavy deps); `ci-lint` is the test target
- **Status:** SANDBOX-ENVELOPE-EXCEEDED
- **Renewal at day 14:** n/a

### Comments posted

| Date | PR | Verdict | Conf | Phalanx diagnosis (1-line) | Maintainer reaction |
| ---- | -- | ------- | ---- | -------------------------- | ------------------- |
| 2026-05-20 | #1 | (suppressed) | n/a | (sandbox setup timed out; suppress matrix killed comment) | n/a |

### Feedback by category

| Category | Reaction | Action taken |
| -------- | -------- | ------------ |
| Trust              | Suppression worked — no fake comment landed on PR | none |
| Confusion          | n/a (no maintainer audience) | none |
| Usefulness         | Zero — but correctly zero, not misleadingly something | none |
| Annoyance          | n/a | none |
| Wording            | n/a | none |
| Escalation clarity | n/a | none |

### Retention signal at day 14

- [x] Would keep installed: **n/a — operator-owned fork, not a retention test**
- Observation in 1 line: **xorq is outside Phalanx's sandbox-bootstrap envelope today.** Stuck-task detector caught it cleanly. No maintainer-visible artifact. Pilot data point: heavy-dep Python repos (44 MB+ with native arrow/datafusion deps) currently exceed our sandbox window.

---

## Maintainer slot 2 — (operator) — usephalanx/opentelemetry-hooks (fork of o11y-dev)

- **Enrolled:** 2026-05-20 (validation surface, not retention test)
- **Relationship:** operator-owned fork; path C from runbook
- **Repo profile:** 0.4 MB pure Python, ideal envelope shape, `ruff` lint workflow
- **Status:** SHIPPED_PROPOSED (post-Option-β, 2026-05-21)
- **Renewal at day 14:** n/a

### Comments posted

| Date | PR | Verdict | Conf | Phalanx diagnosis (1-line) | Maintainer reaction |
| ---- | -- | ------- | ---- | -------------------------- | ------------------- |
| 2026-05-20 | #1 | (suppressed) | n/a | sandbox apt timeout; suppress matrix killed comment | n/a |
| 2026-05-21 | #1 | SHIPPED_PROPOSED | 93% | unused top-level `import string` triggers F401; proposes exact 1-line removal | n/a (operator-owned) |

### Feedback by category

| Category | Reaction | Action taken |
| -------- | -------- | ------------ |
| Trust              | AI authorship banner, operator @-mention, exact diff — comment reads as honest, not autopilot | none |
| Confusion          | None — diagnosis names the file + line, diff is 1 line | none |
| Usefulness         | High — diagnosed synthetic violation in 245 s, end-to-end | none |
| Annoyance          | None | none |
| Wording            | "shadow mode, read-only" + "Nothing in this repo was modified beyond this comment" landed clearly | none |
| Escalation clarity | Footer explains how to disable + @-mention operator | none |

### Retention signal at day 14

- [x] Would keep installed: **n/a — operator-owned fork, not a retention test**
- Observation: **Option β unblocked the envelope.** Prebaked `phalanx-sandbox-base:py312-prebaked-v1` made the `apt_install_baseline` step a complete no-op (`0 upgraded, 0 newly installed, 0 to remove`). SRE setup completed in 94s; full run SHIPPED at 245s @ 93% confidence on PR#1 (comment id 4504979043). The same envelope (small Python repo, ruff lint failure, synthetic single-line violation) now produces the maintainer-trust experience the pilot was designed to validate.

---

## Maintainer slot 3 — [name] — [owner/repo]

- **Enrolled:**
- **Relationship:** [warm / cool / skeptical / org]
- **Repo profile:**
- **Status:** PENDING
- **Renewal at day 14:**

### Comments posted

| Date | PR | Verdict | Conf | Phalanx diagnosis (1-line) | Maintainer reaction |
| ---- | -- | ------- | ---- | -------------------------- | ------------------- |
|      |    |         |      |                            |                     |

### Feedback by category

| Category | Reaction (verbatim or paraphrased) | Action taken |
| -------- | ---------------------------------- | ------------ |
| Trust              |  |  |
| Confusion          |  |  |
| Usefulness         |  |  |
| Annoyance          |  |  |
| Wording            |  |  |
| Escalation clarity |  |  |

### Retention signal at day 14

- [ ] Would keep installed: yes / no / not yet sure
- Reason in their own words:

---

<!-- Add maintainer slots 4–5 only after slots 1–3 have produced signal. -->

---

## Cross-maintainer patterns (data-required)

> Update this section once ≥5 comments are in the log AND ≥2 maintainers have given feedback.
> Single data points are noise. Look for things ≥2 maintainers independently report.

### Observed patterns

| Pattern | Maintainers reporting | Proposed small fix | Status |
| ------- | ---------------------- | ------------------ | ------ |
|         |                        |                    |        |

### Soft signals (note, don't act yet)

| Signal | Reported by | Decision |
| ------ | ----------- | -------- |

---

## Hard escalations

If any of these happen, **stop new dispatches on the affected repo and investigate**:

| Date | Maintainer | What happened | Phalanx artifact (ledger_id / comment URL) | Action |
| ---- | ---------- | ------------- | ------------------------------------------ | ------ |
|      |            |               |                                            |        |
