from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ctgov_location_context import (  # noqa: E402
    canonical_location_key,
    ctgov_region_evidence_is_quarantined,
    parse_geopoint,
    study_locations,
)
import query_interventions as qi  # noqa: E402


def _study(locations):
    return {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT00000001"},
            "contactsLocationsModule": {"locations": locations},
        }
    }


def test_location_objects_never_borrow_neighboring_context():
    rows = list(study_locations(_study([
        {
            "facility": "First Hospital",
            "city": "Springfield",
            "state": "Illinois",
            "country": "United States",
            "geoPoint": {"lat": 39.8, "lon": -89.64},
        },
        {
            "facility": "Second Hospital",
            "city": "Springfield",
            "state": "Queensland",
            "country": "Australia",
        },
    ])))
    assert [row.location_index for row in rows] == [0, 1]
    assert rows[0].country == "United States"
    assert rows[0].state == "Illinois"
    assert rows[0].geopoint_status == "valid"
    assert rows[1].country == "Australia"
    assert rows[1].state == "Queensland"
    assert rows[1].latitude is None


def test_contextual_key_distinguishes_same_city_and_ignores_facility():
    rows = list(study_locations(_study([
        {"facility": "A", "city": "Springfield", "state": "Illinois", "country": "United States"},
        {"facility": "B", "city": "Springfield", "state": "Queensland", "country": "Australia"},
        {"facility": "C", "city": "Springfield", "state": "Illinois", "country": "United States"},
    ])))
    assert canonical_location_key(rows[0]) != canonical_location_key(rows[1])
    assert canonical_location_key(rows[0]) == canonical_location_key(rows[2])
    assert "facility" not in canonical_location_key(rows[0])


def test_malformed_and_out_of_range_geopoints_are_not_valid():
    assert parse_geopoint({"geoPoint": "not-an-object"}) == (None, None, "malformed")
    assert parse_geopoint({"geoPoint": {"lat": "x", "lon": 3}}) == (None, None, "malformed")
    assert parse_geopoint({"geoPoint": {"lat": 91, "lon": 3}}) == (None, None, "out_of_range")


def test_known_defective_study_is_excluded_from_region_evidence():
    assert ctgov_region_evidence_is_quarantined("NCT01108796")
    assert ctgov_region_evidence_is_quarantined("nct01108796")
    assert not ctgov_region_evidence_is_quarantined("NCT00000001")


def test_query_consumer_uses_contextual_key_and_never_bare_city(monkeypatch):
    study = _study([{
        "facility": "First Hospital",
        "city": "Springfield",
        "state": "Illinois",
        "country": "United States",
    }])
    context = next(iter(study_locations(study)))
    country_uri = "https://sws.geonames.org/6252001/"
    adm1_uri = "https://sws.geonames.org/4896861/"
    monkeypatch.setattr(
        qi,
        "get_ctgov_regions_xwalk",
        lambda: {
            canonical_location_key(context): adm1_uri,
            "United States": country_uri,
            # This deliberately wrong mapping proves the consumer does not
            # consult a globally ambiguous bare-city key.
            "Springfield": "https://sws.geonames.org/999999999/",
        },
    )

    assert qi.ctgov_location_region_uris(study) == [adm1_uri, country_uri]
    legacy_mapped = qi.map_ctgov_study(study, "US")
    assert legacy_mapped["_location_region_uris"] == []

    mapped = qi.map_ctgov_study(study, "US", include_region_evidence=True)
    assert mapped["_location_region_uris"] == [adm1_uri, country_uri]
