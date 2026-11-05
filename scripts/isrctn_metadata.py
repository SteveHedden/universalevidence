"""Additive ISRCTN planned specifications and document metadata, never estimates."""
from __future__ import annotations
import hashlib
import json
from urllib.parse import quote

FIELDS = ('outcome_specifications', 'report_metadata', 'result_availability')
OUTPUT_ATTRS = {'id','outputType','artefactType','dateCreated','dateUploaded','peerReviewed','patientFacing'}
FILE_ATTRS = {'fileId','originalFilename','downloadFilename','mimeType','version','length','md5sum','sha1sum','sha256sum','sha512sum','checksum','checksumType'}
RESULT_TYPES = {'resultsarticle','basicresults','plainenglishresults'}


def name(node): return node.tag.rsplit('}',1)[-1]
def local(node): return name(node).lower()
def text(node): return ''.join(node.itertext()).strip() or None

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]


def paths(root):
    result={}
    def visit(node,path):
        result[node]=path; counts={}
        for child in node:
            tag=name(child); counts[tag]=counts.get(tag,0)+1
            visit(child,path+'/'+tag+'['+str(counts[tag])+']')
    visit(root,'/'+name(root)); return result


def normalize_space(value): return ' '.join((value or '').split())


def extract_metadata(trial,study_id,response_hash):
    location=paths(trial); url='https://www.isrctn.com/'+study_id
    def provenance(node):return {'record_url':url,'xml_path':location[node],'response_sha256':response_hash}
    specs=[]; legacy=[]
    def walk(node,role='unspecified'):
        tag=local(node)
        role={'primaryoutcomes':'primary','primaryoutcome':'primary','secondaryoutcomes':'secondary','secondaryoutcome':'secondary'}.get(tag,role)
        if tag=='outcomemeasure':
            fields={k:next((text(c) for c in node if local(c)==k),None) for k in ('variable','method','timepoints')}
            if any(fields.values()):
                specs.append({'role':role,'source_id':node.get('id'),'representation':'structured',**fields,'legacy_text':None,'provenance':[provenance(node)]})
                return
        if tag in {'primaryoutcome','secondaryoutcome','outcomemeasure','outcomemeasures'}:
            # Never flatten a structural container into a duplicate prose measure.
            if not any(local(c) in {'outcomemeasure','variable','method','timepoints','primaryoutcome','secondaryoutcome','primaryoutcomes','secondaryoutcomes'} for c in node):
                value=text(node)
                if value:legacy.append({'role':role,'source_id':node.get('id'),'representation':'legacy_prose','variable':None,'method':None,'timepoints':None,'legacy_text':value,'provenance':[provenance(node)]})
                return
        for child in node:walk(child,role)
    walk(trial)
    # Only exact whitespace-normalized equivalence. No semantic/fuzzy matching.
    # Only an unambiguous same-role, same-variable fallback becomes an alias.
    # Historical prose and ambiguous alternatives retain independent records.
    for old in legacy:
        matches=[s for s in specs if s['representation']=='structured' and s['role']==old['role'] and normalize_space(s['variable'])==normalize_space(old['legacy_text'])]
        distinct={fingerprint({k:v for k,v in s.items() if k not in ('provenance','legacy_representations')}):s for s in reversed(matches)}
        match=next(iter(distinct.values())) if len(distinct)==1 else None
        if match:
            match.setdefault('legacy_representations',[]).append({'text':old['legacy_text'],'source_id':old['source_id'],'provenance':old['provenance']})
        else:specs.append(old)
    unique={}
    for spec in specs:
        semantic={k:v for k,v in spec.items() if k not in ('provenance','legacy_representations')}
        key=fingerprint(semantic)
        if key in unique:
            unique[key]['provenance'].extend(spec['provenance'])
        else:unique[key]=spec
    specs=list(unique.values())
    bases={}
    for spec in specs:
        base=url+'#planned-'+spec['role']+'-'+(quote(spec['source_id'],safe='') if spec['source_id'] else fingerprint({k:v for k,v in spec.items() if k not in ('provenance','legacy_representations')}))
        bases.setdefault(base,[]).append(spec)
    for base,variants in bases.items():
        for spec in variants:
            spec['id']=base+('-variant-'+fingerprint({k:v for k,v in spec.items() if k not in ('provenance','legacy_representations')}) if len(variants)>1 else '')
            spec['type']='pre-specified-measure'
    results=[];outputs=[]
    for node in trial.iter():
        if local(node)=='results':
            selected={name(c):text(c) for c in node if local(c) in {'publicationstage','publicationdetails','basicreport','plainenglishreport'}}
            results.append({'fields':selected,'provenance':provenance(node)})
        if local(node)!='outputs':continue
        for output in node:
            if local(output)!='output':continue
            attrs={k:v for k,v in output.attrib.items() if k in OUTPUT_ATTRS}
            links=[];files=[];descriptions=[]
            for child in output:
                if local(child)=='externallink':links.append({'url':child.get('url'),'provenance':provenance(child)})
                elif local(child)=='localfile':files.append({'attributes':{k:v for k,v in child.attrib.items() if k in FILE_ATTRS},'provenance':provenance(child)})
                elif local(child)=='description' and text(child):descriptions.append(text(child))
            identity=attrs.get('id') or fingerprint({'attributes':attrs,'links':[x['url'] for x in links],'files':[x['attributes'] for x in files],'descriptions':descriptions})
            outputs.append({'id':url+'#output-'+quote(identity,safe=''),'attributes':attrs,'external_links':links,'local_files':files,'descriptions':descriptions,'provenance':provenance(output)})
    typed=any(o['attributes'].get('outputType','').lower() in RESULT_TYPES and (any(x['url'] for x in o['external_links']) or any(any(f['attributes'].values()) for f in o['local_files'])) for o in outputs)
    reported=any(any(v for k,v in r['fields'].items() if k.lower() in {'basicreport','plainenglishreport','publicationdetails'}) for r in results)
    return {'outcome_specifications':specs,'report_metadata':{'schema':'isrctn-document-metadata-1','results':results,'outputs':outputs},
            'result_availability':{'planned_specification_count':len(specs),'results_documents':'typed_metadata_present' if typed else 'publication_or_report_text_only' if reported else 'not_identified',
              'structured_numerical_results':'not_extracted','reason':'This adapter preserves specifications and document metadata; it does not interpret or extract numerical results.'}}


def outcome_projection(specs):
    """Keep legacy type/measure fields; structured context is additive."""
    rows=[];seen=set()
    for s in specs:
        value=s['legacy_text'] or s['variable']
        if not value:continue
        row={'type':'pre-specified-measure','measure':value}
        if s['representation']=='structured':
            row.update(specification_id=s['id'],role=s['role'],method=s['method'],time_frame=s['timepoints'])
        key=json.dumps(row,sort_keys=True)
        if key not in seen:rows.append(row);seen.add(key)
    return rows


def copy_metadata(target,source):
    """Explicit allowlist; don't widen public responses to arbitrary XML fields."""
    for field in FIELDS:
        if field in source:target[field]=source[field]
    return target


def merge_metadata(target, source):
    """Preserve metadata from every duplicate search row, including conflicts.

    A source specification ID alone is not sufficient to overwrite another
    version. Equivalent specs union their provenance; differing context remains.
    """
    for field in FIELDS:
        if field not in source:continue
        if field not in target:target[field]=source[field];continue
        if field=='outcome_specifications':
            merged={}
            for spec in [*target[field],*source[field]]:
                key=json.dumps({k:v for k,v in spec.items() if k not in ('provenance','legacy_representations')},sort_keys=True)
                if key not in merged:
                    merged[key]=dict(spec,provenance=list(spec.get('provenance',[])))
                    if 'legacy_representations' in spec:merged[key]['legacy_representations']=list(spec['legacy_representations'])
                else:
                    for name in ('provenance','legacy_representations'):
                        for value in spec.get(name,[]):
                            if value not in merged[key].setdefault(name,[]):merged[key][name].append(value)
            target[field]=list(merged.values())
        elif field=='report_metadata':
            combined=dict(target[field])
            for name in ('results','outputs'):
                values=list(combined.get(name,[]))
                for value in source[field].get(name,[]):
                    if value not in values:values.append(value)
                combined[name]=values
            target[field]=combined
        else:
            rank={'not_identified':0,'publication_or_report_text_only':1,'typed_metadata_present':2}
            previous=target[field];incoming=source[field]
            status=max((previous['results_documents'],incoming['results_documents']),key=lambda v:rank[v])
            target[field]=dict(previous,results_documents=status,planned_specification_count=len(target.get('outcome_specifications',[])))
    return target
