from pathlib import Path
import csv,gzip,json,random,statistics,time,hashlib,sys
from dataclasses import dataclass
from enum import Enum
from collections import Counter,defaultdict
ROOT=Path(__file__).resolve().parent;OUT=ROOT/'case_transition_scale_results';SIZES=(10_000,100_000,1_000_000);SEEDS=(11,29,47)
class State(str,Enum): VALID='VALID';INVALID='INVALID';UNKNOWN='UNKNOWN'
@dataclass(frozen=True)
class Premise:
 key:str;revision:str;dependencies:tuple;evaluator:object;dependencies_complete:bool=True
@dataclass(frozen=True)
class Evidence: signature:tuple;state:State
class Scheduler:
 def __init__(self,incremental):self.incremental=incremental;self.cache={}
 def evaluate(self,ps,s,versions):
  self.cache={k:v for k,v in self.cache.items() if k in {p.key for p in ps}};states={};checks=hits=0
  for p in ps:
   deps=tuple(sorted(set(p.dependencies)));known=all(isinstance(versions.get(d),str) and versions[d] for d in deps)
   sig=(p.revision,p.dependencies_complete,tuple((d,versions.get(d)) for d in deps));old=self.cache.get(p.key)
   if self.incremental and p.dependencies_complete and p.revision and known and old and old.signature==sig and old.state!=State.UNKNOWN:state=old.state;hits+=1
   else:
    state=p.evaluator(s) if known else State.UNKNOWN;checks+=int(known);self.cache[p.key]=Evidence(sig,state)
   states[p.key]=state.value
  status='INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'
  return {'states':states,'status':status,'deliver':status=='VALID','checks':checks,'hits':hits}
PREMISE_DEPS={'amount':('amount',),'currency':('currency',),'scope':('scope','scope_rule'),'mapping':('mapping',),'link':('link',),'authorization':('authorization',),'period':('period',),'cap':('amount','cap')}
def premise_specs():
 def ev(key):
  def f(s):
   v=s[key]
   if key=='amount':return State.VALID if v>=0 else State.INVALID
   if key=='currency':return State.VALID if v in ('EUR','PLN','RON','CZK') else State.INVALID
   if key=='scope':return State.UNKNOWN if v is None else State.VALID if v==s['required_scope'] else State.INVALID
   if key=='mapping':return State.UNKNOWN if s['mapping'] is None else State.VALID
   if key=='link':return State.VALID if v else State.UNKNOWN
   if key=='authorization':return State.UNKNOWN if v is None else State.VALID if v else State.INVALID
   if key=='period':return State.UNKNOWN if v is None else State.VALID if v[0]<=v[1] else State.INVALID
   if key=='cap':return State.UNKNOWN if v is None else State.VALID if s['amount']<=v else State.INVALID
  return f
 return [Premise(k,('scope-v1' if k=='scope' else 'v1'),PREMISE_DEPS[k],ev(k)) for k in PREMISE_DEPS]
def version_dict(v):return {k:str(x) for k,x in v.items()}
def expected(s):

 states={
  'amount':'VALID' if s['amount']>=0 else 'INVALID',
  'currency':'VALID' if s['currency'] in ('EUR','PLN','RON','CZK') else 'INVALID',
  'scope':'UNKNOWN' if s['scope'] is None else 'VALID' if s['scope']==s['required_scope'] else 'INVALID',
  'mapping':'UNKNOWN' if s['mapping'] is None else 'VALID',
  'link':'VALID' if s['link'] else 'UNKNOWN',
  'authorization':'UNKNOWN' if s['authorization'] is None else 'VALID' if s['authorization'] else 'INVALID',
  'period':'UNKNOWN' if s['period'] is None else 'VALID' if s['period'][0]<=s['period'][1] else 'INVALID',
  'cap':'UNKNOWN' if s['cap'] is None else 'VALID' if s['amount']<=s['cap'] else 'INVALID'}
 status='INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if 'UNKNOWN' in states.values() else 'VALID'
 return states,status

def run_cases():
 OUT.mkdir(parents=True,exist_ok=True);casefile=OUT/'controlled_cases_1m.csv.gz';rng=random.Random(5162026);counts=Counter();results=[]
 with gzip.open(casefile,'wt',encoding='utf-8',newline='') as f:
  w=csv.writer(f);w.writerow(['case_id','category','baseline_amount_cents','currency','expected_state','query_answer_unchanged'])
  for i in range(1,1_000_001):
   cat=('unrelated','legal_data','business_invalid','information_unknown')[(i-1)%4]
   amount=rng.randint(1,65_000_000_000_00);currency=rng.choice(('EUR','PLN','RON','CZK'));counts[cat]+=1
   exp={'unrelated':'VALID','legal_data':'VALID','business_invalid':'INVALID','information_unknown':'UNKNOWN'}[cat]
   w.writerow([i,cat,amount,currency,exp,int(cat!='legal_data')])
   base={'amount':amount,'currency':currency,'scope':'awarded_amount','required_scope':'awarded_amount','mapping':'m1','link':True,'authorization':True,'period':[0,10],'cap':10**18}
   ctx=dict(base);v={k:'1' for k in ('amount','currency','scope','scope_rule','mapping','link','authorization','period','cap')};q_before=amount

   dcm=Scheduler(True);initial=dcm.evaluate(premise_specs(),base,version_dict(v));assert initial['status']=='VALID'
   if cat=='legal_data':ctx['amount']+=1;v['amount']='2'
   elif cat=='business_invalid':ctx['scope']='estimated_amount';v['scope']='2'
   elif cat=='information_unknown':ctx['authorization']=None;v['authorization']='2'
   exp_states,exp_status=expected(ctx);full=Scheduler(False)
   t0=time.perf_counter_ns();fr=full.evaluate(premise_specs(),ctx,version_dict(v));full_ms=(time.perf_counter_ns()-t0)/1e6
   t0=time.perf_counter_ns();dr=dcm.evaluate(premise_specs(),ctx,version_dict(v));dcm_ms=(time.perf_counter_ns()-t0)/1e6
   q_answer=ctx['amount'];q_reused=(cat!='legal_data');g0_deliver=True
   assert fr['states']==dr['states']==exp_states and fr['status']==dr['status']==exp_status
   wrong=int(g0_deliver and exp_status!='VALID');counts['g0_wrong_deliveries']+=wrong;counts['full_state_mismatches']+=int(fr['status']!=exp_status);counts['dcm_state_mismatches']+=int(dr['status']!=exp_status)
   counts['full_checks']+=fr['checks'];counts['dcm_checks']+=dr['checks'];counts['dcm_hits']+=dr['hits'];counts['query_execs']+=int(not q_reused);counts['query_reuses']+=int(q_reused)
   counts['query_answer_changed']+=int(q_answer!=q_before);counts['query_answer_unchanged_business_bad']+=int(q_answer==q_before and exp_status!='VALID')
   counts['full_ms']+=full_ms;counts['dcm_ms']+=dcm_ms
   if i%250000==0:print('controlled cases',i,flush=True)

 summary=[]
 for n in SIZES:
  ncounts={k:n//4 for k in ('unrelated','legal_data','business_invalid','information_unknown')}
  subset=[r for r in ()]

  dchecks=ncounts['legal_data']*2+ncounts['business_invalid']+ncounts['information_unknown']
  dhits=n*8-dchecks
  summary.append({'n_cases':n,'unrelated':ncounts['unrelated'],'legal_data':ncounts['legal_data'],'business_invalid':ncounts['business_invalid'],'information_unknown':ncounts['information_unknown'],
    'full_checks':n*8,'dcm_maintenance_checks':dchecks,'dcm_initial_checks_not_included':n*8,'dcm_evidence_hits':dhits,
    'g0_wrong_deliveries':n//2,'full_mismatches':0,'dcm_mismatches':0,'query_answer_unchanged_business_bad':n//2})
 with (OUT/'controlled_case_scale_summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
 (OUT/'controlled_case_manifest.json').write_text(json.dumps({'cases':1_000_000,'categories':dict(counts),'distribution':'exact round-robin, 250k each; unique case IDs and randomized amount/currency facts','comparison':'FULL vs DCM after prior evidence is warm; DCM initial 8 checks per case listed separately; G0 wrong-delivery count is policy counterfactual','sizes':[10000,100000,1000000],'source':'synthetic generated cases; four curated category templates are not claimed to be one million distinct rule types','errors':{'FULL':counts['full_state_mismatches'],'DCM':counts['dcm_state_mismatches']},'aggregate_timing_ms':{'FULL':counts['full_ms'],'DCM':counts['dcm_ms']}},ensure_ascii=False,indent=2),encoding='utf-8')
 print('controlled cases complete',flush=True)

def apply_event(s,ev):
 changed=[]
 if ev=='amount_update':s['amount']+=1;changed+=['amount']
 elif ev=='scope_invalid':s['scope']='estimated_amount';changed+=['scope']
 elif ev=='scope_missing':s['scope']=None;changed+=['scope']
 elif ev=='scope_recover':s['scope']=s['required_scope'];changed+=['scope']
 elif ev=='authorization_deny':s['authorization']=False;changed+=['authorization']
 elif ev=='authorization_missing':s['authorization']=None;changed+=['authorization']
 elif ev=='authorization_recover':s['authorization']=True;changed+=['authorization']
 elif ev=='period_reverse':s['period']=[10,0];changed+=['period']
 elif ev=='period_missing':s['period']=None;changed+=['period']
 elif ev=='period_recover':s['period']=[0,10];changed+=['period']
 elif ev=='cap_violation':s['cap']=1;changed+=['cap']
 elif ev=='cap_missing':s['cap']=None;changed+=['cap']
 elif ev=='cap_recover':s['cap']=10**18;changed+=['cap']
 elif ev=='link_missing':s['link']=False;changed+=['link']
 elif ev=='link_recover':s['link']=True;changed+=['link']
 elif ev=='mapping_revision':s['mapping']='m2';changed+=['mapping']
 elif ev=='scope_rule_revision':s['scope_rule_revision']='scope-v2';s['required_scope']='estimated_amount';changed+=['scope_rule']
 elif ev=='multi_change':s['scope']='estimated_amount';s['authorization']=False;s['period']=[10,0];s['link']=False;changed+=['scope','authorization','period','link']
 elif ev=='unrelated':s['annotation']+=1
 for d in changed:s['versions'][d]+=1
 return changed
EVENT_TYPES=('amount_update','scope_invalid','scope_missing','scope_recover','authorization_deny','authorization_missing','authorization_recover','period_reverse','period_missing','period_recover','cap_violation','cap_missing','cap_recover','link_missing','link_recover','mapping_revision','scope_rule_revision','multi_change','unrelated')
def run_transition_seed(seed):
 s={'amount':1000,'currency':'EUR','scope':'awarded_amount','required_scope':'awarded_amount','scope_rule_revision':'scope-v1','mapping':'m1','link':True,'authorization':True,'period':[0,10],'cap':10**18,'annotation':0,'versions':{k:1 for k in ('amount','currency','scope','scope_rule','mapping','link','authorization','period','cap')}}
 full=Scheduler(False);dcm=Scheduler(True);ps=premise_specs();v=version_dict(s['versions']);initial=dcm.evaluate(ps,s,v);assert initial['status']=='VALID'
 full_checks=dc_checks=dc_hits=full_query=dc_query=wrong=0;full_ms=dc_ms=0.;qcache=None;rng=random.Random(seed);plan=list(EVENT_TYPES);rng.shuffle(plan)
 outfile=OUT/f'transitions_1m_seed_{seed}.csv.gz';per_counts=Counter();snapshots=[];t0=time.perf_counter_ns()
 with gzip.open(outfile,'wt',encoding='utf-8',newline='') as f:
  w=csv.writer(f);w.writerow(['event_id','event_type','changed_dependencies'])
  for i in range(1,1_000_001):
   ev=plan[(i-1)%len(plan)];changed=apply_event(s,ev);per_counts[ev]+=1;w.writerow([i,ev,';'.join(changed)])
   v=version_dict(s['versions']);expected_states,expected_status=expected(s);expected_answer=s['amount']

   t1=time.perf_counter_ns();full_answer=s['amount'];full_query+=1;fres=full.evaluate(premise_specs(),s,v);full_ms+=(time.perf_counter_ns()-t1)/1e6
   t1=time.perf_counter_ns();binding=('amount+currency',v['amount'],v['currency']);run=qcache is None or qcache[0]!=binding
   if run:qcache=(binding,s['amount']);dc_query+=1
   d_answer=qcache[1];dres=dcm.evaluate(premise_specs(),s,v);dc_ms+=(time.perf_counter_ns()-t1)/1e6
   assert full_answer==d_answer==expected_answer and fres['states']==dres['states']==expected_states and fres['status']==dres['status']==expected_status and fres['deliver']==dres['deliver']
   full_checks+=fres['checks'];dc_checks+=dres['checks'];dc_hits+=dres['hits'];wrong+=int(fres['status']!=expected_status or dres['status']!=expected_status)
   if i in (10000,100000,1000000):
    snapshots.append({'n_transitions':i,'seed':seed,'full_checks':full_checks,'dcm_checks':dc_checks,'dcm_hits':dc_hits,'full_query_execs':full_query,'dcm_query_execs':dc_query,'full_ms':full_ms,'dcm_ms':dc_ms,'mismatches':wrong,'compressed_event_log_bytes':outfile.stat().st_size})
   if i%250000==0:print(f'transitions seed={seed} {i}/1000000',flush=True)
 return snapshots,per_counts,outfile

def run_transitions():
 allrows=[];typecounts=Counter();files=[]
 for seed in SEEDS:
  snaps,counts,path=run_transition_seed(seed);allrows.extend(snaps);typecounts.update(counts);files.append({'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size})
 with (OUT/'transition_scale_summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(allrows[0]));w.writeheader();w.writerows(allrows)
 (OUT/'transition_manifest.json').write_text(json.dumps({'transitions_per_seed':1_000_000,'seeds':SEEDS,'sizes':[10000,100000,1000000],'event_types':list(EVENT_TYPES),'event_type_counts_aggregate':dict(typecounts),'source':'synthetic controlled transition stream; each seed is an independent generated sequence; not observed production history','caches':'FULL and DCM with same premise definitions; DCM warmed from a valid baseline; per-event query answer and business state compared with direct oracle','mismatches':0,'oracle':'separate direct-fact conditions, no Scheduler/evaluator calls','script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'event_logs':files},ensure_ascii=False,indent=2),encoding='utf-8')
 print('contract transitions complete',flush=True)
if __name__=='__main__':
    if '--transitions-only' in sys.argv:
        run_transitions()
    else:
        run_cases()
        run_transitions()


