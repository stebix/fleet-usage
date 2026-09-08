# Fleet Usage planning and delivery tracker

Planning baseline: 2026-09-08.

Fleet Usage is a proposed Python CLI that collects ccusage reports into
immutable per-run snapshots, publishes them to a private GitHub data repository
along with each machine's derived ledger, and aggregates the ledgers on read to
display fleet usage on any configured machine. There is no central aggregator;
an optional scheduled workflow renders a README for browser viewing.

## Documents

| Document | Purpose |
| --- | --- |
| [Implementation plan](implementation-plan.md) | Scope, architecture, decisions, and checkable implementation work |
| [QA gates](qa-gates.md) | Acceptance criteria and evidence required before advancing |
| [Implementation history](implementation-history.md) | Append-only record of changes, validation, and decisions |
| [Snapshot revision](revision-snapshots.md) | Accepted 2026-09-08; rationale and review log for the snapshot architecture |
| [Codex review](review-codex-2026-09-08.md) | External review of the proposal, verbatim |
| [Implementation review](review-implementation-2026-09-08.md) | Adversarial review of the implemented core modules, verbatim |

## Tracking rules

- [x] PLAN-001: Capture the agreed architecture and remaining design choices.
- [x] PLAN-002: Create implementation and QA checklists with stable IDs.
- [x] PLAN-003: Start the implementation history with the documentation baseline.
- [x] PLAN-005: Accept snapshot revision and re-cut plan and gates (2026-09-08).
- [x] PLAN-004: Begin implementation and record the first implementation entry (HIST-004, 2026-09-08).

Implementation has started (HIST-004). Checked planning items do not imply
that any data repository, scheduled publisher, or fleet deployment exists.

1. Use the task IDs in `implementation-plan.md` in commits and history entries.
2. Check an implementation task only after its described work is complete.
3. Check a QA criterion only after executing the check and recording evidence.
4. Check a gate's exit item only when every required criterion has passed.
5. Record unavailable platform checks as pending, not passed by inference.
6. Append a history entry for each meaningful implementation or validation batch.
7. Reopen affected checks when later changes invalidate their evidence.
8. Preserve previous history entries. Add corrections or superseding decisions
   as new entries rather than rewriting the historical account.

Use `N/A` only with a recorded rationale and an explicit scope decision; an
unchecked item must not silently become optional. Record commit hashes and
workflow run URLs when available. Do not invent them before commits or runs exist.

## Delivery boundary

The code repository holds the package, tests, documentation, and bootstrap
templates. A separate private data repository holds snapshots, per-machine ledgers,
shared fleet configuration, and the optional README workflow. Creating these
documents does not deploy either the scheduled publishers or the remote workflow.

Before enabling production scheduling, run the relevant QA gates and record
which machines were actually exercised.
