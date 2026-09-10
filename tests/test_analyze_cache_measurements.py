from scripts.analyze_cache_measurements import percentile, summarize


def test_percentile_uses_inclusive_linear_interpolation():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([10, 20], 75) == 17.5
    assert percentile([], 99) == 0


def test_summary_reports_all_size_variants_and_ctgov_duplication():
    measurements = [
        {
            "rows": 2,
            "raw_json_bytes": 100,
            "stored_bytes": 40,
            "estimated_python_bytes": 300,
            "ctgov_duplicate_rows": 1,
            "over_ceiling": False,
            "storage_representation": "gzip-json",
        },
        {
            "rows": 4,
            "raw_json_bytes": 300,
            "stored_bytes": 90,
            "estimated_python_bytes": 800,
            "ctgov_duplicate_rows": 2,
            "over_ceiling": True,
            "storage_representation": "gzip-json",
        },
    ]

    result = summarize(measurements)

    assert result["store_candidates"] == 2
    assert result["over_ceiling"] == 1
    assert result["rows"] == 6
    assert result["ctgov_duplicate_rows"] == 3
    assert result["distributions"]["raw_json_bytes"]["p50"] == 200
    assert result["bytes_per_row"]["stored_bytes"]["min"] == 20
