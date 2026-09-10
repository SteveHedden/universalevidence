import pytest

from api.query_cache import query_result_cache


@pytest.fixture(autouse=True)
def reset_query_result_cache():
    query_result_cache.clear()
    yield
    query_result_cache.clear()
