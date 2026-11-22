from pathlib import Path
from collections import defaultdict, Counter
from decimal import Decimal
from dataclasses import replace
import argparse, csv, hashlib, json, os, platform, random, statistics, sys, time
import rdflib
from rdflib import Graph, Literal, URIRef
from rdflib.compare import to_isomorphic
import benchmark_ted_contract as old
from benchmark_ted_three_way import footprint_and_versions, premises
from dcm_scheduler import Scheduler, State, Premise

ROOT=Path(__file__).resolve().parent
METHODS=('FULL','QUERY_CACHE_FULL','VERSION_CACHE','DCM')
def dump(p,x): p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def csvwrite(p,rows):
    with p.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
def aggregate(states):
    return 'INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'

class VersionCache:



    def __init__(self): self.entries={}
    def evaluate(self,specs,snapshot,versions):
        states={}; checks=hits=0
        active={p.key for p in specs}
        self.entries={k:v for k,v in self.entries.items() if k in active}
        for p in specs:
            bindings={d:versions.get(d) for d in set(p.dependencies)}
            known=all(isinstance(v,str) and v for v in bindings.values())
            key=json.dumps([p.revision,p.dependencies_complete,sorted(bindings.items())])
            entry=self.entries.get(p.key)
            if known and p.revision and p.dependencies_complete and entry and entry[0]==key and entry[1]!='UNKNOWN':
                state=entry[1]; hits+=1
            else:
                state=p.evaluator(snapshot).value if known else 'UNKNOWN'
                checks+=int(known)
                self.entries[p.key]=(key,state)
            states[p.key]=state
        state=aggregate(states)
        return dict(premise_states=states,business_state=state,deliver=state=='VALID',premise_checks=checks,evidence_hits=hits)

def independent_states(s):



    g=s['graph']; nodes=set(g.subjects(old.EPO.hasAmountValue,None))
    a=c='VALID' if nodes else 'UNKNOWN'
    for n in nodes:
        av=list(g.objects(n,old.EPO.hasAmountValue)); cv=list(g.objects(n,old.EPO.hasCurrency))
        try:
            if len(av)!=1 or not isinstance(av[0],Literal) or av[0].datatype!=old.DECIMAL or not Decimal(str(av[0])).is_finite(): a='INVALID'
        except Exception: a='INVALID'
        if len(cv)!=1: c='INVALID'
        else:
            code=str(cv[0])[len(old.CUR):]
            if not isinstance(cv[0],URIRef) or not str(cv[0]).startswith(old.CUR) or len(code)!=3 or any(x not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' for x in code): c='INVALID'
    mg=s['mapping']; m='VALID'
    for name in ('LotAwardOutcomeAwardedMonetaryValue','LotAwardOutcomeAwardedMonetaryValueModification'):
        poms=set(mg.objects(old.TM[name],old.RR.predicateObjectMap))
        am=[o for p in poms if (p,old.RR.predicate,old.EPO.hasAmountValue) in mg for o in mg.objects(p,old.RR.objectMap)]
        if not am or any((o,old.RR.datatype,old.DECIMAL) not in mg for o in am) or not any((p,old.RR.predicate,old.EPO.hasCurrency) in mg for p in poms): m='INVALID'
    v=s['declared_scope']
    return dict(amount=a,currency=c,scope='UNKNOWN' if v is None else 'VALID' if v=='awarded_amount' else 'INVALID',mapping=m)

class Runner:
    def __init__(self,method):
        self.method=method; self.cached=None
        self.scheduler=VersionCache() if method=='VERSION_CACHE' else Scheduler(method=='DCM')
    def step(self,s,mh,specs=None,versions_override=None):
        specs=specs or premises()
        t=time.perf_counter_ns(); start=t
        fp,versions=footprint_and_versions(s,mh)
        if versions_override: versions.update(versions_override)
        h=time.perf_counter_ns(); signature_ms=(h-t)/1e6
        execute=self.method=='FULL' or self.cached is None or self.cached[0]!=fp
        if execute: answer=old.query_answer(s['graph']); self.cached=(fp,answer)
        else: answer=self.cached[1]
        t=time.perf_counter_ns(); query_ms=(t-h)/1e6
        result=self.scheduler.evaluate(specs,s,versions)
        delivered=result['deliver']; stop=time.perf_counter_ns()
        return dict(**result,answer=answer,query_executed=int(execute),query_reused=int(not execute),
                    signature_ms=signature_ms,query_ms=query_ms,validation_ms=(stop-t)/1e6,total_ms=(stop-start)/1e6,
                    delivered=delivered,versions=versions)

CONFIGS=[('irrelevant80',.8,.1,.1),('balanced',.4,.3,.1),('business70',.1,.7,.1),('data70_narrow',.1,.2,.1),('data70_wide',.1,.2,1.0)]
SEEDS=[11,29,47]
def plan(seed,u,b,length):

    kinds=['unrelated']*round(length*u)+['business']*round(length*b)
    kinds+=['data']*(length-len(kinds)); random.Random(seed).shuffle(kinds)
    return ['initial']+kinds

def ted(out,adapter,length):
    audit=json.loads((adapter/'summary.json').read_text(encoding='utf-8'))
    assert audit['audit_passed'] and len(audit['records'])==128
    mapping=Graph().parse(adapter/'adapted_mapping.ttl'); mh=sha(adapter/'adapted_mapping.ttl')
    am=json.loads((adapter/'manifest.json').read_text(encoding='utf-8'))
    assert str(to_isomorphic(mapping).graph_digest())==am['derived_mapping_sha256']
    buckets=defaultdict(list); failures=[]; plans=[]; rowcount=0; equivalence=0

    old.query_answer(Graph()); Scheduler(True).evaluate(premises(),dict(graph=Graph(),mapping=mapping,declared_scope='awarded_amount'),dict.fromkeys(('amount','currency','scope','mapping'),'warm'))
    with (out/'ted_events.jsonl').open('w',encoding='utf-8') as f:
        for ci,(name,u,b,scope) in enumerate(CONFIGS):
            for seed in SEEDS:
                events=plan(seed,u,b,length); plans.append(dict(config=name,seed=seed,events=events,impact_fraction=scope))
                for si,record in enumerate(audit['records']):
                    source=record['source']; p=adapter/'notices'/hashlib.sha256(source.encode()).hexdigest()[:16]/'output.ttl'
                    assert sha(p)==record['rdf_sha256']
                    g=Graph().parse(p); s=dict(graph=g,mapping=mapping,declared_scope='awarded_amount')
                    runners={m:Runner(m) for m in METHODS}; rng=random.Random(seed*1000+si)
                    for ei,event in enumerate(events):
                        affected=0
                        if event=='unrelated': g.set((URIRef('urn:test:annotation'),URIRef('urn:test:comment'),Literal(ei)))
                        elif event=='business': s['declared_scope']=rng.choice(['awarded_amount','estimated_amount',None])
                        elif event=='data':
                            nodes=sorted(set(g.subjects(old.EPO.hasAmountValue,None)),key=str)
                            chosen=rng.sample(nodes,max(1,round(len(nodes)*scope))) if nodes else []
                            affected=len(chosen)
                            for n in chosen: g.set((n,old.EPO.hasAmountValue,Literal(Decimal(str(next(g.objects(n,old.EPO.hasAmountValue))))+1,datatype=old.DECIMAL)))

                        expected_answer=old.query_answer(g); arithmetic=old.independent_oracle(s)['answer']
                        assert expected_answer==arithmetic
                        expected=independent_states(s); results={}
                        offset=(si+seed+ei+ci)%4; order=METHODS[offset:]+METHODS[:offset]
                        for pos,m in enumerate(order):
                            r=runners[m].step(s,mh); results[m]=r
                            errors=[key for key,ok in [('answer',r['answer']==expected_answer),('states',r['premise_states']==expected),('delivery',r['delivered']==(aggregate(expected)=='VALID'))] if not ok]
                            row=dict(config=name,seed=seed,source=source,event_index=ei,event=event,method=m,position=pos,affected_amount_nodes=affected,
                                     scope=s['declared_scope'],expected_states=expected,expected_answer=expected_answer,errors=errors,**r)
                            f.write(json.dumps(row,ensure_ascii=False)+'\n'); rowcount+=1
                            if errors: failures.append(row)
                            buckets[(name,seed,m,ei==0)].append({k:r[k] for k in ('total_ms','signature_ms','query_ms','validation_ms','premise_checks','evidence_hits','query_executed','query_reused')})
                        for k in ('premise_states','delivered','premise_checks','evidence_hits','query_executed','answer'):
                            assert results['VERSION_CACHE'][k]==results['DCM'][k],(source,ei,k)
                        equivalence+=1
                print('TED',name,seed,'complete',flush=True)
    totals=[]
    for (name,seed,m,init),rs in buckets.items():
        totals.append(dict(config=name,seed=seed,method=m,phase='initialization' if init else 'maintenance',steps=len(rs),**{k:sum(r[k] for r in rs) for k in rs[0]}))
    csvwrite(out/'ted_totals.csv',totals); dump(out/'ted_plans.json',plans)
    expected_rows=len(CONFIGS)*len(SEEDS)*128*(length+1)*4
    assert rowcount==expected_rows
    dump(out/'ted_validation.json',dict(passed=not failures,rows=rowcount,expected_rows=expected_rows,cache_dcm_equivalent_steps=equivalence,failures=failures,input_rdf_sha256={r['source']:r['rdf_sha256'] for r in audit['records']}))
    if failures: raise AssertionError('TED oracle mismatches')

def boundaries(out,adapter):
    mapping=Graph().parse(adapter/'adapted_mapping.ttl'); mh=sha(adapter/'adapted_mapping.ttl')
    g=Graph(); n=URIRef('urn:amount:1'); g.add((n,old.EPO.hasAmountValue,Literal('10',datatype=old.DECIMAL))); g.add((n,old.EPO.hasCurrency,URIRef(old.CUR+'EUR')))
    base=dict(graph=g,mapping=mapping,declared_scope='awarded_amount')
    records=[]
    for case in ('rule_revision','dependency_set_change','multi_object_change','mapping_change','missing_version','incomplete_dependency','unknown_retry','lost_version_update','undeclared_dependency','checker_mutation'):
        s=dict(base); s['graph']=Graph(); s['mapping']=Graph()
        for t in g: s['graph'].add(t)
        for t in mapping: s['mapping'].add(t)
        runners={m:Runner(m) for m in ('VERSION_CACHE','DCM')}; specs=premises()
        for r in runners.values(): r.step(s,mh)
        overrides={}; expected=independent_states(s)
        if case=='rule_revision':
            specs=[replace(p,revision='scope-v2',evaluator=lambda _:State.INVALID) if p.key=='scope' else p for p in specs]; expected['scope']='INVALID'
        elif case=='dependency_set_change':
            s['extra']=False; overrides['extra']='v1'
            specs=[replace(p,dependencies=('scope','extra'),evaluator=lambda x:State.VALID if x['extra'] else State.INVALID) if p.key=='scope' else p for p in specs]; expected['scope']='INVALID'
        elif case=='multi_object_change':
            s['declared_scope']='estimated_amount'; s['graph'].set((n,old.EPO.hasCurrency,Literal('EUR'))); expected=independent_states(s)
        elif case=='mapping_change':
            for t in list(s['mapping'].triples((None,old.RR.datatype,old.DECIMAL))): s['mapping'].remove(t)
            mh='modified-mapping'; expected=independent_states(s)
        elif case=='missing_version': overrides['scope']=None; expected['scope']='UNKNOWN'
        elif case=='incomplete_dependency':
            s['declared_scope']='estimated_amount'; specs=[replace(p,dependencies_complete=False) if p.key=='scope' else p for p in specs]; expected=independent_states(s)
        elif case=='unknown_retry':
            s['declared_scope']=None; expected=independent_states(s)
            for r in runners.values(): r.step(s,mh)
        elif case=='lost_version_update':
            s['declared_scope']='estimated_amount'; overrides['scope']=old.signature('awarded_amount'); expected=independent_states(s)
        elif case=='undeclared_dependency':

            specs=[replace(p,dependencies=()) if p.key=='scope' else p for p in specs]
            for r in runners.values(): r.step(s,mh,specs)
            s['declared_scope']='estimated_amount'; expected=independent_states(s)
        elif case=='checker_mutation':
            s['declared_scope']='estimated_amount'; specs=[replace(p,revision='mutant',evaluator=lambda _:State.VALID) if p.key=='scope' else p for p in specs]; expected=independent_states(s)
        fault=case in ('lost_version_update','undeclared_dependency','checker_mutation')
        for m,r in runners.items():
            actual=r.step(s,mh,specs,overrides); mismatch=actual['premise_states']!=expected
            assert mismatch==fault,(case,m,actual,expected)
            records.append(dict(case=case,method=m,assumptions_violated=fault,expected_states=expected,actual=actual,oracle_detected_mismatch=mismatch,passed=True))
    dump(out/'boundaries.json',records)

def synthetic(out):





    configs=[]
    for q in (64,256,1024): configs.append((f'size{q}',q,q//2,.5,.05,128,0))
    for sharing in (0,.9): configs.append((f'sharing{sharing}',256,128,sharing,.05,128,0))
    for impact in (.01,1.): configs.append((f'impact{impact}',256,128,.5,impact,128,0))
    configs.extend([('long_history',256,128,.5,.05,512,512),('long_nohistory',256,128,.5,.05,512,0)])
    totals=[]; rows=0
    with (out/'scale_events.jsonl').open('w',encoding='utf-8') as f:
        for name,nq,nm,sharing,impact,length,retention in configs:
            nd=max(1,round(nm*(1-sharing))); pd={f'p{i}':tuple(f'b{4*(i//4%nd)+k}' for k in range(4)) for i in range(nm*4)}
            qd={f'q{i}':f'd{i%nm}' for i in range(nq)}
            reverse=defaultdict(set)
            for p,deps in pd.items():
                for d in deps: reverse[d].add(p)
            for q,d in qd.items(): reverse[d].add(q)
            for seed in SEEDS:
                rng=random.Random(seed); values={d:1 for d in reverse}; versions={d:'0' for d in values}

                def checker(deps):
                    def check(s):
                        vv=[s[d] for d in deps]
                        for _ in range(8): hashlib.sha256(bytes(vv)).digest()
                        return State.VALID if all(vv) else State.INVALID
                    return check
                specs=[Premise(p,'r1',deps,checker(deps),True) for p,deps in pd.items()]
                runners={m:VersionCache() if m=='VERSION_CACHE' else Scheduler(m=='DCM') for m in METHODS}
                caches={m:{} for m in METHODS}; histories={m:[] for m in METHODS}; accum={m:Counter() for m in METHODS}
                peak={m:0 for m in METHODS}; initms={m:0 for m in METHODS}
                for ei in range(length+1):
                    changed=rng.sample(sorted(values),max(1,round(len(values)*impact))) if ei and ei%5 else []
                    for d in changed: values[d]=rng.choice([0,1]); versions[d]=str(ei)
                    expected={p:'VALID' if all(values[d] for d in deps) else 'INVALID' for p,deps in pd.items()}
                    answers={q:sum(values[d] for _ in range(32)) for q,d in qd.items()}


                    offset=(ei+seed)%4; results={}
                    for m in METHODS[offset:]+METHODS[:offset]:
                        start=time.perf_counter_ns(); t=start
                        affected=set()
                        for d in changed: affected.update(reverse[d])
                        t1=time.perf_counter_ns(); sig=old.signature(sorted(versions.items())); t2=time.perf_counter_ns()
                        r=runners[m].evaluate(specs,values,versions); t3=time.perf_counter_ns()
                        executed=0; actual={}
                        for q,d in qd.items():
                            if m=='FULL' or q not in caches[m] or caches[m][q][0]!=versions[d]:
                                caches[m][q]=(versions[d],sum(values[d] for _ in range(32))); executed+=1
                            actual[q]=caches[m][q][1]
                        if retention:
                            histories[m].append((ei,dict(r['premise_states']),sig))
                            if len(histories[m])>retention: del histories[m][0]
                        stop=time.perf_counter_ns()
                        assert r['premise_states']==expected and actual==answers
                        metrics=dict(total_ms=(stop-start)/1e6,traversal_ms=(t1-t)/1e6,signature_ms=(t2-t1)/1e6,validation_ms=(t3-t2)/1e6,query_history_ms=(stop-t3)/1e6,premise_checks=r['premise_checks'],evidence_hits=r['evidence_hits'],query_executed=executed)

                        entries=runners[m].entries if m=='VERSION_CACHE' else runners[m].cache
                        storage=len(repr(entries).encode())+len(repr(histories[m]).encode())
                        peak[m]=max(peak[m],storage)
                        if ei: accum[m].update(metrics)
                        else: initms[m]=metrics['total_ms']
                        row=dict(config=name,seed=seed,event_index=ei,method=m,affected_objects=len(changed),reachable_nodes=len(affected),storage_serialized_proxy_bytes=storage,**metrics)
                        f.write(json.dumps(row)+'\n'); rows+=1; results[m]=r
                    assert results['VERSION_CACHE']==results['DCM']
                for m in METHODS:
                    totals.append(dict(config=name,seed=seed,method=m,queries=nq,mappings=nm,premises=nm*4,policy_groups=nd,sharing=sharing,impact=impact,events=length,history_retention=retention,initialization_ms=initms[m],peak_storage_proxy_bytes=peak[m],**accum[m]))
            print('SCALE',name,'complete',flush=True)
    csvwrite(out/'scale_totals.csv',totals); dump(out/'scale_validation.json',dict(passed=True,rows=rows,configs=len(configs),seeds=SEEDS))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out',type=Path,required=True); ap.add_argument('--adapter',type=Path,default=ROOT/'results'/'ted_amount_adapter_v2'); ap.add_argument('--mode',choices=['all','ted','scale','boundary'],default='all'); ap.add_argument('--events',type=int,default=12); a=ap.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    manifest=dict(command=sys.argv,python=sys.version,executable=sys.executable,rdflib=rdflib.__version__,platform=platform.platform(),processor=platform.processor(),cpu_count=os.cpu_count(),code_sha256={p.name:sha(p) for p in ROOT.glob('*.py')},methods={'FULL':'Every event full query plus all business premises','QUERY_CACHE_FULL':'Version/footprint query reuse plus all business premises','VERSION_CACHE':'Independent ordinary versioned cache; same checks and gate','DCM':'Existing Scheduler incremental=True; same query cache'},seeds=SEEDS,event_count=a.events,warmup='One discarded empty-graph SPARQL and Scheduler call; fresh method caches for every notice/seed',ordering='Rotated by seed/source/event; single-thread sequential',delays_ms=0,timing='perf_counter_ns; includes signatures, validation, query/cache, gate; excludes input parse/mutation/oracle/I-O/cache allocation and storage sampling',history='scale long_history retains optional audit states inside measured maintenance; not intrinsic to existing Scheduler',storage='UTF-8 repr byte proxy of actual evidence and optional history; not RSS or database bytes',scale_signature='Full supplied version vector hashed for each method; dependency index constructed before timing, per-event traversal timed',started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    dump(a.out/'manifest.json',manifest)
    try:
        if a.mode in ('all','boundary'): boundaries(a.out,a.adapter)
        if a.mode in ('all','ted'): ted(a.out,a.adapter,a.events)
        if a.mode in ('all','scale'): synthetic(a.out)
        manifest['passed']=True
    except Exception as e:
        manifest.update(passed=False,error=repr(e)); raise
    finally:
        manifest['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); dump(a.out/'manifest.json',manifest)
if __name__=='__main__': main()
