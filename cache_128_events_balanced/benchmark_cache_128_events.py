from pathlib import Path
import csv, hashlib, json, random, statistics, sys, time
ROOT=Path(__file__).resolve().parent; EXP=ROOT.parent/'expansion_v2'; sys.path.insert(0,str(EXP))
import benchmark_ted_paired as x
from cache_encoding_control import TupleCache
SIZES=(8,64,512,2048,8192,16384,32768); SEEDS=(11,29,47); REPEATS_BY_SIZE={8:5,64:5,512:5,2048:5,8192:1,16384:1,32768:1}; EVENTS=128
class JsonCache:
    def __init__(self): self.entries={}
    def evaluate(self,ps,s,versions):
        active={p.key for p in ps};self.entries={k:v for k,v in self.entries.items() if k in active}
        states={};checks=hits=0
        for p in ps:
            deps=tuple(sorted(set(p.dependencies)));bindings={d:versions.get(d) for d in deps}
            known=all(isinstance(v,str) and v for v in bindings.values())
            key=json.dumps([p.revision,p.dependencies_complete,sorted(bindings.items())])
            old=self.entries.get(p.key)
            if p.dependencies_complete and p.revision and known and old and old[0]==key and old[1]!='UNKNOWN':state=old[1];hits+=1
            else:
                state=p.evaluator(s).value if known else 'UNKNOWN';checks+=int(known);self.entries[p.key]=(key,state)
            states[p.key]=state
        status='INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'
        return dict(premise_states=states,business_state=status,deliver=status=='VALID',premise_checks=checks,evidence_hits=hits)
def run_one(n,seed,repeat):
    nd=max(4,n//4);rng=random.Random(seed*100000+repeat);state={str(i):1 for i in range(nd)};versions={d:'0' for d in state}
    def evaluator_for(deps):
        def evaluate(snapshot):
            vals=bytes(snapshot[d] for d in deps)
            for _ in range(8):hashlib.sha256(vals).digest()
            return x.State.VALID if all(vals) else x.State.INVALID
        return evaluate
    ps=[]
    for i in range(n):
        deps=tuple(str((i+j)%nd) for j in range(4));ps.append(x.Premise(str(i),'v1',deps,evaluator_for(deps),True))
    runners={'JSON_CACHE':JsonCache(),'TUPLE_CACHE':TupleCache(),'DCM':x.Scheduler(True)}
    init_ms={m:0.0 for m in runners};follow_ms={m:0.0 for m in runners};checks={m:0 for m in runners};hits={m:0 for m in runners}
    expected={p.key:('VALID' if all(state[d] for d in p.dependencies) else 'INVALID') for p in ps}
    order=list(runners);offset=(repeat+seed)%3;order=order[offset:]+order[:offset];initial={}
    for method in order:
        start=time.perf_counter_ns();r=runners[method].evaluate(ps,state,versions);elapsed=(time.perf_counter_ns()-start)/1e6
        assert r['premise_states']==expected;initial[method]=r;init_ms[method]=elapsed;checks[method]+=r['premise_checks'];hits[method]+=r['evidence_hits']
    assert initial['JSON_CACHE']==initial['TUPLE_CACHE']==initial['DCM']
    for ei in range(1,EVENTS+1):
        if ei%5:
            for d in rng.sample(sorted(state),max(1,round(nd*.05))):state[d]=rng.choice([0,1]);versions[d]=str(ei)
        expected={p.key:('VALID' if all(state[d] for d in p.dependencies) else 'INVALID') for p in ps}
        order=list(runners);offset=(ei+repeat+seed)%3;order=order[offset:]+order[:offset];outputs={}
        for method in order:
            start=time.perf_counter_ns();r=runners[method].evaluate(ps,state,versions);elapsed=(time.perf_counter_ns()-start)/1e6
            assert r['premise_states']==expected,(n,seed,repeat,ei,method)
            outputs[method]=r;follow_ms[method]+=elapsed;checks[method]+=r['premise_checks'];hits[method]+=r['evidence_hits']
        assert outputs['JSON_CACHE']==outputs['TUPLE_CACHE']==outputs['DCM']
    return [{'premises':n,'seed':seed,'repeat':repeat,'method':m,'initial_ms':init_ms[m],'followup_128_ms':follow_ms[m],'checks_total_including_initial':checks[m],'hits_total_including_initial':hits[m]} for m in runners]
def write_csv(p,rows):
    with p.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
OUT=ROOT/'results';OUT.mkdir(exist_ok=True);runs=[]
for n in SIZES:
    reps=REPEATS_BY_SIZE[n]
    for seed in SEEDS:
        for rep in range(reps):runs.extend(run_one(n,seed,rep))
        print(f'completed premises={n} seed={seed} repeats={reps}',flush=True)
write_csv(OUT/'runs.csv',runs)
by={}
for r in runs:by.setdefault((r['premises'],r['seed'],r['repeat']),{})[r['method']]=r
paired=[]
for (n,seed,rep),g in by.items():
    row={'premises':n,'seed':seed,'repeat':rep}
    for m in ('JSON_CACHE','TUPLE_CACHE','DCM'):
        row[m.lower()+'_initial_ms']=g[m]['initial_ms'];row[m.lower()+'_followup_128_ms']=g[m]['followup_128_ms'];row[m.lower()+'_checks']=g[m]['checks_total_including_initial'];row[m.lower()+'_hits']=g[m]['hits_total_including_initial']
    row['dcm_vs_tuple_saving_pct']=(g['TUPLE_CACHE']['followup_128_ms']-g['DCM']['followup_128_ms'])/g['TUPLE_CACHE']['followup_128_ms']*100
    paired.append(row)
write_csv(OUT/'paired_runs.csv',paired)
summary=[]
for n in SIZES:
    group=[r for r in runs if r['premises']==n];pg=[r for r in paired if r['premises']==n];d={'premises':n,'paired_runs':len(pg)}
    for m in ('JSON_CACHE','TUPLE_CACHE','DCM'):
        d[m.lower()+'_followup_128_ms_median']=statistics.median(float(r['followup_128_ms']) for r in group if r['method']==m)
        d[m.lower()+'_initial_ms_median']=statistics.median(float(r['initial_ms']) for r in group if r['method']==m)
        d[m.lower()+'_checks_median']=statistics.median(int(r['checks_total_including_initial']) for r in group if r['method']==m)
        d[m.lower()+'_hits_median']=statistics.median(int(r['hits_total_including_initial']) for r in group if r['method']==m)
    sv=[float(r['dcm_vs_tuple_saving_pct']) for r in pg]
    d['dcm_vs_tuple_saving_pct_median']=statistics.median(sv);d['dcm_vs_tuple_saving_pct_min']=min(sv);d['dcm_vs_tuple_saving_pct_max']=max(sv);d['outputs_identical']=True;summary.append(d)
write_csv(OUT/'summary.csv',summary)
manifest={'sizes':SIZES,'seeds':SEEDS,'repeats_per_seed':REPEATS_BY_SIZE,'followup_events':128,'initialization':'one initial cache-population evaluation, timed separately and excluded from followup_128_ms','dependency_count_per_premise':4,'changed_dependency_fraction':0.05,'update_every_nth_event':5,'scope':'scheduler-only CPU microbenchmark; excludes SPARQL, RDF projection/hash, query cache and disk persistence','methods':'JSON_CACHE uses json.dumps signature in dict; TUPLE_CACHE stores tuple signature in dict; DCM uses Scheduler/Evidence; identical rules, versions, evaluator, hit policy','timing':'each of 128 post-initial events timed per evaluate call; method order rotated per event; medians across paired seed/repeat trajectories','assertions':'all three premise states, aggregate state, delivery, check and hit counts agree at initialization and every event','script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print('SUMMARY')
for r in summary:print(r)
