"""Study-scoped CT.gov Indicator keys; no network or canonical writes.

Keep source restrictions in the key so applying a TTL never depends on an
optional decision sidecar. Numeric values remain in the live source response.
"""
import json
import re
from urllib.parse import quote, unquote

PREFIX = "ctgov-indicator:v1|"


def outcome_context(study_id: str, outcome: dict, class_index: int | None = None, category_index: int | None = None) -> dict:
    if not re.fullmatch(r"NCT\d{8}", study_id):
        raise ValueError("Expected a ClinicalTrials.gov NCT ID")
    title = outcome.get("title", outcome.get("measure"))
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Outcome title/measure is required")
    fields = {"record_id": study_id, "title": title}
    for name in ("description", "timeFrame", "populationDescription", "unitOfMeasure", "paramType"):
        value = outcome.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Outcome {name} must be text or null")
        fields[name] = value
    if class_index is not None:
        classes = outcome.get('classes', [])
        if type(class_index) is not int or not 0 <= class_index < len(classes):
            raise ValueError('Class index must identify an existing source class')
        title = classes[class_index].get('title')
        if title is not None and not isinstance(title, str):
            raise ValueError('Class title must be text or null')
        fields['class_selector'] = {'index': class_index, 'title': title}
    if category_index is not None:
        if class_index is None:raise ValueError('Category selector requires class scope')
        categories = outcome['classes'][class_index].get('categories', [])
        if type(category_index) is not int or not 0 <= category_index < len(categories):
            raise ValueError('Category index must identify an existing category')
        title = categories[category_index].get('title')
        if title is not None and not isinstance(title, str):raise ValueError('Category title must be text or null')
        fields['category_selector'] = {'index': category_index, 'title': title}
    return fields


def indicator_key(study_id: str, outcome: dict, class_index: int | None = None, category_index: int | None = None) -> str:
    # Percent encoding protects literal whitespace inside source text from
    # the generic crosswalk writer's whitespace normalization.
    return PREFIX + quote(json.dumps(outcome_context(study_id, outcome, class_index, category_index),
        ensure_ascii=False, sort_keys=True, separators=(",", ":")), safe="")


def parent_indicator_key(key: str) -> str:
    """Return the containing outcome key; malformed keys cannot select a scope."""
    if not key.startswith(PREFIX):return key
    try:
        context = json.loads(unquote(key[len(PREFIX):]))
        if not isinstance(context, dict) or 'class_selector' not in context:return key
        context.pop('class_selector');context.pop('category_selector',None)
        return PREFIX + quote(json.dumps(context, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')), safe='')
    except (ValueError, TypeError):return key
