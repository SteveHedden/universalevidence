"""Query-time CT.gov result adapter CLI; output is transient unless redirected."""
import argparse
import json
from pathlib import Path
from rdflib import Graph
try:
    from .ctgov_result_rules import load_rules
except ImportError:
    from ctgov_result_rules import load_rules
try:
    from .ctgov_results import query_studies
except ImportError:
    from ctgov_results import query_studies


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('study_ids',nargs='+')
    parser.add_argument('--crosswalk',type=Path,required=True)
    parser.add_argument('--taxonomy',type=Path,action='append',required=True,
                        help='Repeat for indicators, states and subjects')
    parser.add_argument('--rules',type=Path,required=True)
    parser.add_argument('--format',choices=['json','turtle'],default='json')
    args=parser.parse_args()
    taxonomy=Graph()
    for path in args.taxonomy:taxonomy.parse(path)
    result=query_studies(args.study_ids,Graph().parse(args.crosswalk),taxonomy,load_rules(args.rules))
    if args.format=='turtle':print(result['graph'].serialize(format='turtle'))
    else:
        result.pop('graph');responses=result.pop('responses')
        result['sources']=[{'url':r.url,'sha256':r.sha256,'retrieved_at':r.retrieved_at} for r in responses]
        print(json.dumps(result,indent=2))
    if result['failures']:raise SystemExit(2)

if __name__=='__main__':main()
