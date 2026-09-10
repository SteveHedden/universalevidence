"""Application compatibility for retired vocabulary identifiers."""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def vocabulary_replacements() -> dict[str, str]:
    path = Path(__file__).parent / "resources" / "vocabulary-replacements.json"
    replacements = json.loads(path.read_text())
    for old, new in replacements.items():
        if (
            not isinstance(new, str)
            or not old.startswith("https://universalevidence.com/vocab/")
            or old.rsplit("/", 1)[0] != new.rsplit("/", 1)[0]
            or new in replacements
        ):
            raise ValueError("Vocabulary replacements must point directly to a current identifier in the same vocabulary")
    return replacements


def canonical_vocabulary_uri(uri: str) -> str:
    return vocabulary_replacements().get(uri, uri)
