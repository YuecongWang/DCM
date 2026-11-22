import argparse
import json
from config import DATA_ROOT, RESULT_ROOT
from fabric.common import read_json
from fabric.connectors import capture_event
from fabric.catalog import Catalog, metadata_graph, field_uri
from fabric.engine import execute
from fabric.sql import parse_sql


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--sql',default='SELECT SUM(core.amount) FROM core')
    ap.add_argument('--event',type=int,default=1)
    ap.add_argument('--impact',help='logical field, e.g. core.amount')
    args=ap.parse_args()
    if not (DATA_ROOT/'events.json').exists():
        from build_fixtures import build
        build()
    manifest=read_json(DATA_ROOT/'events.json'); event=manifest['events'][args.event]
    snapshot=capture_event(event)
    if args.impact:
        g=metadata_graph(snapshot,event['queries'])
        value={'field':args.impact,'impacted_queries':g.impacted_queries([field_uri(*args.impact.split('.'))])}
    else:
        q=parse_sql(args.sql); scan=[0]
        value={'sql':args.sql,'query':q,'result':execute(snapshot,q,scan),'scanned_cells':scan[0]}
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
