from config import RESULT_ROOT, RUNTIME_ROOT, IMPORT_PROJECT_EXCEL
from build_fixtures import build
from fabric.common import write_json
from fabric.connectors import capture_event
from fabric.catalog import Catalog, metadata_graph
from fabric.engine import execute, quality_report
from fabric.sql import parse_sql


def main():
    RESULT_ROOT.mkdir(parents=True,exist_ok=True); RUNTIME_ROOT.mkdir(parents=True,exist_ok=True)
    events,queries=build(); snapshot=capture_event(events[1])
    catalog=Catalog(RUNTIME_ROOT/'demo_catalog.sqlite'); catalog.ingest(snapshot)
    graph=metadata_graph(snapshot,queries); inferred=graph.infer()
    graph.persist(RUNTIME_ROOT/'demo_graph.sqlite')
    write_json(RESULT_ROOT/'semantic_graph.json',graph.export())
    graph.export_neo4j(RESULT_ROOT/'semantic_graph.cypher')
    write_json(RESULT_ROOT/'catalog.json',catalog.entities())
    write_json(RESULT_ROOT/'semantic_search.json',{'search_term':'交易金额',**graph.semantic_search('交易金额')})
    sql='SELECT core.amount, billing.counter FROM core JOIN billing ON core.row_id = billing.row_id WHERE core.amount >= 1.00'
    q=parse_sql(sql); scan=[0]; answer=execute(snapshot,q,scan)
    write_json(RESULT_ROOT/'demo_result.json',{'sql':sql,'plan':q,'answer':answer,
                'scanned_cells':scan[0],'inferred_triples':inferred,'consistency_violations':graph.consistency(),
                'quality':quality_report(snapshot),'snapshot_bindings':{k:v['content_id'] for k,v in snapshot['sources'].items()},
                'limitations':'Local snapshot coordinator; not a distributed MPP/JDBC/GraphQL implementation.'})
    if IMPORT_PROJECT_EXCEL:
        from import_project_metadata import import_metadata
        imported=import_metadata()
        print('Project metadata:',imported.get('nodes'),'nodes;',imported.get('edges'),'declared links')
    print('Demo:',len(answer['answer'] or []),'rows;',inferred,'inferred triples; results:',RESULT_ROOT)


if __name__=='__main__': main()
