from pathlib import Path
from collections import defaultdict,Counter
from dataclasses import replace
from decimal import Decimal
import sys,json,hashlib,random,time,csv,platform,statistics,argparse
ROOT=Path(__file__).resolve().parent
PRIOR=ROOT.parent
BASE=PRIOR
sys.path.insert(0,str(PRIOR)); sys.path.insert(0,str(BASE))
import rdflib
from rdflib import Graph,URIRef,Literal
import test_dependency_boundaries as diag
import benchmark_ted_contract as e
from dcm_scheduler import Premise,State,Scheduler
from benchmark_version_cache import VersionCache,independent_states
METHODS=('FULL','QUERY_CACHE_FULL','VERSION_CACHE','DCM')
QUERIES=dict(diag.QUERIES)
QUERIES['amount_filter']=diag.PREFIX+'SELECT ?node ?amount WHERE { ?node epo:hasAmountValue ?amount . FILTER(?amount >= 100) }'
QUERIES['currency_count']=diag.PREFIX+'SELECT ?currency (COUNT(?node) AS ?count) WHERE { ?node epo:hasAmountValue ?amount; epo:hasCurrency ?currency . } GROUP BY ?currency'
DEPS={q:(e.EPO.hasAmountValue,e.EPO.hasCurrency) for q in QUERIES}
DEPS['amount_filter']=(e.EPO.hasAmountValue,)
DEPS['linked_objects']+=(e.EPO.hasAwardedValue,)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def csvwrite(p,rs):
    with p.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
def execute(q,g):
    if q in diag.QUERIES:return diag.execute_query(q,g)
    rows=list(g.query(QUERIES[q]))
    if q=='amount_filter':return sorted((r.node.n3(),e.number(r.amount)) for r in rows)
    return sorted((r.currency.n3(),int(r['count'])) for r in rows)
def oracle(q,g):
    if q in diag.QUERIES:return diag.query_oracle(q,g)
    if q=='amount_filter':return sorted((s.n3(),str(Decimal(str(a)).normalize())) for s,a in g.subject_objects(e.EPO.hasAmountValue) if Decimal(str(a))>=100)
    count=Counter()
    for n,a in g.subject_objects(e.EPO.hasAmountValue):
        for c in g.objects(n,e.EPO.hasCurrency):count[c.n3()]+=1
    return sorted(count.items())
def projection(g,preds):
    return e.signature(sorted((s.n3(),p.n3(),o.n3()) for p in preds for s,_,o in g.triples((None,p,None))))
def linkage(s):
    nodes=set(s['graph'].subjects(e.EPO.hasAmountValue,None))
    return State.VALID if nodes and all(any(s['graph'].subjects(e.EPO.hasAwardedValue,n)) for n in nodes) else State.UNKNOWN
def authorization(s):
    v=s.get('authorized'); return State.UNKNOWN if v is None else State.VALID if v is True else State.INVALID
def period(s):
    v=s.get('period');return State.UNKNOWN if v is None else State.VALID if v[0]<=v[1] else State.INVALID
def cap(s):
    v=s.get('cap');nodes=list(s['graph'].objects(None,e.EPO.hasAmountValue))
    return State.UNKNOWN if v is None or not nodes else State.VALID if all(Decimal(str(a))<=Decimal(str(v)) for a in nodes) else State.INVALID
def expected_states(s,extended=True):
    states=independent_states(s)
    if s.get('mapping_version') is None:states['mapping']='UNKNOWN'
    if extended:
        nodes=set(s['graph'].subjects(e.EPO.hasAmountValue,None))
        linked=set(s['graph'].objects(None,e.EPO.hasAwardedValue))
        states['link']='VALID' if nodes and nodes<=linked else 'UNKNOWN'
        states['authorization']='UNKNOWN' if s.get('authorized') is None else 'VALID' if s['authorized']==True else 'INVALID'
        p=s.get('period');states['period']='UNKNOWN' if p is None else 'INVALID' if p[0]>p[1] else 'VALID'
        ceiling=s.get('cap'); vals=[Decimal(str(a)) for a in s['graph'].objects(None,e.EPO.hasAmountValue)]
        states['cap']='UNKNOWN' if ceiling is None or not vals else 'INVALID' if max(vals)>Decimal(str(ceiling)) else 'VALID'
    return states
def specs(extended):
    ps=[Premise('amount','v1',('amount',),e.amounts,True),Premise('currency','v1',('amount','currency'),e.currencies,True),Premise('scope','v1',('scope',),e.scope,True),Premise('mapping','v1',('mapping',),e.mapping_definition,True)]
    if extended:ps += [Premise('link','v1',('amount','link'),linkage,True),Premise('authorization','v1',('authorized',),authorization,True),Premise('period','v1',('period',),period,True),Premise('cap','v1',('amount','cap'),cap,True)]
    return ps
class Runner:
    def __init__(self,method,extended=True):
        self.method=method;self.ps=specs(extended);self.cache={}
        self.scheduler=VersionCache() if method=='VERSION_CACHE' else Scheduler(method=='DCM')
    def step(self,s,qs):
        start=time.perf_counter_ns()
        g=s['graph'];versions={k:projection(g,(p,)) for k,p in [('amount',e.EPO.hasAmountValue),('currency',e.EPO.hasCurrency),('link',e.EPO.hasAwardedValue)]}
        versions.update({k:e.signature(s.get(k)) for k in ('scope','authorized','period','cap')});versions['scope']=e.signature(s['declared_scope'])

        versions['mapping']=e.signature([s['mapping_version'],s['mapping_digest']]) if s.get('mapping_version') else None
        t1=time.perf_counter_ns(); answers={}; executed={}
        for q in qs:
            binding=(QUERIES[q],tuple(str(p) for p in DEPS[q]),projection(g,DEPS[q]),s.get('mapping_version'))
            cached=self.cache.get(q);complete=s.get('query_dependencies_complete',True)


            run=self.method=='FULL' or not complete or not s.get('mapping_version') or cached is None or cached[0]!=binding
            if run: answer=execute(q,g);self.cache[q]=(binding,answer)
            else:answer=cached[1]
            answers[q]=answer;executed[q]=run
        t2=time.perf_counter_ns(); result=self.scheduler.evaluate(self.ps,s,versions);end=time.perf_counter_ns()
        return dict(**result,answers=answers,executed=executed,query_executions=sum(executed.values()),signature_ms=(t1-start)/1e6,query_ms=(t2-t1)/1e6,validation_ms=(end-t2)/1e6,total_ms=(end-start)/1e6)
def mdigest(g):return projection(g,set(g.predicates()))
def load_inputs():
    import os
    adapter=Path(os.environ.get('DCM_TED_ADAPTER', BASE/'results/ted_amount_adapter_v2'));audit=json.loads((adapter/'summary.json').read_text(encoding='utf-8'));manifest=json.loads((adapter/'manifest.json').read_text(encoding='utf-8'))
    assert audit['audit_passed'];seen=set();records=[]
    for r in audit['records']:
        h=manifest['source_hashes'][r['source']]
        if h in seen:continue
        seen.add(h);p=adapter/'notices'/hashlib.sha256(r['source'].encode()).hexdigest()[:16]/'output.ttl';assert sha(p)==r['rdf_sha256']
        records.append((r,Graph().parse(p)))
    assert len(records)==124
    mapping=Graph().parse(adapter/'adapted_mapping.ttl');return records,mapping
def snapshot(g,m):return dict(graph=g,mapping=m,mapping_version='m1',mapping_digest=mdigest(m),declared_scope='awarded_amount',authorized=True,period=[0,10],cap='1E30')
def paired(out,records,mapping,seeds):


    totals=defaultdict(Counter);checks=Counter();events=['data']*6+['business']*3+['unrelated']*3
    with (out/'paired_events.jsonl').open('w',encoding='utf-8') as f:
        for seed in seeds:
            plan=list(events);random.Random(seed).shuffle(plan)
            for si,(rec,original) in enumerate(records):
                cases={fraction:snapshot(diag.clone(original),mapping) for fraction in (.1,1.)}
                runners={(fraction,m):Runner(m) for fraction in cases for m in METHODS}
                business=random.Random(seed*10000+si);targets=random.Random(seed*20000+si)
                for ei,event in enumerate(['initial']+plan):
                    nodes=sorted(set(original.subjects(e.EPO.hasAmountValue,None)),key=str);targets.shuffle(nodes)
                    scope=business.choice(['awarded_amount','estimated_amount',None]);auth=business.choice([True,False,None])
                    expected={}; affected={}
                    for fraction,s in cases.items():
                        chosen=nodes[:max(1,round(len(nodes)*fraction))] if event=='data' else []
                        affected[fraction]=len(chosen)
                        for n in chosen:s['graph'].set((n,e.EPO.hasAmountValue,Literal(Decimal(str(next(s['graph'].objects(n,e.EPO.hasAmountValue))))+1,datatype=e.DECIMAL)))
                        if event=='business':s['declared_scope']=scope;s['authorized']=auth
                        if event=='unrelated':s['graph'].set((URIRef('urn:annotation'),URIRef('urn:comment'),Literal(ei)))
                        expected[fraction]=(expected_states(s),{q:oracle(q,s['graph']) for q in QUERIES})
                    assert cases[.1]['declared_scope']==cases[1.]['declared_scope'] and cases[.1]['authorized']==cases[1.]['authorized']
                    order=list(runners);offset=(seed+si+ei)%len(order);order=order[offset:]+order[:offset];actual={}
                    for pos,(fraction,method) in enumerate(order):
                        s=cases[fraction];r=runners[fraction,method].step(s,QUERIES);ex,qa=expected[fraction]
                        assert r['premise_states']==ex and r['answers']==qa,(rec['source'],ei,method)
                        actual[fraction,method]=r
                        row=dict(seed=seed,source=rec['source'],fraction=fraction,event_index=ei,event=event,method=method,position=pos,affected_nodes=affected[fraction],scope=s['declared_scope'],authorized=s['authorized'],expected_states=ex,**r)
                        f.write(json.dumps(row,ensure_ascii=False)+'\n');checks['rows']+=1
                        if ei:totals[seed,fraction,method].update({k:r[k] for k in ('total_ms','signature_ms','query_ms','validation_ms','premise_checks','evidence_hits','query_executions')})
                    for fraction in cases:
                        a,b=actual[fraction,'VERSION_CACHE'],actual[fraction,'DCM']
                        for k in ('premise_states','answers','executed','premise_checks','evidence_hits','deliver'):assert a[k]==b[k]
                if (si+1)%31==0:print('paired',seed,si+1,'/124',flush=True)
    csvwrite(out/'paired_totals.csv',[dict(seed=k[0],fraction=k[1],method=k[2],**v) for k,v in totals.items()]);return dict(**checks,queries=6,premises=8,seeds=seeds,sources=124,normal_mismatches=0)
def boundary(out,records,mapping):
    graph,smallmap=diag.fixture();real=next((r,g) for r,g in records if r['scope_records']>0)
    legacy=diag.suite_rows((real[1],mapping,real[0]['source']));save(out/'legacy_boundary_rerun.json',dict(summary=diag.summarize(legacy),records=legacy))
    rows=[]
    for label,g,m,scenarios in [('synthetic',graph,smallmap,diag.SCENARIOS),('TED',real[1],mapping,('unrelated_annotation','amount_change','currency_change','delete_all_amounts','delete_currency','scope_invalid','scope_missing','delete_target_link'))]:
        for q in diag.QUERIES:
            for scenario in scenarios:
                after,mv,ms=diag.configure_event(scenario,g,m);before=snapshot(diag.clone(g),m)
                if scenario=='scope_recovery':before['declared_scope']=None
                after.update(mapping_version=mv,mapping_digest=mdigest(after['mapping']))
                if scenario=='query_dependencies_marked_incomplete':after['query_dependencies_complete']=False
                runners={method:Runner(method,False) for method in METHODS}
                if q=='linked_objects':
                    for r in runners.values():r.ps.append(Premise('link','v1',('amount','link'),linkage,True))
                for r in runners.values():
                    if scenario in ('business_dependencies_marked_incomplete','hidden_business_dependency_falsely_marked_complete'):r.ps=[replace(p,dependencies=('amount',),dependencies_complete=scenario.startswith('hidden')) if p.key=='scope' else p for p in r.ps]
                for ei,s in enumerate((before,after)):
                    for method,r in runners.items():
                        if ei and scenario=='evidence_rule_revision':r.ps=[replace(p,revision='scope-v2') if p.key=='scope' else p for p in r.ps]
                        actual=r.step(s,[q]);expected=expected_states(s,False)
                        if q=='linked_objects':
                            nodes=set(s['graph'].subjects(e.EPO.hasAmountValue,None));links=set(s['graph'].objects(None,e.EPO.hasAwardedValue))
                            expected['link']='VALID' if nodes and nodes<=links else 'UNKNOWN'
                        wrong=actual['premise_states']!=expected or actual['answers'][q]!=oracle(q,s['graph'])
                        force=ei==0 or method=='FULL' or (ei and (scenario in ('mapping_proven_equivalent','mapping_equivalence_unknown','mapping_contract_invalid','mapping_version_unknown','query_dependencies_marked_incomplete')))
                        if force:assert actual['executed'][q]
                        expected_fault=ei==1 and scenario.startswith('hidden') and method in ('VERSION_CACHE','DCM')
                        assert wrong==expected_fault,(q,scenario,method,expected,actual)
                        rows.append(dict(fixture=label,query=q,scenario=scenario,event_index=ei,method=method,expected_states=expected,safety_passed=not wrong,assumption_fault=expected_fault,**actual))
    save(out/'revised_boundary.json',rows)
    return dict(legacy=diag.summarize(legacy),revised_rows=len(rows),revised_failures=sum(not r['safety_passed'] for r in rows),remaining_failure_type='Falsely declared complete hidden scope dependency; no automatic detection',policy='Conservative recompute on ALL mapping version changes, including annotation-only; no arbitrary equivalence certificate claimed')
def synthetic(out,seeds):

    totals=[]
    for n in (32,128,512):
        for seed in seeds:
            g,m=diag.fixture();g=Graph()
            for i in range(n):
                node=URIRef(f'urn:synthetic:{i}');g.add((node,e.EPO.hasAmountValue,Literal(i+1,datatype=e.DECIMAL)));g.add((node,e.EPO.hasCurrency,URIRef(e.CUR+('EUR','USD','GBP')[i%3])));g.add((URIRef(f'urn:outcome:{i//4}'),e.EPO.hasAwardedValue,node))
            for fraction in (.01,.1,1.):
                s=snapshot(diag.clone(g),m);runners={x:Runner(x) for x in METHODS};acc={x:Counter() for x in METHODS};rng=random.Random(seed)
                for ei in range(17):
                    changed=rng.sample(list(range(n)),max(1,round(n*fraction))) if ei and ei%4 else []
                    for i in changed:
                        node=URIRef(f'urn:synthetic:{i}');s['graph'].set((node,e.EPO.hasAmountValue,Literal(Decimal(str(next(s['graph'].objects(node,e.EPO.hasAmountValue))))+1,datatype=e.DECIMAL)))
                    if ei%4==0 and ei:s['authorized']=not s['authorized']
                    expected=expected_states(s);answer={q:oracle(q,s['graph']) for q in QUERIES}
                    order=list(METHODS);offset=(ei+seed)%4;order=order[offset:]+order[:offset]
                    for method in order:
                        r=runners[method].step(s,QUERIES);assert r['answers']==answer and r['premise_states']==expected
                        if ei:acc[method].update({k:r[k] for k in ('total_ms','signature_ms','query_ms','validation_ms','premise_checks','query_executions')})
                for method in METHODS:totals.append(dict(objects=n,seed=seed,fraction=fraction,method=method,**acc[method]))
            print('RDF scale',n,seed,flush=True)
    csvwrite(out/'rdf_scale.csv',totals);return dict(configurations=9,seeds=len(seeds),rows=len(totals),normal_mismatches=0,synthetic=True)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);ap.add_argument('--mode',choices=['all','boundary','paired','scale'],default='all');ap.add_argument('--seeds',default='11,29,47');a=ap.parse_args();a.out.mkdir(exist_ok=False,parents=True)
    seeds=list(map(int,a.seeds.split(',')));records,mapping=load_inputs()
    manifest=dict(python=sys.version,rdflib=rdflib.__version__,platform=platform.platform(),code_sha256=sha(Path(__file__)),seeds=seeds,queries=QUERIES,predicate_dependencies={q:list(map(str,p)) for q,p in DEPS.items()},sources={r['source']:r['rdf_sha256'] for r,g in records},business_metadata='scope, authorization, period and ceiling are application test facts; not extracted real TED labels',timing='post-RDF maintenance, excludes parse, event generation, oracle and output; no artificial delay; method order rotated',mapping_limit='current RDF snapshots only, no RML rematerialization',started=time.time());save(a.out/'manifest.json',manifest)
    results={}
    if a.mode in ('all','boundary'):results['boundary']=boundary(a.out,records,mapping);print('boundary complete',flush=True)
    for q in QUERIES:execute(q,records[0][1])
    if a.mode in ('all','paired'):results['paired']=paired(a.out,records,mapping,seeds)
    if a.mode in ('all','scale'):results['scale']=synthetic(a.out,seeds)
    save(a.out/'summary.json',results);manifest.update(completed=time.time(),completed_successfully=True);save(a.out/'manifest.json',manifest)
    print(json.dumps(results,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
