from pathlib import Path
import csv, hashlib, json, random, statistics, time
import benchmark_ted_paired as x
from cache_encoding_control import TupleCache

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'cache_encoding_scale_v2'
SIZES=(8,64,512,2048,8192,16384)
SEEDS=(11,29,47)
REPEATS=5
EVENTS=128

def run_one(n, seed, repeat):
    nd=max(4,n//4)
    rng=random.Random(seed*100000+repeat)
    state={str(i):1 for i in range(nd)}
    versions={d:'0' for d in state}
    def evaluator_for(deps):
        def evaluate(snapshot):
            vals=bytes(snapshot[d] for d in deps)
            for _ in range(8):
                hashlib.sha256(vals).digest()
            return x.State.VALID if all(vals) else x.State.INVALID
        return evaluate
    premises=[]
    for i in range(n):
        deps=tuple(str((i+j)%nd) for j in range(4))
        premises.append(x.Premise(str(i),'v1',deps,evaluator_for(deps),True))
    runners={'TUPLE_CACHE':TupleCache(),'DCM':x.Scheduler(True)}
    totals={m:0.0 for m in runners}; checks={m:0 for m in runners}; hits={m:0 for m in runners}
    per_event=[]
    for ei in range(EVENTS+1):
        if ei and ei%5:
            for d in rng.sample(sorted(state),max(1,round(nd*.05))):
                state[d]=rng.choice([0,1]); versions[d]=str(ei)
        expected={p.key:('VALID' if all(state[d] for d in p.dependencies) else 'INVALID') for p in premises}
        order=['TUPLE_CACHE','DCM'] if (ei+repeat+seed)%2==0 else ['DCM','TUPLE_CACHE']
        row={'premises':n,'seed':seed,'repeat':repeat,'event_index':ei}
        outputs={}
        for method in order:
            start=time.perf_counter_ns()
            result=runners[method].evaluate(premises,state,versions)
            elapsed=(time.perf_counter_ns()-start)/1e6
            assert result['premise_states']==expected, (n,seed,repeat,ei,method)
            outputs[method]=result
            totals[method]+=elapsed; checks[method]+=result['premise_checks']; hits[method]+=result['evidence_hits']
            row[method+'_ms']=elapsed
            row[method+'_checks']=result['premise_checks']
            row[method+'_hits']=result['evidence_hits']
        assert outputs['TUPLE_CACHE']==outputs['DCM']
        per_event.append(row)
    summary=[]
    for method in runners:
        summary.append({'premises':n,'seed':seed,'repeat':repeat,'method':method,'total_ms':totals[method],
                        'checks':checks[method],'hits':hits[method]})
    return per_event,summary

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    events=[]; summaries=[]
    for n in SIZES:
        for seed in SEEDS:
            for rep in range(REPEATS):
                er,sr=run_one(n,seed,rep); events.extend(er); summaries.extend(sr)
                print(f'completed premises={n}, seed={seed}, repeat={rep+1}/{REPEATS}',flush=True)
    for name,rows in [('events.csv',events),('runs.csv',summaries)]:
        with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

    grouped={}
    for r in summaries:grouped.setdefault((r['premises'],r['seed'],r['repeat']),{})[r['method']]=r
    pairs=[]
    for (n,seed,rep),g in grouped.items():
        tup=g['TUPLE_CACHE']['total_ms'];dcm=g['DCM']['total_ms']
        pairs.append({'premises':n,'seed':seed,'repeat':rep,'tuple_ms':tup,'dcm_ms':dcm,
                      'dcm_saving_pct':(tup-dcm)/tup*100,'dcm_overhead_pct':(dcm-tup)/tup*100,
                      'tuple_checks':g['TUPLE_CACHE']['checks'],'dcm_checks':g['DCM']['checks'],
                      'tuple_hits':g['TUPLE_CACHE']['hits'],'dcm_hits':g['DCM']['hits']})
    with (OUT/'paired_runs.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(pairs[0]));w.writeheader();w.writerows(pairs)
    summary=[]
    for n in SIZES:
        group=[r for r in pairs if r['premises']==n]
        saving=[r['dcm_saving_pct'] for r in group]
        overhead=[r['dcm_overhead_pct'] for r in group]
        summary.append({'premises':n,'paired_runs':len(group),
          'tuple_ms_median':statistics.median(r['tuple_ms'] for r in group),
          'dcm_ms_median':statistics.median(r['dcm_ms'] for r in group),
          'dcm_saving_pct_median':statistics.median(saving),
          'dcm_saving_pct_q1':statistics.quantiles(saving,n=4)[0],
          'dcm_saving_pct_q3':statistics.quantiles(saving,n=4)[2],
          'dcm_overhead_pct_median':statistics.median(overhead),
          'checks_equal':all(r['tuple_checks']==r['dcm_checks'] for r in group),
          'hits_equal':all(r['tuple_hits']==r['dcm_hits'] for r in group)})
    with (OUT/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
    manifest={'sizes':SIZES,'seeds':SEEDS,'repeats_per_seed':REPEATS,'events_per_run':EVENTS+1,
      'dependency_count_per_premise':4,'changed_dependency_fraction':0.05,'update_every_nth_event':5,
      'scope':'scheduler-only CPU microbenchmark; no SPARQL, RDF projection/hash, query cache or disk persistence',
      'timing':'sum of per-event evaluate() wall-clock measurements; method order alternates; 15 paired trajectories per size',
      'assertions':'premise state dictionaries and aggregate result counters are identical between tuple cache and DCM',
      'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print('SUMMARY')
    for r in summary:print(r)
if __name__=='__main__':main()
