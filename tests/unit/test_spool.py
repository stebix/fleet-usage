"""Tests for the local snapshot spool."""

import datetime as dt
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import pytest

from fleet_usage.models import (
    AgentSnapshot,
    CollectorInfo,
    DayRecord,
    Snapshot,
)
from fleet_usage.snapshot import agents_hash, snapshot_file_name
from fleet_usage.spool import (
    TMP_MAX_AGE_SECONDS,
    TMP_SUFFIX,
    list_spool,
    read_snapshot,
    remove,
    write_snapshot,
)

MACHINE_ID = '3f7c2f66-3d0f-4a1e-9a5a-1f4e4f0a1d11'
MOMENT = dt.datetime(2026, 9, 8, 13, 0, 4, tzinfo=dt.UTC)


def make_snapshot(collected_at=MOMENT, tokens=1):
    agents = {
        'claude': AgentSnapshot(
            status='ok',
            days=[
                DayRecord(
                    date=dt.date(2026, 9, 7),
                    input_tokens=tokens,
                    output_tokens=2,
                    cost_usd=Decimal('1.2345'),
                    cost_status='estimated',
                )
            ],
        ),
        'codex': AgentSnapshot(status='error', message='boom'),
    }
    return Snapshot(
        machine_id=MACHINE_ID,
        label='fedora-mobile',
        collected_at=collected_at,
        timezone='Europe/Berlin',
        collector=CollectorInfo(version='20.0.20'),
        agents_hash=agents_hash(agents),
        agents=agents,
    )


@pytest.fixture
def spool_dir(tmp_path):
    return tmp_path / 'state' / 'spool'


# ----------------------------------------------------------------- write


def test_write_creates_the_directory_and_file(spool_dir):
    snapshot = make_snapshot()
    path = write_snapshot(spool_dir, snapshot)
    assert path.parent == spool_dir
    assert path.name == snapshot_file_name(snapshot)
    assert path.is_file()


def test_written_file_is_pretty_sorted_json(spool_dir):
    path = write_snapshot(spool_dir, make_snapshot())
    text = path.read_text(encoding='utf-8')
    assert text.endswith('\n')
    assert '\n  "agents"' in text
    payload = json.loads(text)
    assert list(payload) == sorted(payload)
    assert payload['collected_at'] == '2026-09-08T13:00:04Z'
    assert payload['agents']['claude']['days'][0]['cost_usd'] == '1.2345'


def test_write_leaves_no_temporary_file(spool_dir):
    write_snapshot(spool_dir, make_snapshot())
    assert list(spool_dir.glob(f'*{TMP_SUFFIX}')) == []


def test_round_trip(spool_dir):
    snapshot = make_snapshot()
    restored = read_snapshot(write_snapshot(spool_dir, snapshot))
    assert restored.to_canonical_json() == snapshot.to_canonical_json()
    assert restored.agents['claude'].days[0].cost_usd == Decimal('1.2345')
    assert restored.agents['codex'].status == 'error'


def test_rewriting_the_same_snapshot_is_idempotent(spool_dir):
    snapshot = make_snapshot()
    first = write_snapshot(spool_dir, snapshot)
    second = write_snapshot(spool_dir, snapshot)
    assert first == second
    assert list_spool(spool_dir) == [first]


# ------------------------------------------------------------------ list


def test_list_of_a_missing_directory_is_empty(tmp_path):
    assert list_spool(tmp_path / 'nowhere') == []


def test_list_is_sorted_by_name(spool_dir):
    moments = [
        dt.datetime(2026, 9, 8, 15, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 8, 13, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 9, 1, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 10, 1, 0, 0, 0, tzinfo=dt.UTC),
    ]
    for index, moment in enumerate(moments):
        write_snapshot(spool_dir, make_snapshot(moment, tokens=index + 1))
    names = [path.name for path in list_spool(spool_dir)]
    assert names == sorted(names)
    assert names[0].startswith('20260908T130000Z')
    assert names[-1].startswith('20261001T000000Z')


def test_list_ignores_temporary_and_foreign_files(spool_dir):
    kept = write_snapshot(spool_dir, make_snapshot())
    (spool_dir / '20260908T140000Z-abcdef12.json.tmp').write_text(
        'half written', encoding='utf-8'
    )
    (spool_dir / 'notes.txt').write_text('hello', encoding='utf-8')
    (spool_dir / 'subdir.json').mkdir()
    assert list_spool(spool_dir) == [kept]


def test_list_keeps_fresh_temporary_files(spool_dir):
    spool_dir.mkdir(parents=True)
    fresh = spool_dir / '20260908T140000Z-abcdef12.json.tmp'
    fresh.write_text('in flight', encoding='utf-8')
    assert list_spool(spool_dir) == []
    assert fresh.is_file()


def test_list_removes_stale_temporary_files(spool_dir):
    spool_dir.mkdir(parents=True)
    stale = spool_dir / '20260908T140000Z-abcdef12.json.tmp'
    stale.write_text('abandoned', encoding='utf-8')
    old = time.time() - TMP_MAX_AGE_SECONDS - 60
    os.utime(stale, (old, old))
    assert list_spool(spool_dir) == []
    assert not stale.exists()


# -------------------------------------------------------------- crashing


def test_a_crash_before_the_replace_leaves_nothing_complete(
    spool_dir, monkeypatch
):
    def exploding_replace(self, target):
        raise OSError('power loss')

    monkeypatch.setattr(Path, 'replace', exploding_replace)
    with pytest.raises(OSError, match='power loss'):
        write_snapshot(spool_dir, make_snapshot())
    assert list_spool(spool_dir) == []
    assert len(list(spool_dir.glob(f'*{TMP_SUFFIX}'))) == 1
    monkeypatch.undo()

    # The next run completes and the spool holds exactly one readable
    # snapshot; the leftover temporary file is still not listed.
    path = write_snapshot(spool_dir, make_snapshot())
    assert list_spool(spool_dir) == [path]
    assert read_snapshot(path).machine_id == MACHINE_ID


def test_every_listed_file_is_complete(spool_dir):
    snapshot = make_snapshot()
    write_snapshot(spool_dir, snapshot)
    (spool_dir / '20260101T000000Z-deadbeef.json.tmp').write_text(
        '{"schema_ver', encoding='utf-8'
    )
    for path in list_spool(spool_dir):
        assert read_snapshot(path).agents_hash == snapshot.agents_hash


# ---------------------------------------------------------------- remove


def test_remove_deletes_the_file(spool_dir):
    path = write_snapshot(spool_dir, make_snapshot())
    remove(path)
    assert not path.exists()
    assert list_spool(spool_dir) == []


def test_remove_is_forgiving(spool_dir):
    spool_dir.mkdir(parents=True)
    remove(spool_dir / 'never-existed.json')


def test_remove_only_touches_one_snapshot(spool_dir):
    first = write_snapshot(spool_dir, make_snapshot(tokens=1))
    second = write_snapshot(
        spool_dir,
        make_snapshot(dt.datetime(2026, 9, 8, 14, 0, 0, tzinfo=dt.UTC), 2),
    )
    remove(first)
    assert list_spool(spool_dir) == [second]
