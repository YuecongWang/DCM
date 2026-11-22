from pathlib import Path
import csv,gzip,json,random,sqlite3,statistics,time,hashlib
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
import numpy as np
HERE=Path(__file__).resolve().parent; DATA=HERE/'ted_derived_synthetic_1m.csv.gz'; OUT=HERE/'record_scale_results'
SIZES=(10000,100000,1000000); FRACTIONS=(.01,.10,1.0); SEEDS=(11,29,47); THRESHOLD=1_000_000
SQL={
 'currency_sum':('SELECT currency, SUM(amount_cents) FROM records GROUP BY currency',('amount','currency')),
 'currency_count':('SELECT currency, COUNT(*) FROM records GROUP BY currency',('currency',)),
 'optional_currency_count':('SELECT currency, COUNT(*) FROM records WHERE currency IS NOT NULL GROUP BY currency',('currency',)),
 'linked_amount_count':('SELECT currency, COUNT(*) FROM records WHERE linked=1 GROUP BY currency',('currency','link')),
 'amount_threshold_count':(f'SELECT currency, COUNT(*) FROM records WHERE amount_cents >= {THRESHOLD} GROUP BY currency',('amount','currency')),
 'linked_threshold_sum':(f'SELECT currency, SUM(amount_cents) FROM records WHERE linked=1 AND amount_cents >= {THRESHOLD} GROUP BY currency',('amount','currency','link'))}
class State(str,Enum): VALID='VALID'; INVALID='INVALID'; UNKNOWN='UNKNOWN'
@dataclass(frozen=True)
class Premise:
 key:str; revision:str; dependencies:tuple; evaluator:object; dependencies_complete:bool=True
@dataclass(frozen=True)
class Evidence:
 signature:tuple; state:State
class TupleCache:
 def __init__(self):self.entries={}
 def evaluate(self,ps,s,versions):
  active={p.key for p in ps};self.entries={k:v for k,v in self.entries.items() if k in active};states={};checks=hits=0
  for p in ps:
   deps=tuple(sorted(set(p.dependencies)));known=all(isinstance(versions.get(d),str) and versions[d] for d in deps)
   key=(p.revision,p.dependencies_complete,tuple((d,versions.get(d)) for d in deps));old=self.entries.get(p.key)
   if p.dependencies_complete and p.revision and known and old and old[0]==key and old[1]!='UNKNOWN':state=old[1];hits+=1
   else:state=p.evaluator(s).value if known else 'UNKNOWN';checks+=int(known);self.entries[p.key]=(key,state)
   states[p.key]=state
  status='INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'
  return {'premise_states':states,'business_state':status,'deliver':status=='VALID','premise_checks':checks,'evidence_hits':hits}
class Scheduler:
 def __init__(self,incremental):self.incremental=incremental;self.cache={}
 def evaluate(self,premises,snapshot,versions):
  active={p.key for p in premises};self.cache={k:v for k,v in self.cache.items() if k in active};states={};checks=hits=0
  for p in premises:
   deps=tuple(sorted(set(p.dependencies)));known=all(isinstance(versions.get(d),str) and versions[d] for d in deps)
   sig=(p.revision,p.dependencies_complete,tuple((d,versions.get(d)) for d in deps));old=self.cache.get(p.key)
   reusable=p.dependencies_complete and bool(p.revision) and known
   if self.incremental and reusable and old is not None and old.signature==sig and old.state!=State.UNKNOWN:state=old.state;hits+=1
   else:
    if not known:state=State.UNKNOWN
    else:state=p.evaluator(snapshot);checks+=1
    self.cache[p.key]=Evidence(sig,state)
   states[p.key]=state.value
  status='INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'
  return {'premise_states':states,'business_state':status,'deliver':status=='VALID','premise_checks':checks,'evidence_hits':hits}
class JsonVersionCache:
 def __init__(self):self.entries={}
 def evaluate(self,ps,s,versions):
  active={p.key for p in ps};self.entries={k:v for k,v in self.entries.items() if k in active};states={};checks=hits=0
  for p in ps:
   bindings={d:versions.get(d) for d in set(p.dependencies)};known=all(isinstance(v,str) and v for v in bindings.values())
   key=json.dumps([p.revision,p.dependencies_complete,sorted(bindings.items())]);entry=self.entries.get(p.key)
   if known and p.revision and p.dependencies_complete and entry and entry[0]==key and entry[1]!='UNKNOWN':state=entry[1];hits+=1
   else:state=p.evaluator(s).value if known else 'UNKNOWN';checks+=int(known);self.entries[p.key]=(key,state)
   states[p.key]=state
  status='INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'
  return {'premise_states':states,'business_state':status,'deliver':status=='VALID','premise_checks':checks,'evidence_hits':hits}
def aggregate(d):return 'INVALID' if 'INVALID' in d.values() else 'UNKNOWN' if not d or 'UNKNOWN' in d.values() else 'VALID'
def sqlrows(conn,q):return tuple((str(a),int(b or 0)) for a,b in conn.execute(q).fetchall())
def oracle_answers(amounts,codes,currency_by_code):
 ans={k:[] for k in SQL}
 for ci,name in enumerate(currency_by_code):
  mask=(codes==ci); aa=amounts[mask]; filt=aa[aa>=THRESHOLD]
  if aa.size:
   ans['currency_sum'].append((name,int(aa.sum(dtype=np.int64))))
   ans['currency_count'].append((name,int(aa.size)))
   ans['optional_currency_count'].append((name,int(aa.size)))
   ans['linked_amount_count'].append((name,int(aa.size)))
  if filt.size:
   ans['amount_threshold_count'].append((name,int(filt.size)))
   ans['linked_threshold_sum'].append((name,int(filt.sum(dtype=np.int64))))
 return {k:tuple(sorted(v)) for k,v in ans.items()}
def run_case(n,fraction,seed):
 conn=sqlite3.connect(':memory:');conn.execute('PRAGMA journal_mode=OFF');conn.execute('PRAGMA synchronous=OFF');conn.execute('PRAGMA temp_store=MEMORY');conn.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,linked INTEGER NOT NULL)')
 vals=[];currs=[];batch=[];curset=set();t0=time.perf_counter_ns()
 with gzip.open(DATA,'rt',encoding='utf-8',newline='') as f:
  for row in csv.DictReader(f):
   idx=int(row['record_id'])
   if idx>n:break
   a=int(row['amount_cents']);c=row['currency'];vals.append(a);currs.append(c);curset.add(c);batch.append((idx,a,c,1))
   if len(batch)>=20000:conn.executemany('INSERT INTO records VALUES(?,?,?,?)',batch);batch.clear()
 if batch:conn.executemany('INSERT INTO records VALUES(?,?,?,?)',batch)
 conn.commit();load_ms=(time.perf_counter_ns()-t0)/1e6
 currencies=sorted(curset);code_map={c:i for i,c in enumerate(currencies)};amounts=np.asarray(vals,dtype=np.int64);codes=np.fromiter((code_map[c] for c in currs),dtype=np.int16,count=n);del vals,currs
 maxval=int(amounts.max());ctx={'conn':conn,'scope':'awarded_amount','mapping_version':'m1','authorized':True,'period':[0,10],'cap':maxval+10**15,'currencies':currencies}
 ph=','.join('?' for _ in currencies)
 def ea(s):return State.INVALID if s['conn'].execute('SELECT 1 FROM records WHERE amount_cents<0 LIMIT 1').fetchone() else State.VALID
 def ec(s):return State.INVALID if s['conn'].execute(f'SELECT 1 FROM records WHERE currency NOT IN ({ph}) LIMIT 1',currencies).fetchone() else State.VALID
 def es(s):return State.UNKNOWN if s['scope'] is None else State.VALID if s['scope']=='awarded_amount' else State.INVALID
 def em(s):return State.UNKNOWN if not s.get('mapping_version') else State.VALID
 def el(s):return State.INVALID if s['conn'].execute('SELECT 1 FROM records WHERE linked<>1 LIMIT 1').fetchone() else State.VALID
 def eu(s):return State.UNKNOWN if s['authorized'] is None else State.VALID if s['authorized'] else State.INVALID
 def ep(s):return State.UNKNOWN if s['period'] is None else State.VALID if s['period'][0]<=s['period'][1] else State.INVALID
 def ecap(s):m=s['conn'].execute('SELECT MAX(amount_cents) FROM records').fetchone()[0];return State.UNKNOWN if s['cap'] is None or m is None else State.VALID if m<=s['cap'] else State.INVALID
 ps=[Premise('amount','a1',('amount',),ea),Premise('currency','c1',('currency',),ec),Premise('scope','s1',('scope',),es),Premise('mapping','m1',('mapping',),em),Premise('link','l1',('link',),el),Premise('authorization','u1',('authorized',),eu),Premise('period','p1',('period',),ep),Premise('cap','cap1',('amount','cap'),ecap)]
 sched={'FULL':Scheduler(False),'QUERY_CACHE_FULL':Scheduler(False),'VERSION_CACHE':JsonVersionCache(),'DCM':Scheduler(True)}; qcache={m:{} for m in sched};vers={k:0 for k in ('amount','currency','link','scope','mapping','authorized','period','cap')};k=max(1,round(n*fraction));plan=['data','business','unrelated','data'];random.Random(seed).shuffle(plan);fields=['scope','authorized','period'];bv={'scope':['estimated_amount',None,'awarded_amount'],'authorized':[False,None,True],'period':[[10,0],None,[0,10]]};bi=0;update_ms=0.;rows=[]
 for ei,event in enumerate(['initial']+plan):
  if ei and event=='data':
   u0=time.perf_counter_ns();conn.execute('UPDATE records SET amount_cents=amount_cents+1 WHERE id<=?',(k,));conn.commit();update_ms+=(time.perf_counter_ns()-u0)/1e6;amounts[:k]+=1;vers['amount']+=1
  elif ei and event=='business':
   field=fields[bi%3];ctx[field]=bv[field][bi%3];bi+=1;vers[field]+=1
  answers_ref=oracle_answers(amounts,codes,currencies);states={'amount':'VALID' if np.all(amounts>=0) else 'INVALID','currency':'VALID','scope':'UNKNOWN' if ctx['scope'] is None else 'VALID' if ctx['scope']=='awarded_amount' else 'INVALID','mapping':'VALID','link':'VALID','authorization':'UNKNOWN' if ctx['authorized'] is None else 'VALID' if ctx['authorized'] else 'INVALID','period':'UNKNOWN' if ctx['period'] is None else 'VALID' if ctx['period'][0]<=ctx['period'][1] else 'INVALID','cap':'VALID' if int(amounts.max())<=ctx['cap'] else 'INVALID'};status=aggregate(states)
  order=list(sched);rot=(ei+seed)%4;order=order[rot:]+order[:rot]
  for method in order:
   t0=time.perf_counter_ns();answers={};exe=hitsq=0
   for q,(sql,deps) in SQL.items():
    binding=(q,sql,tuple((d,vers[d]) for d in deps));old=qcache[method].get(q);run=method=='FULL' or old is None or old[0]!=binding
    if run:ans=sqlrows(conn,sql);qcache[method][q]=(binding,ans);exe+=1
    else:ans=old[1];hitsq+=1
    answers[q]=ans
   result=sched[method].evaluate(ps,ctx,{d:str(v) for d,v in vers.items()});ms=(time.perf_counter_ns()-t0)/1e6
   assert answers==answers_ref,(n,fraction,seed,ei,method,'query mismatch')
   assert result['premise_states']==states and result['business_state']==status and result['deliver']==(status=='VALID'),(n,fraction,seed,ei,method,'state mismatch')
   rows.append({'n':n,'target_fraction':fraction,'seed':seed,'event_index':ei,'event':event,'method':method,'maintenance_ms':ms,'query_execs':exe,'query_hits':hitsq,'premise_checks':result['premise_checks'],'evidence_hits':result['evidence_hits'],'state_match':1,'query_match':1,'delivery_match':1})
 conn.close();return rows,load_ms,update_ms

def main():
 OUT.mkdir(parents=True,exist_ok=True);rows=[];loads=[]
 for n in SIZES:
  for f in FRACTIONS:
   for seed in SEEDS:
    r,lm,um=run_case(n,f,seed);rows.extend(r);loads.append({'n':n,'target_fraction':f,'seed':seed,'load_ms':lm,'update_ms':um});print(f'completed n={n} target={f} seed={seed}',flush=True)
 for name,data in [('maintenance_events.csv',rows),('load_update_times.csv',loads)]:
  with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
   w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
 totals=[];groups=defaultdict(list)
 for r in rows:groups[r['n'],r['target_fraction'],r['method'],r['seed']].append(r)
 for (n,f,m,seed),g in groups.items():totals.append({'n':n,'target_fraction':f,'method':m,'seed':seed,'maintenance_ms':sum(x['maintenance_ms'] for x in g),'query_execs':sum(x['query_execs'] for x in g),'query_hits':sum(x['query_hits'] for x in g),'premise_checks':sum(x['premise_checks'] for x in g),'evidence_hits':sum(x['evidence_hits'] for x in g),'mismatches':sum(not(x['state_match'] and x['query_match'] and x['delivery_match']) for x in g)})
 with (OUT/'paired_totals.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(totals[0]));w.writeheader();w.writerows(totals)
 summary=[]
 for n in SIZES:
  for f in FRACTIONS:
   for m in ('FULL','QUERY_CACHE_FULL','VERSION_CACHE','DCM'):
    g=[r for r in totals if r['n']==n and r['target_fraction']==f and r['method']==m]
    summary.append({'n':n,'target_fraction':f,'method':m,'median_ms':statistics.median(r['maintenance_ms'] for r in g),'min_ms':min(r['maintenance_ms'] for r in g),'max_ms':max(r['maintenance_ms'] for r in g),'query_execs_median':statistics.median(r['query_execs'] for r in g),'premise_checks_median':statistics.median(r['premise_checks'] for r in g),'evidence_hits_median':statistics.median(r['evidence_hits'] for r in g),'mismatches':sum(r['mismatches'] for r in g)})
 with (OUT/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
 manifest={'sizes':SIZES,'target_fractions':FRACTIONS,'seeds':SEEDS,'events_per_run':5,'queries':list(SQL),'premises':8,'dataset':'1M synthetic rows sampled with replacement from 9,769 official TED API notices with monetary value and currency; prefix subsets at 10K and 100K; not additional real notices','backend':'SQLite relational representation, not full XML/RML/RDF/SPARQL execution','versions':'trusted column-level event version counters','timing':'per-event maintenance includes version binding, SQL query/cache, business scheduling and delivery; excludes data loading, UPDATE transaction and independent NumPy oracle; load/update times separate','oracle':'independent NumPy aggregation plus direct current-fact business predicates','mismatches':0,'event_rows':len(rows),'dataset_sha256':hashlib.sha256(DATA.read_bytes()).hexdigest()}
 (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 print('PASS',json.dumps({'event_rows':len(rows),'mismatches':0},ensure_ascii=False),flush=True)
if __name__=='__main__':main()

