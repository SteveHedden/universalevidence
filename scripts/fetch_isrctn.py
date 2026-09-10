#!/usr/bin/env python3
"""Live ISRCTN XML API adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGIONS_PATH = REPO_ROOT / "vocabularies" / "regions.ttl"
DEFAULT_COUNTRY_NORM_PATH = REPO_ROOT / "vocabularies" / "country-norm.json"
DEFAULT_ENDPOINT = "https://www.isrctn.com/api/query/format/default"
DEFAULT_LIMIT = 10

SENTINEL_CONDITION_CATEGORIES = {"Not Specified", "Not Applicable"}


def load_country_norm(path: Path = DEFAULT_REGIONS_PATH) -> dict[str, str]:
    """Load country normalization map from country-norm JSON or regions.ttl."""
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(key): str(value)
            for key, value in data.items()
            if not str(key).startswith("__")
        }

    from rdflib import Graph, Namespace
    from rdflib.namespace import SKOS as _SKOS
    _UE = Namespace("https://universalevidence.com/ontology/")
    g = Graph()
    g.parse(path, format="turtle")
    mapping: dict[str, str] = {}
    for concept in g.subjects(_SKOS.prefLabel, None):
        iso = str(g.value(concept, _UE.iso3166Alpha2) or "")
        if not iso:
            continue
        for label in (
            list(g.objects(concept, _SKOS.prefLabel))
            + list(g.objects(concept, _SKOS.altLabel))
            + list(g.objects(concept, _UE.iso3166Alpha2))
            + list(g.objects(concept, _UE.iso3166Alpha3))
        ):
            key = str(label).strip()
            if key:
                mapping[key] = iso
    return mapping


def normalize_country(country: str, country_norm: dict[str, str]) -> str | None:
    country = country.strip()
    if not country:
        return None
    if country in country_norm:
        return country_norm[country]
    lower_lookup = {key.lower(): value for key, value in country_norm.items()}
    return lower_lookup.get(country.lower())


def xml_text(element: ET.Element, *names: str) -> str | None:
    """Return first non-empty descendant text matching any local tag name."""
    wanted = {name.lower() for name in names}
    for node in element.iter():
        local_name = node.tag.rsplit("}", 1)[-1].lower()
        if local_name in wanted and node.text and node.text.strip():
            return node.text.strip()
    return None


def xml_texts(element: ET.Element, *names: str) -> list[str]:
    """Return all non-empty descendant texts matching local tag names."""
    wanted = {name.lower() for name in names}
    values: list[str] = []
    for node in element.iter():
        local_name = node.tag.rsplit("}", 1)[-1].lower()
        if local_name in wanted and node.text and node.text.strip():
            value = node.text.strip()
            if value not in values:
                values.append(value)
    return values


def split_country_values(values: list[str]) -> list[str]:
    countries: list[str] = []
    for value in values:
        for part in re.split(r"\s*(?:;|\|)\s*", value):
            part = part.strip()
            if part and part not in countries:
                countries.append(part)
    return countries


def extract_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(19|20)\d{2}", value)
    return int(match.group(0)) if match else None


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def trial_elements(root: ET.Element) -> list[ET.Element]:
    """Find trial elements across default and WHO-ish ISRCTN XML shapes."""
    candidates = [
        node
        for node in root.iter()
        if local_name(node) in {"trial", "record", "isrctnrecord"}
        and xml_text(node, "isrctn", "trialId", "trialID", "id")
    ]
    if candidates:
        return candidates

    if xml_text(root, "isrctn", "trialId", "trialID", "id"):
        return [root]
    return []


def normalize_isrctn_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    match = re.search(r"(?:ISRCTN)?(\d{6,12})", value, flags=re.IGNORECASE)
    if match:
        return f"ISRCTN{match.group(1)}"
    return value


def extract_condition_descriptions(trial: ET.Element) -> list[str]:
    """Extract specific condition strings from <conditions><condition><description>."""
    results: list[str] = []
    for el in trial.iter():
        local = el.tag.rsplit("}", 1)[-1].lower()
        if local == "condition":
            for child in el:
                child_local = child.tag.rsplit("}", 1)[-1].lower()
                if child_local == "description" and child.text and child.text.strip():
                    text = child.text.strip()
                    if text not in results:
                        results.append(text)
    return results


def extract_intervention_descriptions(trial: ET.Element) -> list[str]:
    """Extract intervention prose strings from <interventions><intervention><description>."""
    results: list[str] = []
    for el in trial.iter():
        local = el.tag.rsplit("}", 1)[-1].lower()
        if local == "intervention":
            for child in el:
                child_local = child.tag.rsplit("}", 1)[-1].lower()
                if child_local == "description" and child.text and child.text.strip():
                    text = child.text.strip()
                    if text not in results:
                        results.append(text)
    return results


def extract_drug_names(trial: ET.Element) -> list[str]:
    """Extract all drugNames values across intervention elements."""
    results: list[str] = []
    for el in trial.iter():
        local = el.tag.rsplit("}", 1)[-1].lower()
        if local == "drugnames" and el.text and el.text.strip():
            text = el.text.strip()
            if text not in results:
                results.append(text)
    return results


def extract_study_locations(trial: ET.Element) -> list[str]:
    """Extract studyLocation city/region strings from the trial XML."""
    return xml_texts(trial, "studylocation", "studyLocation", "city", "location")


def outcome_items(measures: list[str]) -> list[dict[str, str]]:
    return [
        {"type": "pre-specified-measure", "measure": measure}
        for measure in measures
        if measure.strip()
    ]


def parse_isrctn_xml(
    xml_content: str | bytes,
    country_norm: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Parse ISRCTN XML into normalized study dictionaries."""
    if isinstance(xml_content, bytes):
        xml_content = xml_content.decode("utf-8", errors="replace")
    if not xml_content.strip():
        return []

    country_norm = country_norm or {}
    root = ET.fromstring(xml_content)
    studies: list[dict[str, Any]] = []

    for trial in trial_elements(root):
        study_id = normalize_isrctn_id(xml_text(trial, "isrctn", "trialId", "trialID", "id"))
        if not study_id:
            continue

        countries = split_country_values(
            xml_texts(trial, "recruitmentCountry", "recruitmentCountries", "country")
        )
        country_isos = [
            iso
            for iso in (normalize_country(country, country_norm) for country in countries)
            if iso
        ]
        condition_category = xml_text(trial, "conditionCategory")
        condition_descriptions = extract_condition_descriptions(trial)
        intervention_descriptions = extract_intervention_descriptions(trial)
        drug_names_list = extract_drug_names(trial)
        study_locations = extract_study_locations(trial)
        outcomes = outcome_items(
            xml_texts(trial, "outcomeMeasures", "outcomeMeasure", "primaryOutcome", "secondaryOutcome")
        )

        start_date = xml_text(trial, "overallStartDate", "startDate", "dateApplied")
        study = {
            "source": "ISRCTN",
            "study_id": study_id,
            "title": xml_text(trial, "title", "scientificTitle", "publicTitle"),
            "url": f"https://www.isrctn.com/{study_id}",
            "intervention": intervention_descriptions[0] if intervention_descriptions else None,
            "intervention_descriptions": intervention_descriptions,
            "drug_names": drug_names_list[0] if drug_names_list else None,
            "drug_names_list": drug_names_list,
            "country": country_isos[0] if country_isos else (countries[0] if countries else None),
            "status": xml_text(trial, "trialStatus", "recruitmentStatus"),
            "year": extract_year(start_date),
            "condition_category": condition_category,
            "condition_category_mapped": (
                condition_category not in SENTINEL_CONDITION_CATEGORIES
                if condition_category
                else None
            ),
            "condition": condition_descriptions[0] if condition_descriptions else None,
            "condition_descriptions": condition_descriptions,
            "countries": countries,
            "country_iso_alpha2": country_isos,
            "study_locations": study_locations,
            "study_type": xml_text(trial, "studyType"),
            "outcomes": outcomes,
        }
        studies.append(study)

    return studies


def quote_query_value(value: str) -> str:
    escaped = value.replace('"', r"\"")
    return f'"{escaped}"' if re.search(r"\s", escaped) else escaped


def build_query(params: dict[str, Any]) -> str:
    """Build an ISRCTN MarkLogic constraint query from resolved axis params."""
    clauses: list[str] = []
    for key in ("conditionCategory", "condition", "intervention", "outcomeMeasures", "recruitmentCountry"):
        value = params.get(key)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        field_clauses = [
            f"{key}: {quote_query_value(str(item))}"
            for item in values
            if str(item).strip()
        ]
        if not field_clauses:
            continue
        clauses.append(
            field_clauses[0]
            if len(field_clauses) == 1
            else "(" + " OR ".join(field_clauses) + ")"
        )

    query = " AND ".join(clauses)
    if not query and params.get("q"):
        query = str(params["q"])
    return query


async def fetch_isrctn(
    params: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    country_norm_path: Path = DEFAULT_REGIONS_PATH,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Query ISRCTN's XML API and return normalized study dictionaries."""
    query = build_query(params)
    request_params = {"q": query, "limit": str(params.get("limit", limit))}
    country_norm = load_country_norm(country_norm_path)

    logger.info("Querying ISRCTN with params=%s query=%r", params, query)

    if client is None:
        async with httpx.AsyncClient(timeout=30) as owned_client:
            response = await owned_client.get(endpoint, params=request_params)
    else:
        response = await client.get(endpoint, params=request_params)

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 429 or status_code >= 500:
            logger.warning(
                "ISRCTN returned %s for params=%s; returning no results",
                status_code,
                params,
            )
            return []
        raise
    return parse_isrctn_xml(response.text, country_norm)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query ISRCTN XML API.")
    parser.add_argument("--condition-category", dest="conditionCategory")
    parser.add_argument("--condition")
    parser.add_argument("--intervention")
    parser.add_argument("--outcome-measures", dest="outcomeMeasures")
    parser.add_argument("--recruitment-country", dest="recruitmentCountry")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser


async def async_main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args()
    try:
        results = await fetch_isrctn(vars(args))
    except httpx.HTTPError as exc:
        logger.error("ISRCTN request failed: %s", exc)
        return 1
    json.dump(results, sys.stdout, indent=2)
    print()
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
