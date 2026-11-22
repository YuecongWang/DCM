from pathlib import Path
import csv, hashlib, json, random, statistics, sys, time
ROOT=Path(__file__).resolve().parent
EXP=ROOT.parent/'expansion_v2'
sys.path.insert(0,str(EXP))
import benchmark_ted_paired as x
from cache_encoding_control import TupleCache

SIZES=(8,64,512,2048,8192,16384,32768)
SEEDS=(11,29,47)
REPEATS=5
EVENTS=128

class JsonCache:

    def __init__(self): self.entries={}
    def evaluate(self,ps,s,versions):
        active={p.key for p in ps}
        self.entries={k:v for k,v in self.entries.items() if k in active}
        states={}; checks=hits=0
        for p in ps:
            deps=tuple(sorted(set(p.dependencies)))
            bindings={d:versions.get(d) for d in deps}
            known=all(isinstance(v,str) and v for v in bindings.values())
            key=json.dumps([p.revision,p.dependencies_complete,sorted(bindings.items())])
            old=self.entries.get(p.key)
            if p.dependencies_complete and p.revision and known and old and old[0]==key and old[1]!='UNKNOWN':
                state=old[1];hits+=1
            else:
                state=p.evaluator(s).value if known else 'UNKNOWN'
                checks+=int(known);self.entries[p.key]=(key,state)
            states[p.key]=state
        status='INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'
        return dict(premise_states=states,business_state=status,deliver=status=='VALID',premise_checks=checks,evidence_hits=hits)

def run_one(n,seed,repeat):
    nd=max(4,n//4);rng=random.Random(seed*100000+repeat)
    state={str(i):1 for i in range(nd)};versions={d:'0' for d in state}
    def evaluator_for(deps):
        def evaluate(snapshot):
            vals=bytes(snapshot[d] for d in deps)
            for _ in range(8): hashlib.sha256(vals).digest()
            return x.State.VALID if all(vals) else x.State.INVALID
        return evaluate
    premises=[]
    for i in range(n):
        deps=tuple(str((i+j)%nd) for j in range(4))
        premises.append(x.Premise(str(i),'v1',deps,evaluator_for(deps),True))
    runners={'JSON_CACHE':JsonCache(),'TUPLE_CACHE':TupleCache(),'DCM':x.Scheduler(True)}
    totals={m:0.0 for m in runners};checks={m:0 for m in runners};hits={m:0 for m in runners}
    for ei in range(EVENTS+1):
        if ei and ei%5:
            for d in rng.sample(sorted(state),max(1,round(nd*.05))):
                state[d]=rng.choice([0,1]);versions[d]=str(ei)
        expected={p.key:('VALID' if all(state[d] for d in p.dependencies) else 'INVALID') for p in premises}
        order=list(runners);offset=(ei+repeat+seed)%3;order=order[offset:]+order[:offset]
        outputs={}
        for method in order:
            start=time.perf_counter_ns();result=runners[method].evaluate(premises,state,versions);ms=(time.perf_counter_ns()-start)/1e6
            assert result['premise_states']==expected,(n,seed,repeat,ei,method)
            outputs[method]=result;totals[method]+=ms;checks[method]+=result['premise_checks'];hits[method]+=result['evidence_hits']
        assert outputs['JSON_CACHE']==outputs['TUPLE_CACHE']==outputs['DCM']
    return [{'premises':n,'seed':seed,'repeat':repeat,'method':m,'total_ms':totals[m],'checks':checks[m],'hits':hits[m]} for m in runners]

OUT=ROOT/'results';OUT.mkdir(exist_ok=True)
runs=[]
for n in SIZES:
    reps=1 if n==32768 else REPEATS
    for seed in SEEDS:
        for repeat in range(reps):
            runs.extend(run_one(n,seed,repeat))
        print(f'completed premises={n} seed={seed} repeats={reps}',flush=True)

def write_csv(path,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
write_csv(OUT/'runs.csv',runs)
paired=[]
by={}
for r in runs:by.setdefault((r['premises'],r['seed'],r['repeat']),{})[r['method']]=r
for (n,seed,repeat),g in by.items():
    row={'premises':n,'seed':seed,'repeat':repeat}
    for m in ('JSON_CACHE','TUPLE_CACHE','DCM'):
        row[m.lower()+'_ms']=g[m]['total_ms'];row[m.lower()+'_checks']=g[m]['checks'];row[m.lower()+'_hits']=g[m]['hits']
    row['dcm_vs_tuple_saving_pct']=(g['TUPLE_CACHE']['total_ms']-g['DCM']['total_ms'])/g['TUPLE_CACHE']['total_ms']*100
    paired.append(row)
write_csv(OUT/'paired_runs.csv',paired)
summary=[]
for n in SIZES:
    group=[r for r in runs if r['premises']==n]
    pgroup=[r for r in paired if r['premises']==n]
    record={'premises':n,'paired_runs':len(pgroup)}
    for m in ('JSON_CACHE','TUPLE_CACHE','DCM'):
        record[m.lower()+'_ms_median']=statistics.median(float(r['total_ms']) for r in group if r['method']==m)
        record[m.lower()+'_checks_median']=statistics.median(int(r['checks']) for r in group if r['method']==m)
        record[m.lower()+'_hits_median']=statistics.median(int(r['hits']) for r in group if r['method']==m)
    record['dcm_vs_tuple_saving_pct_median']=statistics.median(float(r['dcm_vs_tuple_saving_pct']) for r in pgroup)
    record['dcm_vs_tuple_saving_pct_min']=min(float(r['dcm_vs_tuple_saving_pct']) for r in pgroup)
    record['dcm_vs_tuple_saving_pct_max']=max(float(r['dcm_vs_tuple_saving_pct']) for r in pgroup)
    record['outputs_identical']=True;summary.append(record)
write_csv(OUT/'summary.csv',summary)
manifest={'sizes':SIZES,'seeds':SEEDS,'repeats_per_seed':{n:(1 if n==32768 else REPEATS) for n in SIZES},'events_per_run':EVENTS+1,'dependency_count_per_premise':4,'changed_dependency_fraction':0.05,'update_every_nth_event':5,'scope':'scheduler-only CPU microbenchmark; excludes SPARQL, RDF projection/hash, query cache and disk persistence','methods':'JSON_CACHE stores json.dumps signatures in dict; TUPLE_CACHE stores tuple signatures in dict; DCM uses Scheduler/Evidence; identical dependencies, versions, evaluator and hit rules','timing':'sum of per-event evaluate wall-clock; three methods order rotated; matched seed/repeat/event trajectories','assertions':'all three premise-state dictionaries, aggregate status, deliveries, check and hit counts are identical on every event','source_tuple_dcm_baseline':'expansion_v2/cache_encoding_scale_v2.py','script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print('SUMMARY')
for r in summary:print(r)
