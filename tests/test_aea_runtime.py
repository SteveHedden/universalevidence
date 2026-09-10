from scripts import query_interventions


def test_aea_region_query_reads_registry_source_field(monkeypatch):
    monkeypatch.setattr(
        query_interventions,
        "resolve_region_countries",
        lambda _uri: [{"country": "https://sws.geonames.org/192950/"}],
    )

    query = query_interventions.build_aea_crosswalk_sparql_query(
        {"region": "https://universalevidence.com/vocab/regions/SubSaharanAfrica"}
    )

    country_predicate = f"<{query_interventions.AEA}country>"
    assert query is not None
    assert query.count(country_predicate) == 1
    assert "aea:country ?country" in query
