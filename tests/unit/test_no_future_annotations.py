"""The code base must not use postponed annotation evaluation."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = (ROOT / 'src', ROOT / 'tests')
# Assembled at runtime so that this file does not match itself.
BANNED = 'from __' + 'future__ import annotations'


def python_files():
    for directory in SOURCE_DIRS:
        yield from sorted(directory.rglob('*.py'))


@pytest.mark.parametrize(
    'path', list(python_files()), ids=lambda p: str(p.name)
)
def test_no_future_annotations_import(path):
    assert BANNED not in path.read_text(encoding='utf-8')


def test_the_check_sees_files():
    assert len(list(python_files())) > 10
