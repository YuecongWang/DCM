from pathlib import Path
import csv,gzip,json,random,sqlite3,statistics,sys,time,hashlib
from collections import defaultdict
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent.parent))
from dcm_scheduler import Scheduler,Premise,State
from benchmark_version_cache import VersionCache
DATA=HERE/'ted_derived_synthetic_1m.csv.gz'
OUT=HERE/'record_scale_results'
SIZES=(10000,100000,1000000)
FRACTIONS=(0.01,0.10,1.0)
SEEDS=(11,29,47)
THRESHOLD=1_000_000
SQL={
 'currency_sum':('SELECT currency, SUM(amount_cents) FROM records GROUP BY currency',('amount','currency')),
 'currency_count':('SELECT currency, COUNT(*) FROM records GROUP BY currency',('currency',)),
 'optional_currency_count':('SELECT currency, COUNT(*) FROM records WHERE currency IS NOT NULL GROUP BY currency',('currency',)),
 'linked_amount_count':('SELECT currency, COUNT(*) FROM records WHERE linked=1 GROUP BY currency',('currency','link')),
 'amount_threshold_count':(f'SELECT currency, COUNT(*) FROM records WHERE amount_cents >= {THRESHOLD} GROUP BY currency',('amount','currency')),
 'linked_threshold_sum':(f'SELECT currency, SUM(amount_cents) FROM records WHERE linked=1 AND amount_cents >= {THRESHOLD} GROUP BY currency',('amount','currency','link')),
}

def aggregate(states):
    return 'INVALID' if 'INVALID' in states.values() else 'UNKNOWN' if not states or 'UNKNOWN' in states.values() else 'VALID'

def db_rows(conn,sql):return tuple((str(a),int(b or 0)) for a,b in conn.execute(sql).fetchall())

def run_case(n,fraction,seed):
    conn=sqlite3.connect(':memory:');conn.execute('PRAGMA journal_mode=OFF');conn.execute('PRAGMA synchronous=OFF');conn.execute('PRAGMA temp_store=MEMORY')
    conn.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, linked INTEGER NOT NULL)')
    amounts=[];curr_codes=[];currency_set=set();batch=[];load_start=time.perf_counter_ns()
    with gzip.open(DATA,'rt',encoding='utf-8',newline='') as f:
        rd=csv.DictReader(f)
        for row in rd:
            if int(row['record_id'])>n: break
            a=int(row['amount_cents']);c=row['currency'];amounts.append(a);currency_set.add(c);batch.append((int(row['record_id']),a,c,1))
            if len(batch)>=20000:conn.executemany('INSERT INTO records VALUES(?,?,?,?)',batch);batch.clear()
    if batch:conn.executemany('INSERT INTO records VALUES(?,?,?,?)',batch)
    load_ms=(time.perf_counter_ns()-load_start)/1e6
    currencies=sorted(currency_set);codes={c:i for i,c in enumerate(currencies)}
    with gzip.open(DATA,'rt',encoding='utf-8',newline='') as f:
        for row in csv.DictReader(f):
            if int(row['record_id'])>n:break
            curr_codes.append(codes[row['currency']])
    ctx={'conn':conn,'scope':'awarded_amount','mapping_version':'m1','authorized':True,'period':[0,10],
         'cap':max(amounts,default=0)+10**15,'currencies':currencies}
    placeholders=','.join('?' for _ in currencies)
    def check_amount(s):return State.INVALID if s['conn'].execute('SELECT 1 FROM records WHERE amount_cents<0 LIMIT 1').fetchone() else State.VALID
    def check_currency(s):return State.INVALID if s['conn'].execute(f'SELECT 1 FROM records WHERE currency NOT IN ({placeholders}) LIMIT 1',currencies).fetchone() else State.VALID
    def check_scope(s):return State.UNKNOWN if s['scope'] is None else State.VALID if s['scope']=='awarded_amount' else State.INVALID
    def check_mapping(s):return State.UNKNOWN if not s.get('mapping_version') else State.VALID
    def check_link(s):return State.INVALID if s['conn'].execute('SELECT 1 FROM records WHERE linked<>1 LIMIT 1').fetchone() else State.VALID
    def check_auth(s):return State.UNKNOWN if s['authorized'] is None else State.VALID if s['authorized'] else State.INVALID
    def check_period(s):return State.UNKNOWN if s['period'] is None else State.VALID if s['period'][0]<=s['period'][1] else State.INVALID
    def check_cap(s):
        m=s['conn'].execute('SELECT MAX(amount_cents) FROM records').fetchone()[0]
        return State.UNKNOWN if s['cap'] is None or m is None else State.VALID if m<=s['cap'] else State.INVALID
    ps=[Premise('amount','a1',('amount',),check_amount,True),Premise('currency','c1',('currency',),check_currency,True),
        Premise('scope','s1',('scope',),check_scope,True),Premise('mapping','m1',('mapping',),check_mapping,True),
        Premise('link','l1',('link',),check_link,True),Premise('authorization','u1',('authorized',),check_auth,True),
        Premise('period','p1',('period',),check_period,True),Premise('cap','cap1',('amount','cap'),check_cap,True)]
    methods={'FULL':Scheduler(False),'QUERY_CACHE_FULL':Scheduler(False),'VERSION_CACHE':VersionCache(),'DCM':Scheduler(True)}
    qcache={m:{} for m in methods}; versions={'amount':0,'currency':0,'link':0,'scope':0,'mapping':0,'authorized':0,'period':0,'cap':0}
    k=max(1,round(n*fraction));events=['data']*6+['business']*3+['unrelated']*3;random.Random(seed).shuffle(events)
    business_fields=['scope','authorized','period'];business_i=0;business_values={'scope':['estimated_amount',None,'awarded_amount'],'authorized':[False,None,True],'period':[[10,0],None,[0,10]]}
    update_ms=0.0;rows=[];data_event=0
    for ei,event in enumerate(['initial']+events):
        if ei:
            if event=='data':
                t=time.perf_counter_ns();conn.execute('UPDATE records SET amount_cents=amount_cents+1 WHERE id<=?',(k,));conn.commit();update_ms+=(time.perf_counter_ns()-t)/1e6;versions['amount']+=1;data_event+=1

                for i in range(k):amounts[i]+=1
            elif event=='business':
                field=business_fields[business_i%len(business_fields)];ctx[field]=business_values[field][business_i%3];business_i+=1;versions[field]+=1


        sums=defaultdict(int);counts=defaultdict(int);links=defaultdict(int);fcounts=defaultdict(int);fsums=defaultdict(int);flinks=defaultdict(int);minmax={}
        for i,a in enumerate(amounts):
            c=curr_codes[i];name=currencies[c];sums[name]+=a;counts[name]+=1;links[name]+=1
            if a>=THRESHOLD:fcounts[name]+=1;fsums[name]+=a;flinks[name]+=1
            old=minmax.get(name)
            minmax[name]=(a,a) if old is None else (min(old[0],a),max(old[1],a))
        oracle={'currency_sum':tuple(sorted(sums.items())),
          'currency_count':tuple(sorted(counts.items())),
          'optional_currency_count':tuple(sorted(counts.items())),
          'linked_amount_count':tuple(sorted(links.items())),
          'amount_threshold_count':tuple(sorted(fcounts.items())),
          'linked_threshold_sum':tuple(sorted(fsums.items()))}
        states={'amount':'VALID' if n else 'UNKNOWN','currency':'VALID' if n else 'UNKNOWN',
          'scope':'UNKNOWN' if ctx['scope'] is None else 'VALID' if ctx['scope']=='awarded_amount' else 'INVALID',
          'mapping':'VALID' if ctx['mapping_version'] else 'UNKNOWN','link':'VALID' if n else 'UNKNOWN',
          'authorization':'UNKNOWN' if ctx['authorized'] is None else 'VALID' if ctx['authorized'] else 'INVALID',
          'period':'UNKNOWN' if ctx['period'] is None else 'VALID' if ctx['period'][0]<=ctx['period'][1] else 'INVALID',
          'cap':'VALID' if max(amounts,default=0)<=ctx['cap'] else 'INVALID'}
        status=aggregate(states);expected_deliver=status=='VALID'
        order=list(methods);offset=(ei+seed)%len(order);order=order[offset:]+order[:offset]
        for method in order:
            start=time.perf_counter_ns();answers={};executed=0;qhits=0
            for q,(sql,deps) in SQL.items():
                binding=(q,sql,tuple((d,versions[d]) for d in deps))
                old=qcache[method].get(q)
                must=method=='FULL' or old is None or old[0]!=binding
                if must:ans=db_rows(conn,sql);qcache[method][q]=(binding,ans);executed+=1
                else:ans=old[1];qhits+=1
                answers[q]=ans
            result=methods[method].evaluate(ps,ctx,{d:str(v) for d,v in versions.items()})
            elapsed=(time.perf_counter_ns()-start)/1e6
            assert answers==oracle,(n,fraction,seed,ei,method,'query mismatch')
            assert result['premise_states']==states,(n,fraction,seed,ei,method,'state mismatch',result['premise_states'],states)
            assert result['business_state']==status and result['deliver']==expected_deliver
            rows.append({'n':n,'target_fraction':fraction,'seed':seed,'event_index':ei,'event':event,'method':method,
                'maintenance_ms':elapsed,'update_ms':update_ms if ei else 0,'query_execs':executed,'query_hits':qhits,
                'premise_checks':result['premise_checks'],'evidence_hits':result['evidence_hits'],
                'state_match':True,'query_match':True,'delivery_match':True})
    conn.close();return rows,load_ms

def main():
    OUT.mkdir(parents=True,exist_ok=True);allrows=[];loads=[]
    for n in SIZES:
      for fraction in FRACTIONS:
       for seed in SEEDS:
        r,lm=run_case(n,fraction,seed);allrows.extend(r);loads.append({'n':n,'target_fraction':fraction,'seed':seed,'load_ms':lm})
        print(f'completed n={n} target={fraction} seed={seed}',flush=True)
    with (OUT/'maintenance_events.csv').open('w',encoding='utf-8-sig',newline='') as f:
      w=csv.DictWriter(f,fieldnames=list(allrows[0]));w.writeheader();w.writerows(allrows)
    with (OUT/'load_times.csv').open('w',encoding='utf-8-sig',newline='') as f:
      w=csv.DictWriter(f,fieldnames=list(loads[0]));w.writeheader();w.writerows(loads)
    grouped=defaultdict(list)
    for r in allrows:grouped[r['n'],r['target_fraction'],r['method'],r['seed']].append(r)
    totals=[]
    for (n,f,method,seed),rr in grouped.items():
      totals.append({'n':n,'target_fraction':f,'method':method,'seed':seed,
        'maintenance_ms':sum(x['maintenance_ms'] for x in rr),'query_execs':sum(x['query_execs'] for x in rr),
        'query_hits':sum(x['query_hits'] for x in rr),'premise_checks':sum(x['premise_checks'] for x in rr),
        'evidence_hits':sum(x['evidence_hits'] for x in rr),'mismatches':sum(not(x['state_match'] and x['query_match'] and x['delivery_match']) for x in rr)})
    with (OUT/'paired_totals.csv').open('w',encoding='utf-8-sig',newline='') as f:
      w=csv.DictWriter(f,fieldnames=list(totals[0]));w.writeheader();w.writerows(totals)
    summary=[]
    for n in SIZES:
      for f in FRACTIONS:
       for method in ('FULL','QUERY_CACHE_FULL','VERSION_CACHE','DCM'):
        vals=[r['maintenance_ms'] for r in totals if r['n']==n and r['target_fraction']==f and r['method']==method]
        r0=[r for r in totals if r['n']==n and r['target_fraction']==f and r['method']==method]
        summary.append({'n':n,'target_fraction':f,'method':method,'median_ms':statistics.median(vals),
          'min_ms':min(vals),'max_ms':max(vals),'query_execs_median':statistics.median(r['query_execs'] for r in r0),
          'premise_checks_median':statistics.median(r['premise_checks'] for r in r0),
          'evidence_hits_median':statistics.median(r['evidence_hits'] for r in r0),'mismatches':sum(r['mismatches'] for r in r0)})
    with (OUT/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
      w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
    manifest={'sizes':SIZES,'target_fractions':FRACTIONS,'seeds':SEEDS,'events_per_run':13,'queries':list(SQL),'premises':8,
      'data_source':'10,000 unique official TED Search API metadata records used as an empirical bootstrap distribution; scaled rows are synthetic and do not count as additional real notices',
      'backend':'SQLite relationalization of amount, currency, and linkage graph facts; six SQL aggregate equivalents; not a full RDFLib/RML/SPARQL run',
      'version_policy':'trusted column-level event version counters; no full-column digest scan',
      'timing':'maintenance phase includes query binding, SQL execution or cached answers, business scheduling, delivery; excludes initial dataset load, actual UPDATE transaction, and independent Python oracle; update and load times reported separately',
      'correctness':'six query answers computed independently by Python accumulator over in-memory arrays; eight business states computed from current facts; all rows matched',
      'rows':len(allrows),'mismatches':0,'synthetic_dataset_sha256':hashlib.sha256(DATA.read_bytes()).hexdigest()}
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS',json.dumps({'event_rows':len(allrows),'mismatches':0},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
