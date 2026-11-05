"""Lossless CIDS alignment for UE outcome measurements.

Contextual CIDS nodes classify results; effect estimates remain UE/STATO.
Do not treat an odds ratio or a difference as the measured prevalence/level.
This is a UE application profile, not a complete CIDS reporting-tier capsule.
"""
import hashlib
from rdflib import Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

UE=Namespace('https://universalevidence.com/ontology/')
CIDS=Namespace('https://ontology.commonapproach.org/cids#')
I72=Namespace('http://ontology.eil.utoronto.ca/ISO21972/iso21972#')
OBO=Namespace('http://purl.obolibrary.org/obo/')

def scoped(study,kind,uri):
    return URIRef(study+'/cids/'+kind+'/'+hashlib.sha256(uri.encode()).hexdigest()[:24])

def add_cids(g,findings):
    for prefix,ns in [('cids',CIDS),('i72',I72)]:g.bind(prefix,ns)
    for identity,f in sorted(findings.items()):
        node=URIRef(identity);study=f['study_id'];state=f['state_uri']
        outcome=scoped(study,'outcome',state);code=scoped(study,'code',state)
        g.add((node,UE.contextualOutcome,outcome))
        g.add((outcome,RDF.type,CIDS.Outcome))
        g.add((outcome,CIDS.hasName,Literal('Study outcome: '+state.rsplit('/',1)[-1])))
        g.add((outcome,CIDS.hasCode,code));g.add((code,RDF.type,CIDS.Code))
        g.add((code,CIDS.hasSpecification,Literal(state,datatype=XSD.anyURI)))
        if f.get('indicator_uri'):
            uri=f['indicator_uri'];indicator=scoped(study,'indicator',uri);icode=scoped(study,'code',uri)
            g.add((node,UE.contextualIndicator,indicator))
            g.add((outcome,CIDS.hasIndicator,indicator));g.add((indicator,RDF.type,CIDS.Indicator))
            g.add((indicator,CIDS.forOutcome,outcome))
            g.add((indicator,CIDS.hasName,Literal('Study indicator: '+uri.rsplit('/',1)[-1])))
            g.add((indicator,CIDS.hasCode,icode));g.add((icode,RDF.type,CIDS.Code))
            g.add((icode,CIDS.hasSpecification,Literal(uri,datatype=XSD.anyURI)))
        if f.get('value') is not None:
            estimate=URIRef(identity+'/estimate')
            # The numerical measure here belongs to the statistic, not to a
            # cids:IndicatorReport for the underlying prevalence/HAZ indicator.
            g.add((estimate,RDF.type,I72.Measure))
            g.add((estimate,I72.numerical_value,Literal(f['value'])))
            if f.get('unit','').lower()=='odds ratio':
                g.add((estimate,RDF.type,OBO.STATO_0000182))
            elif 'mean difference' in f.get('measure','').lower():
                g.add((estimate,RDF.type,OBO.STATO_0000457))
            # Other effects retain generic statistic type plus faithful unit/text;
            # do not guess risk-ratio/standardized-mean-difference classes.
