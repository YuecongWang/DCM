import argparse
import copy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rdflib import BNode, Graph, Literal, URIRef
import benchmark_ted_three_way as dcm
from dcm_scheduler import Scheduler, Premise, State


def fixture():
    e = dcm.existing
    mapping = Graph()
    for name in ('LotAwardOutcomeAwardedMonetaryValue', 'LotAwardOutcomeAwardedMonetaryValueModification'):
        amount_pom, currency_pom, om = BNode(), BNode(), BNode()
        mapping.add((e.TM[name], e.RR.predicateObjectMap, amount_pom))
        mapping.add((amount_pom, e.RR.predicate, e.EPO.hasAmountValue))
        mapping.add((amount_pom, e.RR.objectMap, om))
        mapping.add((om, e.RR.datatype, e.DECIMAL))
        mapping.add((e.TM[name], e.RR.predicateObjectMap, currency_pom))
        mapping.add((currency_pom, e.RR.predicate, e.EPO.hasCurrency))
    graph = Graph()
    graph.add((URIRef('urn:test:amount'), e.EPO.hasAmountValue, Literal(Decimal('10.10'), datatype=e.DECIMAL)))
    graph.add((URIRef('urn:test:amount'), e.EPO.hasCurrency, URIRef(e.CUR+'EUR')))
    return graph, mapping


class ThreeWayTests(unittest.TestCase):
    def setUp(self):
        self.graph, self.mapping = fixture()
        self.rows = dcm.run_case(self.graph, self.mapping, 'frozen', 'synthetic', 0, 0, 'input')
        self.index = {(r['method'], r['event']): r for r in self.rows}

    def test_initial_and_unrelated_actual_query_calls(self):
        snapshot = {'graph': self.graph, 'mapping': self.mapping, 'declared_scope': 'awarded_amount'}
        for method, calls in [('B0', 2), ('B1', 1), ('B2', 1)]:
            with self.subTest(method=method):
                runner = dcm.Runner(method, 'frozen')
                with patch.object(dcm.existing, 'query_answer', wraps=dcm.existing.query_answer) as query:
                    first = runner.step(snapshot)
                    second = runner.step(snapshot)
                self.assertEqual(query.call_count, calls)
                self.assertTrue(first['query_executed'])
                self.assertEqual(second['query_reused'], method != 'B0')
                self.assertEqual(self.index[method, 'unrelated_annotation']['query_executed'], method == 'B0')

    def test_legal_amount_update_executes_and_matches_oracle(self):
        for method in dcm.METHODS:
            row = self.index[method, 'legal_amount_update']
            self.assertTrue(row['query_executed'])
            self.assertTrue(row['answer_matches_oracle'])
            self.assertEqual(row['answer'], [(dcm.existing.CUR+'EUR', '11.1')])

    def test_invalid_scope_same_answer_blocks(self):
        for method in dcm.METHODS:
            row = self.index[method, 'business_scope_invalid']
            self.assertEqual(row['answer'], self.index[method, 'legal_amount_update']['answer'])
            self.assertEqual(row['business_state'], 'INVALID')
            self.assertEqual(row['delivery_decision'], 'BLOCK')

    def test_unknown_scope_blocks_and_recovery_rechecks(self):
        for method in dcm.METHODS:
            self.assertEqual(self.index[method, 'scope_unknown']['business_state'], 'UNKNOWN')
            self.assertEqual(self.index[method, 'scope_unknown']['delivery_decision'], 'BLOCK')
            for event in ('scope_recovery', 'scope_known_again'):
                row = self.index[method, event]
                self.assertEqual(row['business_state'], 'VALID')
                self.assertEqual(row['delivery_decision'], 'DELIVER')
                self.assertGreaterEqual(row['premise_checks'], 1)
                self.assertNotIn('scope', row['evidence_hit_premises'])

    def test_all_oracles_agreement_and_accounting(self):
        validation = dcm.validate_records(self.rows, ['synthetic'], 1)
        self.assertTrue(validation['passed'], validation)
        self.assertEqual(validation['actual_rows'], 21)
        summary = dcm.summarize(self.rows, ['synthetic'], 1, 1, validation)
        self.assertTrue(summary['B1_B2_state_and_delivery_agree'])
        for m in summary['methods']:
            self.assertEqual(m['query_executed_total']+m['query_reused_total'], 7)
            self.assertTrue(m['all_correctness_checks_passed'])
            self.assertEqual(m['wrong_delivery_total'], 0)
        self.assertEqual(summary['methods'][0]['per_repeat'][0]['query_executed'], 6)

    def test_row_coverage_rejects_missing_duplicate_and_unsafe_rows(self):
        for rows in [self.rows[:-1], self.rows+[self.rows[0]]]:
            self.assertFalse(dcm.validate_records(rows, ['synthetic'], 1)['passed'])
        rows = copy.deepcopy(self.rows)
        next(r for r in rows if r['business_state'] == 'INVALID')['delivery_decision'] = 'DELIVER'
        self.assertFalse(dcm.validate_records(rows, ['synthetic'], 1)['passed'])

    def test_no_cross_source_or_repeat_caches(self):
        rows = []
        for source in ('synthetic', 'second'):
            for repeat in range(2):
                rows.extend(dcm.run_case(self.graph, self.mapping, 'frozen', source, repeat, 0, 'input'))
        validation = dcm.validate_records(rows, ['synthetic', 'second'], 2)
        self.assertTrue(validation['passed'], validation)
        for row in rows:
            if row['event'] == 'initial':
                self.assertTrue(row['query_executed'])
                self.assertEqual(row['evidence_hits'], 0)
                self.assertEqual(row['premise_checks'], 4)

    def test_empty_amounts_stay_unknown_on_recovery(self):
        rows = dcm.run_case(Graph(), self.mapping, 'frozen', 'empty', 0, 0, 'input')
        validation = dcm.validate_records(rows, ['empty'], 1)
        self.assertTrue(validation['passed'], validation)
        for r in rows:
            self.assertEqual(r['business_state'], 'INVALID' if r['event']=='business_scope_invalid' else 'UNKNOWN')
            self.assertEqual(r['delivery_decision'], 'BLOCK')
            if r['method'] != 'B0' and r['event'] == 'legal_amount_update':
                self.assertTrue(r['query_reused'])

    def test_currency_and_subject_identity_changes_force_query(self):
        e = dcm.existing
        for method in ('B1', 'B2'):
            graph, mapping = fixture()
            snapshot = {'graph': graph, 'mapping': mapping, 'declared_scope': 'awarded_amount'}
            runner = dcm.Runner(method, 'frozen')
            runner.step(snapshot)
            graph.set((URIRef('urn:test:amount'), e.EPO.hasCurrency, URIRef(e.CUR+'USD')))
            changed = runner.step(snapshot)
            self.assertTrue(changed['query_executed'])
            self.assertEqual(changed['answer'], e.independent_oracle(snapshot)['answer'])
            for s,p,o in list(graph):
                graph.remove((s,p,o)); graph.add((URIRef('urn:test:new'),p,o))
            self.assertTrue(runner.step(snapshot)['query_executed'])
            graph.remove((None, e.EPO.hasAmountValue, None))
            removed = runner.step(snapshot)
            self.assertTrue(removed['query_executed'])
            self.assertEqual(removed['answer'], [])

    def test_evidence_signature_and_premise_identity(self):
        p = Premise('p', 'v1', ('d',), lambda s: State.VALID, True)
        runner = Scheduler(True)
        runner.evaluate([p], {}, {'d':'1'})
        old = dict(runner.cache)
        self.assertEqual(dcm.matching_evidence(old, [p], {'d':'1'}), ['p'])
        for changed, versions in [
            (Premise('other', 'v1', ('d',), p.evaluator, True), {'d':'1'}),
            (Premise('p', 'v2', ('d',), p.evaluator, True), {'d':'1'}),
            (p, {'d':'2'}), (p, {'d':None}), (p, {}),
            (Premise('p', 'v1', ('d',), p.evaluator, False), {'d':'1'})]:
            self.assertEqual(dcm.matching_evidence(old, [changed], versions), [])
            fresh = Scheduler(True); fresh.cache = dict(old)
            self.assertEqual(fresh.evaluate([changed], {}, versions)['evidence_hits'], 0)

    def test_unknown_and_incomplete_evidence_never_approve_by_reuse(self):
        for complete in (True, False):
            p = Premise('p', 'v1', ('d',), lambda s: State.UNKNOWN, complete)
            runner = Scheduler(True)
            runner.evaluate([p], {}, {'d':'1'})
            result = runner.evaluate([p], {}, {'d':'1'})
            self.assertEqual(result['premise_checks'], 1)
            self.assertEqual(result['evidence_hits'], 0)
            self.assertFalse(result['deliver'])
        p = Premise('p', 'v1', ('d',), lambda s: State.VALID, False)
        runner = Scheduler(True); runner.evaluate([p], {}, {'d':'1'})
        self.assertEqual(runner.evaluate([p], {}, {'d':'1'})['premise_checks'], 1)

    def test_output_refusal_and_safe_defaults(self):
        defaults = dcm.parser().parse_args([])
        self.assertFalse(defaults.full)

        (Path.cwd() / 'runtime').mkdir(exist_ok=True)
        temp = tempfile.mkdtemp(prefix='dcm_output_guard_', dir=Path.cwd() / 'runtime')
        destination = Path(temp)/'old'
        destination.mkdir()
        marker = destination/'keep.txt'; marker.write_text('unchanged')
        defaults.out = destination
        with self.assertRaisesRegex(FileExistsError, 'new --out'):
            dcm.execute(defaults)
        self.assertEqual(marker.read_text(), 'unchanged')
        for limit in (0, 128):
            args = argparse.Namespace(adapter=Path(temp)/'missing', out=Path(temp)/'new', limit=limit, repeats=1, full=False)
            with self.assertRaisesRegex(ValueError, '--full'):
                dcm.execute(args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
