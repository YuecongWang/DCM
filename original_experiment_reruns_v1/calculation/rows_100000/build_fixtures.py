import copy
import csv
import sqlite3
from decimal import Decimal
from config import DATA_ROOT, ROWS, SHARED_QUERIES
from fabric.common import digest, write_json


def decimal_rule(column,scale='1',revision='1',concept='amount'):
    return {'column':column,'kind':'decimal','scale':scale,'quantum':'0.01','rounding':'HALF_EVEN',
            'revision':revision,'null_policy':'None-or-empty','error_policy':'fail-query',
            'input_digits':28,'input_adjusted_exponent_limit':28,'concept':concept}


def build():
    DATA_ROOT.mkdir(parents=True,exist_ok=True)
    base=[{'x':str(Decimal(i)/10),'y':str(i)} for i in range(ROWS)]
    if ROWS<10: raise ValueError('ROWS must be >=10')
    base[0]['x']=None; base[1]['x']=''; base[2]['x']='1.005'; base[3]['x']='1.015'
    states=[]
    def add(name,rows,scale='1',revision='1',force_version=False,query_change=False,missing=False):
        states.append({'name':name,'rows':copy.deepcopy(rows),'scale':scale,'revision':revision,
                       'force_version':force_version,'query_change':query_change,'missing':missing})
    add('initial',base)
    scaled=[dict(x=str(Decimal(r['x'])*100) if r['x'] not in (None,'') else r['x'], y=r['y']) for r in base]
    add('representation_mapping_equivalent',scaled,'0.01')
    changed=copy.deepcopy(scaled); changed[4]['x']='999'
    add('normalized_changed',changed,'0.01')
    partial=[dict(r,y=str(Decimal(r['y'])+1)) for r in changed]
    add('partial_only_billing_changed',partial,'0.01')
    add('data_version_only',partial,'0.01',force_version=True)
    add('transform_definition_only',partial,'0.01','2')
    broken=copy.deepcopy(partial); broken[5]['x']='NaN'; broken[6]['x']='bad'
    add('conversion_failure',broken,'0.01','2')
    add('recovery',partial,'0.01','2')
    precision=copy.deepcopy(partial); precision[2]['x']='100.500000000000000000000001'; precision[7]['x']='1E100'
    add('precision_boundary_range_error',precision,'0.01','2')
    add('unrelated_source_change',precision,'0.01','2')
    add('query_definition_change',precision,'0.01','2',query_change=True)
    add('mapping_missing',precision,'0.01','2',missing=True)
    queries=[{'id':f'amount_{i}','columns':['core.amount'],'op':['sum','count','sort','project'][i%4]} for i in range(SHARED_QUERIES)]
    queries += [{'id':'billing_sum','columns':['billing.counter'],'op':'sum'},
                {'id':'joined_projection','columns':['core.amount','billing.counter'],'op':'project',
                 'join':{'left':'core.row_id','right':'billing.row_id'}}]
    versions={'core':0,'billing':0,'telemetry':0}; previous={}; events=[]


    for i,state in enumerate(states):
        folder=DATA_ROOT/f'event_{i:02d}'; folder.mkdir(exist_ok=True)
        core_rows=[{'row_id':str(j),'amount_raw':r['x']} for j,r in enumerate(state['rows'])]
        billing_rows=[{'row_id':str(j),'counter_raw':r['y']} for j,r in enumerate(state['rows'])]
        telemetry_rows=[{'row_id':'0','metric_raw':str(2 if i>=9 else 1)}]
        sources=[]
        for sid,rows in [('core',core_rows),('billing',billing_rows),('telemetry',telemetry_rows)]:
            h=digest(rows)
            if previous.get(sid)!=h or state['force_version']:
                versions[sid]+=1
            previous[sid]=h
            if sid=='core':
                path=folder/'core.sqlite'
                con=sqlite3.connect(path)
                with con:
                    con.execute('CREATE TABLE IF NOT EXISTS transactions(row_id TEXT PRIMARY KEY, amount_raw TEXT)')
                    con.execute('DELETE FROM transactions')
                    con.executemany('INSERT INTO transactions VALUES (?,?)',[(r['row_id'],r['amount_raw']) for r in rows])
                con.close()
                spec={'id':sid,'kind':'sqlite','path':str(path),'table':'transactions','key':'row_id','version':str(versions[sid])}
            else:
                path=folder/(sid+'.csv')
                with path.open('w',encoding='utf-8',newline='') as stream:
                    w=csv.DictWriter(stream,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
                spec={'id':sid,'kind':'csv','path':str(path),'key':'row_id','version':str(versions[sid])}
            sources.append(spec)
        key={'column':'row_id','kind':'text','revision':'1','concept':'row_id'}
        views={'core':{'source':'core','key':'row_id','fields':{'row_id':key,'amount':decimal_rule('amount_raw',state['scale'],state['revision'])}},
               'billing':{'source':'billing','key':'row_id','fields':{'row_id':key,'counter':decimal_rule('counter_raw',concept='counter')}},
               'telemetry':{'source':'telemetry','key':'row_id','fields':{'row_id':key,'metric':decimal_rule('metric_raw',concept='counter')}}}
        if state['missing']: del views['core']['fields']['amount']
        q=copy.deepcopy(queries)
        if state['query_change']: q[1]['op']='sum'

        events.append({'name':state['name'],'sources':sources,'views':views,'queries':q,'revision':'1'})
    write_json(DATA_ROOT/'events.json',{'events':events,'queries':queries})
    return events,queries


if __name__=='__main__':
    events,queries=build(); print(f'Created {len(events)} events and {len(queries)} queries in {DATA_ROOT}')
