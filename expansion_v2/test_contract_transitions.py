from pathlib import Path
from dataclasses import replace
from decimal import Decimal
import json
import benchmark_ted_paired as x
from rdflib import Literal,URIRef
ROOT=Path(__file__).resolve().parent
def main():
    records,mapping=x.load_inputs();synthetic,sm=x.diag.fixture()
    selected=[('synthetic',synthetic,sm)]+[(r['source'],g,mapping) for r,g in records if r['scope_records']>0][:12]
    rows=[]
    scenarios=('initial','authorization_false','authorization_missing','authorization_recovery','period_reversed','period_missing','period_recovery','cap_violated','cap_missing','cap_recovery','link_missing','link_recovery','scope_invalid','scope_recovery','multi_object_change')
    for source,g,m in selected:
        s=x.snapshot(x.diag.clone(g),m);runners={method:x.Runner(method) for method in x.METHODS};links=list(g.triples((None,x.e.EPO.hasAwardedValue,None)))
        for ei,event in enumerate(scenarios):
            if event=='authorization_false':s['authorized']=False
            elif event=='authorization_missing':s['authorized']=None
            elif event=='authorization_recovery':s['authorized']=True
            elif event=='period_reversed':s['period']=[10,0]
            elif event=='period_missing':s['period']=None
            elif event=='period_recovery':s['period']=[0,10]
            elif event=='cap_violated':s['cap']=str(min(Decimal(str(a)) for a in g.objects(None,x.e.EPO.hasAmountValue))-1)
            elif event=='cap_missing':s['cap']=None
            elif event=='cap_recovery':s['cap']='1E30'
            elif event=='link_missing':s['graph'].remove((None,x.e.EPO.hasAwardedValue,None))
            elif event=='link_recovery':
                for t in links:s['graph'].add(t)
            elif event=='scope_invalid':s['declared_scope']='estimated_amount'
            elif event=='scope_recovery':s['declared_scope']='awarded_amount'
            elif event=='multi_object_change':s['authorized']=False;s['period']=[10,0];s['cap']='-1'
            expected=x.expected_states(s);qa={q:x.oracle(q,s['graph']) for q in x.QUERIES}
            for method,runner in runners.items():
                r=runner.step(s,x.QUERIES);assert r['premise_states']==expected and r['answers']==qa
                assert r['deliver']==all(v=='VALID' for v in expected.values())
                rows.append(dict(source=source,event=event,method=method,expected_states=expected,passed=True,**r))

    g,m=x.diag.fixture();n=URIRef('urn:test:150');g.add((n,x.e.EPO.hasAmountValue,Literal('150',datatype=x.e.DECIMAL)))
    g.add((n,x.e.EPO.hasCurrency,URIRef(x.e.CUR+'EUR')))
    s=x.snapshot(g,m);rr={method:x.Runner(method) for method in x.METHODS};old=x.QUERIES['amount_filter']
    for r in rr.values():r.step(s,['amount_filter'])
    try:
        x.QUERIES['amount_filter']=old.replace('>= 100','>= 200')
        for method,r in rr.items():
            actual=r.step(s,['amount_filter']);assert actual['executed']['amount_filter'] and actual['answers']['amount_filter']==[]
            rows.append(dict(source='synthetic-query-revision',event='query_text_revision',method=method,expected_answer=[],passed=True,**actual))
    finally:x.QUERIES['amount_filter']=old

    s=x.snapshot(g,m);rr={method:x.Runner(method) for method in x.METHODS}
    for r in rr.values():r.step(s,['currency_sum'])
    for method,r in rr.items():
        r.ps=[replace(p,revision='scope-v2',evaluator=lambda s:x.State.VALID if s['declared_scope']=='estimated_amount' else x.State.INVALID) if p.key=='scope' else p for p in r.ps]
        actual=r.step(s,['currency_sum']);assert actual['premise_states']['scope']=='INVALID' and not actual['deliver'] and actual['premise_checks']>=1
        rows.append(dict(source='synthetic-rule-revision',event='rule_semantics_changed',method=method,expected_scope='INVALID',passed=True,**actual))
    x.save(ROOT/'test_contract_transitions.json',dict(records=rows,passed=True,sources=len(selected),steps=len(rows),note='12 real-derived source graphs plus synthetic fixture; authorization/period/cap/scope are controlled application facts; query/rule revisions independently asserted'))
    print('Contract transitions:',len(rows),'passed')
if __name__=='__main__':main()
