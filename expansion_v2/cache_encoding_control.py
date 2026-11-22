from pathlib import Path
import random,time,hashlib,csv,json
import benchmark_ted_paired as x
class TupleCache:
    def __init__(self):self.entries={}
    def evaluate(self,ps,s,versions):
        active={p.key for p in ps};self.entries={k:v for k,v in self.entries.items() if k in active}
        states={};checks=hits=0
        for p in ps:
            deps=tuple(sorted(set(p.dependencies)));known=all(isinstance(versions.get(d),str) and versions[d] for d in deps)
            key=(p.revision,p.dependencies_complete,tuple((d,versions.get(d)) for d in deps));old=self.entries.get(p.key)
            if p.dependencies_complete and p.revision and known and old and old[0]==key and old[1]!='UNKNOWN':state=old[1];hits+=1
            else:
                state=p.evaluator(s).value if known else 'UNKNOWN';checks+=int(known);self.entries[p.key]=(key,state)
            states[p.key]=state
        status='INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'
        return dict(premise_states=states,business_state=status,deliver=status=='VALID',premise_checks=checks,evidence_hits=hits)
def main():
    root=Path(__file__).resolve().parent;rows=[]
    p=x.Premise('p','r1',('d',),lambda s:x.State[s['state']],True)
    sanity=[x.VersionCache(),TupleCache(),x.Scheduler(True)]
    for value,version,checks in [('VALID','1',1),('VALID','1',0),('INVALID','2',1),('UNKNOWN','3',1),('UNKNOWN','3',1),('VALID','4',1),('VALID',None,0),('VALID','4',1)]:
        actual=[r.evaluate([p],{'state':value},{'d':version}) for r in sanity]
        assert actual[0]==actual[1]==actual[2] and actual[0]['premise_checks']==checks
    for n in (8,512,2048):
        nd=max(4,n//4)
        for seed in (11,29,47):
            rng=random.Random(seed);s={str(i):1 for i in range(nd)};v={d:'0' for d in s}
            def evaluator(ds):
                def check(s):
                    vals=bytes(s[d] for d in ds)
                    for _ in range(8):hashlib.sha256(vals).digest()
                    return x.State.VALID if all(vals) else x.State.INVALID
                return check
            ps=[x.Premise(str(i),'v1',tuple(str((i+j)%nd) for j in range(4)),evaluator(tuple(str((i+j)%nd) for j in range(4))),True) for i in range(n)]
            runners={'JSON_CACHE':x.VersionCache(),'TUPLE_CACHE':TupleCache(),'DCM':x.Scheduler(True)}
            for ei in range(129):
                if ei and ei%5:
                    for d in rng.sample(sorted(s),max(1,round(nd*.05))):s[d]=rng.choice([0,1]);v[d]=str(ei)
                expected={p.key:'VALID' if all(s[d] for d in p.dependencies) else 'INVALID' for p in ps}
                methods=list(runners);offset=(ei+seed)%3;methods=methods[offset:]+methods[:offset];results={}
                for method in methods:
                    start=time.perf_counter_ns();r=runners[method].evaluate(ps,s,v);ms=(time.perf_counter_ns()-start)/1e6
                    assert r['premise_states']==expected;results[method]=r
                    rows.append(dict(premises=n,seed=seed,event_index=ei,method=method,total_ms=ms,checks=r['premise_checks'],hits=r['evidence_hits']))
                assert results['JSON_CACHE']==results['TUPLE_CACHE']==results['DCM']
            print('encoding control',n,seed,flush=True)
    x.csvwrite(root/'cache_encoding_events.csv',rows)
    x.save(root/'cache_encoding_acceptance.json',dict(passed=True,rows=len(rows),scope='Scheduler-only CPU microbenchmark, no SPARQL, RDF hashes, snapshot generation or disk persistence; method order rotated',ordinary_tuple_implementation='Independent plain dictionary cache; no Scheduler/Evidence reuse',code_sha256=x.sha(Path(__file__))))
if __name__=='__main__':main()
