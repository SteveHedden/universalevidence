import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import generate_dependency_notices as notices


def test_collects_transitive_extra_dependencies_and_ignores_inactive_markers(tmp_path, monkeypatch):
    def distribution(name, dependencies):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'LICENSE').write_text(f'{name} license text')
        return SimpleNamespace(version='1.2.3', requires=dependencies,
                               files=[Path('LICENSE')], locate_file=lambda p: folder / p)
    packages = {
        'root': distribution('root', ['child; extra == "feature"', 'missing; python_version < "2"']),
        'child': distribution('child', []),
    }
    monkeypatch.setattr(notices.metadata, 'distribution', packages.__getitem__)
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('root[feature]\nmissing; python_version < "2"\n')
    output = tmp_path / 'notices.txt'
    notices.generate(requirements, output)
    assert {p['name'] for p in json.loads(output.with_suffix('.json').read_text())} == {'root', 'child'}
    assert 'child license text' in output.read_text()


def test_missing_license_stops_distribution_notice_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(notices.metadata, 'distribution', lambda _: SimpleNamespace(
        version='1', requires=[], files=[]))
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('unlicensed\n')
    with pytest.raises(RuntimeError, match='No packaged license text'):
        notices.generate(requirements, tmp_path / 'notices.txt')
