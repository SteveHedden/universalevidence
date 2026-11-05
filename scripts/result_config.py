"""Resolve maintained result asset paths relative to the project config."""
import json
from pathlib import Path

def load_result_config(path):
 path=Path(path).resolve();config=json.loads(path.read_text())
 root=(path.parent/config.pop('project_root','..')).resolve()
 for key in ['crosswalk','rules','interventions','intervention_crosswalk','evidence','comparison_components','source_registry','result_interpretation','aggregation_rules','api_semantics']:
  if key in config:config[key]=str((root/config[key]).resolve())
 if 'report_semantics' in config:config['report_semantics']=[str((root/p).resolve()) for p in config['report_semantics']]
 if 'taxonomy' in config:config['taxonomy']=[str((root/p).resolve()) for p in config['taxonomy']]
 return config
