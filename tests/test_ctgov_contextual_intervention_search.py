import pytest
from scripts import query_v2 as engine
from scripts.ctgov_intervention_identity import contextual_key

STUDY = "NCT06669676"
TARGET = "https://universalevidence.com/vocab/interventions/AcupunctureTherapyIntervention"
MAPPING = {contextual_key(STUDY, "acupuncture"): (TARGET, "Acupuncture therapy")}


def study(study_id=STUDY):
    return {"nctId": study_id, "title": "Test", "outcomes": [],
            "_intervention_names": ["No acupuncture", "acupuncture"]}


@pytest.mark.parametrize("specific", [False, True])
def test_study_scoped_search_mapping_uses_active_label(specific):
    rows = engine._ctgov_presentation([study()], {}, {}, {}, MAPPING, {}, {},
                                     specific_attribution=specific)
    mapped = [r for r in rows if r.get("intervention_concept_uri")]
    assert [(r["intervention"], r["intervention_concept_uri"]) for r in mapped] == [("acupuncture", TARGET)]
    other = engine._ctgov_presentation([study("NCT00000001")], {}, {}, {}, MAPPING, {}, {},
                                      specific_attribution=specific)
    assert not any(r.get("intervention_concept_uri") for r in other)


def test_intervention_filter_does_not_assign_concept_to_first_comparator():
    rows = engine._ctgov_presentation([study()], {"intervention": TARGET},
                                     {"intervention": "Acupuncture therapy"}, {}, MAPPING, {}, {})
    assert len(rows) == 1
    assert rows[0]["intervention"] == "acupuncture"


def test_legacy_search_reads_same_contextual_identity():
    active = engine.v1._attach_ctgov_intervention_concept(
        {"study_id": STUDY, "intervention": "acupuncture"}, MAPPING)
    comparator = engine.v1._attach_ctgov_intervention_concept(
        {"study_id": STUDY, "intervention": "No acupuncture"}, MAPPING)
    assert active["intervention_concept_uri"] == TARGET
    assert not comparator.get("intervention_concept_uri")
