from concurrent.futures import ThreadPoolExecutor

import pytest
from rdflib import Literal, URIRef

from api.routes import vocab as v


def write(path, label):
    path.write_text(f'<urn:test> <urn:label> "{label}" .\n')


@pytest.fixture
def config(tmp_path):
    v._GRAPH_CACHE.clear()
    path = tmp_path / 'states.ttl'
    write(path, 'old')
    yield v.ResourceConfig('states', path, 'urn:')
    v._GRAPH_CACHE.clear()


def labels(graph):
    return set(graph.objects(URIRef('urn:test'), URIRef('urn:label')))


def test_reuses_graph_and_concurrent_requests(config):
    with ThreadPoolExecutor(max_workers=4) as pool:
        graphs = list(pool.map(lambda _: v._load_graph(config), range(12)))
    assert all(g is graphs[0] for g in graphs)


def test_reloads_replacement_and_preserves_previous_snapshot(config):
    old = v._load_graph(config)
    replacement = config.path.with_suffix('.new')
    write(replacement, 'new')
    replacement.replace(config.path)
    new = v._load_graph(config)
    assert new is not old
    assert labels(old) == {Literal('old')}
    assert labels(new) == {Literal('new')}
    write(config.path, 'edited in place')
    assert labels(v._load_graph(config)) == {Literal('edited in place')}


def test_indicator_scheme_is_cached_separately(config):
    first = v._load_graph(config)
    indicator = config.path.with_name('indicators.ttl')
    write(indicator, 'indicator')
    indicator_config = v.ResourceConfig('indicators', indicator, 'urn:')
    assert labels(v._load_graph(config)) == {Literal('old')}
    assert labels(v._load_graph(indicator_config)) == {Literal('indicator')}
    write(indicator, 'changed')
    assert labels(v._load_graph(indicator_config)) == {Literal('changed')}
    assert v._load_graph(config) is first
    indicator.unlink()
    assert labels(v._load_graph(config)) == labels(first)


def test_missing_and_invalid_files_do_not_serve_stale_graph(config):
    v._load_graph(config)
    config.path.unlink()
    assert v._load_graph(config) is None
    config.path.write_text('invalid turtle !!!')
    with pytest.raises(Exception):
        v._load_graph(config)
    assert config not in v._GRAPH_CACHE
    write(config.path, 'recovered')
    assert labels(v._load_graph(config)) == {Literal('recovered')}


def test_cache_is_bounded(config, tmp_path):
    for i in range(v._GRAPH_CACHE_LIMIT + 3):
        path = tmp_path / f'{i}.ttl'
        write(path, str(i))
        v._load_graph(v.ResourceConfig(str(i), path, 'urn:'))
    assert len(v._GRAPH_CACHE) == v._GRAPH_CACHE_LIMIT


def test_file_changed_during_parse_is_retried(config, monkeypatch):
    original = v.Graph.parse
    changed = False

    def parse(graph, *args, **kwargs):
        nonlocal changed
        result = original(graph, *args, **kwargs)
        if not changed:
            changed = True
            write(config.path, 'updated during parse')
        return result

    monkeypatch.setattr(v.Graph, 'parse', parse)
    assert labels(v._load_graph(config)) == {Literal('updated during parse')}
