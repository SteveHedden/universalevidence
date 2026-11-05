"""RDF configuration for the deterministic CT.gov result adapter.

The mapping-rule namespace is an application configuration profile, not a
change to the UE ontology or a claim that its terms are CIDS vocabulary.
"""
from pathlib import Path
from urllib.parse import quote, unquote
from rdflib import Graph, Namespace, URIRef, Literal, RDF
from rdflib.collection import Collection

RULE = Namespace('https://universalevidence.com/mapping-rules/results/')
LEGACY_RULE = Namespace('https://universalevidence.com/mapping-rules/ctgov/')
ROOT = RULE.rules
URI_FIELDS = {'indicator', 'state'}


def save_rules(rules, path):
    """One predicate per rule field; nested rules are nodes, ordered operands lists."""
    g=Graph();g.bind('rule',RULE);g.bind('field',Namespace(str(RULE)+'field/'));g.bind('rdf',RDF)
    root=RULE['sets/'+quote(rules['version'],safe='')]
    def encode(value,node,field=None):
        if value is None:return RULE.Null
        if isinstance(value,dict):
            g.add((node,RDF.type,RULE.RuleSet if node==root else RULE.Record))
            for key,item in value.items():
                g.add((node,RULE['field/'+quote(key,safe='')],encode(item,URIRef(str(node)+'/'+quote(key,safe='')),key)))
            return node
        if isinstance(value,list):
            if not value:return RDF.nil
            Collection(g,node,[encode(item,URIRef(str(node)+'/'+str(i)),field) for i,item in enumerate(value)])
            return node
        if field in URI_FIELDS:return URIRef(value)
        if isinstance(value,(str,bool,int,float)):return Literal(value)
        raise ValueError('Unsupported rule value: '+repr(value))
    encode(rules,root)
    g.serialize(destination=path,format='turtle')


def load_rules(path):
    """Read Turtle only; refuse ambiguous values, cycles or unknown structures."""
    g=Graph().parse(Path(path),format='turtle')
    namespaces=[ns for ns in (RULE,LEGACY_RULE) if any(g.subjects(RDF.type,ns.RuleSet))]
    if len(namespaces)!=1:raise ValueError('Expected one result rule namespace')
    rule_ns=namespaces[0]
    roots=set(g.subjects(RDF.type,rule_ns.RuleSet))
    if len(roots)!=1:raise ValueError('Expected one CT.gov rule set')
    root=next(iter(roots))
    active=set()
    def decode(node,field=None):
        if isinstance(node,Literal):return node.toPython()
        if node==rule_ns.Null:return None
        if node==RDF.nil:return []
        if field in URI_FIELDS:return str(node)
        if node in active:raise ValueError('Cyclic rule structure')
        active.add(node)
        try:
            if (node,RDF.type,rule_ns.Record) in g or node==root:
                result={}
                for p,o in g.predicate_objects(node):
                    if p==RDF.type:continue
                    if not str(p).startswith(str(rule_ns)+'field/'):raise ValueError('Unknown rule predicate')
                    key=unquote(str(p).removeprefix(str(rule_ns)+'field/'))
                    if key in result:raise ValueError('Multiple values for rule field '+key)
                    result[key]=decode(o,key)
                return result
            if (node,RDF.first,None) in g:
                result=[];seen=set();current=node
                while current!=RDF.nil:
                    if current in seen:raise ValueError('Cyclic rule list')
                    seen.add(current)
                    first=list(g.objects(current,RDF.first));rest=list(g.objects(current,RDF.rest))
                    if len(first)!=1 or len(rest)!=1:raise ValueError('Invalid rule list')
                    result.append(decode(first[0],field));current=rest[0]
                return result
            raise ValueError('Unknown rule node '+str(node))
        finally:active.remove(node)
    result=decode(root)
    if not isinstance(result.get('outcomes'),list) or not isinstance(result.get('version'),str):raise ValueError('Invalid rule set')
    return result
