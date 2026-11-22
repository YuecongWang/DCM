import copy
import unittest
from freeze_cases import base_state,fact,target
from semantic import check_premise,aggregate,delivery
from scheduling import BusinessMaintenance


def assessment(s):
    return aggregate({p['id']:check_premise(s,p) for p in s['premises']},s)


class SeparationUnits(unittest.TestCase):
    def test_g0_computation_success_delivers_without_business_assessment(self):
        from benchmark_controlled_cases import computation_only_delivery
        good={'q':{'answer':'1.00','errors':[]}}
        self.assertEqual(computation_only_delivery(good),
                         {'decision':'COMPUTATION_ONLY_DELIVERED','approved':True})

    def test_g0_any_computation_error_blocks_delivery(self):
        from benchmark_controlled_cases import computation_only_delivery
        mixed={'good':{'answer':'1.00','errors':[]},
               'bad':{'answer':None,'errors':[{'code':'INVALID_DECIMAL'}]}}
        self.assertEqual(computation_only_delivery(mixed),
                         {'decision':'COMPUTATION_FAILED','approved':False})

    def test_valid_requires_four_current_premises(self):
        self.assertEqual(assessment(base_state())['state'],'VALID')

    def test_missing_is_unknown(self):
        s=base_state();del s['resources']['fact:m_a:object']
        result=assessment(s)
        self.assertEqual(result['state'],'UNKNOWN');self.assertFalse(result['mappings']['m_a']['violations'])

    def test_invalid_dominates_and_preserves_missing_reasons(self):
        s=base_state();fact(s,'m_a','basis','Paid');del s['resources']['fact:m_a:unit']
        result=assessment(s)['mappings']['m_a']
        self.assertEqual(result['state'],'INVALID');self.assertTrue(result['violations']);self.assertTrue(result['unknowns'])

    def test_names_alone_do_not_establish_incompatibility(self):
        s=base_state();fact(s,'m_a','object','Vendor');s['resources']['rules:object']['payload']['triples']=[]
        self.assertEqual(assessment(s)['state'],'UNKNOWN')

    def test_explicit_incompatibility_is_invalid(self):
        s=base_state();fact(s,'m_a','object','Vendor')
        self.assertEqual(assessment(s)['state'],'INVALID')

    def test_subclass_proof_supports_object(self):
        s=base_state();fact(s,'m_a','object','StaffMember')
        p=next(p for p in s['premises'] if p['id']=='m_a:object');r=check_premise(s,p)
        self.assertEqual(r['state'],'VALID');self.assertEqual(r['supports'][0]['rule'],'rdfs-type-inheritance')

    def test_subproperty_proof_supports_basis(self):
        s=base_state();fact(s,'m_a','basis','Payable','demo:hasSalaryBasis')
        p=next(p for p in s['premises'] if p['id']=='m_a:basis');r=check_premise(s,p)
        self.assertEqual(r['state'],'VALID');self.assertEqual(r['supports'][0]['rule'],'rdfs-subproperty')

    def test_skos_relations_do_not_approve_basis(self):
        for relation in ['skos:closeMatch','skos:related','skos:exactMatch']:
            s=base_state();fact(s,'m_a','basis','PayableAlias')
            s['resources']['rules:basis']['payload']['triples'].append(['demo:PayableAlias',relation,'demo:Payable'])
            self.assertEqual(assessment(s)['state'],'UNKNOWN')

    def test_only_approved_transform_can_authorize_unit(self):
        s=base_state();s['scales']['m_a']='12'
        self.assertEqual(assessment(s)['state'],'INVALID')

    def test_monthly_to_annual_has_no_default_approval(self):
        s=base_state();target(s,'unit','CNY_PER_YEAR');s['scales']['m_a']='12';fact(s,'m_a','unit','CNY_PER_MONTH')
        self.assertEqual(assessment(s)['state'],'UNKNOWN')

    def test_stale_conversion_approval_not_usable(self):
        a=base_state();g=BusinessMaintenance('G2',a)
        b=copy.deepcopy(a);b['resources']['approval:cny']['version']='2'
        r,m=g.update(b)
        self.assertEqual(r['state'],'UNKNOWN');self.assertEqual(m['premises_rechecked'],2)
        self.assertEqual(m['evidence_invalidations'],2)

    def test_support_concept_version_invalidates_old_derivation(self):
        a=base_state();g=BusinessMaintenance('G2',a);b=copy.deepcopy(a)
        b['resources']['concept:Employee']['version']='2'
        r,m=g.update(b);self.assertEqual(r['state'],'UNKNOWN');self.assertEqual(m['premises_rechecked'],2)

    def test_shared_constraint_reaches_both_mappings(self):
        a=base_state();g=BusinessMaintenance('G2',a);b=copy.deepcopy(a);target(b,'basis','Paid')
        r,m=g.update(b);self.assertEqual(m['mappings_rechecked'],2);self.assertEqual(m['premises_rechecked'],2)
        self.assertTrue(all(x['state']=='INVALID' for x in r['mappings'].values()))

    def test_unrelated_change_does_not_recheck_or_invalidate(self):
        a=base_state();g=BusinessMaintenance('G2',a);b=copy.deepcopy(a)
        b['resources']['concept:UnrelatedCar']['version']='2'
        r,m=g.update(b);self.assertEqual(r['state'],'VALID');self.assertEqual(m['premises_rechecked'],0)
        self.assertEqual(m['evidence_hits'],8)

    def test_reachability_only_schedules_not_invalidates(self):
        a=base_state();g=BusinessMaintenance('G2',a);b=copy.deepcopy(a)
        b['resources']['requirement:basis']['version']='2'
        r,m=g.update(b);self.assertEqual(r['state'],'VALID');self.assertEqual(m['premises_rechecked'],2)

    def test_multievent_missing_violation_recovery_g1_g2_same(self):
        a=base_state();g1=BusinessMaintenance('G1',a);g2=BusinessMaintenance('G2',a)
        missing=copy.deepcopy(a);del missing['resources']['fact:m_a:unit']
        composite=copy.deepcopy(missing);fact(composite,'m_a','basis','Paid')
        for state,expected in [(missing,'UNKNOWN'),(composite,'INVALID'),(a,'VALID')]:
            r1,_=g1.update(state);r2,_=g2.update(state)
            self.assertEqual(r1,r2);self.assertEqual(r2['state'],expected)

    def test_delivery_separates_business_and_computational_errors(self):
        good={'q':{'answer':'1.00','errors':[]}}
        bad={'q':{'answer':None,'errors':[{'code':'INVALID_DECIMAL'}]}}
        self.assertEqual(delivery({'state':'UNKNOWN'},good)['decision'],'PENDING_VERIFICATION')
        self.assertEqual(delivery({'state':'INVALID'},good)['decision'],'BUSINESS_BLOCKED')
        self.assertEqual(delivery({'state':'VALID'},bad)['decision'],'COMPUTATION_FAILED')
        self.assertEqual(delivery({'state':'NOT_ASSESSED'},good)['approved'],None)


if __name__=='__main__':unittest.main()
