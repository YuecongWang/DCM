import copy
import uuid
import unittest
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fabric.engine import convert, execute, normalized_column, check_equivalence, scope_identity
from fabric.graph import Graph, vocabulary_graph, NAMESPACES
from fabric.catalog import metadata_graph, changed_fields, field_uri
from fabric.sql import parse_sql
from fabric.connectors import capture_source
from build_fixtures import decimal_rule
from config import TEST_WORK_ROOT


def snapshot(values=('1.005','1.015',None)):
    return {'name':'test','sources':{'s':{'content_id':'actual-test-binding','identity':{'version':'1','kind':'test','path':'test'},
             'schema':[{'name':'k','type':'TEXT'},{'name':'x','type':'TEXT'}],
             'rows':[{'k':str(i),'x':v} for i,v in enumerate(values)]}},
            'views':{'v':{'source':'s','key':'k','fields':{'amount':decimal_rule('x'),'key':{'column':'k','kind':'text'}}}}}


class Correctness(unittest.TestCase):
    def test_decimal_null_precision_errors(self):
        r=decimal_rule('x')
        self.assertEqual(convert('1.005',r),'1.00')
        self.assertEqual(convert('1.015',r),'1.02')
        self.assertEqual(convert('-0.0001',r),'0.00')
        self.assertIsNone(convert(None,r)); self.assertIsNone(convert('',r))
        self.assertEqual(convert('1.005000000000000000000001',r),'1.01')
        for v,code in [('NaN','NONFINITE'),('Infinity','NONFINITE'),('bad','INVALID_DECIMAL'),('1E100','RANGE'),(1.005,'TYPE')]:
            with self.assertRaisesRegex(ValueError,code): convert(v,r)

    def test_independent_known_query_answers(self):
        s=snapshot(); q={'id':'q','op':'sum','columns':['v.amount']}
        self.assertEqual(execute(s,q,[0]),{'answer':'2.02','errors':[]})
        q['op']='count'; self.assertEqual(execute(s,q,[0])['answer'],2)
        q['op']='project'; self.assertEqual(execute(s,q,[0])['answer'],[['1.00'],['1.02'],[None]])
        q['op']='sum'; self.assertIsNone(execute(snapshot([]),q,[0])['answer'])

    def test_failed_check_forces_execution_even_identical_errors(self):
        s=snapshot(['bad']); self.assertFalse(check_equivalence(s,s,['v.amount'],[0])['equivalent'])
        self.assertEqual(execute(s,{'id':'q','op':'sum','columns':['v.amount']},[0])['errors'][0]['code'],'INVALID_DECIMAL')

    def test_binding_scope_version_definition_change(self):
        s=snapshot(); original=scope_identity(s,['v.amount'])
        t=copy.deepcopy(s); t['sources']['s']['content_id']='new-content-id'
        self.assertNotEqual(original,scope_identity(t,['v.amount']))
        t=copy.deepcopy(s); t['views']['v']['fields']['amount']['revision']='2'
        self.assertNotEqual(original,scope_identity(t,['v.amount']))
        self.assertNotEqual(original,scope_identity(s,['v.key','v.amount']))

    def test_actual_connector_content_changes_without_version_increment(self):
        TEST_WORK_ROOT.mkdir(parents=True,exist_ok=True)
        p=TEST_WORK_ROOT/(uuid.uuid4().hex+'.csv')
        p.write_text('row_id,x\n0,1.00\n',encoding='utf-8')
        spec={'id':'csv','kind':'csv','path':str(p),'key':'row_id','version':'same'}
        a=capture_source(spec)
        p.write_text('row_id,x\n0,2.00\n',encoding='utf-8')
        b=capture_source(spec)
        self.assertNotEqual(a['content_id'],b['content_id'])
        spec['version']='next'; c=capture_source(spec)
        self.assertNotEqual(b['content_id'],c['content_id'])

    def test_connector_duplicate_keys_rejected(self):
        TEST_WORK_ROOT.mkdir(parents=True,exist_ok=True)
        p=TEST_WORK_ROOT/(uuid.uuid4().hex+'.csv')
        p.write_text('row_id,x\n0,1\n0,2\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'DUPLICATE_KEY'):
            capture_source({'id':'csv','kind':'csv','path':str(p),'key':'row_id','version':'1'})

    def test_equivalent_mapping_requires_real_scan(self):
        a=snapshot(['1.005']); b=snapshot(['100.5'])
        b['views']['v']['fields']['amount']['scale']='0.01'
        scan=[0]; self.assertTrue(check_equivalence(a,b,['v.amount'],scan)['equivalent'])
        self.assertEqual(scan[0],2)
        b['sources']['s']['rows'][0]['x']='100.500000000000000000000001'
        self.assertFalse(check_equivalence(a,b,['v.amount'],[0])['equivalent'])

    def test_printed_equivalence_is_not_typed_query_equivalence(self):
        a=snapshot(['1.00']); b=copy.deepcopy(a)
        b['views']['v']['fields']['amount']={'kind':'text','column':'x'}
        self.assertEqual(normalized_column(a,'v.amount',[0])[0],normalized_column(b,'v.amount',[0])[0])
        self.assertFalse(check_equivalence(a,b,['v.amount'],[0])['equivalent'])
        q={'id':'q','op':'sum','columns':['v.amount']}
        self.assertEqual(execute(a,q,[0])['errors'],[])
        self.assertEqual(execute(b,q,[0])['errors'][0]['code'],'QUERY_NUMERIC_TYPE')

    def test_missing_schema_and_mapping(self):
        s=snapshot(); s['sources']['s']['schema']=[{'name':'k','type':'TEXT'}]
        self.assertEqual(normalized_column(s,'v.amount',[0])[1][0]['code'],'MISSING_COLUMN')
        del s['views']['v']['fields']['amount']
        self.assertEqual(normalized_column(s,'v.amount',[0])[1][0]['code'],'MISSING_MAPPING')

    def test_invalid_definition_even_all_null(self):
        s=snapshot([None]); s['views']['v']['fields']['amount']['rounding']='UNKNOWN'
        self.assertFalse(check_equivalence(s,s,['v.amount'],[0])['equivalent'])
        self.assertEqual(normalized_column(s,'v.amount',[0])[1][0]['code'],'UNSUPPORTED_RULE')

    def test_numeric_query_on_text_rejected(self):
        result=execute(snapshot(),{'id':'q','op':'sum','columns':['v.key']},[0])
        self.assertEqual(result['errors'][0]['code'],'QUERY_NUMERIC_TYPE')

    def test_join_bag_semantics_and_nulls(self):
        s=snapshot(['1','2',None]); s['sources']['t']=copy.deepcopy(s['sources']['s'])
        s['sources']['t']['rows']=[{'k':'0','x':'1'},{'k':'1','x':'1'},{'k':'2','x':None}]
        s['views']['w']=dict(source='t',key='k',fields={'amount':decimal_rule('x')})
        q={'id':'q','op':'project','columns':['v.amount','w.amount'],'join':{'left':'v.amount','right':'w.amount'}}
        self.assertEqual(execute(s,q,[0])['answer'],[['1.00','1.00'],['1.00','1.00']])

    def test_lineage_impact_and_removed_mapping(self):
        s=snapshot(); q={'id':'q','op':'sum','columns':['v.amount']}
        g=metadata_graph(s,[q]); self.assertEqual(g.impacted_queries([field_uri('v','amount')]),['q'])
        t=copy.deepcopy(s); del t['views']['v']['fields']['amount']
        self.assertIn('v.amount',changed_fields(s,t))

    def test_semantics_related_is_not_equivalence(self):
        g=vocabulary_graph()
        for n in ['a','b','c']: g.node(NAMESPACES['demo']+n,['Concept'])
        a,b,c=[NAMESPACES['demo']+n for n in ['a','b','c']]
        g.edge(a,'skos:broader',b); g.edge(b,'skos:broader',c); g.edge(a,'skos:related',c)
        g.infer()
        self.assertIn((a,'skos:broaderTransitive',c),g.edges)
        self.assertIn((b,'skos:narrower',a),g.edges)
        self.assertNotIn((a,'skos:exactMatch',c),g.edges)
        self.assertFalse(g.edges[(a,'skos:broaderTransitive',c)]['asserted'])

    def test_rdfs_type_and_disjoint_validation(self):
        g=Graph()
        for n in ['a','b','x']: g.node('urn:'+n,['Class'])
        g.edge('urn:a','rdfs:subClassOf','urn:b'); g.edge('urn:x','rdf:type','urn:a')
        g.edge('urn:a','owl:disjointWith','urn:b'); g.infer()
        self.assertIn(('urn:x','rdf:type','urn:b'),g.edges)
        self.assertTrue(g.consistency())

    def test_sql_strict_subset(self):
        q=parse_sql('SELECT SUM(v.amount) FROM v WHERE v.amount >= 1.01')
        self.assertEqual(execute(snapshot(),q,[0])['answer'],'1.02')
        with self.assertRaises(ValueError): parse_sql('DROP TABLE v')
        with self.assertRaises(ValueError): parse_sql('SELECT * FROM v')

    def test_declared_semantic_search_finds_logical_field(self):
        s=snapshot(); q={'id':'q','op':'sum','columns':['v.amount']}
        g=metadata_graph(s,[q]); g.infer()
        result=g.semantic_search('交易金额')
        self.assertEqual([n['uri'] for n in result['assets']],[field_uri('v','amount')])
        self.assertTrue(result['evidence'])

    def test_graph_roundtrip(self):
        g=vocabulary_graph(); g.infer()
        folder=TEST_WORK_ROOT
        folder.mkdir(parents=True,exist_ok=True)
        p=folder/(uuid.uuid4().hex+'.sqlite'); g.persist(p)
        self.assertEqual(g.export(),Graph.load(p).export())


if __name__=='__main__': unittest.main()
