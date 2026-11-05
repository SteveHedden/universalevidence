"""Bounded HTML-only pilot; saved evidence selections, not live query results.

Refreshing the selections is a separate publication step. RDF representations
continue to be generated from the canonical vocabulary by the normal router.
"""
from functools import lru_cache
from pathlib import Path

PILOTS = {
    "https://universalevidence.com/vocab/states/Stunting": "stunting",
    "https://universalevidence.com/vocab/states/Malaria": "malaria",
    "https://universalevidence.com/vocab/interventions/InsecticideTreatedNets": "bednets",
}


@lru_cache(maxsize=3)
def pilot_html(uri: str) -> str | None:
    slug = PILOTS.get(uri)
    if slug is None:
        return None
    return (Path(__file__).with_suffix("") / f"{slug}.html").read_text()
