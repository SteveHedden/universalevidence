#!/usr/bin/env python3
"""Summarize cache_store_measurement JSON log records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
from typing import Iterable


LOG_MARKER = "cache_store_measurement "
SIZE_FIELDS = ("raw_json_bytes", "stored_bytes", "estimated_python_bytes")
PERCENTILES = (50, 75, 90, 95, 99)


def percentile(values: list[float], percentage: int) -> float:
    """Return an inclusive, linearly interpolated percentile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentage / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def read_measurements(paths: Iterable[Path]) -> list[dict]:
    measurements = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            marker_at = line.find(LOG_MARKER)
            if marker_at < 0:
                continue
            measurements.append(json.loads(line[marker_at + len(LOG_MARKER) :]))
    return measurements


def distribution(values: list[float]) -> dict[str, int | float]:
    if not values:
        return {"count": 0, "min": 0, "max": 0, "total": 0, "mean": 0}
    result: dict[str, int | float] = {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "total": sum(values),
        "mean": statistics.fmean(values),
    }
    result.update({f"p{p}": percentile(values, p) for p in PERCENTILES})
    return result


def summarize(measurements: list[dict]) -> dict:
    summary = {
        "store_candidates": len(measurements),
        "over_ceiling": sum(bool(item.get("over_ceiling")) for item in measurements),
        "rows": sum(int(item.get("rows", 0)) for item in measurements),
        "ctgov_duplicate_rows": sum(
            int(item.get("ctgov_duplicate_rows", 0)) for item in measurements
        ),
        "representations": sorted(
            {str(item.get("storage_representation", "unknown")) for item in measurements}
        ),
        "distributions": {},
        "bytes_per_row": {},
    }
    for field in SIZE_FIELDS:
        sizes = [float(item[field]) for item in measurements if field in item]
        per_row = [
            float(item[field]) / int(item["rows"])
            for item in measurements
            if field in item and int(item.get("rows", 0)) > 0
        ]
        summary["distributions"][field] = distribution(sizes)
        summary["bytes_per_row"][field] = distribution(per_row)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rendered = json.dumps(summarize(read_measurements(args.logs)), indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
