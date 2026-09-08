# Fleet Usage QA gates

Status: all gate exits pending; individual criteria checked from 2026-09-08 carry test evidence in the history (HIST-004). Revised 2026-09-08 for the snapshot architecture.

Created: 2026-09-08.

Related: [implementation plan](implementation-plan.md) and
[implementation history](implementation-history.md).

## How to use these gates

Check each criterion only after performing the described validation. Record the
implementation revision, command or procedure, environment, result, and evidence
in the history. A fixture or mock is not proof that a real scheduler or private
GitHub workflow works. Mark missing credentials or unavailable platforms as
pending and explain the gap.

Each gate has an explicit exit checkbox. Check it only when its required criteria
pass. A failed later change reopens the affected criterion and gate; preserve
the original result in history and append the new result.

Proposed local quality commands, once the package exists:

```bash
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src/fleet_usage
uv run pytest
uv build
```

Add an AST-based convention check if the configured tooling does not enforce
the prohibition on `from __future__ import annotations`. Run installed-artifact
checks outside the source tree. These commands are planned checks, not results.

## QA-G0: Contracts and supported environment

Related implementation: IMP-001 through IMP-005.

- [x] G0-01: Capture actual ccusage version/help output and representative JSON from the selected release; distinguish documentation from observed behavior.
- [x] G0-02: Verify selected source commands, daily keys, model fields, token semantics, pricing metadata availability, and missing/unpriced behavior.
- [ ] G0-03: Record Python/uv and ccusage runtime compatibility for native Windows, Fedora, Debian 11, and Debian 13; identify any unsupported architecture.
- [ ] G0-04: Define versioned settings, snapshot, ledger, manifest, and README-renderer input schemas with examples and rejection rules.
- [ ] G0-05: Define stable identity, the settle rule, anomaly handling, and ledger recovery via `rebuild` before implementing persistence.
- [ ] G0-06: Record actual source/authentication selections or clearly identify the defaults used.
- [ ] G0-07: Document the complete-history preconditions and inability to deduplicate copied sessions from daily totals.
- [ ] QA-G0: Exit gate passed; contract and compatibility evidence recorded.

## QA-G1: Packaging, style, configuration, and CLI foundation

Related implementation: IMP-101 through IMP-106.

- [x] G1-01: Build wheel and sdist successfully; verify package resources, console entry point, and `py.typed` are included.
- [x] G1-02: Install the wheel with uv into a clean environment and invoke the CLI outside the repository.
- [x] G1-03: Ruff lint and format checks pass with the agreed line length, single-quoted literals, and NumPy-style docstrings.
- [x] G1-04: Type checking passes; verify public interfaces are typed and the prohibited future import is absent.
- [ ] G1-05: Verify Linux/XDG and Windows platformdirs paths, explicit config overrides, relative-path resolution, and Unicode/space-containing paths.
- [x] G1-06: Verify documented configuration precedence; a process secret overrides `.env`, and a working-directory `.env` is never picked up accidentally.
- [x] G1-07: Repeated `init` preserves machine identity, existing settings, and secrets; malformed configuration produces an actionable error.
- [ ] G1-08: Redacted config, exceptions, dry-run output, and logs do not reveal token values; verify secret-file permissions/access guidance.
- [x] G1-09: Viewer-only installation works without ccusage or its runtime and does not require data-write credentials.
- [x] G1-10: CLI exit codes distinguish configuration errors, collection failure, spooled-but-not-uploaded, remote conflict, and successful upload.
- [ ] QA-G1: Exit gate passed; package and configuration evidence recorded.

## QA-G2: Collection, snapshots, and ledger

Related implementation: IMP-201 through IMP-207 (IMP-206 removed).

- [x] G2-01: Known fixtures normalize correctly across selected agents; per-agent totals and model breakdowns are not counted twice.
- [x] G2-02: Unknown JSON versions, missing required fields, negative/invalid counts, malformed JSON, and non-finite costs fail validation without writing a spool file.
- [x] G2-03: Empty valid usage is distinguishable from source/permission failure; a failed agent is recorded with `status = error` in the snapshot, not as healthy zero usage.
- [x] G2-04: Known token/cache/reasoning semantics remain correct, and missing cost data is surfaced rather than silently priced at zero.
- [x] G2-05: Subprocess timeout, nonzero exit, and noisy stderr are handled; supported Windows launchers work without unsafe shell interpolation.
- [x] G2-06: A snapshot's `agents_hash` is stable across collection times for identical usage, and an unchanged collection is not spooled; the snapshot filename is verifiable from its content.
- [x] G2-07: Forced interruption during collection leaves either no spool file or one complete, parseable spool file; temporary files are never uploaded.
- [x] G2-08: A second local publisher instance exits immediately via the process lock without touching the spool or the remote ledger.
- [x] G2-09: Offline collections remain spooled across restart and reconnect; failure to collect new data does not discard or block draining of valid older spool files.
- [x] G2-10: A dry run writes no spool file and performs no remote write.
- [x] G2-11: A ledger rebuilt from snapshots equals the incrementally maintained ledger in any application order, including the Monday-morning/Sunday late-total case and permuted or duplicated snapshots.
- [x] G2-12: Settled records are never replaced; unsettled decreases are applied and logged; settled decreases are refused and logged with both values and the snapshot path.
- [ ] QA-G2: Exit gate passed; failure-injection and ledger determinism evidence recorded.
## QA-G3: GitHub upload and repository bootstrap

Related implementation: IMP-301 through IMP-306.

- [x] G3-01: Bootstrap an explicitly designated disposable private data repository with the manifest, README workflow, and renderer script; repeat without duplicate or destructive changes; `--update` refreshes only the workflow and script.
- [ ] G3-02: Existing repository initialization does not create another repository; creation occurs only with the explicit creation option; `repo register` appends exactly one manifest entry per identity.
- [ ] G3-03: Routine publisher credentials can upload snapshots and ledgers; reader credentials cannot upload; workflow-file permission is needed only for `repo init` and is documented separately.
- [x] G3-04: Re-uploading an identical snapshot produces no extra file or duplicate successful commit; different content at the same path is rejected as a hard error.
- [x] G3-05: Simulate server acceptance followed by lost client response; the retry verifies the existing snapshot by content and deletes the spool file once.
- [x] G3-06: A ledger PUT with a stale blob SHA is retried after refetch and remerge; concurrent uploads from different publishers survive branch-reference races without lost snapshots or force pushes.
- [x] G3-07: Exercise bounded network/rate-limit retries, backlog throttling below secondary limits, and terminal authentication errors; spooled data survives all failures.
- [ ] G3-08: Validate manifest identity/timezone/schema/freeze-window policies before accepting incompatible records into normal reports.
- [x] G3-09: The README workflow runs from the bundled script and manifest only, with no code-repository checkout, no private-code credential, and no dependency installation.
- [x] G3-10: Confirm application upload payloads contain only approved usage metadata, not `.env`, credentials, prompts, project names, or raw source logs.
- [x] G3-11: A crash between snapshot upload and ledger update is repaired on the next run by Trees-API listing after `applied_through`; the ledger then matches `rebuild`.
- [ ] QA-G3: Exit gate passed; real GitHub and transport-failure evidence recorded.
## QA-G4: Aggregation, README publication, and viewing

Related implementation: IMP-401 through IMP-406 (IMP-403 removed).

- [x] G4-01: Settle-rule replay cases pass: `$3 -> retry $3 -> $5 -> late $3` yields `$5`; Monday-morning 100 then Sunday 150 yields 150; a decrease after settling is refused and logged.
- [ ] G4-02: Aggregating permuted, duplicated, and partially missing ledgers across multiple machines yields totals matching the logical latest daily records.
- [x] G4-03: `rebuild` from remote snapshots without a prior ledger produces a ledger identical to the incrementally maintained one, excluding `last_run_at`.
- [ ] G4-04: Verify all retained daily records contribute once; missing newer records do not delete history, and replacement model breakdowns do not leave stale model rows.
- [ ] G4-05: Anomalies are surfaced in `show` and the README with agent, date, kept and observed values, and snapshot path; the reserved corrections hook is exercised with a fixture.
- [ ] G4-06: Timezone and freeze-window policy changes cannot silently blend incompatible history; unknown cost coverage is visible.
- [ ] G4-07: The README workflow tolerates a publisher advancing the branch between checkout and write; the Contents API write retries with a fresh SHA and never force-pushes.
- [x] G4-08: README commits made with the built-in token do not trigger workflows; the workflow is schedule and manual dispatch only.
- [ ] G4-09: A failed README render leaves the previous README in place and the failure visible in the Actions log.
- [ ] G4-10: Ledger and README output identify snapshot path, collector version, collection time, last run time, and render time as distinct fields.
- [ ] G4-11: Missing expected machines, stale machines, unregistered ledgers, and collection anomalies appear in CLI and README output.
- [x] G4-12: Machine/agent/model/date filters and daily/monthly totals match a hand-calculated fixture, including decimal cost arithmetic.
- [ ] G4-13: Read-only authenticated viewing works from a separate machine; conditional requests, raw-media fetch above 1 MB, and invalid-response cache handling work correctly.
- [x] G4-14: `--offline` makes no network request; stale fallback is labeled, and interrupted downloads preserve a valid cache.
- [x] G4-15: JSON/CSV outputs contain no ANSI formatting; labels are safely rendered in terminal, Markdown, and spreadsheet-oriented exports.
- [ ] G4-16: Browser viewing opens the private README; the README renderer and `show` agree on totals for the same ledgers (parity test); the Mermaid chart is smoke-tested on GitHub or omitted.
- [ ] QA-G4: Exit gate passed; aggregation and viewing evidence recorded.
## QA-G5: Real scheduler lifecycle

Related implementation: IMP-501 through IMP-506.

- [x] G5-01: Crontab installation preserves unrelated jobs and variables and repeated installation updates one managed block only; systemd installation writes one user timer/service pair with `OnCalendar` and `Persistent=true`.
- [x] G5-02: Linux preview makes no scheduler changes for either backend; uninstall removes only the application's block or units and is safe when repeated; `auto` picks systemd only when a user manager is available.
- [ ] G5-03: Verify paths containing spaces, quotes, Unicode, and cron-significant characters; scheduled execution succeeds with a minimal environment.
- [ ] G5-04: Verify hourly scheduling, login catch-up via `--if-due`, restart, clock movement, and DST changes; repeat triggers never double count usage.
- [ ] G5-05: On Windows, repeated registration creates one task with the correct principal, absolute executable, arguments, and config path.
- [ ] G5-06: On Windows, logged-in mode publishes while the desktop is locked and resumes appropriately after a new login or missed start.
- [ ] G5-07: On Windows, uninstall removes only the application task; preview and status do not modify task state.
- [ ] G5-08: Optional logged-out mode performs a real GitHub upload while the user is signed out; record whether S4U succeeded, and if password-backed logon is used, credentials are managed by Task Scheduler with no password leakage.
- [ ] G5-09: Missing account rights, unavailable scheduler services, or unsupported modes produce clear diagnostics without partially enabling a broken schedule.
- [ ] G5-10: Long-running collection does not overlap another instance; configured timeout and local logs make failures diagnosable.
- [ ] G5-11: Package upgrade and changed installation paths have a tested repair/update procedure for existing schedules.
- [ ] G5-12: Sleep/power-off behavior is documented accurately; wake-from-sleep is not enabled implicitly.
- [ ] QA-G5: Exit gate passed; actual Unix and Windows task evidence recorded.

## QA-G6: Fleet pilot and release readiness

Related implementation: IMP-601 through IMP-606.

- [ ] G6-01: All prior gates pass or an explicit scope revision records why a criterion is not applicable; no unsupported platform is reported as validated.
- [ ] G6-02: Clean installation and scheduled collection succeed on the actual Windows workstation.
- [ ] G6-03: Clean installation and scheduled collection succeed on the actual Fedora mobile machine.
- [ ] G6-04: Clean installation and scheduled collection succeed on the Debian 11 server.
- [ ] G6-05: Clean installation and scheduled collection succeed independently on both Debian 13 servers.
- [ ] G6-06: Observe at least two scheduled collection opportunities per publisher; verify the aggregate distinguishes new usage from repeated observations.
- [ ] G6-07: Disconnect the mobile publisher, collect while offline, reconnect, and verify spooled snapshots arrive once and the ledger matches `rebuild`.
- [ ] G6-08: View the same aggregate revision from Windows and Linux, including a reader-only installation and an offline cached view.
- [ ] G6-09: Compare representative source reports and hand-checked totals with fleet output; document any source limitations or pricing differences.
- [ ] G6-10: Record collection duration, snapshot size, ledger size, README workflow duration, API behavior, Actions consumption, and projected annual repository growth.
- [ ] G6-11: Verify ledger restoration from remote snapshots, token rotation, scheduler removal, and rollback to a previous compatible package revision.
- [ ] G6-12: Installation, repository bootstrap, local retention guidance, upgrades, failure recovery, and known limitations are documented.
- [ ] G6-13: Record release revision/version, artifact checks, remaining risks, and the complete evidence index in implementation history.
- [ ] QA-G6: Exit gate passed; fleet pilot and release evidence recorded.

## Evidence template

Copy this into the implementation history for a completed validation batch:

```markdown
### YYYY-MM-DD — Validation title

- History ID: HIST-NNN
- Implementation revision: commit hash, or explicitly uncommitted work
- Implementation tasks: IMP-...
- QA criteria: G... and any gate exit item
- Environment: OS/version, architecture, Python, uv, ccusage, package version
- Procedure/commands: exact commands or manual steps
- Expected result: observable acceptance condition
- Actual result: pass/fail/pending and relevant measurements
- Evidence: test output, workflow URL, or repository-relative artifact path
- Limitations: mocked components, unavailable platforms, remaining uncertainty
- Follow-up: next tasks or reopened criteria
```

Use sanitized evidence. Never commit credentials, raw conversation logs, or
unnecessary private account details as QA artifacts.
