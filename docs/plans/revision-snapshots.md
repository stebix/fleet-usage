# Revision proposal: snapshot primitive and read-side aggregation

Status: Accepted 2026-09-08 and merged into
[implementation-plan.md](implementation-plan.md) and [qa-gates.md](qa-gates.md);
retained for rationale and review log.

Created: 2026-09-08. Revised: 2026-09-08 after
[Codex review](review-codex-2026-09-08.md).

## 1. Why revise

The baseline plan is sized for a multi-tenant telemetry pipeline. The fleet is
five machines owned by one person. Two decisions drive most of its complexity
and both can be dropped:

1. Changed-record delta exports with per-publisher sequence numbers. These
   require SQLite, atomic sequence allocation, and remote sequence
   reconciliation, and they block publishing when local state is lost while
   offline.
2. A GitHub Actions aggregator. With five publishers, a viewer can aggregate on
   read. The aggregator also brings a pinned code checkout, private-code
   credentials, a report branch, run serialization, and a budget problem:
   hourly pushes from five machines are about 3,600 workflow runs per month,
   which exceeds the free private-repository Actions quota.

Additional defects found in review of the baseline:

- Claude Code deletes session files per file after a configurable cleanup
  period, so old daily totals shrink partially before disappearing. Treating
  every decrease as an anomaly makes the anomaly signal fire daily.
- A minute-tick cron plus `publish --if-due` spawns a Python process 1,440
  times a day to support intervals nobody asked for.
- Fedora Workstation does not ship cronie by default; systemd user timers are
  the native choice there and provide catch-up after suspend.
- The `profile` field is used in the key but never defined.
- No path exists for a new machine to register itself in `fleet.toml`.
- Per-project or per-instance ccusage breakdowns would ship project directory
  names to GitHub; this needs an explicit decision.
- The Windows S4U logon type is ruled out by documentation wording; HTTPS with
  a bearer token may work under it and this should be tested.

## 2. The snapshot primitive

A snapshot is one immutable JSON file per collection run containing every
daily record ccusage currently reports on that machine. It is complete on its
own and carries identity and provenance.

Path, keyed by UTC collection time plus a short hash of the `agents` object.
Names sort chronologically; the hash makes two runs in the same second
collision-free and makes the name verifiable from the content:

```text
snapshots/<machine-id>/<YYYY-MM>/<YYYYMMDDTHHMMSSZ>-<agents-hash-8>.json
```

Content:

```json
{
  "schema_version": 1,
  "machine_id": "9f1c2d3e-...",
  "label": "fedora-mobile",
  "collected_at": "2026-09-08T13:00:04Z",
  "timezone": "Europe/Berlin",
  "collector": {"name": "ccusage", "version": "17.1.3", "mode": "calculate", "pricing": "offline"},
  "agents_hash": "sha256:...",
  "agents": {
    "claude": {
      "status": "ok",
      "days": [
        {
          "date": "2026-09-07",
          "input_tokens": 12345,
          "output_tokens": 6789,
          "cache_create_tokens": 40210,
          "cache_read_tokens": 918822,
          "cost_usd": "1.2345",
          "models": [
            {"model": "claude-fable-5-1", "input_tokens": 12345, "output_tokens": 6789,
             "cache_create_tokens": 40210, "cache_read_tokens": 918822, "cost_usd": "1.2345"}
          ]
        }
      ]
    },
    "codex": {"status": "error", "message": "exit 1: ..."}
  }
}
```

`agents_hash` is the hash of the canonical JSON of `agents` only, so two
collections with identical usage but different times hash equally. Cost is a
pure function of the pinned collector version because pricing is offline and
the cost mode is explicit; the collector block is the pricing provenance.

Snapshots contain daily totals and model breakdowns only. No per-project or
per-instance breakdown is uploaded.

## 3. The machine ledger and merge rule

Each publisher maintains one derived file holding its own daily records after
the merge rule, plus per-agent status. It is what viewers read and it is the
heartbeat. One file per machine, fetched with the raw media type so it may
exceed 1 MB:

```text
machines/<machine-id>.json
```

```json
{
  "schema_version": 1,
  "machine_id": "9f1c2d3e-...",
  "label": "fedora-mobile",
  "merge_policy": {"version": 1, "freeze_window_days": 5, "timezone": "Europe/Berlin"},
  "last_run_at": "2026-09-08T13:00:04Z",
  "applied_through": "snapshots/9f1c2d3e-.../2026-09/20260908T130004Z-3fa9c1b2.json",
  "last_agents_hash": "sha256:...",
  "agents": {
    "claude": {
      "last_success_at": "2026-09-08T13:00:04Z",
      "last_error": null,
      "days": [
        {"date": "2026-09-07", "source_collected_at": "2026-09-08T13:00:04Z",
         "frozen": true, "input_tokens": 12345, "...": "..."}
      ]
    }
  },
  "anomalies": [
    {"agent": "claude", "date": "2026-08-01", "field": "input_tokens",
     "kept": 50000, "observed": 41000, "snapshot": "snapshots/.../20260901T...json",
     "kind": "decrease_after_freeze"}
  ]
}
```

Merge rule when applying a snapshot with collection time `T` to the ledger,
per `(agent, date D)` where the agent's snapshot status is `ok`:

1. The record is **settled** once its `source_collected_at` is at or after the
   end of `D` plus the freeze window, evaluated in the reporting timezone.
   Settled records are never replaced by data.
2. If the ledger has no record for `D`, insert the snapshot record with
   `source_collected_at = T`, at any age.
3. Otherwise replace the record only if `T` is later than the record's
   `source_collected_at` and the record is not settled.
4. Any replacement or refused replacement in which a token count decreases is
   appended to `anomalies` with both values, the snapshot path, and whether
   the decrease was applied or refused.
5. Dates absent from the snapshot are kept. Agents whose snapshot status is not
   `ok` are untouched except for `last_error`.

Because the rule is last-writer-by-collection-time per record and settling is
a function of the record's own source time, the result is independent of the
order in which snapshots are applied. A machine shut down between a Monday
morning collection and the following Sunday gets Monday's complete total from
Sunday's snapshot, because the Monday record's source was collected on Monday
and is not yet settled. Sunday's snapshot then settles it. A later retention
shrink is refused and logged.

Known limits, unchanged in kind from the baseline: partial retention deletion
before a record settles is applied and only logged; copied Claude Code
histories on two machines double count. A manual correction file per machine
under `corrections/` is reserved for a later phase and applied at read time.

The ledger is derived state. `rebuild` re-derives it by applying that
machine's snapshots in any order and yields the same result.

## 4. Publish sequence

Local state is a spool directory of pending snapshots and a process lock.
Nothing else. The ledger is always fetched from the remote before use.

1. Acquire the local process lock. A second instance exits immediately.
2. GET the remote ledger with its blob SHA. If absent, start empty.
3. Run ccusage per configured agent with pinned executable, offline pricing,
   explicit cost mode, and the fleet timezone. Build the snapshot. If its
   `agents_hash` equals the ledger's `last_agents_hash`, do not spool it.
   Otherwise write it to the spool via temporary file and rename. A collection
   failure for one agent is recorded in the snapshot status; a failure of all
   agents produces no snapshot but the run continues.
4. Drain the spool in name order, throttled below GitHub's secondary
   content-creation limits. PUT each file to its path. On any error response,
   GET the path: identical content is success, different content is a hard
   error surfaced to the user, absence means retry with bounded backoff.
   Delete a spooled file only after its remote presence is confirmed.
5. List snapshots after `applied_through` using the Git Trees API on the
   machine's subtree, and apply every one to the ledger in name order. This
   covers a crash between step 4 and step 6 on an earlier run and any
   snapshot uploaded by a rebuild elsewhere.
6. Update per-agent status and `last_run_at`, then PUT the ledger with the
   SHA from step 2. On a conflict, repeat from step 2. Two writers of the same
   ledger can only be two runs on the same machine, which the lock prevents.

A snapshot whose name sorts before `applied_through` because of a clock
rollback is not applied incrementally; `doctor` warns when the local clock is
behind `last_run_at`, and `rebuild` repairs the ledger. Unuploaded spool
content is by definition not remotely recoverable; the spool is the
offline buffer and nothing more.

Exit codes: success, configuration error, collection failure, snapshot
spooled but not uploaded, remote conflict.

## 5. View sequence

`show` fetches `fleet.toml` and the ledger of every machine listed in it,
applies any corrections, sums, and renders. Freshness per machine and per
agent comes from the ledger status fields. A manifest machine with no ledger
is reported as never reported. Ledgers not listed in the manifest are ignored
and mentioned as unregistered. Conditional requests and the offline cache
apply to these files.

## 6. Browser presentation via README

Optional; nothing depends on it. The data repository carries a scheduled
workflow that regenerates `README.md` on the default branch from the ledgers.

- The rendering logic lives in one dependency-free module inside the package.
  `fleet-usage repo init` copies that module verbatim into the data repository
  as `scripts/render_readme.py` next to `.github/workflows/readme.yml`. The
  workflow therefore needs no checkout of the code repository, no private-code
  credential, and no dependency installation. `repo init --update` refreshes
  both files; the README records the renderer version.
- The script reads `fleet.toml` and applies the same manifest filtering and
  corrections as `show`, so both views agree.
- Trigger: `schedule` every few hours at an off-zero minute, plus
  `workflow_dispatch`. Not on push. The 60-day inactivity disablement applies
  to public repositories only, and pushes keep the repository active anyway.
- Job: `permissions: contents: write`, checkout, run the script with the
  runner's Python, then write `README.md` through the Contents API with the
  current blob SHA and a bounded retry, since a publisher may advance the
  branch between checkout and push. Pushes made with the built-in token do
  not trigger workflows, and the schedule-only trigger prevents loops anyway.
- `concurrency` with a single group and `cancel-in-progress: true`.
- Content: fleet totals, a per-machine table with absolute collection and
  render timestamps, a per-model table, and a Mermaid `xychart-beta` daily
  cost chart if a smoke test confirms GitHub renders it; tables are kept
  regardless.
- `show --web` opens the repository's default branch page, which requires the
  viewer to be signed in to GitHub.

Alternative considered: each publisher writes README on every run. Rejected
because it needs every publisher to fetch all ledgers and adds a write per
run for a view few people will read.

## 7. Scheduling revision

- Intervals restricted to cron-expressible values, hourly by default. No
  minute tick. `publish --if-due` remains for login catch-up only.
- Linux backends: systemd user timer using `OnCalendar=hourly` with
  `Persistent=true`, and user crontab, both in the first version.
  `backend = 'auto'` picks systemd when a user manager is available, crontab
  otherwise. Document `loginctl enable-linger` for servers using timers.
- Windows: logged-in default unchanged. Test S4U for logged-out mode before
  building password-backed logon; keep password-backed logon as the fallback
  if S4U cannot reach GitHub.

## 8. Manifest and identity

- `profile` is removed. One publisher identity per OS user per machine.
- `fleet-usage init` prints the machine entry; `fleet-usage repo register`
  appends it to `fleet.toml` with the publisher token.
- Sources: the configuration lists agent commands explicitly. Whether the
  selected ccusage release ships agents as one command or separate packages
  is verified in G0-01, not assumed.

## 9. Removed from the baseline plan

Removed: SQLite, sequence numbers, atomic sequence allocation, remote sequence
reconciliation, changed-record diffing, export IDs, the Actions aggregator,
the pinned code checkout and private-code credential, the report branch, run
serialization, and stale-report protection. IMP-203, IMP-206, IMP-304, and
IMP-403 are dropped; IMP-204 and IMP-401 shrink to the merge rule; IMP-402
and IMP-405 reduce to the README workflow.

Kept, because they are cheap in this design and the review confirmed they
are needed: the process lock, temporary-file-and-rename spool writes,
deterministic replay via `rebuild`, truthful per-agent heartbeats, the lost
response check, and the failure-injection gates that cover them.

## 10. Remaining known limits

Copied Claude Code histories on two machines double count. Partial retention
deletion before a record settles is applied and logged rather than corrected.
A machine powered off longer than the Claude Code cleanup period loses the
unsettled days. All require an event-level design that is out of scope.

## 11. Review log

2026-09-08, Codex CLI 0.153.4, read-only sandbox, prompt and full output in
[review-codex-2026-09-08.md](review-codex-2026-09-08.md). Sixteen findings.

Accepted and applied: settle rule evaluated against the record's own source
time rather than wall clock (findings 1, 2, 5); ledger derived from remote
snapshots with a checkpoint so crashes and stale caches self-heal (3, 4);
hash in snapshot names and process lock kept (5); spool always drained,
dedup hash excludes collection time (7); single ledger file with per-agent
status instead of yearly shards (8); error classification by content
comparison (9); Trees API listing and throttled backlog (10); README uses the
manifest and shared logic (11); Contents API write with SHA retry and explicit
permissions in the workflow (12); absolute timestamps and off-zero schedule
(13); chart smoke test (14); ccusage packaging left to G0 (15);
`OnCalendar` for persistent timers and Windows fallback retained (16).

Accepted as documented limits rather than mechanisms: durable tombstones and
corrections (6) are reserved under `corrections/` for a later phase.

Rejected: removing deduplication entirely (7). Idle servers would otherwise
upload identical hourly snapshots; the hash lives in the remote ledger, so it
adds no local state.
