"""Execute compact RDF result mappings against caller-supplied API/local data.

No retrieval, persistence, source-text inference or ontology mutation. This is
an opt-in application configuration profile, not a CIDS vocabulary extension.
"""
import json
import hashlib
from pathlib import Path
from urllib.parse import urlparse,unquote
from rdflib import Graph, Namespace, URIRef, RDF, Literal
from rdflib.collection import Collection
try:
    from .ctgov_results import digest, semantic_context, validate_source, classify, number, numerical_validation
except ImportError:
    from ctgov_results import digest, semantic_context, validate_source, classify, number, numerical_validation
U=Namespace('https://universalevidence.com/ontology/')
P=Namespace('http://data.cochrane.org/ontologies/pico/')
R=Namespace('https://universalevidence.com/mapping-rules/results/')
F=Namespace(str(R)+'field/')
D=Namespace('http://purl.org/dc/terms/')
C=Namespace('https://ontology.commonapproach.org/cids#')


def local_context(graph, nodes):
    predicates=[U.measurementStudy,U.ofState,U.ofIndicator,U.measure,U.unit,U.comparison,
                U.timing,U.subgroup,U.measurementStatus,U.outcomeStatus,U.sourceLocator]
    return sorted((str(n),str(p),str(v)) for n in nodes for p in predicates for v in graph.objects(n,p))


def pointer(obj, path):
    if not path:return obj
    try:
        for part in path.lstrip('/').split('/'):
            part=part.replace('~1','/').replace('~0','~')
            obj=obj[int(part)] if isinstance(obj,list) else obj[part]
        return obj
    except (KeyError,IndexError,TypeError,ValueError):return None


def load_crosswalk(path):
    """Load a standalone graph or a checksummed local manifest with shared source registry."""
    path=Path(path).resolve()
    graph=Graph().parse(path)
    keys=('source_registry','semantic_crosswalk','result_interpretation')
    if not any(any(graph.triples((None,F[key],None))) for key in keys):
        return graph
    roots=list(graph.subjects(RDF.type,R.Crosswalk))
    if len(roots)!=1:raise ValueError('Expected one crosswalk manifest')
    root=roots[0]
    # A four-layer profile additionally integrity-checks its aggregation policy.
    if any(graph.objects(root,F.aggregation_rules)) or any(graph.objects(root,F.aggregation_rules_sha256)):
        keys += ('aggregation_rules',)
    for key in keys:
        links=list(graph.objects(root,F[key]));hashes=list(graph.objects(root,F[key+'_sha256']))
        if len(links)!=1 or len(hashes)!=1:raise ValueError('Missing or ambiguous crosswalk layer')
        parsed=urlparse(str(links[0]))
        if parsed.scheme!='file' or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('Crosswalk layers must be local files')
        layer=Path(unquote(parsed.path)).resolve()
        if key=='source_registry':
            expected=(path.parent.parent.parent/'vocabularies/sources.ttl').resolve()
            if layer!=expected:raise ValueError('Unexpected shared source registry')
        elif layer.parent!=path.parent:raise ValueError('Crosswalk layer outside manifest directory')
        data=layer.read_bytes()
        if hashlib.sha256(data).hexdigest()!=str(hashes[0]):raise ValueError('Crosswalk layer checksum mismatch')
        graph.parse(data=data.decode('utf-8'),format='turtle',publicID=layer.as_uri())
    return graph


def execute(path, api_responses, local_graph):
    """Return resolved records. Guards reject changed semantics before extraction."""
    g=load_crosswalk(path) if not isinstance(path,Graph) else path
    def one(n,k,required=True,seen=None):
        seen=set() if seen is None else seen
        if n in seen:raise ValueError('Cyclic mapping template')
        seen.add(n)
        vs=list(g.objects(n,F[k]))
        templates=list(g.objects(n,F.template))
        if len(templates)>1:raise ValueError('Ambiguous mapping template')
        if not vs and templates and k!='template':return one(templates[0],k,required,seen)
        if len(vs)>1 or (required and not vs):raise ValueError(f'Ambiguous/missing {k}: {n}')
        return vs[0] if vs else None
    def field(n,k,required=True):
        v=one(n,k,required);return str(v) if v is not None else None
    def rule(n):return {str(p)[len(str(F)):]:str(v) for p,v in g.predicate_objects(n) if str(p).startswith(str(F))}
    mappings=sorted(g.subjects(RDF.type,R.ResultMapping),key=str)
    if len(mappings)!=len(set(mappings)):raise ValueError('Duplicate result mapping')
    studies={one(m,'study_mapping') for m in mappings};contexts={}
    for s in studies:
        kind=field(s,'source_kind')
        if kind=='ctgov-json':
            payload=api_responses[field(s,'record_id')];validate_source(payload,field(s,'record_id'))
            outcome=pointer(payload,field(s,'outcome_pointer'))
            if not isinstance(outcome,dict) or digest(semantic_context(payload,outcome))!=field(s,'context_sha256'):
                raise ValueError('API semantic context changed; mapping requires review')
            contexts[s]=outcome
        elif kind=='local-rdf':
            nodes=[one(m,'source_result') for m in mappings if one(m,'study_mapping')==s]
            if digest(local_context(local_graph,nodes))!=field(s,'context_sha256'):
                raise ValueError('Local result semantics changed; mapping requires review')
        else:raise ValueError('Unsupported source kind')
    resolved={};active=set()
    def resolve(m):
        if m in resolved:return resolved[m]
        if m in active:raise ValueError('Cyclic derived operands')
        if (m,RDF.type,R.ResultMapping) not in g:raise ValueError('Undefined result mapping')
        active.add(m)
        s=one(m,'study_mapping');extractor=one(m,'extractor');kind=field(extractor,'kind')
        r={'mapping':str(m),'study':field(s,'study'),'subject':field(s,'subject'),
           'indicator':field(m,'indicator'),'state':field(m,'state'),'kind':kind,
           'source':field(s,'source'),'confidence':'not assessed'}
        interpretation=rule(one(m,'interpretation'))
        if kind=='local':
            source=one(m,'source_result');r['source_result']=str(source)
            for key in ('value','unit','timing','uncertainty','subgroup','comparison','measure'):
                predicate=one(extractor,key);value=local_graph.value(source,predicate)
                if value is None:r[key]=None
                else:
                    try:r[key]=json.loads(str(value)) if key in ('timing','uncertainty','comparison') else str(value)
                    except ValueError:r[key]=str(value)
            r['provenance']=[str(v) for v in local_graph.objects(source,one(extractor,'source'))]
            if local_graph.value(source,U.ofIndicator)!=one(m,'indicator') or local_graph.value(source,U.ofState)!=one(m,'state'):
                raise ValueError('Local indicator/State mismatch')
            r['kind']='source_comparison'
            ci=r.get('uncertainty') or {}
            if not isinstance(ci,dict):raise ValueError('Malformed local uncertainty')
            raw={};context={}
            if ci.get('kind')=='standard-error':
                policy=R['interpretations/reportedStandardError']
                expected={'kind':'standard-error','operation':'preserve-reported',
                          'domain':'finite-nonnegative','interval_policy':'no-automatic-conversion',
                          'confidence_policy':'not-assessed'}
                if any(field(policy,k,False)!=v for k,v in expected.items()):
                    raise ValueError('Unsupported standard-error interpretation policy')
                raw={'dispersionType':'Standard Error','dispersionValue':ci.get('value')}
                r['uncertainty_assessment']='Reported standard error; interval and significance not inferred'
            elif ci.get('kind')=='confidence-interval':
                raw={'ciLowerLimit':ci.get('lower'),'ciUpperLimit':ci.get('upper'),'ciPctValue':ci.get('level')}
            elif ci:raise ValueError('Unsupported local uncertainty kind')
        elif kind in ('arm_level','within_arm_change','source_comparison'):
            outcome=contexts[s];select=field(m,'selector');source=pointer(outcome,select)
            if not isinstance(source,dict):raise ValueError('Result selector no longer resolves')
            r['source_result']=field(s,'source')+'#'+field(s,'outcome_pointer')+select
            r['source_version']=digest(api_responses[field(s,'record_id')])
            r['measure']=pointer(outcome,field(extractor,'measure'))
            r['value']=pointer(source,field(extractor,'value'));raw=source
            r['population']=pointer(outcome,field(extractor,'population'))
            r['denominators']=pointer(outcome,field(extractor,'denominators'))
            r['uncertainty']={}
            for key in ('p_value','confidence_level','lower','upper','spread'):
                select_field=field(extractor,key,False)
                if select_field:
                    value=pointer(source,select_field)
                    if value is not None:r['uncertainty'][select_field.rsplit('/',1)[-1]]=value
            if kind in ('arm_level','within_arm_change'):
                cl=pointer(outcome,field(m,'class_pointer'))
                r['class_denominators']=pointer(cl,field(extractor,'class_denominators'))
                r['unit']=pointer(outcome,field(extractor,'unit'))
                r['timing']={'source':pointer(outcome,field(extractor,'timing')),'class':pointer(cl,field(extractor,'class_title'))}
                if pointer(source,field(extractor,'group_id'))!=field(one(m,'group'),'source_group_id'):raise ValueError('Group mismatch')
                context={'statistic':pointer(outcome,field(extractor,'statistic')),'dispersionType':pointer(outcome,field(extractor,'dispersion_type'))}
                if field(extractor,'preserve_summary_metadata',False)=='true':
                    r['statistic']=context['statistic'];r['dispersion_type']=context['dispersionType']
                    r['category']=pointer(outcome,select.rsplit('/measurements/',1)[0]).get('title')
                    r['allocation']=api_responses[field(s,'record_id')].get('protocolSection',{}).get('designModule',{}).get('designInfo',{}).get('allocation')
            else:
                # An analysis p-value has no effect unit or direction.
                r['unit']=None;r['timing']=pointer(source,field(extractor,'timing')) or outcome.get('timeFrame')
                r['model']=pointer(source,field(extractor,'model'));context={}
                r['source_group_ids']=pointer(source,field(extractor,'group_ids'))
        elif kind=='derived_comparison':
            left,right=resolve(one(m,'left')),resolve(one(m,'right'))
            if interpretation.get('formula')!='left - right':raise ValueError('Unsupported formula')
            if left['study']!=right['study'] or left['indicator']!=right['indicator'] or left['state']!=right['state'] or left['timing']!=right['timing'] or left['unit']!=right['unit']:
                raise ValueError('Incompatible operands')
            expected=interpretation.get('operand_statistic')
            if expected and (left.get('statistic')!=expected or right.get('statistic')!=expected or left['kind']!='arm_level' or right['kind']!='arm_level'):
                raise ValueError('Incompatible summary statistics or result kinds')
            if expected and (left.get('category')!=right.get('category') or left.get('population')!=right.get('population')):
                raise ValueError('Incompatible category or population')
            comparison=one(m,'comparison')
            if one(one(m,'left'),'group')!=g.value(comparison,P.interventionGroup) or one(one(m,'right'),'group')!=g.value(comparison,P.comparatorGroup):raise ValueError('Reversed comparison operands')
            a,b=number(left['value']),number(right['value'])
            usable=left['numerically_usable'] and right['numerically_usable']
            r.update(value=str(a-b) if usable and a is not None and b is not None else None,
                     unit=left['unit'] if interpretation.get('unit')=='operand-unit' else interpretation.get('unit'),timing=left['timing'],uncertainty={},
                     operands=[left['source_result'],right['source_result']],
                     operand_denominators=[left['denominators'],right['denominators']],
                     limitations=interpretation.get('limitations'),measure=left['measure'],source_version=left['source_version'])
            if expected:
                r['statistic']='difference of '+expected.lower()+'s'
                r['direction_caveat']='Direction of the mapped indicator only. Reported zero is zero at source precision, not proof of no underlying effect.'
                r['effect_scope']='descriptive unadjusted between-group contrast; no causal attribution or pooled confidence'
                r['operand_uncertainty']=[left['uncertainty'],right['uncertainty']]
                r['operand_dispersion_types']=[left.get('dispersion_type'),right.get('dispersion_type')]
                r['operand_class_denominators']=[left.get('class_denominators'),right.get('class_denominators')]
            raw={};context={}
        else:raise ValueError('Unsupported extractor')
        r['numerical_validation']=numerical_validation({'raw':raw,'context':context,'comparison':interpretation,'unit':r['unit'],'value':r['value'],'kind':r['kind']})
        if kind=='local' and (r.get('uncertainty') or {}).get('kind')=='standard-error':
            se=number(r['uncertainty'].get('value'))
            # Missing, nonfinite or negative SEs must not enter aggregation.
            r['numerical_validation']['dispersion']={
                'status':'valid' if se is not None and se>=0 else 'invalid',
                'reason':'Reported standard error must be finite and nonnegative'}
        if r.get('dispersion_type')=='Inter-Quartile Range':
            low=number(raw.get('lowerLimit'));high=number(raw.get('upperLimit'));v=number(r['value'])
            valid=(low is not None and high is not None and low<=high and (v is None or low<=v<=high))
            r['numerical_validation']['dispersion']={'status':'valid' if valid else 'invalid','reason':'Source IQR; not a confidence interval'}
        r['numerically_usable']=(r['numerical_validation']['estimate']['status']=='valid' and r['numerical_validation']['confidence_interval']['status']!='invalid' and r['numerical_validation']['dispersion']['status']!='invalid')
        r['direction']=classify(r['value'],interpretation)['direction'] if r['numerically_usable'] else 'unknown/unsupported'
        comparison=one(m,'comparison',False);group=one(m,'group',False)
        if comparison:
            left=g.value(comparison,P.interventionGroup);right=g.value(comparison,P.comparatorGroup)
            if left is None or right is None or left==right:raise ValueError('Comparison requires distinct defined groups')
            group_nodes=[left,right];r['comparison_id']=str(comparison)
        elif group:group_nodes=[group]
        else:
            seq=one(m,'groups',False);group_nodes=list(Collection(g,seq)) if seq else []
        r['groups']=[]
        for group in group_nodes:
            if (group,RDF.type,P.InterventionGroup) not in g or one(group,'study_mapping')!=s:raise ValueError('Undefined or wrong-study group')
            r['groups'].append({'id':str(group),'components':sorted(map(str,g.objects(group,P.intervention))),
                                'complete':one(group,'assigned_components_complete').toPython() is True})
            group_selector=field(group,'source_group_pointer',False)
            if group_selector:
                source_group=pointer(contexts[s],group_selector)
                if not isinstance(source_group,dict) or source_group.get('id')!=field(group,'source_group_id'):raise ValueError('Source group changed')
                r['groups'][-1]['source_context']=source_group
            note=field(group,'review_note',False)
            if note:r['groups'][-1]['review_note']=note
        if kind=='source_comparison' and 'source_group_ids' in r:
            if set(r['source_group_ids'] or [])!={field(n,'source_group_id') for n in group_nodes}:raise ValueError('Analysis group mismatch')
        if comparison and all(group['complete'] for group in r['groups']):
            l,rhs=(set(x['components']) for x in r['groups']);r['shared_components']=sorted(l&rhs);r['left_only']=sorted(l-rhs);r['right_only']=sorted(rhs-l)
        resolved[m]=r;active.remove(m);return r
    return [resolve(m) for m in mappings]


def to_rdf(records, taxonomy=None, local_graph=None):
    """Transient UE/CIDS projection, retaining comparison groups and source fields."""
    graph=Graph()
    PROV=Namespace('http://www.w3.org/ns/prov#');OBO=Namespace('http://purl.obolibrary.org/obo/')
    for prefix,ns in [('ue',U),('pico',P),('cids',C),('dct',D)]:graph.bind(prefix,ns)
    for r in records:
        node=URIRef(r['mapping']+'/resolved');study=URIRef(r['study'])
        graph.add((node,RDF.type,U.OutcomeMeasurement));graph.add((node,U.measurementStudy,study));graph.add((study,RDF.type,U.Study))
        for pred,key in [(U.ofIndicator,'indicator'),(U.ofState,'state')]:graph.add((node,pred,URIRef(r[key])))
        graph.add((URIRef(r['indicator']),U.bearer,URIRef(r['subject'])))
        graph.add((URIRef(r['indicator']),U.measures,URIRef(r['state'])))
        if taxonomy is not None:
            from rdflib.namespace import SKOS
            for concept in (r['indicator'],r['state'],r['subject']):
                for pred in (RDF.type,SKOS.prefLabel,SKOS.definition,SKOS.inScheme,U.bearer):
                    for obj in taxonomy.objects(URIRef(concept),pred):graph.add((URIRef(concept),pred,obj))
        graph.add((node,U.derivation,Literal(json.dumps(r,sort_keys=True))))
        graph.add((node,U.measure,Literal(r['measure'])))
        graph.add((node,U.comparison,Literal(json.dumps({'kind':r['kind'],'comparison':r.get('comparison_id'),'groups':r['groups'],'source_comparison':r.get('comparison')},sort_keys=True))))
        if r.get('provenance'):
            if local_graph is None:raise ValueError('Local provenance graph required for RDF export')
            original=URIRef(r['source_result'])
            for locator in local_graph.objects(original,U.sourceLocator):
                graph.add((node,U.sourceLocator,locator))
                for triple in local_graph.triples((locator,None,None)):graph.add(triple)
            for doc in local_graph.objects(original,PROV.wasDerivedFrom):
                graph.add((node,PROV.wasDerivedFrom,doc))
                for triple in local_graph.triples((doc,None,None)):graph.add(triple)
        else:
            doc=URIRef(r['source']+'#sha256-'+r['source_version'])
            graph.add((doc,RDF.type,U.ReportDocument))
            for pred,value in [(D.identifier,r['study']),(D.hasVersion,r['source_version']),(D.bibliographicCitation,r['source']),(U.identityVerification,'CT.gov nctId verified by guarded adapter'),(U.reportStatus,'active')]:graph.add((doc,pred,Literal(value)))
            graph.add((doc,D.source,URIRef(r['source'])));graph.add((doc,OBO.IAO_0000136,study))
            graph.add((node,PROV.wasDerivedFrom,doc))
            for loc in ([r['source_result']] if r.get('source_result') else r['operands']):
                locator=URIRef(loc)
                graph.add((node,U.sourceLocator,locator))
                graph.add((locator,D.source,doc))
                graph.add((locator,D.description,Literal(loc.split('#',1)[1])))
                graph.add((locator,U.supportedFields,Literal(json.dumps(['value','uncertainty','timing','comparison','measure']))))
            if r['value'] is None:graph.add((node,U.result_text,Literal('No usable numerical effect estimate; retain source analysis and validation details.')))
        graph.add((node,U.outcomeStatus,Literal('reported')))
        graph.add((node,U.measurementStatus,Literal('active')))
        for pred,key in [(U.contextualIndicator,'indicator'),(U.contextualOutcome,'state')]:
            context=URIRef(str(study)+'/cids/'+digest(r[key]))
            graph.add((node,pred,context));graph.add((context,RDF.type,C.Indicator if key=='indicator' else C.Outcome))
            code=URIRef(str(context)+'/code');graph.add((context,C.hasCode,code));graph.add((code,RDF.type,C.Code))
            from rdflib.namespace import XSD
            graph.add((code,C.hasSpecification,Literal(r[key],datatype=XSD.anyURI)))
        indicator=graph.value(node,U.contextualIndicator);outcome=graph.value(node,U.contextualOutcome)
        graph.add((outcome,C.hasIndicator,indicator));graph.add((indicator,C.forOutcome,outcome))
        for key,pred in [('unit',U.unit),('timing',U.timing),('uncertainty',U.uncertainty),('direction',U.direction)]:
            v=r.get(key)
            if v is not None:graph.add((node,pred,Literal(json.dumps(v,sort_keys=True) if isinstance(v,(dict,list)) else v)))
        if r['numerically_usable']:graph.add((node,U.value,Literal(number(r['value']))))
        if r.get('source_result'):graph.add((node,D.source,URIRef(r['source_result'])))
        for operand in r.get('operands',[]):graph.add((node,D.source,URIRef(operand)))
        if r.get('comparison_id'):
            comparison=URIRef(r['comparison_id']);graph.add((comparison,RDF.type,P.PICO));graph.add((node,D.isPartOf,comparison));graph.add((study,U.studyContext,comparison))
            for pred,group in zip((P.interventionGroup,P.comparatorGroup),r['groups']):graph.add((comparison,pred,URIRef(group['id'])))
        for group in r['groups']:
            group_node=URIRef(group['id']);graph.add((group_node,RDF.type,P.InterventionGroup));graph.add((node,D.references,group_node))
            for component in group['components']:graph.add((group_node,P.intervention,URIRef(component)))
    return graph
