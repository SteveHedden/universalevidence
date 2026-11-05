"""One-time, offline SQLite -> authoritative TTL migration. Never used at runtime.

The input is read-only and retained. Archive the source only after validation
and switching all worker/API configurations. No network or deployment is performed.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import evidence_enrichment as e

def migrate(source,destination):
    destination=Path(destination)
    if destination.exists():raise ValueError('Migration destination must be new')
    conn=sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if conn.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('Legacy integrity check failed')
        with e.writer(destination) as target:
            target.meta.update(dict(conn.execute('select key,value from enrichment_meta')))
            for table in ('studies','documents','findings'):
                target.data[table]={identity:json.loads(payload) for identity,payload in conn.execute('select id,payload from '+table)}
            for revision,checksum,payload,at in conn.execute('select revision,batch_hash,payload,created_at from enrichment_events order by revision'):
                target.data['events'][checksum]={'id':checksum,'sequence':revision,'batch':json.loads(payload),'created_at':at}
            tables={r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
            if 'outcome_manifests' in tables:
                target.workflow['outcome_manifests']={i:json.loads(p) for i,p in conn.execute('select id,payload from outcome_manifests')}
                target.workflow['outcome_work']={}
                for row in conn.execute('select manifest,study,payload,state,owner,token,expires,attempt from outcome_work'):
                    obj=dict(zip(('manifest','study','payload','state','owner','token','expires','attempt'),row));obj['payload']=json.loads(obj['payload'])
                    if obj['state']=='claimed':raise ValueError('Stop/finish outstanding claims before migration')
                    target.workflow['outcome_work'][e.canonical([obj['manifest'],obj['study']])]=obj
                target.workflow['outcome_receipts']={}
                for row in conn.execute('select token,manifest,study,owner,disposition,payload,created_at from outcome_receipts'):
                    obj=dict(zip(('token','manifest','study','owner','disposition','payload','created_at'),row));obj['payload']=json.loads(obj['payload']);target.workflow['outcome_receipts'][obj['token']]=obj
            expected={t:dict(v) for t,v in target.data.items()}
        with e.reader(destination,budget=None) as actual:
            if actual.data!=expected:raise ValueError('RDF round-trip differs from legacy records')
            review=e.review_store(actual)
            if any(x['validation_errors'] for x in review['studies']):raise ValueError('Migrated source validation failed')
        return {t:len(expected[t]) for t in ('studies','documents','findings','events')}
    finally:conn.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--destination',required=True);a=p.parse_args()
    print(json.dumps(migrate(a.source,a.destination),indent=2))
if __name__=='__main__':main()
