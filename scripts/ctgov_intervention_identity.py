"""Study-scoped CT.gov intervention keys; preserve the original label's case."""
import re
from urllib.parse import quote, unquote

PREFIX = "ctgov-intervention:v1|study="


def normalize(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def contextual_key(study_id, raw_text):
    study_id = str(study_id).rsplit("/", 1)[-1]
    if not re.fullmatch(r"NCT\d{8}", study_id) or not normalize(raw_text):
        raise ValueError("A contextual intervention key requires an NCT ID and label")
    return PREFIX + study_id + "|name=" + quote(normalize(raw_text), safe="")


def parse_key(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return None
    study, separator, label = value[len(PREFIX):].partition("|name=")
    if not separator or contextual_key(study, unquote(label)) != value:
        raise ValueError("Malformed contextual intervention key")
    return study, unquote(label)


def lookup(mapping, study_id, raw_text):
    if study_id and re.fullmatch(r"NCT\d{8}", str(study_id).rsplit("/", 1)[-1]):
        scoped = mapping.get(contextual_key(study_id, raw_text))
        if scoped is not None:
            return scoped
    return mapping.get(normalize(raw_text))
