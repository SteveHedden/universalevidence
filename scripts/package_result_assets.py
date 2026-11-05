"""Freeze maintained, reviewed assets into an immutable local release directory.
No network writes. Runtime requires this manifest; ops artifacts are not inputs.
"""
import argparse
import hashlib
import json
from pathlib import Path
from result_config import load_result_config


def package(config_path, destination):
    config=load_result_config(config_path)
    dest=Path(destination)
    dest.mkdir(parents=True,exist_ok=False)
    manifest={}
    for key in ('crosswalk','rules','interventions','intervention_crosswalk','evidence','taxonomy','comparison_components','report_semantics','source_registry','result_interpretation','aggregation_rules','api_semantics'):
        if key not in config: continue
        paths=config[key] if key in ('taxonomy','report_semantics') else [config[key]]
        names=[]
        for index,path in enumerate(paths):
            source=Path(path)
            if 'ops' in source.parts: raise ValueError('Review artifacts cannot be runtime assets')
            before=source.stat(); data=source.read_bytes(); after=source.stat()
            if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size): raise ValueError('Asset changed during capture')
            name=f'{key}-{index}.ttl'; (dest/name).write_bytes(data); names.append(name)
            manifest[name]={'sha256':hashlib.sha256(data).hexdigest(),'source':str(source),'bytes':len(data)}
        config[key]=names if key in ('taxonomy','report_semantics') else names[0]
    # Verify the entire capture again, detecting concurrent Teacher publication.
    for entry in manifest.values():
        if hashlib.sha256(Path(entry['source']).read_bytes()).hexdigest()!=entry['sha256']:
            raise ValueError('Maintained assets changed; discard candidate and capture again')
    config.update(project_root='.',review_only=False,include_all_statuses=True,source_timeout=5,workers=4,max_result_requests=20)
    config.pop('search_url',None)
    (dest/'config.json').write_text(json.dumps(config,indent=2)+'\n')
    manifest['config.json']={'sha256':hashlib.sha256((dest/'config.json').read_bytes()).hexdigest()}
    (dest/'manifest.json').write_text(json.dumps({'schema':1,'files':manifest},indent=2)+'\n')
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    package(a.config,a.output)
