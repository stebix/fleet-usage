# Adversarial implementation review, 2026-09-08

Reviewer: Claude Opus subagent, read-only, against the uncommitted tree after
all five implementation phases and the coordinator's integration fixes
(647 tests passing). Target modules: `publisher.py`, `ledger.py`,
`github.py`, `fetch.py`, `spool.py`, `snapshot.py`, `collector.py`,
`commands/repo.py`, `templates/readme.yml`, `readme_render.py`.
Disposition of each finding is recorded in HIST-005 in
[implementation-history.md](implementation-history.md).

## Baseline and live check

`uv run pytest -q`: 647 passed. Real dry run with a throwaway config:

```text
collected at 2026-09-08T14:56:18Z
claude ok 8 days 2026-09-08 182,100,149 tok 252.311036 (+1 unknown)
codex  ok 11 days 2026-09-08  58,754,608 tok  45.142194 (+1 unknown)
dry run: nothing was written   EXIT=0
```

The dry run wrote nothing except an empty `publish.lock` in the real state
directory: no spool file, no due marker, user config untouched. Note that
`--config` moves settings only; spool, lock, and cache stay in the platform
user directories.

## Confirmed by reading or running

1. **A GitHub rate limit is reported as an authentication failure and aborts
   the run.** `RateLimitError` subclasses `AuthError`, and three `except
   AuthError` sites in `publisher.py` do not catch `RateLimitError` first.
   A 403 secondary rate limit on the snapshot PUT yields exit 6 and the
   ledger heartbeat silently stops. Fix: catch `RateLimitError` before
   `AuthError` and map it to the network outcome.
2. **Anomalies grow without bound during log-retention shrink.** One
   anomaly per shrinking field, per snapshot, per hour, no dedup or cap.
   Once Claude Code prunes a settled day, roughly 700 anomalies per week per
   agent accumulate and the ledger bloats. Fix: key anomalies by
   (agent, date, field, kind) and cap the list.
3. **An offline machine spools one identical snapshot per hour.** Dedup
   compares only against the remote ledger's `last_agents_hash`, which is
   stale while offline. A day offline produces 24 identical blobs and 24
   commits on reconnect. Nothing is lost. Fix: also compare against the
   newest spooled file.
4. **One stray `.json` under `snapshots/<id>/` blocks publishing
   permanently.** A `ValidationError` on a remote snapshot becomes exit 5,
   the ledger is never written, and the file is still there next run. Fix:
   log, report, and skip unparseable remote snapshots.
5. **`repo init --update` overwrites files in any repository.** It rewrites
   the workflow and script without checking that `fleet.toml` exists and
   parses, contradicting plan section 9. The Actions-permission check the
   plan describes is also not implemented. Fix: require a parseable manifest.
6. **The merge is not fully order-independent** when two snapshots both
   collected after the settle boundary are applied for a date with no prior
   record: forward order keeps the earlier value with a refused-decrease
   anomaly, reverse order keeps the later value silently. Both call sites
   apply in path order so practice is safe, and the permutation test does
   not contain such a pair. Fix: define "earliest post-boundary observation
   wins" and implement it symmetrically.
7. **Unhandled `OSError` escapes as a traceback** from spool writes and lock
   acquisition; a full or read-only state directory prints a stack trace.
8. **`doctor` never warns about a clock behind `last_run_at`,** which the
   plan requires as the mitigation for a rolled-back clock making a snapshot
   sort before `applied_through`.
9. **`machine.id` is only checked for non-blankness** although it is
   interpolated into remote paths. Fix: restrict it to `[A-Za-z0-9._-]+`.

## Verified with no finding

- PUT-error verification compares raw bytes, then JSON equality, never a
  blob SHA; identical snapshots cannot be misreported as conflicts nor the
  reverse.
- Settle boundary adds calendar days then localises midnight; both DST
  transitions are tested.
- `list_tree` handles `truncated` via subtree walks then a Contents-API
  fallback; an empty repository and a missing branch both return an empty
  list. Month directories are zero-padded so lexical order is chronological.
- `_request` never retries a non-rate-limit 401 or 403; PUT bodies are
  base64 with an explicit branch; GETs pass `ref`; files above 1 MB fall
  back to the raw media type. The token appears only in the client header.
- Collector uses `shutil.which`, `shell=False`, a timeout, and
  `stdin=DEVNULL`; cost 0 with tokens maps to unknown; no float reaches
  `Decimal` except through `str`.
- `readme_render.py` is stdlib-only, escapes Markdown metacharacters, and
  its unknown-cost arithmetic matches `reporting.py`.
- The workflow template is schedule plus dispatch only, `contents: write`,
  concurrency-guarded, and writes the README through the Contents API with
  SHA retry.

## Suspected, not verified end to end

- `templates/readme.yml` invokes `python`, not `python3`, with no
  `setup-python` step.
- `readme_render._sum_records` returns an unknown cost for an empty record
  set, so a machine with no usage renders a dash; the README rounds to four
  decimals while `show` prints full precision.
- `repo register` PUTs `fleet.toml` with no conflict retry, so two machines
  registering at once give one of them exit 5.
- The ledger is re-PUT every hour even when deduplicated, about 120 commits
  per day across five idle machines; intended as the heartbeat, but worth
  measuring against the pilot growth budget.
- The publisher test double ignores `sha` on `put_file`, so optimistic
  concurrency on the ledger is only simulated by injected exceptions; no
  test covers a second offline run or a rate-limited run.
