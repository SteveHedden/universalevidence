#!/usr/bin/env python3
"""Shared CT.gov location-object normalization for scoring and query lookup."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any, Iterable
from urllib.parse import quote


LOCATION_KEY_VERSION = "ctgov-location:v1"
CTGOV_REGION_QUARANTINED_STUDY_IDS = frozenset({"NCT01108796"})


def ctgov_region_evidence_is_quarantined(study_id: object) -> bool:
    """Return whether a known source defect makes region evidence unusable."""
    return str(study_id or "").strip().upper() in CTGOV_REGION_QUARANTINED_STUDY_IDS


def normalize_whitespace(value: object) -> str:
    return " ".join(str(value or "").split())


def _coordinate(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_geopoint(location: dict[str, Any]) -> tuple[float | None, float | None, str]:
    point = location.get("geoPoint")
    if point in (None, ""):
        return None, None, "missing"
    if not isinstance(point, dict):
        return None, None, "malformed"
    latitude = _coordinate(point.get("lat", point.get("latitude")))
    longitude = _coordinate(point.get("lon", point.get("longitude")))
    if latitude is None or longitude is None:
        return None, None, "malformed"
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None, None, "out_of_range"
    return latitude, longitude, "valid"


@dataclass(frozen=True)
class LocationContext:
    study_id: str
    location_index: int
    facility: str
    city: str
    state: str
    postal_code: str
    country: str
    latitude: float | None
    longitude: float | None
    geopoint_status: str

    @property
    def source_record(self) -> str:
        return (
            f"https://clinicaltrials.gov/study/{self.study_id}"
            f"#location-{self.location_index:04d}"
        )

    @property
    def location_id(self) -> str:
        return f"{self.study_id}:location:{self.location_index:04d}"

    def as_evidence(self) -> dict[str, Any]:
        return {**asdict(self), "location_id": self.location_id}


def study_locations(study: dict[str, Any]) -> Iterable[LocationContext]:
    protocol = study.get("protocolSection") or {}
    identification = protocol.get("identificationModule") or {}
    study_id = normalize_whitespace(identification.get("nctId"))
    if not study_id:
        return
    module = protocol.get("contactsLocationsModule") or {}
    for index, raw in enumerate(module.get("locations") or []):
        if not isinstance(raw, dict):
            continue
        latitude, longitude, status = parse_geopoint(raw)
        yield LocationContext(
            study_id=study_id,
            location_index=index,
            facility=normalize_whitespace(raw.get("facility")),
            city=normalize_whitespace(raw.get("city")),
            state=normalize_whitespace(raw.get("state")),
            postal_code=normalize_whitespace(raw.get("zip")),
            country=normalize_whitespace(raw.get("country")),
            latitude=latitude,
            longitude=longitude,
            geopoint_status=status,
        )


def canonical_location_key(context: LocationContext) -> str:
    """Return the versioned key written to and read from the CT.gov crosswalk.

    Facility, postal code, coordinates, and study ID are evidence but are not
    part of the reusable geographic key. Percent encoding makes separators
    unambiguous and keeps the key deterministic across producer/consumer code.
    """
    parts = {
        "country": context.country,
        "state": context.state,
        "city": context.city,
    }
    encoded = "|".join(
        f"{name}={quote(value, safe='')}" for name, value in parts.items()
    )
    return f"{LOCATION_KEY_VERSION}|{encoded}"


def context_from_json(record: dict[str, Any]) -> LocationContext:
    fields = {
        "study_id",
        "location_index",
        "facility",
        "city",
        "state",
        "postal_code",
        "country",
        "latitude",
        "longitude",
        "geopoint_status",
    }
    missing = sorted(fields - record.keys())
    if missing:
        raise ValueError("location evidence lacks: " + ", ".join(missing))
    return LocationContext(**{name: record[name] for name in fields})


def json_line(context: LocationContext) -> str:
    return json.dumps(context.as_evidence(), ensure_ascii=False, sort_keys=True)
