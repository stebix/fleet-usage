# Fleet Usage implementation history

This is an append-only record of implementation, validation, and changes to the
plan. See the [plan](implementation-plan.md), [QA gates](qa-gates.md), and
[tracking rules](README.md).

## Current delivery state

| Area | State |
| --- | --- |
| Planning documents | Snapshot revision accepted 2026-09-08; plan and gates re-cut; checklists updated in HIST-004 |
| Python package | Implemented (uncommitted); quality gate green with 675 tests after the HIST-005 review fixes; wheel installs cleanly outside the repository |
| ccusage compatibility contract | Validated locally against ccusage 20.0.20 (unified `daily --json --by-agent`); other OSes pending |
| Local configuration and spool | Implemented with tests; process lock, tmp-and-rename spool, dry run |
| Remote GitHub repositories/workflow | Client, publish sequence, `repo init/register`, README workflow implemented against mocked HTTP; no repository created |
| Report generation and viewing | Implemented: read-side aggregation, table/JSON/CSV, cache, `--web`, dependency-free README renderer with parity test |
| Unix/Windows scheduler integration | crontab, systemd user timer, and Task Scheduler backends implemented with injected runners; nothing installed on any machine |
| Fleet pilot | Not started |
| QA-G0 through QA-G6 | All gate exits pending; criteria with test evidence checked in HIST-004 |

Update this table as work progresses, but preserve the dated entries below.

## Entries

### 2026-09-08 — Planning baseline

- History ID: HIST-001.
- Request: write the detailed agreed plan and checkable QA gates under
  `docs/plans`, retaining an implementation history.
- Location: `/home/stebix/code/fleet-usage/docs/plans`.
- Workspace finding: the shared workspace contained other projects but no
  existing Fleet Usage repository. A new local project location was selected
  for these documents; no existing project's files were changed.
- Local repository: initialized Git with branch `main`; no remote was configured.
- Deliverables: planning index, implementation plan, QA gates, and this history.
- Decisions captured: separate code/private-data repositories, hourly push,
  durable offline outbox, central aggregation using pinned code, local report
  viewing, and Windows logged-in scheduling with an optional logged-out mode.
- Proposed defaults retained explicitly: Python 3.12+, package naming,
  all supported ccusage sources, token authentication, Europe/Berlin reporting,
  and user-crontab scheduling on Unix.
- Open deployment inputs: GitHub owner/repository names and visibility,
  credentials, actual source versions, machine labels, and account setup.
- Implementation tasks completed: none. PLAN-001 through PLAN-003 describe
  documentation work only.
- QA gates completed: none. No package or platform behavior has been tested.
- Documentation validation: checked relative Markdown links, uniqueness of
  checklist IDs, implementation-to-gate references, whitespace, final newlines,
  and balanced code fences using a read-only Perl validation command.
- Validation result: PASS; 133 unique checkbox IDs, all seven QA gate exit
  items present, no broken relative links or undefined task/gate references.
- Commit/release: no commit or release created as part of this baseline.
- Next step: begin Phase 0 and collect actual compatibility/schema evidence.

### 2026-09-08 — First implementation batch: all five phases scaffolded and implemented

- History ID: HIST-004.
- Request/context: accept the snapshot revision and prepare the
  implementation. Phase 1 (scaffold, configuration, `init`, `doctor`,
  `config show`) was implemented by one opus agent; Phases 2, 3, 4, and 5
  were then implemented by four opus agents concurrently against the Phase 1
  stub signatures. The coordinator integrated the result: wired the `doctor`
  GitHub reachability check to `github.check_access`, collapsed duplicated
  helpers (`ledger_remote_path`, `parse_manifest`) onto `fetch`, quantized
  parsed costs to micro-dollars, removed the stale "not implemented" surface
  test, and stubbed the reachability probe in doctor tests so no test
  reaches GitHub. An adversarial review of the core modules is still running;
  its findings are not part of this entry.
- Implementation tasks: completed IMP-001, IMP-002, IMP-004, IMP-101 to
  IMP-105, IMP-201 to IMP-205, IMP-301 to IMP-304, IMP-401, IMP-402,
  IMP-404, IMP-405, IMP-501 to IMP-503. Left open on purpose: IMP-003
  (source/auth selections are still defaults, not user choices), IMP-305
  (no manifest timezone/freeze-window compatibility check exists in code and
  the root `README.md` documents token scope but not rotation), IMP-504
  (both S4U and password logon modes are implemented, but the S4U upload
  test the task describes has not run on Windows), IMP-505, and every gate
  exit and Phase 6 item.
- Changes: `pyproject.toml`, `uv.lock`, `.python-version`, `README.md`,
  `.gitignore`, `.github/workflows/ci.yml`; `src/fleet_usage/` modules
  `__init__`, `__main__`, `cli`, `exit_codes`, `paths`, `config`, `models`,
  `logging_setup`, `ui`, `collector`, `snapshot`, `spool`, `ledger`,
  `github`, `publisher`, `fetch`, `reporting`, `readme_render`,
  `commands/{_common,init,doctor,config_cmd,repo,publish,show,rebuild,schedule}`,
  `scheduling/{base,cron,systemd,windows}`, `templates/readme.yml`,
  `py.typed`; tests `tests/unit/test_{models,snapshot_hash,config,paths,
  exit_codes,logging_setup,no_future_annotations,collector,snapshot,spool,
  ledger,github,publisher,fetch,reporting,readme_render,scheduling_base,
  scheduling_cron,scheduling_systemd,scheduling_windows}.py`,
  `tests/integration/test_cli_{init,config_show,doctor,surface,publish,
  repo,show,rebuild,schedule}.py`; fixtures
  `tests/fixtures/ccusage-20.0.20/` (real `daily --json --by-agent`
  output, `daily.json`, top-level and daily help text) and
  `tests/fixtures/ledgers/` (three hand-built ledgers plus a four-machine
  manifest).
- Decision changes:
  - Collector: one unified `ccusage daily --json --by-agent --offline
    --timezone <tz>` call replaces per-agent commands (ccusage 20.0.20
    reports every detected agent in one document); the unified command has
    no cost-mode flag, so `collector.mode` is recorded as `auto`.
  - Costs: ccusage reports unpriced models as `0.0`; zero cost with nonzero
    tokens is recorded as unknown cost. Parsed costs are quantized to six
    decimal places (`COST_QUANTUM`) because finer digits are float noise.
  - Errors and exit codes: `RateLimitError` subclasses `AuthError` and is
    caught first; priority when several failures occur is auth (6) >
    conflict/invalid remote (5) > pending upload/network (4) > collection
    (3); a missing token is a configuration error (2), not an auth failure.
  - `--if-due`: the due marker is written after every non-dry, non-skipped
    run including failed ones, so a broken collector cannot spin.
  - Scheduling: intervals are restricted to 15, 30, 60, 120, 180, 240, 360,
    720, and 1440 minutes; sub-hourly cron uses explicit minute lists
    (`7,22,37,52 * * * *`) because `*/n` is not POSIX; the minute offset is
    derived from the machine identity to spread load.
  - Ledger: `last_error` is never cleared by a later success (clearing
    cannot be made order-independent without an error timestamp); the
    anomaly list is order-dependent by construction and only day records,
    `settled`, `last_success_at`, `applied_through`, and `last_agents_hash`
    are guaranteed order-independent.
- Validation (all from the repository root, CPython 3.12.13 managed by uv):
  - `uv run ruff check .` — All checks passed.
  - `uv run ruff format --check .` — 72 files already formatted.
  - `uv run mypy src/fleet_usage` — Success: no issues found in 34 source
    files (strict).
  - `uv run pytest -q` — 647 passed.
  - `uv build` — wheel `fleet_usage-0.1.0-py3-none-any.whl` and sdist
    built; wheel contains `fleet_usage/py.typed` and
    `fleet_usage/templates/readme.yml`; `dist/` removed afterwards.
  - Clean install: fresh `uv venv --python 3.12` in a scratch directory
    outside the repository, `uv pip install <wheel>`, then from `/`:
    `fleet-usage --version` printed `fleet-usage 0.1.0`; importlib.resources
    confirmed `py.typed` and `templates/readme.yml` inside the installed
    package; `fleet-usage --config <scratch settings> doctor` from the
    installed copy found bunx and reported `ccusage 20.0.20`.
  - Real collector: `fleet-usage --config <scratch>/settings.toml init
    --label e2e-test --repo example/fleet-usage-data`, then `doctor` (all
    checks ok except the placeholder-token warning and the skipped
    reachability probe) and `publish --dry-run`, which executed
    `bunx ccusage@20.0.20 daily --json --by-agent -z Europe/Berlin
    --offline` and printed a per-agent summary (claude 8 days, codex 11
    days, one unknown-cost day each); nothing was written.
  - Read-only scheduler checks on this machine: `crontab -l` reports no
    crontab; no `fleet-usage.timer` exists; `systemd-analyze calendar`
    accepts all nine generated `OnCalendar=` expressions and
    `systemd-analyze verify --user` accepts the generated unit pair
    (both guarded tests in `test_scheduling_systemd.py`).
- QA criteria checked, with the encoding tests as evidence:
  - G0-01: `tests/fixtures/ccusage-20.0.20/{help-top.txt,help-daily.txt,
    daily-by-agent.json,daily.json}` captured from the real release;
    `test_models.py::test_snapshot_round_trip_from_fixture`.
  - G0-02: `test_collector.py::test_parse_fixture_day_counts`,
    `::test_parse_fixture_matches_row_level_totals`,
    `::test_parse_fixture_known_cost_is_exact`,
    `::test_parse_fixture_zero_cost_with_tokens_is_unknown`,
    `::test_missing_by_agent_shape_is_rejected`, plus the dry run above.
  - G1-01 and G1-02: the `uv build` and clean-install procedure above.
  - G1-03 and G1-04: the ruff and mypy commands above;
    `test_no_future_annotations.py::test_no_future_annotations_import`.
  - G1-06: `test_config.py::test_process_env_beats_dotenv`,
    `::test_dotenv_is_read_next_to_settings_only`,
    `::test_settings_path_precedence`.
  - G1-07: `test_cli_init.py::test_init_refuses_to_overwrite`,
    `::test_force_preserves_machine_id`, `::test_force_keeps_existing_env_file`,
    `::test_init_is_idempotent_with_force`;
    `test_config.py::test_malformed_toml_is_actionable`,
    `::test_validation_error_names_the_key`.
  - G1-09: `test_config.py::test_viewer_only_settings`,
    `test_cli_doctor.py::test_doctor_reports_viewer_only`,
    `test_cli_publish.py::test_a_viewer_only_install_exits_two`,
    `test_github.py::test_check_access_reports_read_only`.
  - G1-10: `test_exit_codes.py::test_documented_values`,
    `test_publisher.py::test_exit_codes_are_ordered_by_severity`,
    `test_cli_publish.py::test_a_bad_token_exits_six`,
    `::test_a_network_failure_keeps_the_spool_and_exits_four`,
    `::test_a_missing_settings_file_exits_two`,
    `test_publisher.py::test_an_unreadable_remote_ledger_exits_five`.
  - G2-01: `test_collector.py::test_parse_fixture_matches_row_level_totals`,
    `::test_parse_fixture_day_cost_is_sum_of_models`,
    `::test_unconfigured_agent_is_kept`.
  - G2-02: `test_collector.py::test_malformed_documents_are_rejected`,
    `::test_missing_token_field_is_rejected`, `::test_missing_cost_field_is_rejected`,
    `::test_a_late_error_does_not_yield_a_partial_result`,
    `::test_run_collector_invalid_json`; `test_models.py::test_token_counts_must_be_non_negative`.
  - G2-03: `test_collector.py::test_configured_agent_without_usage_is_empty_and_ok`,
    `test_ledger.py::test_an_errored_agent_only_sets_the_last_error`,
    `test_publisher.py::test_error_agents_are_recorded_in_the_ledger`.
  - G2-04: `test_collector.py::test_parse_fixture_zero_cost_with_tokens_is_unknown`,
    `::test_agent_with_unknown_model_makes_the_day_unknown`,
    `test_reporting.py::test_unknown_cost_is_never_summed_as_zero`.
  - G2-05: `test_collector.py::test_run_collector_timeout`,
    `::test_run_collector_nonzero_exit_reports_stderr_tail`,
    `::test_run_collector_missing_executable`, `::test_run_collector_closes_stdin`.
    The Windows launcher half is by construction only (argument lists,
    never a shell); no Windows run exists.
  - G2-06: `test_snapshot_hash.py::test_hash_is_deterministic`,
    `::test_hash_ignores_key_order`, `test_snapshot.py::test_file_name_uses_the_short_hash`,
    `test_publisher.py::test_identical_collection_is_not_spooled`.
  - G2-07: `test_spool.py::test_a_crash_before_the_replace_leaves_nothing_complete`,
    `::test_every_listed_file_is_complete`, `::test_list_ignores_temporary_and_foreign_files`.
  - G2-08: `test_publisher.py::test_a_second_instance_exits_zero`.
  - G2-09: `test_publisher.py::test_collection_failure_still_drains_the_spool`,
    `::test_a_network_failure_leaves_the_spool_and_exits_four`,
    `test_spool.py::test_round_trip`.
  - G2-10: `test_publisher.py::test_dry_run_writes_nothing`,
    `test_cli_publish.py::test_dry_run_prints_a_summary_and_writes_nothing`.
  - G2-11: `test_ledger.py::test_rebuild_matches_the_incremental_ledger`,
    `::test_rebuild_is_independent_of_the_input_order`,
    `::test_every_permutation_produces_the_same_ledger`,
    `::test_the_monday_case_is_order_independent`,
    `::test_applying_the_same_snapshot_twice_changes_nothing`.
  - G2-12: `test_ledger.py::test_settled_records_are_never_replaced`,
    `::test_a_decrease_below_the_freeze_window_is_applied_and_logged`,
    `::test_a_decrease_of_a_settled_record_is_refused_and_logged`,
    `::test_every_shrinking_field_is_reported`.
  - G3-04: `test_publisher.py::test_an_identical_remote_document_ends_the_upload`,
    `::test_a_different_remote_document_is_a_conflict` (mocked transport;
    real GitHub pending).
  - G3-05: `test_publisher.py::test_a_lost_upload_response_is_acknowledged`
    (mocked transport; real GitHub pending).
  - G3-07: `test_github.py::test_rate_limit_honours_retry_after`,
    `::test_401_is_an_auth_error_and_is_never_retried`,
    `::test_the_retry_budget_stops_the_loop`,
    `test_publisher.py::test_uploads_are_throttled`,
    `::test_a_network_failure_leaves_the_spool_and_exits_four`
    (mocked transport; real GitHub pending).
  - G4-01: `test_ledger.py::test_applying_the_same_snapshot_twice_changes_nothing`,
    `::test_an_older_snapshot_never_overwrites_a_newer_record`,
    `::test_monday_morning_is_corrected_by_the_sunday_snapshot`,
    `::test_a_decrease_of_a_settled_record_is_refused_and_logged`.
  - G4-03: `test_ledger.py::test_rebuild_matches_the_incremental_ledger`,
    `test_cli_rebuild.py::test_rebuild_folds_every_snapshot_into_a_fresh_ledger`.
  - G4-12: `test_reporting.py::test_by_machine_totals`, `::test_by_agent_totals`,
    `::test_by_model_totals`, `::test_by_day_totals`,
    `::test_by_month_matches_fleet_totals`, `::test_filters_narrow_the_result`.
  - G4-14: `test_fetch.py::test_offline_never_touches_the_client`,
    `::test_network_failure_falls_back_to_the_cache`,
    `::test_invalid_remote_ledger_keeps_the_old_cache`,
    `::test_writes_the_cache_atomically`,
    `test_cli_show.py::test_offline_uses_the_cache_and_no_network`,
    `test_reporting.py::test_summary_reports_cache_use`.
  - G4-15: `test_reporting.py::test_render_json_is_stable_and_free_of_ansi`,
    `::test_render_csv_defuses_formula_injection`,
    `::test_render_csv_quotes_every_dangerous_prefix`,
    `test_readme_render.py::test_untrusted_labels_are_escaped`.
  - G5-01: `test_scheduling_cron.py::test_insert_preserves_everything_else`,
    `::test_install_is_idempotent`, `::test_reinstall_with_a_new_interval_replaces_only_the_block`,
    `test_scheduling_systemd.py::test_timer_unit_contents`,
    `::test_install_writes_units_and_enables_the_timer` (injected runners;
    no real install performed).
  - G5-02: `test_scheduling_cron.py::test_dry_run_changes_nothing`,
    `::test_uninstall_leaves_the_rest_intact`, `::test_uninstall_without_a_block_does_nothing`,
    `test_scheduling_systemd.py::test_dry_run_writes_nothing`,
    `::test_uninstall_disables_and_removes`,
    `test_scheduling_base.py::test_auto_picks_systemd_when_the_user_bus_answers`,
    `::test_auto_falls_back_to_cron_without_a_user_bus` (injected runners).
- QA criteria deliberately left open although partly covered: G1-05
  (Windows paths only via monkeypatched platformdirs), G1-08 (redaction
  tested; secret-file permission guidance not verified), G3-06 (stale-SHA
  retry tested; concurrent publishers not), G3-11 (crash repair tested in
  `test_publisher.py::test_a_snapshot_uploaded_before_a_crash_is_applied_later`;
  equality with `rebuild` afterwards not asserted), G4-02 (multi-machine
  permutation and duplication not encoded), G4-04 (whole-record replacement
  is by construction; no stale-model-row test), G4-05 (anomaly counts and
  README rows tested; field-level display in `show` not), G4-06 (unknown
  cost coverage tested; no policy-mismatch check exists in code). All gate
  exits remain open.
- Environment: Linux x86_64 (Debian, kernel 6.12), uv 0.12.1, CPython
  3.12.13 installed by uv, bun present, node absent, ccusage 20.0.20 via
  `bunx`, package version 0.1.0.
- Evidence: uncommitted working tree; test files named above; scratch
  artifacts were not kept.
- Risks/limitations: no real GitHub repository, token, workflow run,
  scheduler installation, Windows machine, Fedora, or Debian 11 machine has
  been exercised; all transport and scheduler evidence uses fakes; the
  adversarial review of `publisher`, `ledger`, `github`, and `fetch` is
  still pending and may reopen criteria; the Mermaid chart type has not
  been smoke-tested on GitHub.
- Next step: fold in the review findings, commit the baseline, then run
  G3-01 to G3-03 and G4-13 against a disposable private data repository
  with a real token.

## Template for subsequent implementation entries

Copy the following below the existing dated entries and before this template,
or append a new dated entry after the template. Keep history IDs increasing.

```markdown
### YYYY-MM-DD — Concrete change title

- History ID: HIST-NNN.
- Request/context: the work or correction being addressed.
- Implementation tasks: IMP-...; distinguish completed and partial tasks.
- Changes: concrete behavior and relevant repository-relative file paths.
- Decision changes: previous assumption, new decision, and reason; or none.
- Validation: exact commands/procedures and actual pass/fail/pending results.
- QA criteria/gates: checks completed, reopened, or still pending.
- Environment: relevant OS, architecture, runtime, and dependency versions.
- Evidence: commit hash, workflow URL, or sanitized local artifact path.
- Risks/limitations: unresolved behavior or untested platforms.
- Next step: bounded remaining work.
```

Do not mark a task done because its design was described. Do not mark a gate
passed because a different operating system or a mocked implementation passed.
For an interrupted batch, record partial work and leave its remaining boxes open.

### 2026-09-08 — Plan critique, snapshot revision proposal, external review

- History ID: HIST-002.
- Request/context: critique the baseline plan; propose a simpler data
  primitive; obtain Codex feedback on the proposal.
- Implementation tasks: none. Planning only.
- Changes: added `docs/plans/revision-snapshots.md` (proposal replacing delta
  exports, sequence numbers, and the Actions aggregator with immutable
  snapshots, a derived per-machine ledger with an order-independent settle
  rule, and read-side aggregation; scheduled README workflow for browser
  view) and `docs/plans/review-codex-2026-09-08.md` (verbatim review output).
- Decision changes: none recorded yet. The proposal is not accepted until the
  baseline plan is re-cut; sections 7 through 9 of the baseline remain the
  document of record until then.
- Validation: Codex review run read-only; sixteen findings; disposition of
  each in section 11 of the proposal. No code exists to test.
- QA criteria/gates: none affected. IMP-203/206/304/403 are proposed for
  removal, pending acceptance.
- Environment: Codex CLI 0.153.4; uv 0.12.1; bun present, node and ccusage
  absent on the authoring machine.
- Evidence: the two files above; no commit exists yet.
- Risks/limitations: ccusage packaging, timezone flag, Mermaid chart rendering,
  and Windows S4U behavior are unverified assumptions flagged for G0/G5.
- Next step: accept or amend the proposal, re-cut baseline sections 7 through
  9 and the affected IMP/QA items, then run G0-01 with `bunx ccusage`.

### 2026-09-08 — Snapshot revision accepted; plan and gates re-cut

- History ID: HIST-003.
- Request/context: user accepted the revision proposal after the Codex
  review; make it the document of record and prepare for implementation.
- Implementation tasks: none. Planning only.
- Changes: `implementation-plan.md` sections 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
  12, 13, and 14 rewritten for immutable snapshots, a derived per-machine
  ledger with the order-independent settle rule, read-side aggregation, the
  optional scheduled README workflow, systemd plus crontab Linux backends, and
  S4U-first Windows logged-out mode. `qa-gates.md` gates G2, G3, and G4
  re-cut; G0-04, G0-05, G1-10, G5-01, G5-02, G5-04, G5-08, G6-07, G6-10, and
  G6-11 reworded. `README.md` intro, document table, delivery boundary, and
  PLAN-005 updated. `revision-snapshots.md` status set to accepted.
- Decision changes: architecture, offline operation, aggregator, Unix
  scheduling, sources, and Windows alternative rows in the plan's section 2
  table now carry the revised decisions; freeze window 5 days added as a
  proposed default. Previous assumptions remain documented in
  `revision-snapshots.md` section 1.
- Removed items: IMP-206 (sequence reconciliation) and IMP-403 (serialized
  CI publication), shown struck through without checkboxes. No QA IDs were
  removed; G2-07, G2-08, G2-11, G3-06, G3-09, G4-01, G4-07, G4-08, and G4-09
  changed meaning and G3-11 was added.
- Validation: read-only Python check over `docs/plans`: relative links
  resolve, checkbox IDs unique, every IMP referenced by a gate exists, no
  leftover references to sequences, profiles, report branch, aggregate.json,
  totals.csv, outbox, SQLite (except the explicit "no longer used" note), or
  pinned commits outside removed-item annotations.
- Validation result: PASS; 133 unique checkbox IDs (PLAN 5, IMP 40, QA criteria 81, gate exits 7),
  all seven gate exit items present.
- QA criteria/gates: none executed. All gates remain pending.
- Environment: documentation only.
- Evidence: the files above; no commit exists yet.
- Risks/limitations: ccusage packaging, timezone flag, Mermaid chart
  rendering, and Windows S4U behavior remain unverified assumptions for
  G0/G5.
- Next step: PLAN-004; begin Phase 0 with G0-01 using real ccusage output.

### 2026-09-08 — Adversarial implementation review and fixes

- History ID: HIST-005.
- Request/context: review the implemented core modules before any real
  GitHub or scheduler use; fix what the review found.
- Implementation tasks: IMP-204, IMP-205, IMP-302, IMP-303, IMP-105
  hardened; no new tasks completed.
- Changes: review saved verbatim in `review-implementation-2026-09-08.md`.
  Fixes in `src/fleet_usage/publisher.py` (rate limits mapped to the
  network outcome, spool-side dedup, unreadable remote snapshots skipped
  and reported), `ledger.py` (anomalies keyed by agent/date/field/kind,
  capped at 500, earliest post-boundary observation wins), `models.py`
  (optional `source_snapshot` on ledger day records), `config.py`
  (`machine.id` restricted to `[A-Za-z0-9._-]{1,64}`),
  `commands/publish.py` (`OSError` reported as exit 1), `commands/doctor.py`
  (clock-behind-ledger warning), `commands/repo.py` (`--update` requires a
  parseable manifest; `register` retries manifest conflicts three times),
  `templates/readme.yml` (`python3`). Tests added in
  `tests/unit/test_publisher.py`, `test_ledger.py`, `test_config.py`,
  `tests/integration/test_cli_publish.py`, `test_cli_doctor.py`,
  `test_cli_repo.py`.
- Decision changes: among observations collected after the settle boundary
  the earliest wins, so the merge is order-independent for that case too;
  the anomaly list is a bounded, keyed set rather than an append log,
  superseding the HIST-004 note that it was order-dependent by
  construction. The hourly ledger PUT remains the heartbeat by design and
  its commit volume is a pilot measurement item (G6-10).
- Validation: `uv run ruff check .` pass; `uv run ruff format --check .`
  73 files; `uv run mypy src/fleet_usage` 34 files clean;
  `uv run pytest -q` 675 passed. Each fix was verified by reverting it and
  observing its test fail.
- QA criteria/gates: none newly checked. The review's "suspected" items on
  README rounding and empty-machine cost display remain open for G4-16 and
  G6-09.
- Environment: as HIST-004.
- Evidence: uncommitted working tree; the review file above.
- Risks/limitations: still no real GitHub repository, token, scheduler
  install, Windows machine, or second fleet machine exercised; the review
  covered the core modules only, not `reporting.py` or the scheduler
  backends in depth.
- Next step: commit the baseline, then run QA-G3 against a disposable
  private data repository with a real token.

### 2026-09-08 — Baseline committed

- History ID: HIST-006.
- Request/context: the user asked to commit the baseline and will supply
  the data repository name and token.
- Implementation tasks: none.
- Changes: single commit `aeb1110` on `main` containing everything
  described in HIST-001 through HIST-005. Where earlier entries say
  "uncommitted", read that as this commit.
- Validation: quality gate as recorded in HIST-005 at the same tree.
- Next step: `fleet-usage init` on this machine with the real repository,
  token in the generated `.env`, then `repo init --create` and QA-G3.
