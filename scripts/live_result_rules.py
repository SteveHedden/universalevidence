"""Source-field interpretation; no study-specific configuration or network access.

Exact measure/unit contracts deliberately fail closed. A new study ID does not
require a new rule. Full source context is retained on every supported finding.
"""
from __future__ import annotations
import re
import time
from datetime import datetime, timezone
from live_result_identity import contextual_measure, scoped_measure, reviewed_interventions, has_scoped_identity
from ctgov_intervention_identity import contextual_key
from ctgov_results import number, numerical_validation, classify, digest, validate_source, SourceDataError

VERSION = "live-results-v3"
PREFIX = "https://universalevidence.com/"
# Existing taxonomy identities. These are semantic aliases, never study selectors.
MEASURES = (
    ("HeightForAgeZScore", "Height-for-age z-score", ("Stunting", "ChildGrowth"),
     r"(?:height|length)[ -]for[ -]age z[ -]scores?(?: \(haz\)| \(laz\))?|[hl]az",
     {"z score", "z-score", "z-scores", "z score units", "z-score units", "standard deviations", "sd"}, "z-score"),
    ("BloodPressureMeasurement", "Blood pressure measurement", ("CardiovascularDiseases",),
     r"(?:systolic |diastolic )?blood pressure(?: \(sbp\)| \(dbp\))?",
     {"mmhg", "mm hg", "millimeters of mercury"}, "mmHg"),
    ("GlycemicControl", "Glycemic control", ("NonCommunicableDiseases",),
     r"(?:hemoglobin a1c|haemoglobin a1c|glycated hemoglobin|glycosylated hemoglobin|hba1c)(?: \(hba1c\))?",
     {"%", "percent", "percentage", "a1c percent", "percentage of hemoglobin", "percentage of hba1c"}, "%"),
)
PARAMETERS = {"Mean Difference": ("difference", None), "MEAN_DIFFERENCE": ("difference", None),
              "Mean Difference (Final Values)": ("difference", None),
              "Least Square (LS) Mean Difference": ("difference", None),
              "LS Mean Difference": ("difference", None),
              "Risk Ratio": ("ratio", "risk ratio"), "Relative Risk": ("ratio", "relative risk"),
              "Odds Ratio": ("ratio", "odds ratio"), "Hazard Ratio": ("ratio", "hazard ratio")}


def normalized(value):
    return " ".join(str(value or "").casefold().split())


def measure_rule(outcome):
    title = normalized(outcome.get("title"))
    change = title.startswith("change from baseline in ") or title.startswith("change in ")
    title = re.sub(r"^change (?:from baseline )?in ", "", title)
    if change:
        title = re.sub(r" over \d+ months? from enrollment to end of study\.?$", "", title)
    unit = normalized(outcome.get("unitOfMeasure"))
    hits = [m for m in MEASURES if re.fullmatch(m[3], title) and unit in m[4]]
    if len(hits) != 1:
        return None
    m = hits[0]
    return {"indicator": PREFIX+"vocab/indicators/"+m[0], "label": m[1],
            "states": [PREFIX+"vocab/states/"+s for s in m[2]], "unit": m[5],
            "quantity": title, "change": change}


def validate_protocol(study):
    module=study.get('protocolSection',{}).get('armsInterventionsModule',{})
    for field in ('armGroups','interventions'):
        values=module.get(field,[])
        if not isinstance(values,list) or any(not isinstance(x,dict) for x in values):
            raise SourceDataError(f'armsInterventionsModule/{field}: expected an array of objects')
        for index,value in enumerate(values):
            for key in (('label','type','description') if field=='armGroups' else ('name','type','description')):
                if key in value and not isinstance(value[key],str):
                    raise SourceDataError(f'{field}/{index}/{key}: expected text')
            for key in (('interventionNames',) if field=='armGroups' else ('armGroupLabels','otherNames')):
                if key in value and (not isinstance(value[key],list) or any(not isinstance(x,str) for x in value[key])):
                    raise SourceDataError(f'{field}/{index}/{key}: expected an array of text')


def unsupported_estimand(outcome, analysis):
    """Positive context contract, not permission inferred from two group IDs.

    Unrecognized free-text model semantics need a reusable rule review. The numeric
    estimate remains visible; unknown context must never silently become a main effect.
    """
    description=normalized(outcome.get('description'))
    comment=normalized(analysis.get('statisticalComment'))
    method=normalized(analysis.get('statisticalMethod'))
    extra=' '.join(normalized(analysis.get(k)) for k in ('estimateComment','pValueComment','nonInferiorityComment')).strip()
    text=' '.join((description,comment,method,extra,normalized(analysis.get('paramType'))))
    for label,pattern in (
        ('interaction',r'\b(interaction|effect modification|moderation)\b'),
        ('association',r'\b(association|associated|correlation|correlated|observational)\b'),
        ('transformed',r'\b(log|logarithm|logarithmic|transformed|standardized coefficient|reverse.coded)\b')):
        if re.search(pattern,text):return label, 'Source describes an unsupported '+label+' estimand'
    if analysis.get('testedNonInferiority') or analysis.get('nonInferiorityType') or extra:
        return 'unsupported','Estimate/test commentary or non-inferiority context has no reviewed estimand contract'
    if description and description != normalized(outcome.get('title')):
        return 'unsupported','Outcome description requires an explicit reusable estimand rule'
    if comment not in ('','unadjusted','adjusted for baseline','adjusted for baseline value'):
        return 'unsupported','Analysis model context requires an explicit reusable estimand rule'
    if method not in ('','t-test','t test','ancova','anova','analysis of covariance','analysis of variance'):
        return 'unsupported','Statistical method has no reviewed estimand contract'
    return 'pairwise',None


def presentation_record(study):
    """Keep valid registry metadata renderable even when arm metadata is malformed.

    Only the presentation view is sanitized. Interpretation sees the original bytes
    and reports invalid_source, so this cannot legitimize a malformed comparison.
    """
    protocol=study.get('protocolSection',{})
    module=protocol.get('armsInterventionsModule',{})
    if not isinstance(module,dict):module={}
    interventions=module.get('interventions',[])
    if not isinstance(interventions,list):interventions=[]
    clean=[i for i in interventions if isinstance(i,dict) and isinstance(i.get('name'),str)]
    return {**study,'protocolSection':{**protocol,'armsInterventionsModule':{**module,'interventions':clean}}}


def interpret_safely(study, intervention_map):
    """Isolate expected external-data faults; do not swallow arbitrary code failures."""
    try:
        return interpret(study,intervention_map)
    except (SourceDataError,TypeError,ValueError,KeyError,AttributeError) as error:
        return {'rows':[],'status':'invalid_source','reason':str(error),
                'error_type':type(error).__name__,'version':VERSION}


def components(study, group, intervention_map, reviewed_names=None):
    """Resolve exact source arm labels, with scoped identity taking precedence.

    Never interpret missing mappings as an untreated control. A placebo/no-
    intervention arm must say so explicitly in the protocol.
    """
    module = study.get("protocolSection", {}).get("armsInterventionsModule", {})
    if not isinstance(module.get("armGroups", []), list) or not isinstance(module.get("interventions", []), list):
        return None
    if any(not isinstance(x, dict) for x in module.get("armGroups", [])+module.get("interventions", [])):
        return None
    if reviewed_names is not None:
        selected = [i for i in module.get("interventions", []) if i.get("name") in reviewed_names]
        if len(selected) != len(reviewed_names) or len(set(reviewed_names)) != len(reviewed_names):
            return None
        # Names are source-context-pinned, not fuzzy aliases for protocol arms.
        arm = {"label": "reviewed result group", "type": "EXPERIMENTAL"}
        module = {"interventions": [{**i, "armGroupLabels": [arm["label"]]} for i in selected],
                  "armGroups": [arm]}
        group = {"title": arm["label"]}
    arms = [a for a in module.get("armGroups", [])
            if normalized(a.get("label")) == normalized(group.get("title"))]
    if len(arms) != 1:
        return None
    arm = arms[0]
    if arm.get("type") in ("PLACEBO_COMPARATOR", "NO_INTERVENTION", "SHAM_COMPARATOR"):
        # Shared active components must still be retained when present.
        pass
    mapped = set()
    found = False
    for intervention in module.get("interventions", []):
        if arm.get("label") not in intervention.get("armGroupLabels", []):
            continue
        found = True
        if intervention.get("type") == "DRUG" and normalized(intervention.get("name")) == "placebo":
            continue
        sid = study['protocolSection']['identificationModule']['nctId']
        scoped = contextual_key(sid, intervention.get('name'))
        value = intervention_map.get(scoped, intervention_map.get(normalized(intervention.get("name"))))
        if not value:
            return None
        values = [value] if isinstance(value, tuple) and len(value)==2 and isinstance(value[0], str) else value
        if not isinstance(values, (list, tuple)) or len(values) != 1:
            return None
        mapped.add(values[0][0])
    if not found and arm.get("type") not in ("NO_INTERVENTION", "PLACEBO_COMPARATOR", "SHAM_COMPARATOR"):
        return None
    return sorted(mapped)


def orientation(analysis, groups, statistic):
    if len(groups) != 2 or any(g is None for g in groups):
        return None
    left, right = (normalized(g.get("title")) for g in groups)
    if not left or not right or left == right:
        return None
    text = normalized(analysis.get("groupDescription"))
    operators = (" minus ", " - ") if statistic == "difference" else (" divided by ", " / ")
    for op in operators:
        if text == left+op+right: return "forward"
        if text == right+op+left: return "reverse"
    return None


def interaction_contract(outcome, analysis, groups):
    """A deliberately narrow, reusable source grammar for a net change contrast.

    The source must explicitly name the ordered group subtraction AND the two
    assessment classes. Group array order never supplies arithmetic direction.
    """
    orient = orientation(analysis, groups, 'difference')
    comment = normalized(analysis.get('statisticalComment'))
    titles = [normalized(c.get('title')) for c in outcome.get('classes', [])]
    match = re.fullmatch(r'treatment-by-time interaction: change from (.+) to (.+)\.', comment)
    scope = bool(match and match[1] != match[2] and titles.count(match[1]) == titles.count(match[2]) == 1
                 and all(len(c.get('categories', [])) == 1 and not c['categories'][0].get('title')
                         for c in outcome.get('classes', [])))
    clean = (normalized(analysis.get('statisticalMethod')) in ('mixed models analysis', 'mixed model', 'mixed-effects model')
             and not any(analysis.get(k) for k in ('estimateComment','pValueComment','nonInferiorityComment','testedNonInferiority'))
             and analysis.get('nonInferiorityType') in (None,'SUPERIORITY'))
    reasons = []
    if not orient: reasons.append('Source must state ordered group subtraction; group-ID order and "between" do not establish direction')
    if not scope: reasons.append('Source must specify the treatment-by-time change contrast and its two assessment classes')
    if not clean: reasons.append('Model/test context does not meet the reviewed mixed-model interaction contract')
    return orient, (match[1] + ' to ' + match[2]) if scope else None, '; '.join(reasons) or None


def arm_kind(rule, cl):
    if not rule.get('contextual'):
        return 'within_arm_change' if rule['change'] else 'arm_level'
    title = normalized(cl.get('title'))
    if re.match(r'^change (?:from|in) ', title):
        return 'within_arm_change'
    # Explicit assessment classes report observations at those assessments. The
    # outcome title may say "Change in" while its cells remain observed levels.
    if re.fullmatch(r'baseline|post[ -]intervention|\d+ (?:days?|weeks?|months?|years?)', title):
        return 'arm_level'
    if not title and rule.get('change'):
        return 'within_arm_change'
    return 'reported_arm_measurement'


def interpret(study, intervention_map=None):
    started = time.perf_counter()
    normalized_map, ambiguous_keys = {}, set()
    for mapping in (reviewed_interventions(), intervention_map or {}):
        for key, value in mapping.items():
            key = key if key.startswith('ctgov-intervention:v1|') else normalized(key)
            values = [value] if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str) else value
            if not isinstance(values, (list, tuple)) or len(values) != 1 or not isinstance(values[0], (list, tuple)) or len(values[0]) != 2:
                ambiguous_keys.add(key)
            elif key in normalized_map and normalized_map[key][0][0] != values[0][0]:
                ambiguous_keys.add(key)
            else:
                normalized_map[key] = values
    for key in ambiguous_keys:
        normalized_map[key] = []  # Conflicting reviewed identities never silently override one another.
    intervention_map=normalized_map
    try:
        validate_source(study)
        validate_protocol(study)
    except SourceDataError as error:
        return {"rows": [], "status": "invalid_source", "reason": str(error), "version": VERSION}
    sid = study["protocolSection"]["identificationModule"]["nctId"]
    identity = PREFIX+"studies/ctgov/"+sid
    source = "https://clinicaltrials.gov/study/"+sid
    fingerprint = digest(study)
    rows, coverage = [], []
    outcomes = study.get("resultsSection", {}).get("outcomeMeasuresModule", {}).get("outcomeMeasures", [])
    for oi, outcome in enumerate(outcomes):
        locator = "/resultsSection/outcomeMeasuresModule/outcomeMeasures/"+str(oi)
        from live_result_contracts import context_contract
        contract = context_contract(study, outcome)
        def group_components(group):
            return components(study, group, intervention_map,
                contract.get("group_component_names", {}).get(group.get("id")))
        parent_rule = contextual_measure(sid, outcome)
        scoped = has_scoped_identity(sid, outcome)
        rule = parent_rule or measure_rule(outcome)
        if scoped:
            rule = next((r for ci, cl in enumerate(outcome.get('classes', []))
                         for ki, _ in enumerate(cl.get('categories', []))
                         if (r := scoped_measure(sid, outcome, ci, ki))), None)
        reason = None
        if 'never collected' in normalized(outcome.get('populationDescription')):
            coverage.append({'locator':locator,'title':outcome.get('title'),'status':'not_collected',
                             'reason':outcome['populationDescription']})
            continue
        if outcome.get("reportingStatus") != "POSTED" or not study.get("hasResults"):
            reason = "Outcome results are not posted"
        elif not rule:
            reason = "Unsupported or ambiguous measure/unit semantics"
        if reason:
            coverage.append({"locator": locator, "title":outcome.get('title'), "status": "unsupported", "reason": reason})
            for ci, cl in enumerate(outcome.get('classes', [])):
                for ki, cat in enumerate(cl.get('categories', [])):
                    for mi, m in enumerate(cat.get('measurements', [])):
                        coverage.append({'locator':f'{locator}/classes/{ci}/categories/{ki}/measurements/{mi}',
                                         'status':'unsupported','reason':reason})
            continue
        groups = {g["id"]: g for g in outcome.get("groups", [])}
        if len(groups) != len(outcome.get("groups", [])):
            coverage.append({"locator": locator, "status": "unsupported", "reason": "Duplicate group identifiers"})
            continue

        def add(raw, path, kind, context, value, unit, comparison, summary=None):
            from live_result_identity import preferred_labels
            record = {"raw": raw, "context": context, "comparison": comparison, "unit": unit,
                      "kind": kind, "source_value": value, "value": value}
            validation = numerical_validation(record)
            valid = validation["estimate"]["status"] == "valid"
            measurement = source+"/response/"+fingerprint+path
            row = {"study": identity, "measurement": measurement, "indicator": rule["indicator"],
                   "indicator_label": rule["label"], "states": rule["states"],
                   "preferred_labels": preferred_labels(context.get("components", [])),
                   "measure_definition": rule.get('definition'), "scope_notes": rule.get('scope_notes', []),
                   "result_interventions": context.get('components', []),
                   "value": str(value) if valid else None, "unit": unit,
                   "direction": classify(value, comparison)["direction"] if valid and comparison.get('estimand') != 'interaction' else "unknown/unsupported",
                   "comparison": {**comparison, "context": context, "numerical_validation": validation},
                   "timing": outcome.get("timeFrame"), "subgroup": context.get("population"),
                   "uncertainty": {k: v for k, v in {"dispersionType":context.get("dispersionType"),**raw}.items() if k in (
                       "ciPctValue", "ciLowerLimit", "ciUpperLimit", "ciNumSides", "pValue", "dispersionType", "dispersionValue", "spread", "lowerLimit", "upperLimit")},
                   "source_details": [{"url": source, "locator": path, "sha256": fingerprint,
                                       "rule_version": VERSION, "raw": raw, "outcome": {k:v for k,v in outcome.items() if k not in ("classes", "analyses")}}],
                   "citations": [{"url": source, "citation": study["protocolSection"]["identificationModule"].get("briefTitle", "")}],
                   "interpretation": "Source-reported result; no pooled effect or inferred component effect"}
            if summary and valid:
                row["summary_mapping"] = {**summary, "selected": True, "study": identity,
                    "measurement": measurement, "indicator": rule["indicator"], "unit": unit,
                    "contrast_id": digest({"path":path,"context":context}), "model_context": comparison["quantity"], "effect_multiplier": 1 if comparison["orientation"]=="forward" else -1}
            if not valid:
                coverage.append({'locator':path,'status':'invalid_numerical_value','reason':validation['estimate']['reason']})
            rows.append(row)
        for ci, cl in enumerate(outcome.get("classes", [])):
            for ki, category in enumerate(cl.get("categories", [])):
                for mi, m in enumerate(category.get("measurements", [])):
                    path = f"{locator}/classes/{ci}/categories/{ki}/measurements/{mi}"
                    if rule.get('contextual'):
                        selected = scoped_measure(sid, outcome, ci, ki)
                        if not selected:
                            coverage.append({'locator':path,'status':'unsupported','reason':'Missing or conflicting scoped Indicator identity'})
                            continue
                        rule = selected
                    if m.get("groupId") not in groups:
                        coverage.append({"locator":path,"status":"unsupported","reason":"Unknown measurement group"}); continue
                    # Count parameters are counts even when the outcome declares percent units.
                    statistic=outcome.get("paramType")
                    if statistic not in ("MEAN", "LEAST_SQUARES_MEAN", "MEDIAN", "NUMBER", "COUNT_OF_PARTICIPANTS"):
                        coverage.append({"locator":path,"status":"unsupported","reason":"Unsupported arm statistic"}); continue
                    unit="participants" if statistic=="COUNT_OF_PARTICIPANTS" else rule["unit"]
                    group = groups[m['groupId']]
                    mapped = group_components(group)
                    # Explicit actual-receipt strata supersede household assignment.
                    actual_receipt = normalized(cl.get("title")) == "children who received placebo"
                    if actual_receipt:
                        mapped = []
                    kind = contract.get("arm_kind", arm_kind(rule, cl))
                    if kind not in ("arm_level", "within_arm_change", "reported_arm_measurement"):
                        kind = arm_kind(rule, cl)
                    context = {"components": mapped or [], 'mapping_status':'mapped' if mapped is not None else 'unresolved',
                        'mapping_reason':None if mapped is not None else 'Source arm or intervention identity is not unambiguously mapped',
                        "statistic": statistic, "dispersionType": outcome.get("dispersionType"),
                        "group": groups[m["groupId"]], "class": cl.get("title"), "category": category.get("title"),
                        **({"actual_receipt": "placebo"} if actual_receipt else {}),
                        "population": outcome.get("populationDescription"), "timeFrame": outcome.get("timeFrame"),
                        "outcome_denoms": outcome.get("denoms"), "class_denoms": cl.get("denoms")}
                    add(m, path, kind, context,
                        m.get("value"), unit, {"kind": kind,
                        "quantity": rule["quantity"], "reason": "Descriptive measurement; not an intervention effect"})
        for ai, a in enumerate(outcome.get("analyses", [])):
            if scoped:
                coverage.append({'locator':f'{locator}/analyses/{ai}', 'title':outcome.get('title'),
                                 'status':'unsupported_analysis', 'source_analysis':a,
                                 'reason':'Outcome-level analysis has no unambiguous parent Indicator identity; scoped measurements do not assign its contrast'})
                continue
            is_interaction = a.get('paramType') == 'Mean Difference (Net)'
            stat = ('difference', None) if is_interaction else PARAMETERS.get(a.get("paramType"))
            gs = [groups.get(g) for g in a.get("groupIds", [])]
            orient = orientation(a, gs, stat[0]) if stat else None
            # Ratios retain the named statistic/quantity, never relabel odds as a mean.
            cmp = {"kind": "source_comparison", "statistic": stat[0] if stat else None,
                   "orientation": orient, "semantics": "explicit" if orient else "unsupported",
                   "scale": "identity", "quantity": (a.get("paramType", "")+" of "+rule["quantity"])}
            summary = None
            contextual_sections = len(outcome.get('classes', [])) > 1 or any(len(c.get('categories', [])) > 1 for c in outcome.get('classes', []))
            estimand, semantic_reason = unsupported_estimand(outcome,a)
            time_scope = None
            if is_interaction:
                estimand = 'interaction'
                orient, time_scope, semantic_reason = interaction_contract(outcome, a, gs)
                cmp.update(orientation=orient, semantics='explicit' if not semantic_reason else 'unsupported',
                           time_scope=time_scope, model_context=' · '.join(str(a[k]) for k in ('statisticalMethod','statisticalComment') if a.get(k)), summary_eligible=False)
                contextual_sections = False  # Explicit grammar validates its own class/category scope.
            cmp['estimand']=estimand
            if semantic_reason:
                cmp['semantics']='unsupported'
                cmp['reason']=semantic_reason
            if contextual_sections:
                cmp['semantics']='unsupported'
                cmp['reason']='Analysis-to-class/category scope is not explicit'
            if orient and not contextual_sections and not semantic_reason and not is_interaction:
                cs = [group_components(g) for g in gs]
                if all(c is not None for c in cs):
                    tested = sorted(set(cs[0])-set(cs[1])); comparator = sorted(set(cs[1])-set(cs[0])); shared = sorted(set(cs[0])&set(cs[1]))
                    if tested:
                        summary = {"estimand": "pairwise", "tested": tested, "comparator": comparator, "background": shared,
                                   "package": len(tested)>1, "group_labels": [g["title"] for g in gs]}
            cs = [group_components(g) if g else None for g in gs]
            ordered = gs if orient == 'forward' else list(reversed(gs)) if orient == 'reverse' else []
            ordered_cs = cs if orient == 'forward' else list(reversed(cs)) if orient == 'reverse' else []
            cmp['ordered_groups'] = [g['title'] for g in ordered]
            cmp['ordered_components'] = ordered_cs
            if len(ordered_cs) == 2 and all(c is not None for c in ordered_cs):
                left, right = map(set, ordered_cs)
                cmp['attribution'] = {'tested':sorted(left-right), 'comparator':sorted(right-left),
                                      'background':sorted(left & right), 'package':len(left-right)>1}
            if is_interaction and not semantic_reason:
                cmp['displayable'] = True
            context = {'components':sorted({v for c in cs if c for v in c}),
                       'mapping_status':'mapped' if all(c is not None for c in cs) else 'unresolved',
                       "statistic": a.get("paramType"), "groups": gs, "population": outcome.get("populationDescription"),
                       "timeFrame": outcome.get("timeFrame"), "adjustment": a.get("statisticalComment"), "groupDescription": a.get("groupDescription")}
            display = contract.get("analysis_display", {}).get(str(ai), {})
            display = display if display.get("source_param_type") == a.get("paramType") else {}
            if display:
                # A reviewed reported estimate is not permission to infer ordering,
                # isolate a component or generate an effect-direction summary.
                cmp.update(displayable=True, summary_eligible=False,
                           model_context=display["model_context"], reason=display["limitations"])
                context["adjustment"] = display["model_context"]
                summary = None
            add(a, f"{locator}/analyses/{ai}", "source_comparison", context, a.get("paramValue"),
                display.get("unit") or ((stat[1] or rule["unit"]) if stat else "unsupported"), cmp, summary)
            if not summary:
                coverage.append({"locator":f"{locator}/analyses/{ai}","status":"unsupported_summary",
                                 "reason":cmp.get("reason") or ('Reported interaction is displayable but excluded from effect-direction summaries and ranking' if is_interaction and cmp.get('displayable') else "Explicit orientation and unambiguous arm/component mapping required"),
                                 "title":outcome.get('title'),"source_analysis":a})
    # Exact repeated findings are one finding; differing source values remain separate.
    unique = {}
    for row in rows:
        key = digest({k:v for k,v in row.items() if k not in ("measurement", "source_details", "summary_mapping")})
        if key not in unique: unique[key] = row
        else: unique[key]["source_details"].extend(row["source_details"])
    rows=list(unique.values())
    return {"rows": rows, "coverage": coverage, "version": VERSION, "sha256": fingerprint,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "publication_fallback_eligible": study.get("hasResults") is False and not study.get("resultsSection"),
            "status": "available" if any(r["value"] is not None for r in rows) else "unsupported" if outcomes else "no_api_numerical_results",
            "interpretation_seconds": time.perf_counter()-started}
