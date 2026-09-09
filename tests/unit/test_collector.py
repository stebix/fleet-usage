"""Tests for running and parsing the ccusage collector."""

import copy
import datetime as dt
import json
import subprocess
import sys
from decimal import Decimal

import pytest

from fleet_usage.collector import (
    CollectorError,
    build_command,
    collector_version,
    load_fixture,
    parse_daily_by_agent,
    run_collector,
)
from fleet_usage.config import CollectorSettings

AGENTS = ['claude', 'codex']


def settings(**overrides):
    values = {
        'command': ['bunx', 'ccusage@20.0.20'],
        'agents': list(AGENTS),
        'timezone': 'Europe/Berlin',
        'timeout_seconds': 180,
        'offline_pricing': True,
    }
    values.update(overrides)
    return CollectorSettings(**values)


def script_settings(tmp_path, body, name='fake_ccusage.py', **overrides):
    """Return settings that launch a small python program as collector."""
    script = tmp_path / name
    script.write_text(body, encoding='utf-8')
    return settings(command=[sys.executable, str(script)], **overrides)


@pytest.fixture
def payload(ccusage_fixture_dir):
    return load_fixture(ccusage_fixture_dir / 'daily-by-agent.json')


# --------------------------------------------------------------- command


def test_build_command_offline():
    assert build_command(settings()) == [
        'bunx',
        'ccusage@20.0.20',
        'daily',
        '--json',
        '--by-agent',
        '-z',
        'Europe/Berlin',
        '--offline',
    ]


def test_build_command_online_omits_offline():
    command = build_command(settings(offline_pricing=False))
    assert '--offline' not in command
    assert command[-2:] == ['-z', 'Europe/Berlin']


def test_build_command_keeps_configured_prefix():
    command = build_command(settings(command=['ccusage']))
    assert command[0] == 'ccusage'
    assert command[1:4] == ['daily', '--json', '--by-agent']


def test_build_command_does_not_mutate_settings():
    collector = settings()
    build_command(collector)
    assert collector.command == ['bunx', 'ccusage@20.0.20']


# ------------------------------------------------------------------- run


def test_run_collector_returns_parsed_json(tmp_path):
    collector = script_settings(
        tmp_path,
        'import json, sys\n'
        "print(json.dumps({'daily': [], 'argv': sys.argv[1:]}))\n",
    )
    payload = run_collector(collector)
    assert payload['daily'] == []
    assert payload['argv'] == build_command(collector)[2:]


def test_run_collector_keeps_the_configured_name_as_argv0(monkeypatch):
    """The collector must see the name it was configured with.

    ``bunx`` is a copy of ``bun`` that only runs a package when it is
    called ``bunx``, and on Windows ``shutil.which`` reports the
    extension in the case of ``PATHEXT`` (``bunx.EXE``) rather than the
    case on disk. Putting the looked up path into the argument vector
    would therefore make ``bun`` treat ``ccusage@X.Y.Z`` as a script
    file; the path belongs in ``executable`` instead.
    """
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"daily": []}', '')

    monkeypatch.setattr('shutil.which', lambda name: '/usr/bin/BUNX.EXE')
    monkeypatch.setattr('subprocess.run', fake_run)
    collector = settings()
    assert run_collector(collector) == {'daily': []}
    argv, kwargs = calls[0]
    assert argv == build_command(collector)
    assert kwargs['executable'] == '/usr/bin/BUNX.EXE'


def test_run_collector_missing_executable():
    collector = settings(command=['fleet-usage-no-such-collector'])
    with pytest.raises(CollectorError) as error:
        run_collector(collector)
    assert 'not found on PATH' in str(error.value)
    assert 'fleet-usage-no-such-collector' in str(error.value)


def test_run_collector_timeout(tmp_path):
    collector = script_settings(
        tmp_path, 'import time\ntime.sleep(30)\n', timeout_seconds=1
    )
    with pytest.raises(CollectorError, match='timed out after 1s'):
        run_collector(collector)


def test_run_collector_nonzero_exit_reports_stderr_tail(tmp_path):
    collector = script_settings(
        tmp_path,
        'import sys\n'
        'for index in range(40):\n'
        "    print(f'line {index}', file=sys.stderr)\n"
        'sys.exit(3)\n',
    )
    with pytest.raises(CollectorError) as error:
        run_collector(collector)
    message = str(error.value)
    assert 'status 3' in message
    assert 'line 39' in message
    assert 'line 20' in message
    assert 'line 19' not in message


def test_run_collector_invalid_json(tmp_path):
    collector = script_settings(tmp_path, "print('not json at all')\n")
    with pytest.raises(CollectorError, match='valid JSON'):
        run_collector(collector)


def test_run_collector_rejects_non_object_json(tmp_path):
    collector = script_settings(tmp_path, "print('[1, 2, 3]')\n")
    with pytest.raises(CollectorError, match='expected an object'):
        run_collector(collector)


def test_run_collector_closes_stdin(tmp_path):
    collector = script_settings(
        tmp_path,
        'import json, sys\n'
        'data = sys.stdin.read()\n'
        "print(json.dumps({'daily': [], 'stdin': data}))\n",
    )
    assert run_collector(collector)['stdin'] == ''


# --------------------------------------------------------------- version


def test_collector_version(tmp_path):
    collector = script_settings(tmp_path, "print('ccusage 20.0.20')\n")
    assert collector_version(collector) == '20.0.20'


def test_collector_version_uses_last_non_empty_line(tmp_path):
    collector = script_settings(
        tmp_path, "print('noise')\nprint('ccusage 20.0.20')\nprint()\n"
    )
    assert collector_version(collector) == '20.0.20'


def test_collector_version_accepts_bare_version(tmp_path):
    collector = script_settings(tmp_path, "print('20.0.20')\n")
    assert collector_version(collector) == '20.0.20'


def test_collector_version_rejects_unparseable_output(tmp_path):
    collector = script_settings(tmp_path, "print('who knows')\n")
    with pytest.raises(CollectorError, match='cannot parse'):
        collector_version(collector)


def test_collector_version_rejects_empty_output(tmp_path):
    collector = script_settings(tmp_path, 'pass\n')
    with pytest.raises(CollectorError, match='no version'):
        collector_version(collector)


def test_collector_version_reports_failure(tmp_path):
    collector = script_settings(
        tmp_path, 'import sys\nsys.exit(1)\n', name='failing.py'
    )
    with pytest.raises(CollectorError, match='status 1'):
        collector_version(collector)


# ------------------------------------------------------- fixture parsing


def test_parse_fixture_day_counts(payload):
    agents = parse_daily_by_agent(payload, AGENTS)
    assert sorted(agents) == ['claude', 'codex']
    assert all(agent.status == 'ok' for agent in agents.values())
    assert len(agents['claude'].days) == 8
    assert len(agents['codex'].days) == 11


def test_parse_fixture_matches_row_level_totals(payload):
    agents = parse_daily_by_agent(payload, AGENTS)
    row = next(
        item for item in payload['daily'] if item['period'] == '2026-08-09'
    )
    day = dt.date(2026, 8, 9)
    records = [
        next(record for record in agents[name].days if record.date == day)
        for name in ('claude', 'codex')
    ]
    assert sum(record.input_tokens for record in records) == row['inputTokens']
    assert (
        sum(record.output_tokens for record in records) == row['outputTokens']
    )
    assert (
        sum(record.cache_create_tokens for record in records)
        == row['cacheCreationTokens']
    )
    assert (
        sum(record.cache_read_tokens for record in records)
        == row['cacheReadTokens']
    )
    assert sum(record.total_tokens for record in records) == row['totalTokens']
    total = sum(record.cost_usd for record in records)
    assert float(total) == pytest.approx(row['totalCost'])


def test_parse_fixture_known_cost_is_exact(payload):
    agents = parse_daily_by_agent(payload, AGENTS)
    day = next(
        record
        for record in agents['codex'].days
        if record.date == dt.date(2026, 8, 5)
    )
    assert day.cost_usd == Decimal('0.145127')
    assert day.cost_status == 'estimated'
    assert day.models[0].cost_usd == Decimal('0.145127')
    assert day.models[0].model == 'gpt-5.6-sol'


def test_parse_fixture_zero_cost_with_tokens_is_unknown(payload):
    agents = parse_daily_by_agent(payload, AGENTS)
    for name in ('claude', 'codex'):
        day = next(
            record
            for record in agents[name].days
            if record.date == dt.date(2026, 9, 8)
        )
        assert day.total_tokens > 0
        assert day.cost_usd is None
        assert day.cost_status == 'unknown'
        assert [model.cost_status for model in day.models] == ['unknown']


def test_parse_fixture_day_cost_is_sum_of_models(payload):
    agents = parse_daily_by_agent(payload, AGENTS)
    day = next(
        record
        for record in agents['claude'].days
        if record.date == dt.date(2026, 8, 9)
    )
    assert len(day.models) == 2
    assert day.cost_usd == sum(model.cost_usd for model in day.models)
    assert day.cost_status == 'estimated'


def test_parse_fixture_records_are_sorted(payload):
    agents = parse_daily_by_agent(payload, AGENTS)
    for agent in agents.values():
        dates = [record.date for record in agent.days]
        assert dates == sorted(dates)
        for record in agent.days:
            names = [model.model for model in record.models]
            assert names == sorted(names)


def test_configured_agent_without_usage_is_empty_and_ok(payload):
    agents = parse_daily_by_agent(payload, [*AGENTS, 'gemini'])
    assert agents['gemini'].status == 'ok'
    assert agents['gemini'].days == []
    assert agents['gemini'].message is None


def test_unconfigured_agent_is_kept(payload):
    agents = parse_daily_by_agent(payload, ['claude'])
    assert 'codex' in agents
    assert len(agents['codex'].days) == 11


def test_parse_result_is_sorted_by_agent(payload):
    agents = parse_daily_by_agent(payload, ['zeta', 'claude', 'alpha'])
    assert list(agents) == ['alpha', 'claude', 'codex', 'zeta']


# -------------------------------------------------------- zero and empty


def test_zero_cost_without_tokens_is_free():
    document = {
        'daily': [
            {
                'period': '2026-09-01',
                'agents': [
                    {
                        'agent': 'claude',
                        'inputTokens': 0,
                        'outputTokens': 0,
                        'cacheCreationTokens': 0,
                        'cacheReadTokens': 0,
                        'totalCost': 0,
                        'modelBreakdowns': [
                            {
                                'modelName': 'idle',
                                'inputTokens': 0,
                                'outputTokens': 0,
                                'cacheCreationTokens': 0,
                                'cacheReadTokens': 0,
                                'cost': 0,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    day = parse_daily_by_agent(document, ['claude'])['claude'].days[0]
    assert day.cost_usd == Decimal('0')
    assert day.cost_status == 'estimated'
    assert day.models[0].cost_usd == Decimal('0')


def test_empty_daily_gives_configured_agents_only():
    agents = parse_daily_by_agent({'daily': []}, AGENTS)
    assert sorted(agents) == AGENTS
    assert all(agent.days == [] for agent in agents.values())


def test_agent_with_unknown_model_makes_the_day_unknown():
    document = {
        'daily': [
            {
                'period': '2026-09-01',
                'agents': [
                    {
                        'agent': 'claude',
                        'inputTokens': 20,
                        'outputTokens': 0,
                        'cacheCreationTokens': 0,
                        'cacheReadTokens': 0,
                        'totalCost': 1.5,
                        'modelBreakdowns': [
                            {
                                'modelName': 'priced',
                                'inputTokens': 10,
                                'outputTokens': 0,
                                'cacheCreationTokens': 0,
                                'cacheReadTokens': 0,
                                'cost': 1.5,
                            },
                            {
                                'modelName': 'unpriced',
                                'inputTokens': 10,
                                'outputTokens': 0,
                                'cacheCreationTokens': 0,
                                'cacheReadTokens': 0,
                                'cost': 0,
                            },
                        ],
                    }
                ],
            }
        ]
    }
    day = parse_daily_by_agent(document, ['claude'])['claude'].days[0]
    assert day.cost_usd is None
    assert day.cost_status == 'unknown'
    assert day.models[0].cost_usd == Decimal('1.5')
    assert day.models[1].cost_usd is None


# ------------------------------------------------------ malformed inputs


def base_document():
    return {
        'daily': [
            {
                'period': '2026-09-01',
                'agents': [
                    {
                        'agent': 'claude',
                        'inputTokens': 1,
                        'outputTokens': 2,
                        'cacheCreationTokens': 3,
                        'cacheReadTokens': 4,
                        'totalCost': 0.5,
                        'modelBreakdowns': [
                            {
                                'modelName': 'model-a',
                                'inputTokens': 1,
                                'outputTokens': 2,
                                'cacheCreationTokens': 3,
                                'cacheReadTokens': 4,
                                'cost': 0.5,
                            }
                        ],
                    }
                ],
            }
        ]
    }


def mutate(**changes):
    """Return the base document with one row or agent field replaced."""
    document = base_document()
    row = document['daily'][0]
    agent = row['agents'][0]
    for key, value in changes.items():
        target, _, field = key.partition('__')
        if target == 'row':
            row[field] = value
        elif target == 'agent':
            agent[field] = value
        elif target == 'model':
            agent['modelBreakdowns'][0][field] = value
        else:  # pragma: no cover - guards the test helper itself
            raise AssertionError(key)
    return document


MALFORMED = {
    'not-a-document': ([], 'expected an object'),
    'missing-daily': ({'totals': {}}, 'no "daily" key'),
    'daily-not-a-list': ({'daily': {}}, 'expected an array'),
    'row-not-an-object': ({'daily': ['nope']}, 'expected an object'),
    'missing-period': (mutate(row__period=None), 'expected a non-empty'),
    'period-not-a-date': (mutate(row__period='yesterday'), 'YYYY-MM-DD'),
    'period-is-a-number': (mutate(row__period=20260901), 'non-empty string'),
    'agents-not-a-list': (mutate(row__agents={}), 'expected an array'),
    'agent-not-an-object': (mutate(row__agents=[1]), 'expected an object'),
    'agent-name-missing': (mutate(agent__agent=None), 'non-empty string'),
    'agent-name-blank': (mutate(agent__agent='  '), 'non-empty string'),
    'negative-tokens': (mutate(agent__inputTokens=-1), 'must not be negative'),
    'float-tokens': (mutate(agent__inputTokens=1.5), 'integer token count'),
    'bool-tokens': (mutate(agent__inputTokens=True), 'integer token count'),
    'string-tokens': (mutate(agent__outputTokens='7'), 'integer token count'),
    'infinite-cost': (mutate(agent__totalCost=float('inf')), 'finite number'),
    'nan-cost': (mutate(agent__totalCost=float('nan')), 'finite number'),
    'negative-cost': (mutate(agent__totalCost=-1.0), 'must not be negative'),
    'string-cost': (mutate(agent__totalCost='free'), 'expected a number'),
    'model-not-an-object': (mutate(agent__modelBreakdowns=[3]), 'an object'),
    'model-name-missing': (mutate(model__modelName=None), 'non-empty string'),
    'model-negative-cost': (mutate(model__cost=-0.5), 'must not be negative'),
    'model-nan-cost': (mutate(model__cost=float('nan')), 'finite number'),
    'model-negative-tokens': (
        mutate(model__inputTokens=-2),
        'must not be negative',
    ),
}


@pytest.mark.parametrize(
    ('document', 'match'),
    list(MALFORMED.values()),
    ids=list(MALFORMED),
)
def test_malformed_documents_are_rejected(document, match):
    with pytest.raises(CollectorError, match=match):
        parse_daily_by_agent(document, AGENTS)


def test_missing_token_field_is_rejected():
    document = base_document()
    del document['daily'][0]['agents'][0]['cacheReadTokens']
    with pytest.raises(CollectorError, match='cacheReadTokens'):
        parse_daily_by_agent(document, AGENTS)


def test_missing_cost_field_is_rejected():
    document = base_document()
    del document['daily'][0]['agents'][0]['totalCost']
    with pytest.raises(CollectorError, match='totalCost'):
        parse_daily_by_agent(document, AGENTS)


def test_duplicate_agent_and_day_is_rejected():
    document = base_document()
    row = document['daily'][0]
    row['agents'].append(copy.deepcopy(row['agents'][0]))
    with pytest.raises(CollectorError, match='appears twice'):
        parse_daily_by_agent(document, AGENTS)


def test_missing_by_agent_shape_is_rejected(ccusage_fixture_dir):
    payload = load_fixture(ccusage_fixture_dir / 'daily.json')
    assert 'agents' not in payload['daily'][0]
    with pytest.raises(CollectorError) as error:
        parse_daily_by_agent(payload, AGENTS)
    assert '--by-agent' in str(error.value)


def test_a_late_error_does_not_yield_a_partial_result():
    document = base_document()
    broken = copy.deepcopy(document['daily'][0])
    broken['period'] = '2026-09-02'
    broken['agents'][0]['inputTokens'] = -5
    document['daily'].append(broken)
    with pytest.raises(CollectorError, match='must not be negative'):
        parse_daily_by_agent(document, AGENTS)


# --------------------------------------------------------------- fixture


def test_load_fixture_rejects_missing_file(tmp_path):
    with pytest.raises(CollectorError, match='cannot read'):
        load_fixture(tmp_path / 'absent.json')


def test_load_fixture_rejects_invalid_json(tmp_path):
    path = tmp_path / 'broken.json'
    path.write_text('{oops', encoding='utf-8')
    with pytest.raises(CollectorError, match='not valid JSON'):
        load_fixture(path)


def test_load_fixture_rejects_non_object(tmp_path):
    path = tmp_path / 'array.json'
    path.write_text(json.dumps([1, 2]), encoding='utf-8')
    with pytest.raises(CollectorError, match='expected an object'):
        load_fixture(path)
