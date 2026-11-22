import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import statistics
import time
from rdflib import Graph, Literal, Namespace, URIRef
from dcm_scheduler import Scheduler, Premise, State

EPO = Namespace('http://data.europa.eu/a4g/ontology#')
RR = Namespace('http://www.w3.org/ns/r2rml#')
TM = Namespace('http://data.europa.eu/a4g/mapping/sf-rml/')
DECIMAL = URIRef('http://www.w3.org/2001/XMLSchema#decimal')
CUR = 'http://publications.europa.eu/resource/authority/currency/'
QUERY = '''PREFIX epo: <http://data.europa.eu/a4g/ontology#>
SELECT ?currency (SUM(?amount) AS ?total) WHERE {
  ?node epo:hasAmountValue ?amount; epo:hasCurrency ?currency .
} GROUP BY ?currency'''
EVENTS = ['initial', 'unrelated_annotation', 'legal_amount_update',
          'business_scope_invalid', 'scope_recovery', 'scope_unknown', 'scope_known_again']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(path, value):
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def number(value):
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError('Non-finite decimal')
    return str(d.normalize())


def amounts(snapshot):
    g = snapshot['graph']
    nodes = set(g.subjects(EPO.hasAmountValue, None))
    if not nodes:
        return State.UNKNOWN
    for node in nodes:
        vs = list(g.objects(node, EPO.hasAmountValue))
        if len(vs) != 1 or not isinstance(vs[0], Literal) or vs[0].datatype != DECIMAL:
            return State.INVALID
        try:
            number(vs[0])
        except Exception:
            return State.INVALID
    return State.VALID


def currencies(snapshot):
    g = snapshot['graph']
    nodes = set(g.subjects(EPO.hasAmountValue, None))
    if not nodes:
        return State.UNKNOWN
    import re
    for node in nodes:
        vs = list(g.objects(node, EPO.hasCurrency))
        if len(vs) != 1 or not isinstance(vs[0], URIRef) or not re.fullmatch(re.escape(CUR)+'[A-Z]{3}', str(vs[0])):
            return State.INVALID
    return State.VALID


def scope(snapshot):

    v = snapshot['declared_scope']
    return State.UNKNOWN if v is None else State.VALID if v == 'awarded_amount' else State.INVALID


def mapping_definition(snapshot):
    g = snapshot['mapping']
    for name in ['LotAwardOutcomeAwardedMonetaryValue', 'LotAwardOutcomeAwardedMonetaryValueModification']:
        poms = list(g.objects(TM[name], RR.predicateObjectMap))
        amount_maps = [om for p in poms if (p, RR.predicate, EPO.hasAmountValue) in g for om in g.objects(p, RR.objectMap)]
        if not amount_maps or not all((om, RR.datatype, DECIMAL) in g for om in amount_maps):
            return State.INVALID
        if not any((p, RR.predicate, EPO.hasCurrency) in g for p in poms):
            return State.INVALID
    return State.VALID


def projections(graph):
    a = sorted((str(s), o.n3()) for s, _, o in graph.triples((None, EPO.hasAmountValue, None)))
    focus = set(graph.subjects(EPO.hasAmountValue, None))
    c = sorted((str(s), o.n3()) for s in focus for o in graph.objects(s, EPO.hasCurrency))
    return a, c


def query_answer(graph):
    return sorted((str(r.currency), number(r.total)) for r in graph.query(QUERY))


def independent_oracle(snapshot):


    g = snapshot['graph']
    nodes = set(g.subjects(EPO.hasAmountValue, None))
    sums = defaultdict(Decimal)
    data_invalid = False
    for node in nodes:
        av = list(g.objects(node, EPO.hasAmountValue))
        cv = list(g.objects(node, EPO.hasCurrency))
        if len(av) != 1 or len(cv) != 1:
            data_invalid = True
            continue
        try:
            n = Decimal(str(av[0]))
            if not n.is_finite() or av[0].datatype != DECIMAL:
                data_invalid = True
            s = str(cv[0])
            if not isinstance(cv[0], URIRef) or not s.startswith(CUR) or len(s[len(CUR):]) != 3 or not s[len(CUR):].isalpha() or s[len(CUR):] != s[len(CUR):].upper():
                data_invalid = True
            sums[s] += n
        except Exception:
            data_invalid = True
    v = snapshot['declared_scope']
    state = ('INVALID' if data_invalid or (v is not None and v != 'awarded_amount') else
             'UNKNOWN' if not nodes or v is None else 'VALID')
    return {'answer': sorted((k, number(v)) for k, v in sums.items()),
            'business_state': state, 'deliver': state == 'VALID'}


def replay(original, mapping):
    g = Graph()
    for t in original:
        g.add(t)
    descriptor = 'awarded_amount'
    for event in EVENTS:
        if event == 'unrelated_annotation':
            g.add((URIRef('urn:controlled:event'), URIRef('http://www.w3.org/2000/01/rdf-schema#comment'), Literal('unrelated test annotation')))
        elif event == 'legal_amount_update':
            nodes = sorted(set(g.subjects(EPO.hasAmountValue, None)), key=str)
            if nodes:
                old = next(g.objects(nodes[0], EPO.hasAmountValue))
                g.set((nodes[0], EPO.hasAmountValue, Literal(Decimal(str(old))+Decimal('1'), datatype=DECIMAL)))
        elif event == 'business_scope_invalid':
            descriptor = 'estimated_amount'
        elif event in ('scope_recovery', 'scope_known_again'):
            descriptor = 'awarded_amount'
        elif event == 'scope_unknown':
            descriptor = None
        yield event, {'graph': g, 'mapping': mapping, 'declared_scope': descriptor}


def main():
    base = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--adapter', type=Path, default=base/'results/ted_amount_adapter_v2')
    ap.add_argument('--out', type=Path, default=base/'results/or_contract_comparison_v1')
    ap.add_argument('--repeats', type=int, default=5)
    a = ap.parse_args()
    if a.repeats < 1:
        ap.error('repeats must be positive')
    audit = json.loads((a.adapter/'summary.json').read_text(encoding='utf-8'))
    if not audit.get('audit_passed') or audit.get('selected_xml') != 128:
        raise ValueError('128-file amount reconciliation must pass first')
    a.out.mkdir(parents=True, exist_ok=True)
    manifest = {'script_sha256': sha(Path(__file__)), 'D_core_sha256': sha(base/'dcm_scheduler.py'),
        'adapter_manifest_sha256': sha(a.adapter/'manifest.json'), 'adapter_audit_sha256': sha(a.adapter/'summary.json'),
        'query': QUERY, 'repeats': a.repeats, 'events': EVENTS,
        'UNKNOWN_delivery': 'BLOCK', 'scope': 'application-declared awarded amount contract',
        'event_origin': 'controlled perturbations of real TED-derived RDF and explicit metadata; not observed history',
        'label_status': 'programmatic expectations; not independently human-audited',
        'timing_scope': 'post-mapping maintenance only; excludes XML/RML materialization, snapshot creation and oracle',
        'timed_components': ['D dependency signatures', 'query footprint', 'checks/evidence lookup', 'SPARQL when executed', 'delivery decision'],
        'repeats_are_independent_cases': False}
    old = a.out/'manifest.json'
    if old.exists() and json.loads(old.read_text(encoding='utf-8')) != manifest:
        raise ValueError('Configuration changed; use a new --out directory')
    save(old, manifest)
    mapping = Graph().parse(a.adapter/'adapted_mapping.ttl', format='turtle')
    from rdflib.compare import to_isomorphic
    adapter_manifest = json.loads((a.adapter/'manifest.json').read_text(encoding='utf-8'))
    if str(to_isomorphic(mapping).graph_digest()) != adapter_manifest['derived_mapping_sha256']:
        raise ValueError('Adapted mapping differs from reconciled adapter version')
    if mapping_definition({'mapping': mapping}) != State.VALID:
        raise ValueError('Frozen common mapping does not meet the declared mapping contract')
    mapping_hash = sha(a.adapter/'adapted_mapping.ttl')
    premises = [Premise('amount', 'amount-v1', ('amount',), amounts, True),
        Premise('currency', 'currency-v1', ('currency',), currencies, True),
        Premise('scope', 'scope-v1', ('scope',), scope, True),
        Premise('mapping', 'mapping-v1', ('mapping',), mapping_definition, True)]
    rows = []
    plan = [{'source': r['source'], 'events': EVENTS, 'scope_records': r['scope_records'],
             'legal_update_applicable': r['scope_records'] > 0} for r in audit['records']]
    save(a.out/'cases_for_review.json', {'case_plan': plan, 'human_reviewed': False,
        'contracts': ['One finite xsd:decimal per amount object', 'One three-letter currency-authority IRI per amount object',
                      'Declared scope equals awarded_amount', 'Mapped amount datatype and currency predicates present'],
        'business_descriptor': 'Explicit adapter-test metadata; no claim that TED supplies this field'})
    for i, record in enumerate(audit['records'], 1):
        rel = record['source']
        folder = a.adapter/'notices'/hashlib.sha256(rel.encode()).hexdigest()[:16]
        rdf = folder/'output.ttl'
        if sha(rdf) != record['rdf_sha256']:
            raise ValueError('RDF changed: '+rel)
        original = Graph().parse(rdf, format='turtle')
        for repeat in range(a.repeats):
            runners = {name: Scheduler(name == 'OR-D') for name in ['OR-Full', 'OR-D']}
            cached = {}
            for event, snapshot in replay(original, mapping):
                oracle = independent_oracle(snapshot)

                names = ['OR-Full', 'OR-D'] if (i+repeat)%2 else ['OR-D', 'OR-Full']
                for name in names:
                    start = time.perf_counter()
                    pa, pc = projections(snapshot['graph'])
                    footprint = signature([pa, pc, QUERY])
                    if name == 'OR-D':
                        versions = {'amount': signature(pa), 'currency': signature([sorted({s for s, _ in pa}), pc]),
                                    'scope': signature(snapshot['declared_scope']), 'mapping': mapping_hash}
                    else:
                        versions = dict.fromkeys(('amount', 'currency', 'scope', 'mapping'), 'full-check')
                    result = runners[name].evaluate(premises, snapshot, versions)
                    execute = name not in cached or cached[name][0] != footprint
                    if execute:
                        answer = query_answer(snapshot['graph'])
                        cached[name] = (footprint, answer)
                    else:
                        answer = cached[name][1]
                    decision = 'DELIVER' if result['deliver'] else 'BLOCK'
                    seconds = time.perf_counter()-start
                    row = {'source': rel, 'repeat': repeat, 'event': event, 'method': name,
                        'business_state': result['business_state'], 'decision': decision,
                        'premise_checks': result['premise_checks'], 'evidence_hits': result['evidence_hits'],
                        'query_executions': int(execute), 'query_reused': not execute,
                        'answer_matches_oracle': answer == oracle['answer'],
                        'state_matches_oracle': result['business_state'] == oracle['business_state'],
                        'decision_matches_oracle': result['deliver'] == oracle['deliver'],
                        'wrong_delivery': result['deliver'] and not oracle['deliver'],
                        'wrong_query_reuse': not execute and answer != oracle['answer'],
                        'maintenance_ms': seconds*1000, 'premise_states': result['premise_states'],
                        'scope_records': record['scope_records'], 'answer': answer}
                    rows.append(row)
        print(f'[{i}/128] {rel}', flush=True)
    save(a.out/'event_records.json', rows)
    totals = []
    for name in ['OR-Full', 'OR-D']:
        by_repeat = []
        for repeat in range(a.repeats):
            selected = [r for r in rows if r['method'] == name and r['repeat'] == repeat]
            warm = [r for r in selected if r['event'] == 'initial']
            maintenance = [r for r in selected if r['event'] != 'initial']
            by_repeat.append({'repeat': repeat,
                'maintenance_ms': sum(r['maintenance_ms'] for r in maintenance),
                'cold_initialization_ms': sum(r['maintenance_ms'] for r in warm),
                'premise_checks': sum(r['premise_checks'] for r in maintenance),
                'evidence_hits': sum(r['evidence_hits'] for r in maintenance),
                'query_executions': sum(r['query_executions'] for r in maintenance)})
        rs = [r for r in rows if r['method'] == name]
        totals.append({'method': name, 'per_repeat': by_repeat,
            'maintenance_ms_median': statistics.median(r['maintenance_ms'] for r in by_repeat),
            'maintenance_ms_min': min(r['maintenance_ms'] for r in by_repeat),
            'maintenance_ms_max': max(r['maintenance_ms'] for r in by_repeat),
            'answer_consistent': all(r['answer_matches_oracle'] for r in rs),
            'state_consistent': all(r['state_matches_oracle'] for r in rs),
            'decision_consistent': all(r['decision_matches_oracle'] for r in rs),
            'wrong_delivery_records': sum(r['wrong_delivery'] for r in rs),
            'wrong_query_reuse_records': sum(r['wrong_query_reuse'] for r in rs)})
    summary = {'stage': 'controlled_post_OntoRipple_contract_comparison',
        'notices': 128, 'amount_records': audit['expected_amount_records'],
        'event_steps_per_repeat': 128*len(EVENTS), 'event_steps_are_sequential': True, 'repeats': a.repeats,
        'semantic_cases_require_human_review': True,
        'scope': 'application-declared amount/currency contracts, not full ePO SHACL', 'methods': totals,
        'passed': all(x['answer_consistent'] and x['state_consistent'] and x['decision_consistent'] for x in totals)}
    save(a.out/'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('RESULT:', (a.out/'summary.json').resolve())
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
