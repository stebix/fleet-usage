# Codex review of revision-snapshots.md

Date: 2026-09-08. Tool: Codex CLI 0.153.4, `codex exec --sandbox read-only --ephemeral`.
Target: `docs/plans/revision-snapshots.md` as it stood before section 11 was added.
The reviewer was asked for numbered findings with severity and a one-line fix each.
Disposition of each finding is recorded in section 11 of the proposal.

## Output (verbatim)

The simplification is reasonable, but §§3–4 do not yet guarantee that published ledgers equal snapshot replay.

1. **High — Freeze loses late totals (§3).** Collect 100 tokens Monday morning, use another 50, then shut down until Sunday. Sunday’s complete 150 is ignored because Monday already exists and is frozen. “Insert if absent” does not help. **Fix:** define explicit late-correction handling; a five-day gap assumption is insufficient.

2. **High — Replay depends on an unspecified clock (§3).** If age means upload/rebuild time, replaying old snapshots freezes each date at its first observation. Even collection-time evaluation needs an exact five-day boundary and reporting timezone. **Fix:** evaluate against each snapshot’s `collected_at`; persist the merge-policy version, window, and timezone.

3. **High — Acknowledgment precedes durable ledger application (§4).** Upload succeeds, spool file is deleted, then the process crashes before saving the ledger. Ordinary publication never revisits that snapshot, while rebuild includes it. **Fix:** retain pending application until the ledger checkpoint is durable; reconcile remote snapshots after recovery.

4. **High — Existing cached state can erase remote history (§4).** Only fetching when the cache is missing misses restored/stale caches. Fetching a fresh SHA and uploading stale content merely authorizes the overwrite. Initialization also appears after snapshot application. **Fix:** load/reconcile the base before merging; bind cached content to its remote SHA and recompute after conflicts.

5. **High — Timestamp filenames do not replace locking or ordering (§§2–4).** Manual and scheduled runs can collide within one second. Clock rollback or concurrent completion can make incremental application order differ from path-order rebuild, changing totals and anomalies. **Fix:** retain publisher exclusion, collision-resistant IDs, and a deterministic ordering/reconciliation rule.

6. **High — Freeze does not solve deletion or correction (§3).** Partial deletion inside the window still replaces a larger total; logging an anomaly does not preserve accounting. Frozen overcounts and erroneous dates that later disappear cannot be corrected. Claude’s retention is configurable, with cleanup at startup. [Claude settings](https://code.claude.com/docs/en/settings). **Fix:** retain the baseline’s accounting limitations and add durable correction/tombstone records.

7. **Medium — Hash skipping is internally inconsistent (§4).** Whole-snapshot hashes change with `collected_at`. Excluding metadata enables skipping, but jumping to step 4 strands older queued snapshots and leaves heartbeat behavior undefined. Unuploaded spool contents are also not remotely recoverable. **Fix:** preferably remove deduplication; always drain valid queued files, including after collection failure.

8. **Medium — Annual files and health need contracts (§§3–5).** January collections can update December or older years; “PUT the ledger” does not specify all affected files. One machine timestamp can mask a failed agent. Current/previous-year fetching cannot implement arbitrary historical filters. **Fix:** partition by reporting date, update every affected year, and retain per-agent successful-collection timestamps/status.

9. **Medium — Contents conflicts require classification (§4).** Updates require the existing blob SHA; responses include both `409` and `422`, neither universally meaning “path exists.” Separate paths still share branch updates. Snapshot equality correctly handles lost responses, but ledger retries need equivalent verification. [Contents API](https://docs.github.com/en/rest/repos/contents). **Fix:** inspect errors, GET/compare uncertain writes, remerge changed ledgers, and retry branch races with bounded backoff.

10. **Medium — Monthly sharding has no general listing guarantee (§2).** Hourly collection gives at most 744 files/month; half-hourly gives 1,488, exceeding the 1,000-entry limit. Large JSON needs raw retrieval above 1 MB. [Contents limits](https://docs.github.com/en/rest/repos/contents). Catch-up also encounters content-generation limits, generally 80/minute and 500/hour. [Rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api). **Fix:** shard by day, support raw downloads, and throttle backlog uploads.

11. **Medium — README aggregation differs from CLI aggregation (§6).** Globbing `machines/*/*.json` includes unregistered/retired identities and cannot identify expected machines that never reported. This can inflate totals or hide missing publishers. **Fix:** use `fleet.toml` and identical filtering/validation semantics in both renderers.

12. **Medium — Workflow concurrency does not serialize publishers (§6).** Publishers can advance the branch between checkout and README push, causing rejection. **Fix:** fetch and rerender on push races; explicitly grant `contents: write` and retain workflow-file bootstrap authority. [Token permissions](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token), [bootstrap permissions](https://docs.github.com/en/rest/repos/contents).

13. **Low — README is convenient, but freshness is static (§6).** Schedules can be delayed or dropped; opening the page does not refresh data. The 60-day inactivity disablement applies to public repositories. [Scheduling behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). **Fix:** show absolute collection/render timestamps and a refresh link; schedule away from minute zero.

14. **Low — Rendering and loop assumptions mostly hold (§6).** Token pushes do suppress push-triggered workflows; schedule-only triggering already prevents that loop. [Trigger behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow). Mermaid is supported, but I have not verified `xychart-beta` against GitHub’s deployed version. [Diagram support](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams). **Fix:** smoke-test the chart and retain tables; private browser viewing requires authentication.

15. **Medium — ccusage assertions need release qualification (§§2,8).** Current documentation describes unified commands, so “agents are separate packages” is not universally current. [ccusage](https://ccusage.com/guide/claude/). I could not verify 17.1.3’s pricing-cache immutability. **Fix:** verify the selected artifacts/options and record provenance per agent.

16. **Low — Scheduler claims need qualification (§7).** `Persistent=true` requires `OnCalendar`; S4U bearer-HTTPS suitability remains unverified despite Microsoft’s documented network restriction. [systemd](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml), [Microsoft](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type). **Fix:** specify calendar timers and retain the Windows fallback pending testing.

**Section 9 removes too much:** SQLite and central aggregation can go; locking, atomic outbox writes, stale-state recovery, deterministic replay, corrections, truthful heartbeats, and their failure-case gates remain necessary.
