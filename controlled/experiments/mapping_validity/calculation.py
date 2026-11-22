import csv
import sqlite3
import bootstrap
from fabric.connectors import capture_event
from fabric.engine import execute
from fabric.maintenance import maintain
from fabric.common import write_json

QUERIES=[{'id':'sum_a','op':'sum','columns':['core.amount_a']},
         {'id':'project_b','op':'project','columns':['core.amount_b']}]


def events_for_case(case,folder):
    events=[]
    for phase in ['before','after']:
        state=case[phase]; path=folder/(phase+'.sqlite');path.parent.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(path)
        with con:
            con.execute('CREATE TABLE IF NOT EXISTS measurements(row_id TEXT PRIMARY KEY, raw_a TEXT, raw_b TEXT)')
            con.execute('DELETE FROM measurements')
            if len(state['values']['m_a'])!=len(state['values']['m_b']):raise ValueError('Controlled row count mismatch')
            con.executemany('INSERT INTO measurements VALUES (?,?,?)',[(str(i),a,b) for i,(a,b) in enumerate(zip(state['values']['m_a'],state['values']['m_b']))])
        con.close()
        fields={}
        for m,column in [('m_a','a'),('m_b','b')]:
            fields['amount_'+column]={'kind':'decimal','column':'raw_'+column,'scale':state['scales'][m],
                                    'quantum':'0.01','rounding':'HALF_EVEN','null_policy':'None-or-empty',
                                    'error_policy':'fail-query','input_digits':28,'input_adjusted_exponent_limit':28,'revision':'1'}

        events.append({'name':case['id']+':'+phase,'sources':[{'id':'physical','kind':'sqlite','path':str(path),
                        'table':'measurements','key':'row_id','version':'1'}],
                       'views':{'core':{'source':'physical','key':'row_id','fields':fields}},'queries':QUERIES})
    return events


def full_reference(events):

    out=[]
    for event in events:
        snapshot=capture_event(event)
        out.append({q['id']:execute(snapshot,q,[0]) for q in QUERIES})
    return {'old':out[0],'new':out[1],
            'answers_equal':all(out[0][q['id']]['answer']==out[1][q['id']]['answer'] for q in QUERIES),
            'errors_equal':all(out[0][q['id']]['errors']==out[1][q['id']]['errors'] for q in QUERIES)}


def maintain_computation(events,catalog_path):

    records,audits=maintain('C',events,catalog_path,0,0,0)
    return audits[0]['answers'],records[0],audits[0]
