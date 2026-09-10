from __future__ import annotations

import asyncio

import httpx
import pytest

import scripts.query_v2 as engine


STUNTING = "https://universalevidence.com/vocab/states/Stunting"
WASTING = "https://universalevidence.com/vocab/states/Wasting"
MALNUTRITION = "https://universalevidence.com/vocab/states/Malnutrition"
NUTRITION = "https://universalevidence.com/vocab/interventions/NutritionIntervention"
RUTF = (
    "https://universalevidence.com/vocab/interventions/"
    "ReadyToUseTherapeuticFoodIntervention"
)
CASH = "https://universalevidence.com/vocab/interventions/CashTransfer"
COUNSELING = (
    "https://universalevidence.com/vocab/interventions/CounselingIntervention"
)
INHERITED_ONLY = (
    "https://universalevidence.com/vocab/interventions/DirectServiceDelivery"
)
LABELS = {
    NUTRITION: "Nutrition intervention",
    RUTF: "Ready-to-use therapeutic/supplementary food",
    CASH: "Cash transfer",
    COUNSELING: "Counseling interventions",
    INHERITED_ONLY: "Direct service delivery",
}


def _study(study_id: str = "ISRCTN-MULTI") -> dict:
    return {
        "source": "ISRCTN",
        "study_id": study_id,
        "title": "Multi-intervention trial",
        "intervention": "Program A",
        "intervention_concept_uri": INHERITED_ONLY,
        "intervention_concept": LABELS[INHERITED_ONLY],
        "drug_names_list": ["Program A", "Program A"],
        "intervention_descriptions": ["Program B", "Program A"],
        "condition_descriptions": ["Stunting"],
        "countries": [],
        "country_iso_alpha2": [],
        "outcomes": [{"measure": "Stunting"}],
    }


def _direct_crosswalk() -> dict[str, tuple[str, ...]]:
    return {
        engine.v1._normalize_xwalk_key("Program A"): (RUTF, NUTRITION, RUTF),
        engine.v1._normalize_xwalk_key("Program B"): (CASH, RUTF),
    }


async def _resolve(uri: str) -> engine.ConceptResolution:
    if uri == NUTRITION:
        return engine.ConceptResolution(
            label=LABELS[NUTRITION], terms=("Program A", "Program B"), mesh_uris=()
        )
    return engine.ConceptResolution(label="Stunting", terms=("Stunting",), mesh_uris=())


def _adapter(fetch_batch) -> engine.IsrctnAdapter:
    return engine.IsrctnAdapter(
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        ),
        fetch_batch=fetch_batch,
        concept_resolver=_resolve,
    )


def test_search_expands_all_direct_targets_and_deduplicates_raw_repetitions():
    rows = engine._stamp_isrctn(
        [_study()],
        {"intervention": NUTRITION},
        {"intervention": LABELS[NUTRITION]},
        intervention_crosswalk=_direct_crosswalk(),
        intervention_labels=LABELS,
    )
    presented = engine.dedupe_presentation(rows)

    assert {
        row["intervention_concept_uri"] for row in presented
    } == {RUTF, NUTRITION, CASH}
    assert INHERITED_ONLY not in {
        row["intervention_concept_uri"] for row in presented
    }
    assert len(presented) == 3
    assert len({engine.study_key(row) for row in presented}) == 1


def test_search_keeps_wholly_unmapped_isrctn_study_for_other_group():
    rows = engine._stamp_isrctn(
        [_study("ISRCTN-UNMAPPED")],
        {"intervention": NUTRITION},
        {"intervention": LABELS[NUTRITION]},
        intervention_crosswalk={},
        intervention_labels=LABELS,
    )

    assert len(rows) == 1
    assert rows[0]["study_id"] == "ISRCTN-UNMAPPED"
    assert rows[0].get("intervention_concept_uri") is None
    assert rows[0].get("intervention_concept") is None


@pytest.mark.parametrize("axis", ["state", "condition", "outcome", "intervention"])
def test_every_isrctn_search_path_returns_all_direct_mappings_once(
    monkeypatch: pytest.MonkeyPatch,
    axis: str,
):
    async def fetch_batch(*_args):
        return [_study()]

    monkeypatch.setattr(
        engine, "_isrctn_direct_intervention_crosswalk", _direct_crosswalk
    )
    monkeypatch.setattr(engine, "_local_intervention_labels", lambda _uris: LABELS)
    monkeypatch.setattr(engine.v1, "get_isrctn_conditions_xwalk", lambda: {})
    selected_uri = NUTRITION if axis == "intervention" else STUNTING
    query = engine.canonical_query({axis: [selected_uri]}, {axis: "or"})

    rows, meta = asyncio.run(
        engine.execute_source(
            query,
            _adapter(fetch_batch),
            engine.default_region_index(),
        )
    )

    assert {
        row["intervention_concept_uri"] for row in rows
    } == {RUTF, NUTRITION, CASH}
    assert len(rows) == 3
    assert meta["returned_unique_studies"] == 1


def test_graph_uses_direct_multimap_and_excludes_inherited_targets(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fetch_batch(*_args):
        return [_study()]

    monkeypatch.setattr(
        engine, "_isrctn_direct_intervention_crosswalk", _direct_crosswalk
    )
    monkeypatch.setattr(
        engine,
        "_isrctn_direct_state_crosswalk",
        lambda _role: {engine.v1._normalize_xwalk_key("Stunting"): (STUNTING,)},
    )
    monkeypatch.setattr(engine, "_local_state_labels", lambda _uris: {STUNTING: "Stunting"})
    monkeypatch.setattr(engine, "_local_intervention_labels", lambda _uris: LABELS)

    rows, _meta = asyncio.run(
        engine.execute_graph_source(
            engine.canonical_query({"state": [STUNTING]}, {"state": "or"}),
            _adapter(fetch_batch),
            engine.default_region_index(),
        )
    )

    assert {
        row["intervention_concept_uri"] for row in rows
    } == {RUTF, NUTRITION, CASH}
    assert INHERITED_ONLY not in {
        row["intervention_concept_uri"] for row in rows
    }
    assert {row["state_concept_uri"] for row in rows} == {STUNTING}
    assert len(rows) == 3


def test_graph_keeps_interventions_without_inventing_state():
    rows = engine._stamp_isrctn(
        [_study("ISRCTN-STATE-FALLBACK")],
        {"state": STUNTING},
        {"state": "Stunting"},
        condition_crosswalk={},
        intervention_crosswalk=_direct_crosswalk(),
        intervention_labels=LABELS,
        outcome_crosswalk={},
        specific_attribution=True,
    )

    assert {
        row["intervention_concept_uri"] for row in rows
    } == {RUTF, NUTRITION, CASH}
    assert all(not row.get("state_concept_uri") for row in rows)
    assert INHERITED_ONLY not in {
        row.get("intervention_concept_uri") for row in rows
    }
    assert len(rows) == 3


def test_graph_excludes_direct_condition_outside_selected_state_subtree():
    rows = engine._stamp_isrctn(
        [_study("ISRCTN-OUTSIDE-STATE")],
        {"state": STUNTING},
        {"state": "Stunting"},
        condition_crosswalk={"Stunting": (MALNUTRITION,)},
        condition_labels={MALNUTRITION: "Malnutrition"},
        intervention_crosswalk=_direct_crosswalk(),
        intervention_labels=LABELS,
        outcome_crosswalk={},
        specific_attribution=True,
    )

    assert {
        row["intervention_concept_uri"] for row in rows
    } == {RUTF, NUTRITION, CASH}
    assert all(not row.get("state_concept_uri") for row in rows)
    assert all(not row.get("condition_concept_uri") for row in rows)


@pytest.mark.parametrize("direct_state_uris", [(), (MALNUTRITION,)])
def test_graph_adapter_never_stamps_selected_state_without_in_scope_mapping(
    monkeypatch: pytest.MonkeyPatch,
    direct_state_uris,
):
    async def fetch_batch(*_args):
        return [_study("ISRCTN-NO-IN-SCOPE-STATE")]

    monkeypatch.setattr(
        engine, "_isrctn_direct_intervention_crosswalk", _direct_crosswalk
    )
    monkeypatch.setattr(
        engine,
        "_isrctn_direct_state_crosswalk",
        lambda _role: {
            engine.v1._normalize_xwalk_key("Stunting"): direct_state_uris
        },
    )
    monkeypatch.setattr(
        engine,
        "_local_state_labels",
        lambda _uris: {MALNUTRITION: "Malnutrition"},
    )
    monkeypatch.setattr(engine, "_local_intervention_labels", lambda _uris: LABELS)

    rows, meta = asyncio.run(
        engine.execute_graph_source(
            engine.canonical_query({"state": [STUNTING]}, {"state": "or"}),
            _adapter(fetch_batch),
            engine.default_region_index(),
        )
    )

    assert {
        row["intervention_concept_uri"] for row in rows
    } == {RUTF, NUTRITION, CASH}
    assert all(not row.get("state_concept_uri") for row in rows)
    assert all(not row.get("condition_concept_uri") for row in rows)
    assert meta["returned_unique_studies"] == 1


def test_graph_keeps_role_specific_children_and_never_stamps_outcome_ancestor():
    study = _study("ISRCTN-ROLE-SPECIFIC")
    study["outcomes"] = [
        {
            "measure": "Weight-for-height",
            "state_concept_uri": MALNUTRITION,
            "state_concept": "Malnutrition",
        }
    ]
    rows = engine._stamp_isrctn(
        [study],
        {"condition": STUNTING, "outcome": MALNUTRITION},
        {"condition": "Stunting", "outcome": "Malnutrition"},
        condition_crosswalk={"Stunting": (STUNTING,)},
        condition_labels={STUNTING: "Stunting"},
        intervention_crosswalk=_direct_crosswalk(),
        intervention_labels=LABELS,
        outcome_crosswalk={"Weight-for-height": (WASTING,)},
        state_labels={STUNTING: "Stunting", WASTING: "Wasting"},
        specific_attribution=True,
    )

    assert {row["state_concept_uri"] for row in rows} == {STUNTING, WASTING}
    assert {
        row.get("condition_concept_uri") for row in rows
    } == {None, STUNTING}
    assert all(
        not outcome.get("state_concept_uri")
        for row in rows
        for outcome in row["outcomes"]
    )
    assert MALNUTRITION not in {
        row["state_concept_uri"] for row in rows
    }
    assert INHERITED_ONLY not in {
        row.get("intervention_concept_uri") for row in rows
    }
    assert len(rows) == 6


def test_fuseki_fallback_is_source_scoped_and_preserves_multiple_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    observed: list[str] = []

    def select(query: str):
        observed.append(query)
        return [
            {"rawText": "Program A", "concept": CASH},
            {"rawText": "Program A", "concept": RUTF},
            {"rawText": " Program  A ", "concept": CASH},
        ]

    engine._source_direct_intervention_crosswalk.cache_clear()
    engine._isrctn_direct_intervention_crosswalk.cache_clear()
    monkeypatch.setattr(engine, "VOCABULARIES_DIR", tmp_path)
    monkeypatch.setattr(engine.v1, "sparql_select", select)
    try:
        crosswalk = engine._isrctn_direct_intervention_crosswalk()
    finally:
        engine._source_direct_intervention_crosswalk.cache_clear()
        engine._isrctn_direct_intervention_crosswalk.cache_clear()

    assert crosswalk == {
        engine.v1._normalize_xwalk_key("Program A"): tuple(sorted((CASH, RUTF)))
    }
    assert len(observed) == 1
    assert engine.ISRCTN_INTERVENTION_CROSSWALK_PREFIX in observed[0]
    assert "?concept a ue:Intervention" in observed[0]
