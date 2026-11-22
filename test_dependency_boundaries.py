import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import benchmark_ted_three_way as dcm
from rdflib import Graph, Literal, URIRef, BNode
from rdflib.compare import to_isomorphic
from audit_ted_source_identity import safe_xml, csv_write, sha

e=dcm.existing
PREFIX='PREFIX epo: <http://data.europa.eu/a4g/ontology#>\n'
QUERIES={
    'currency_sum':e.QUERY,
    'node_details':PREFIX+'SELECT ?node ?amount ?currency WHERE { ?node epo:hasAmountValue ?amount; epo:hasCurrency ?currency . }',
    'currency_presence_count':PREFIX+'SELECT ?node (COUNT(?currency) AS ?count) WHERE { ?node epo:hasAmountValue ?amount . OPTIONAL { ?node epo:hasCurrency ?currency . } } GROUP BY ?node',
    'linked_objects':PREFIX+'SELECT ?outcome ?node ?amount ?currency WHERE { ?outcome epo:hasAwardedValue ?node . ?node epo:hasAmountValue ?amount; epo:hasCurrency ?currency . }',
}
DEPENDENCIES={
    'currency_sum':['amount subject identity','hasAmountValue','hasCurrency'],
    'node_details':['amount subject identity','hasAmountValue','hasCurrency'],
    'currency_presence_count':['amount subject identity','hasAmountValue','hasCurrency (including absence)'],
    'linked_objects':['outcome identity','hasAwardedValue','amount subject identity','hasAmountValue','hasCurrency'],
}


def normalized_decimal(value):
    number=Decimal(str(value))
    if not number.is_finite():raise ValueError('Non-finite Decimal')
    return str(number.normalize())


def execute_query(name,graph):

    result=[]
    for row in graph.query(QUERIES[name]):
        values=row.asdict()
        if name=='currency_sum':result.append((str(values['currency']),normalized_decimal(values['total'])))
        elif name=='node_details':result.append((values['node'].n3(),normalized_decimal(values['amount']),values['currency'].n3()))
        elif name=='currency_presence_count':result.append((values['node'].n3(),int(values['count'])))
        else:result.append((values['outcome'].n3(),values['node'].n3(),normalized_decimal(values['amount']),values['currency'].n3()))
    return sorted(result)


def query_oracle(name,graph):

    nodes=set(graph.subjects(e.EPO.hasAmountValue,None));result=[];sums=defaultdict(Decimal)
    for node in nodes:
        amounts=list(graph.objects(node,e.EPO.hasAmountValue))
        currencies=list(graph.objects(node,e.EPO.hasCurrency))
        if name=='currency_presence_count':
            result.append((node.n3(),len(amounts)*len(currencies)))
            continue
        for amount in amounts:
            for currency in currencies:
                if name=='currency_sum':sums[str(currency)]+=Decimal(str(amount))
                elif name=='node_details':result.append((node.n3(),normalized_decimal(amount),currency.n3()))
                else:
                    for outcome in graph.subjects(e.EPO.hasAwardedValue,node):
                        result.append((outcome.n3(),node.n3(),normalized_decimal(amount),currency.n3()))
    if name=='currency_sum':result=[(currency,normalized_decimal(amount)) for currency,amount in sums.items()]
    return sorted(result)


def business_oracle(graph,scope,mapping_state,required_link=False):

    nodes=set(graph.subjects(e.EPO.hasAmountValue,None))
    invalid=mapping_state=='INVALID' or (scope is not None and scope!='awarded_amount')
    unknown=mapping_state=='UNKNOWN' or scope is None or not nodes
    for node in nodes:
        amounts=list(graph.objects(node,e.EPO.hasAmountValue));currencies=list(graph.objects(node,e.EPO.hasCurrency))
        if len(amounts)!=1 or len(currencies)!=1:
            invalid=True;continue
        try:
            value=Decimal(str(amounts[0]));suffix=str(currencies[0])[len(e.CUR):]
            if not isinstance(amounts[0],Literal) or amounts[0].datatype!=e.DECIMAL or not value.is_finite():invalid=True
            if not isinstance(currencies[0],URIRef) or not str(currencies[0]).startswith(e.CUR) or len(suffix)!=3 or not suffix.isascii() or not suffix.isalpha() or not suffix.isupper():invalid=True
        except Exception:invalid=True
        if required_link and not any(graph.subjects(e.EPO.hasAwardedValue,node)):unknown=True
    return 'INVALID' if invalid else 'UNKNOWN' if unknown else 'VALID'


def clone(graph):
    result=Graph()
    for triple in graph:result.add(triple)
    return result


def fixture():
    mapping=Graph()
    for name in ('LotAwardOutcomeAwardedMonetaryValue','LotAwardOutcomeAwardedMonetaryValueModification'):
        pom,om,cpom=BNode(),BNode(),BNode()
        for triple in [(e.TM[name],e.RR.predicateObjectMap,pom),(pom,e.RR.predicate,e.EPO.hasAmountValue),
                       (pom,e.RR.objectMap,om),(om,e.RR.datatype,e.DECIMAL),
                       (e.TM[name],e.RR.predicateObjectMap,cpom),(cpom,e.RR.predicate,e.EPO.hasCurrency)]:mapping.add(triple)
    graph=Graph()
    for identifier,amount,currency in [('one','10.1','EUR'),('two','2.30','USD')]:
        node=URIRef('urn:boundary:'+identifier)
        graph.add((node,e.EPO.hasAmountValue,Literal(Decimal(amount),datatype=e.DECIMAL)))
        graph.add((node,e.EPO.hasCurrency,URIRef(e.CUR+currency)))
        graph.add((URIRef('urn:boundary:outcome:'+identifier),e.EPO.hasAwardedValue,node))
    return graph,mapping


SCENARIOS=('unrelated_annotation','amount_change','currency_change','delete_all_amounts','delete_currency',
           'scope_invalid','scope_missing','scope_recovery','mapping_proven_equivalent',
           'mapping_equivalence_unknown','mapping_contract_invalid','mapping_version_unknown',
           'business_dependencies_marked_incomplete','query_dependencies_marked_incomplete',
           'hidden_business_dependency_falsely_marked_complete','evidence_rule_revision',
           'delete_target_link')


def configure_event(scenario,graph,mapping):
    graph,mapping=clone(graph),clone(mapping)
    nodes=sorted(set(graph.subjects(e.EPO.hasAmountValue,None)),key=str)
    node=nodes[0];scope='awarded_amount';version='map-v1';map_state='VALID'
    if scenario=='unrelated_annotation':graph.add((URIRef('urn:annotation'),URIRef('urn:comment'),Literal('unrelated')))
    elif scenario=='amount_change':graph.set((node,e.EPO.hasAmountValue,Literal(Decimal(str(next(graph.objects(node,e.EPO.hasAmountValue))))+1,datatype=e.DECIMAL)))
    elif scenario=='currency_change':graph.set((node,e.EPO.hasCurrency,URIRef(e.CUR+'GBP')))
    elif scenario=='delete_all_amounts':graph.remove((None,e.EPO.hasAmountValue,None))
    elif scenario=='delete_currency':graph.remove((node,e.EPO.hasCurrency,None))
    elif scenario in ('scope_invalid','business_dependencies_marked_incomplete','hidden_business_dependency_falsely_marked_complete'):scope='estimated_amount'
    elif scenario=='scope_missing':scope=None
    elif scenario.startswith('mapping_'):
        version='map-v2'
        if scenario=='mapping_version_unknown':version=None;map_state='UNKNOWN'
        elif scenario=='mapping_contract_invalid':
            mapping.remove((None,e.RR.datatype,e.DECIMAL));map_state='INVALID'
        else:


            mapping.add((URIRef('urn:boundary:mapping'),URIRef('urn:comment'),Literal('annotation only')))
    elif scenario=='delete_target_link':graph.remove((None,e.EPO.hasAwardedValue,node))
    return {'graph':graph,'mapping':mapping,'declared_scope':scope},version,map_state


def run_probe(name,scenario,graph,mapping,fixture_type,source):
    rows=[]
    before_scope=None if scenario=='scope_recovery' else 'awarded_amount'
    before={'graph':clone(graph),'mapping':clone(mapping),'declared_scope':before_scope}
    after,version,mapping_state=configure_event(scenario,graph,mapping)
    if scenario=='query_dependencies_marked_incomplete':after['query_dependencies_complete']=False


    runners={method:dcm.Runner(method,'map-v1') for method in dcm.METHODS}
    for runner in runners.values():
        if scenario in ('business_dependencies_marked_incomplete','hidden_business_dependency_falsely_marked_complete'):
            complete=scenario.startswith('hidden_')
            runner.specs=[replace(p,dependencies=('amount',),dependencies_complete=complete) if p.key=='scope' else p for p in runner.specs]
    calls=Counter()
    active=[None]
    def query(graph):
        calls[active[0]]+=1
        return execute_query(name,graph)
    with patch.object(e,'QUERY',QUERIES[name]),patch.object(e,'query_answer',query):
        for event,snapshot,expected_mapping,expected_version in [('initial',before,'VALID','map-v1'),(scenario,after,mapping_state,version)]:



            mapping_digest=e.signature(sorted((s.n3(),p.n3(),o.n3()) for s,p,o in snapshot['mapping']))
            for method,runner in runners.items():
                runner.mapping_hash=expected_version
                if scenario=='evidence_rule_revision' and event!='initial':
                    runner.specs=[replace(p,revision='scope-v2') if p.key=='scope' else p for p in runner.specs]
                expected_answer=query_oracle(name,snapshot['graph'])
                required_link=name=='linked_objects' and scenario=='delete_target_link'
                expected_state=business_oracle(snapshot['graph'],snapshot['declared_scope'],expected_mapping,required_link)
                expected_execute=method=='B0' or event=='initial' or scenario in ('amount_change','currency_change','delete_all_amounts','delete_currency','mapping_equivalence_unknown','mapping_contract_invalid','mapping_version_unknown','query_dependencies_marked_incomplete') or (name=='linked_objects' and scenario=='delete_target_link')
                active[0]=method;old_calls=calls[method]
                result=runner.step(snapshot)
                failures=[]
                if result['answer']!=expected_answer:failures.append('query_answer_differs_from_independent_oracle')
                if result['business_state']!=expected_state:failures.append('business_state_differs_from_test_contract')
                if result['delivery_decision']!=('DELIVER' if expected_state=='VALID' else 'BLOCK'):failures.append('delivery_differs_from_test_contract')
                if result['query_executed']!=expected_execute:failures.append('query_execution_policy_not_satisfied')
                if calls[method]-old_calls!=int(result['query_executed']):failures.append('reported_query_execution_not_actual')
                if scenario=='business_dependencies_marked_incomplete' and 'scope' in result['evidence_hit_premises']:failures.append('incomplete_business_evidence_reused')
                if scenario=='evidence_rule_revision' and event!='initial' and 'scope' in result['evidence_hit_premises']:failures.append('stale_rule_evidence_reused')
                rows.append({'fixture_type':fixture_type,'source':source,'scenario':scenario,'event':event,'query_template':name,
                    'query_dependencies':DEPENDENCIES[name],'method':method,'input_graph_digest':str(to_isomorphic(snapshot['graph']).graph_digest()),
                    'mapping_version':expected_version,'mapping_snapshot_sha256':mapping_digest,
                    'mapping_expected_contract_state':expected_mapping,
                    'expected_answer':expected_answer,'actual_answer':result['answer'],
                    'expected_business_state':expected_state,'actual_business_state':result['business_state'],
                    'expected_query_executed':expected_execute,'query_executed':result['query_executed'],'query_reused':result['query_reused'],
                    'actual_sparql_calls':calls[method]-old_calls,'premise_checks':result['premise_checks'],'evidence_hits':result['evidence_hits'],
                    'evidence_hit_premises':result['evidence_hit_premises'],'expected_delivery':'DELIVER' if expected_state=='VALID' else 'BLOCK',
                    'actual_delivery':result['delivery_decision'],'safety_requirement_passed':not failures,'failure_reasons':failures,
                    'production_support':'Frozen-mapping benchmark interface; alternative queries/versions are isolated test injection, not a production API.',
                    'dependency_contract':'false completeness declaration (unobservable hidden scope dependency)' if scenario.startswith('hidden_') else 'explicit incomplete marker' if 'incomplete' in scenario else 'declared fixture contract'})
    return rows


def suite_rows(real_fixture=None):
    graph,mapping=fixture();rows=[]
    for name in QUERIES:
        for scenario in SCENARIOS:
            rows.extend(run_probe(name,scenario,graph,mapping,'synthetic RDF fixture','two-currency-synthetic'))
    if real_fixture:
        graph,mapping,source=real_fixture
        for name in QUERIES:
            for scenario in ('unrelated_annotation','amount_change','currency_change','delete_all_amounts','delete_currency','scope_invalid','scope_missing','delete_target_link'):
                rows.extend(run_probe(name,scenario,graph,mapping,'controlled copy of real TED-derived RDF',source))
    return rows


def summarize(rows):
    failures=[r for r in rows if not r['safety_requirement_passed']]
    paired=defaultdict(dict)
    for r in rows:paired[(r['fixture_type'],r['source'],r['query_template'],r['scenario'],r['event'])][r['method']]=r
    differences=[]
    for key,group in paired.items():
        if any(group['B1'][field]!=group['B2'][field] for field in ('actual_answer','actual_business_state','actual_delivery')):
            differences.append(list(key))
    return {'record_count':len(rows),'query_templates':list(QUERIES),'fixture_types':dict(Counter(r['fixture_type'] for r in rows)),
        'safety_pass_records':len(rows)-len(failures),'safety_fail_records':len(failures),
        'safety_acceptance_passed':not failures,'failure_counts_by_scenario':dict(Counter(r['scenario'] for r in failures)),
        'B1_B2_disagreements':differences,'full_benchmark_run':False,
        'interpretation':'Expected diagnostic failures retained; passing unittest assertions do NOT make failed safety requirements pass.'}


class BoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.rows=suite_rows()

    def test_three_core_templates_and_standard_events(self):
        core=[r for r in self.rows if r['query_template']!='linked_objects' and r['scenario'] in ('unrelated_annotation','amount_change','currency_change','delete_all_amounts','delete_currency','scope_invalid','scope_missing','scope_recovery','mapping_proven_equivalent','business_dependencies_marked_incomplete','evidence_rule_revision','delete_target_link')]
        self.assertTrue(core)
        self.assertTrue(all(r['safety_requirement_passed'] for r in core),[r for r in core if not r['safety_requirement_passed']])

    def test_every_B0_query_was_actually_executed(self):
        for r in self.rows:
            if r['method']=='B0':self.assertTrue(r['query_executed']);self.assertEqual(r['actual_sparql_calls'],1)

    def test_observed_mapping_boundary_is_retained(self):
        for scenario in ('mapping_equivalence_unknown','mapping_contract_invalid','mapping_version_unknown'):
            selected=[r for r in self.rows if r['scenario']==scenario and r['event']!='initial' and r['method'] in ('B1','B2')]
            self.assertTrue(all('query_execution_policy_not_satisfied' in r['failure_reasons'] for r in selected))

    def test_observed_unsupported_query_completeness_marker(self):
        selected=[r for r in self.rows if r['scenario']=='query_dependencies_marked_incomplete' and r['event']!='initial' and r['method'] in ('B1','B2')]
        self.assertTrue(selected)
        self.assertTrue(all(not r['safety_requirement_passed'] and r['query_reused'] for r in selected))

    def test_hidden_false_complete_business_declaration_reproduces_unsafe_hit(self):
        selected=[r for r in self.rows if r['scenario'].startswith('hidden_') and r['event']!='initial' and r['method']=='B2']
        self.assertTrue(all(r['actual_business_state']=='VALID' and r['expected_business_state']=='INVALID' and 'scope' in r['evidence_hit_premises'] for r in selected))

    def test_extended_link_dependency_gap_is_not_hidden(self):
        selected=[r for r in self.rows if r['query_template']=='linked_objects' and r['scenario']=='delete_target_link' and r['event']!='initial']
        self.assertTrue(all(not r['safety_requirement_passed'] for r in selected))
        self.assertTrue(all(r['actual_answer']!=r['expected_answer'] for r in selected if r['method'] in ('B1','B2')))

    def test_XML_rejects_DTD_and_external_entities(self):
        for raw in (b'<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///never-read">]><x>&leak;</x>',
                    '<!DOCTYPE x SYSTEM "https://never-contact"><x/>'.encode('utf-16')):
            with self.assertRaises(ValueError):safe_xml(raw)
        self.assertEqual(safe_xml(b'<x id="1"/>').get('id'),'1')

    def test_summary_does_not_relabel_failures(self):
        summary=summarize(self.rows)
        self.assertFalse(summary['safety_acceptance_passed'])
        self.assertGreater(summary['safety_fail_records'],0)
        self.assertEqual(summary['record_count'],len(QUERIES)*len(SCENARIOS)*2*3)


def run(out,full_manifest):

    for name in ('boundary_test_results.json','boundary_test_results.csv','boundary_test_report.md'):
        if (out/name).exists():raise FileExistsError('Refusing existing boundary output; use a new --out')
    m=json.loads(full_manifest.read_text(encoding='utf-8'));adapter=Path(m['adapter'])
    audit=json.loads((adapter/'summary.json').read_text(encoding='utf-8'))
    record=next(r for r in audit['records'] if r['source'] in m['sources'] and r['scope_records']>0)
    path=adapter/'notices'/hashlib.sha256(record['source'].encode()).hexdigest()[:16]/'output.ttl'
    if sha(path)!=m['input_rdf_sha256'][record['source']]:raise ValueError('TED RDF hash mismatch')
    mapping_path=adapter/'adapted_mapping.ttl'
    if sha(mapping_path)!=m['mapping_sha256']:raise ValueError('Mapping hash mismatch')
    graph=Graph().parse(path,format='turtle');mapping=Graph().parse(mapping_path,format='turtle')

    rows=suite_rows((graph,mapping,record['source']));summary=summarize(rows)
    value={'summary':summary,'queries':QUERIES,'query_dependencies':DEPENDENCIES,
           'mapping_snapshot_sha256_interpretation':'Sorted RDF term N3 fingerprint within the run; includes blank node IDs, not a cross-parse semantic equivalence certificate.',
           'records':rows,'test_script_sha256':sha(Path(__file__)),'full_manifest_sha256':sha(full_manifest),
           'input_rdf_sha256':sha(path),'mapping_sha256':sha(mapping_path),
           'production_modules_sha256':{str(p):sha(p) for p in (Path(dcm.__file__),Path(e.__file__),Path(sys.modules['dcm_scheduler'].__file__))}}
    (out/'boundary_test_results.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    csv_write(out/'boundary_test_results.csv',rows)
    lines=['# DCM 机制边界测试','',
        f"共 {len(rows)} 条实际策略记录；通过安全契约 {summary['safety_pass_records']} 条，未通过 {summary['safety_fail_records']} 条。安全验收：{summary['safety_acceptance_passed']}。",
        '', 'unittest 的通过仅表示预期行为（包含负面诊断）成功复现，不代表所有安全要求通过。全部失败记录保留在 JSON/CSV。未修改基准实现，未运行全量实验。', '',
        '## 实际覆盖','', '4 个查询模板：币种聚合、按金额对象明细、按金额对象统计币种存在数量、经 hasAwardedValue 关联的对象明细。前3个依赖金额/币种及对象身份；第4个额外依赖关联边。每个查询使用真实 SPARQL，独立 oracle 直接遍历 RDF 并用 Decimal 规范化，未使用调度器输出生成期望。', '',
        '合成 fixture 含两个对象、EUR与USD两种币种；真实部分取 manifest 中首个有金额公告的 RDF 内存副本，字段已检查实际存在。真实部分仅做8种受控扰动，不是自然历史查询实验。所有图修改只在内存副本，未重物化映射。', '',
        '## 当前实现的边界与失败','',
        '1. 主脚本只承诺冻结 mapping。query footprint 不含 mapping 版本；注入 mapping 版本变化时，B1/B2 可重查业务 mapping 前提，但不自动重执行查询。未知等价、契约失效、版本缺失均未满足新增重执行政策，即使查询答案碰巧相同也保留为失败。',
        '2. query_dependencies_complete 不是现有接口支持的标记；注入 False 不会禁止复用。当前查询证书只由固定投影和固定查询构成，没有通用依赖完整性检查。',
        '3. 第4个查询所需 hasAwardedValue 关联边不在当前 footprint；删除关联时 B1/B2 会复用过期答案。原四项业务前提也不包含必须存在该关联，因而不能声称已实现这个扩展契约。B0 虽重新查询正确，但扩展业务前提仍未实现。',
        '4. dependencies_complete=False 会使业务前提重新验证，未知版本会置 UNKNOWN 并阻断；但外部错误地声称完整且漏报真实依赖时，B2 不能自动观察隐藏依赖，可复用旧 VALID 结论并错误交付。测试明确复现，而非声称可检测任意隐藏依赖。', '',
        '## 映射等价与版本','', 'mapping_proven_equivalent 仅为受控内存 annotation-only 修改：对这些固定查询及当前四项映射检查，所读三元组不变。其“等价”不扩展为任意RML等价证明。mapping_contract_invalid 在副本删除明确的 datatype 声明，独立契约写定 INVALID；版本无法确认写定 UNKNOWN。动态在线映射演化仍未实现。', '',
        '## 计数','', json.dumps(summary,ensure_ascii=False,indent=2), '',
        '## 限制','', 'scope 为显式测试元数据，human_reviewed=false。没有任意 SPARQL 依赖分析、在线 mapping evolution 或完整 ePO/SHACL 覆盖证明。负面场景超出冻结基准的前提，不能据此改写原全量记录的结论；也不能用原基准0错误掩盖这些能力缺口。B1/B2 查询路径与输入完全相同，只切换业务 Scheduler.incremental。', '',
        '如要满足新增安全政策，需要显式的查询依赖完整性证书、mapping版本/等价证书及新查询依赖投影，并扩充相应业务前提。此任务优先独立测试，未悄悄改变主脚本或宣称修复。', '',
        '## 逐场景详情','', '每条记录（包括初始化）均在 boundary_test_results.csv/json 中保存期望/实际答案、业务状态、执行/复用、证据检查/命中、交付、版本和失败原因。下面为按场景聚合的失败记录数量：', '']
    lines += [f'- {scenario}: {count}' for scenario,count in summary['failure_counts_by_scenario'].items()]
    (out/'boundary_test_report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    return summary


if __name__=='__main__':
    if '--out' in sys.argv:
        ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);ap.add_argument('--manifest',type=Path,required=True)
        args=ap.parse_args()
        summary=run(args.out,args.manifest)
        raise SystemExit(0 if summary['safety_acceptance_passed'] else 1)
    else:unittest.main(verbosity=2)
