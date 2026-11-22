import time
from .common import digest, canonical
from .connectors import capture_event
from .catalog import Catalog, metadata_graph, changed_fields, field_uri, scopes_for_query
from .engine import execute, check_equivalence, scope_identity


def maintain(group,events,runtime_path,check_delay_ms=0,execution_delay_ms=0,repeat=0):

    old=capture_event(events[0]); old_queries=events[0]['queries']
    answers={q['id']:execute(old,q,[0]) for q in old_queries}
    definitions={q['id']:digest(q) for q in old_queries}
    catalog=Catalog(runtime_path); catalog.ingest(old)
    old_graph=metadata_graph(old,old_queries); old_graph.infer()
    cache={}; records=[]; audits=[]
    for event in events[1:]:
        queries=event['queries']
        oracle_snapshot=capture_event(event)
        reference={q['id']:execute(oracle_snapshot,q,[0]) for q in queries}
        m=dict(group=group,event=event['name'],repeat=repeat,queries=len(queries),checks=0,
               reexecutions=0,reused=0,evidence_hits=0,expired_entries=0,wrong_reuse=0,
               answer_mismatches=0,error_mismatches=0,check_scanned_cells=0,execution_scanned_cells=0,
               capture_scanned_cells=0,evidence_entries=0,query_definition_invalidations=0,
               analysis_seconds=0.,check_seconds=0.,execution_seconds=0.,
               check_delay_ms=check_delay_ms,execution_delay_ms=execution_delay_ms,
               timing_mode='real' if check_delay_ms==execution_delay_ms==0 else 'injected_delay')
        start=time.perf_counter(); t=time.perf_counter()
        new=capture_event(event)
        catalog.ingest(new)
        graph=metadata_graph(new,queries); graph.infer()
        changed=changed_fields(old,new)

        impacted=set(graph.impacted_queries([field_uri(*f.split('.')) for f in changed]))
        impacted.update(old_graph.impacted_queries([field_uri(*f.split('.')) for f in changed]))
        ordinary={q['id'] for q in queries if set(scopes_for_query(q)) & set(changed)}
        if impacted != ordinary:
            raise AssertionError('Graph and ordinary dependency impact differ')
        m['capture_scanned_cells']=sum(s['scanned_cells'] for s in new['sources'].values())

        valid_keys={canonical([scope_identity(old,scopes_for_query(q)),scope_identity(new,scopes_for_query(q)),scopes_for_query(q),'v1']) for q in queries}
        stale=set(cache)-valid_keys
        m['expired_entries']=len(stale)
        for key in stale: del cache[key]
        m['analysis_seconds']+=time.perf_counter()-t
        event_audit=[]
        for q in queries:
            t=time.perf_counter()
            scope=scopes_for_query(q)
            qchanged=definitions.get(q['id'])!=digest(q)
            affected=q['id'] in impacted or qchanged
            key=canonical([scope_identity(old,scope),scope_identity(new,scope),scope,'v1'])
            cached=cache.get(key) if group=='C' else None
            equivalent=not affected
            m['query_definition_invalidations']+=qchanged
            m['analysis_seconds']+=time.perf_counter()-t
            if affected and not qchanged and group in ('B','C'):
                t=time.perf_counter()
                if cached is not None:
                    m['evidence_hits']+=1; evidence=cached
                else:
                    scan=[0]; evidence=check_equivalence(old,new,scope,scan)
                    m['checks']+=1; m['check_scanned_cells']+=scan[0]
                    if check_delay_ms: time.sleep(check_delay_ms/1000)
                    if group=='C': cache[key]=evidence
                equivalent=evidence['equivalent']
                event_audit.append({'query_id':q['id'],'key_sha256':digest(key),'scope':scope,
                                    'old_binding':scope_identity(old,scope),'new_binding':scope_identity(new,scope),
                                    'hit':cached is not None,**evidence})
                m['check_seconds']+=time.perf_counter()-t
            elif qchanged:
                equivalent=False
            if equivalent:
                result=answers[q['id']]; m['reused']+=1
                m['wrong_reuse']+=result!=reference[q['id']]
            else:
                t=time.perf_counter(); scan=[0]
                result=execute(new,q,scan)
                if execution_delay_ms: time.sleep(execution_delay_ms/1000)
                m['reexecutions']+=1; m['execution_scanned_cells']+=scan[0]
                m['execution_seconds']+=time.perf_counter()-t
            answers[q['id']]=result; definitions[q['id']]=digest(q)
        m['evidence_entries']=len(cache)
        m['total_seconds']=time.perf_counter()-start

        for q in queries:
            actual,expected=answers[q['id']],reference[q['id']]
            m['answer_mismatches']+=actual['answer']!=expected['answer']
            m['error_mismatches']+=actual['errors']!=expected['errors']
        if m['wrong_reuse'] or m['answer_mismatches'] or m['error_mismatches']:
            raise AssertionError('Maintenance correctness failed: '+canonical(m))
        audits.append({'event':event['name'],'changed_fields':changed,'graph_impacted':sorted(impacted),
                       'ordinary_impacted':sorted(ordinary),'evidence':event_audit,
                       'answers':{q['id']:answers[q['id']] for q in queries}})
        records.append(m); old=new; old_graph=graph
    return records,audits
