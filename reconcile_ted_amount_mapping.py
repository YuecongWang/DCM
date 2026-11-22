import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from rdflib import Graph, Literal, Namespace, URIRef

RR = Namespace('http://www.w3.org/ns/r2rml#')
RML = Namespace('http://semweb.mmlab.be/ns/rml#')
TM = Namespace('http://data.europa.eu/a4g/mapping/sf-rml/')
EPO = Namespace('http://data.europa.eu/a4g/ontology#')
CURRENCY = 'http://publications.europa.eu/resource/authority/currency/'
AWARD = '/F03_2014/AWARD_CONTRACT/AWARDED_CONTRACT/'
MOD = '/F03_2014/MODIFICATIONS_CONTRACT/INFO_MODIFICATIONS/'
BEFORE = AWARD+'VAL_TOTAL | '+AWARD+'VALUES/VAL_TOTAL | /F03_2014/RESULTS/AWARDED_PRIZE/VAL_PRIZE | '+MOD+'VAL_TOTAL_BEFORE'
AFTER = MOD+'VAL_TOTAL_AFTER'
PATCHES = {
    'LotAwardOutcome': BEFORE,
    'LotAwardOutcomeAwardedMonetaryValue': BEFORE,
    'LotAwardOutcomeModification': AFTER,
    'LotAwardOutcomeAwardedMonetaryValueModification': AFTER,
    'NoticeAwardInformation': BEFORE.replace('VAL_TOTAL_BEFORE', 'VAL_TOTAL_AFTER'),
}
EXPECTED_PATHS = [
    ('AWARD_CONTRACT/AWARDED_CONTRACT/VAL_TOTAL', 'LotAwardOutcomeAwardedMonetaryValue'),
    ('AWARD_CONTRACT/AWARDED_CONTRACT/VALUES/VAL_TOTAL', 'LotAwardOutcomeAwardedMonetaryValue'),
    ('RESULTS/AWARDED_PRIZE/VAL_PRIZE', 'LotAwardOutcomeAwardedMonetaryValue'),
    ('MODIFICATIONS_CONTRACT/INFO_MODIFICATIONS/VAL_TOTAL_BEFORE', 'LotAwardOutcomeAwardedMonetaryValue'),
    ('MODIFICATIONS_CONTRACT/INFO_MODIFICATIONS/VAL_TOTAL_AFTER', 'LotAwardOutcomeAwardedMonetaryValueModification'),
]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()


def save(path, data):
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def only(values, context):
    values = list(values)
    if len(values) != 1:
        raise ValueError(f'{context}: expected exactly one, found {len(values)}')
    return values[0]


def canonical_decimal(value):
    number = Decimal(str(value).strip())
    if not number.is_finite():
        raise ValueError('Non-finite amount')
    return str(number.normalize())


def prepare_form(source, relative):
    root = ET.parse(source).getroot()
    originals = [e for e in root.iter() if e.tag.split('}')[-1] == 'F03_2014' and e.get('CATEGORY') == 'ORIGINAL']
    if not originals:
        raise ValueError('No ORIGINAL F03 form')
    selected, form = sorted(enumerate(originals), key=lambda x: (x[1].get('LG') != 'EN', x[1].get('LG', ''), x[0]))[0]
    for e in form.iter():
        e.tag = e.tag.split('}')[-1]
        if 'audit_uid' in e.attrib:
            raise ValueError('Reserved audit_uid already exists')


    paths = {}
    def visit(node, path):
        paths[node] = path
        node.set('audit_uid', hashlib.sha256((relative+'|'+str(selected)+'|'+path).encode()).hexdigest())
        counts = Counter()
        for child in node:
            counts[child.tag] += 1
            visit(child, path+'/'+child.tag+'['+str(counts[child.tag])+']')
    visit(form, '/F03_2014[1]')
    expected = []
    included = set()
    for xpath, tm in EXPECTED_PATHS:
        for node in form.findall(xpath):
            included.add(node)
            raw = ''.join(node.itertext()).strip()
            currency = node.get('CURRENCY')
            expected.append({'subject': 'urn:ted-adapter:'+node.get('audit_uid')+':'+tm,
                'path': paths[node], 'raw_amount': raw, 'amount': canonical_decimal(raw),
                'currency': None if currency is None else CURRENCY+currency,
                'triples_map': tm})
    excluded = [{'path': paths[e], 'raw_value': ''.join(e.itertext()).strip(),
                 'currency': e.get('CURRENCY'), 'reason': 'outside_declared_mapping_scope'}
                for e in form.iter() if 'CURRENCY' in e.attrib and e not in included]
    return form, expected, excluded, {'language': form.get('LG'), 'original_forms_available': len(originals),
                                     'selected_original_index': selected}


def adapted_mapping(path):
    graph = Graph().parse(path, format='turtle')
    changes = []
    for s, p, obj in list(graph):
        if isinstance(obj, Literal):
            v = str(obj).replace('%1$s', 'source.xml').replace('%2$s', 'F03_2014').replace('%3$s', 'https://ted.europa.eu/')
            if v != str(obj):
                graph.remove((s, p, obj))
                graph.add((s, p, Literal(v, lang=obj.language, datatype=obj.datatype)))
    for name, iterator in PATCHES.items():
        logical = only(graph.objects(TM[name], RML.logicalSource), name+' logicalSource')
        old_it = only(graph.objects(logical, RML.iterator), name+' iterator')
        sm = only(graph.objects(TM[name], RR.subjectMap), name+' subjectMap')
        old_template = only(graph.objects(sm, RR.template), name+' template')
        template = 'urn:ted-adapter:{@audit_uid}:'+name
        graph.set((logical, RML.iterator, Literal(iterator)))
        graph.set((sm, RR.template, Literal(template)))
        changes.append({'triples_map': str(TM[name]), 'old_iterator': str(old_it),
            'new_iterator': iterator, 'old_subject_template': str(old_template), 'new_subject_template': template})
    return graph.serialize(format='turtle'), changes


def reconcile(graph, expected):
    details = []
    expected_subjects = {URIRef(x['subject']) for x in expected}
    actual_subjects = set(graph.subjects(EPO.hasAmountValue, None))
    for x in expected:
        subject = URIRef(x['subject'])
        amounts = list(graph.objects(subject, EPO.hasAmountValue))
        currencies = list(graph.objects(subject, EPO.hasCurrency))
        amount_ok = (len(amounts) == 1 and isinstance(amounts[0], Literal)
                     and amounts[0].datatype == URIRef('http://www.w3.org/2001/XMLSchema#decimal')
                     and canonical_decimal(amounts[0]) == x['amount'])
        want_currency = [] if x['currency'] is None else [x['currency']]
        currency_ok = sorted(str(c) for c in currencies) == want_currency and all(isinstance(c, URIRef) for c in currencies)
        details.append({**x, 'rdf_amounts': [str(v) for v in amounts],
                        'rdf_currencies': [str(v) for v in currencies],
                        'amount_matches': amount_ok, 'currency_matches': currency_ok})
    extras = sorted(str(v) for v in actual_subjects-expected_subjects)

    extra_currency_subjects = sorted(str(v) for v in set(graph.subjects(EPO.hasCurrency, None))-expected_subjects)
    ok = not extras and not extra_currency_subjects and all(x['amount_matches'] and x['currency_matches'] for x in details)
    return ok, details, extras, extra_currency_subjects


def main():
    base = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-root', type=Path, default=base/'datasets/ted-rdf-mapping/mappings/package_F03/test_data')
    ap.add_argument('--mapping', type=Path, default=base/'results/official_api_20261016_180019/updated_mappings.ttl')
    ap.add_argument('--jar', type=Path, default=base/'tools/rmlmapper-6.2.2-r369-all.jar')
    ap.add_argument('--previous', type=Path, default=base/'results/ted_rdf_batch_v1')
    ap.add_argument('--out', type=Path, default=base/'results/ted_amount_adapter_v2')
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--timeout', type=int, default=180)
    a = ap.parse_args()
    if a.workers < 1 or a.workers > 8 or a.limit < 0 or a.timeout < 1:
        ap.error('Invalid workers/limit/timeout')
    for key in ('data_root', 'mapping', 'jar', 'previous', 'out'):
        setattr(a, key, getattr(a, key).resolve())
    files = sorted(a.data_root.rglob('*.xml'))
    if not files:
        ap.error('No source XML')
    available = len(files)
    if a.limit:
        files = files[:a.limit]
    text, patch_log = adapted_mapping(a.mapping)
    config = {'version': 2, 'script_sha256': digest(Path(__file__)),
        'mapping_sha256': digest(a.mapping), 'jar_sha256': digest(a.jar),
        'source_hashes': {p.relative_to(a.data_root).as_posix(): digest(p) for p in files},
        'derived_mapping_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'scope': [x[0] for x in EXPECTED_PATHS]}


    from rdflib.compare import to_isomorphic
    config['derived_mapping_sha256'] = str(to_isomorphic(Graph().parse(data=text, format='turtle')).graph_digest())
    a.out.mkdir(parents=True, exist_ok=True)
    manifest = a.out/'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != config:
        raise RuntimeError('Input/configuration changed; use a new --out directory')
    save(manifest, config)
    save(a.out/'mapping_changes.json', {'shared_by_both_groups': True, 'component': 'input_adapter_not_D',
        'changes': patch_log, 'identity_rule': 'relative source path + selected form index + indexed XML path',
        'limitations': ['Identity is stable for a fixed XML structure; insertions/reordering require new identity/version handling.',
            'Other example mapping relationships are unchanged; this audit proves only the declared amount/currency scope.']})
    (a.out/'adapted_mapping.ttl').write_text(text, encoding='utf-8')

    def job(source):
        rel = source.relative_to(a.data_root).as_posix()
        key = hashlib.sha256(rel.encode()).hexdigest()[:16]
        folder = a.out/'notices'/key
        folder.mkdir(parents=True, exist_ok=True)
        result_path = folder/'result.json'
        form, expected, excluded, meta = prepare_form(source, rel)
        reused = False
        previous_result = None
        if result_path.exists() and (folder/'output.ttl').is_file():
            previous_result = json.loads(result_path.read_text(encoding='utf-8'))
            reused = previous_result.get('rdf_sha256') == digest(folder/'output.ttl') and previous_result.get('audit_passed') is True
        row = {'source': rel, 'source_sha256': config['source_hashes'][rel], **meta,
               'scope_records': len(expected), 'excluded_currency_elements': excluded,
               'audit_passed': False, 'status': 'failed'}
        start = time.perf_counter()
        try:
            if not reused:
                ET.ElementTree(form).write(folder/'source.xml', encoding='utf-8', xml_declaration=True)
                (folder/'mapping.ttl').write_text(text, encoding='utf-8')
                (folder/'output.ttl').unlink(missing_ok=True)
                with (folder/'mapper.log').open('w', encoding='utf-8') as log:
                    r = subprocess.run(['java', '-Xmx1g', '-jar', str(a.jar), '-m', 'mapping.ttl',
                        '-o', 'output.ttl', '-s', 'turtle'], cwd=folder, stdout=log,
                        stderr=subprocess.STDOUT, timeout=a.timeout)
                if r.returncode:
                    raise RuntimeError('Mapper exit='+str(r.returncode)+'; see mapper.log')
            graph = Graph().parse(folder/'output.ttl', format='turtle')
            ok, details, extras, extra_curr = reconcile(graph, expected)
            row.update(status='ok' if ok else 'mismatch', audit_passed=ok,
                rdf_triples=len(graph), rdf_sha256=digest(folder/'output.ttl'),
                reconciliation=details, unexpected_amount_subjects=extras,
                unexpected_currency_subjects=extra_curr)
            old_path = a.previous/'notices'/key/'output.ttl'
            if old_path.is_file():
                old = Graph().parse(old_path, format='turtle')
                row['before_amount_triples'] = len(list(old.triples((None, EPO.hasAmountValue, None))))
                row['before_currency_triples'] = len(list(old.triples((None, EPO.hasCurrency, None))))
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
        row['seconds_this_invocation'] = time.perf_counter()-start
        row['resumed'] = reused
        save(result_path, row)
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(job, p): p for p in files}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                p = futures[future]
                row = {'source': p.relative_to(a.data_root).as_posix(), 'status': 'failed',
                       'audit_passed': False, 'error': f'{type(exc).__name__}: {exc}'}
            rows.append(row)
            print(f'[{len(rows)}/{len(files)}] {row["status"]} {row["source"]} records={row.get("scope_records", 0)}', flush=True)
    rows.sort(key=lambda r: r['source'])
    details = [dict(source=r['source'], **d) for r in rows for d in r.get('reconciliation', [])]
    subjects = [d['subject'] for d in details]
    unique_ids = len(subjects) == len(set(subjects))
    passed = all(r['audit_passed'] for r in rows) and unique_ids
    summary = {'stage': 'shared_amount_adapter_reconciliation', 'available_xml': available,
        'selected_xml': len(files), 'passed_files': sum(r['audit_passed'] for r in rows),
        'failed_files': sum(not r['audit_passed'] for r in rows),
        'expected_amount_records': sum(r.get('scope_records', 0) for r in rows),
        'matched_amount_records': sum(d['amount_matches'] for d in details),
        'matched_currency_records': sum(d['currency_matches'] for d in details),
        'notices_with_scoped_amount': sum(r.get('scope_records', 0)>0 for r in rows),
        'unique_amount_subjects': unique_ids, 'audit_passed': passed,
        'comparison_experiment_completed': False, 'records': rows,
        'notes': ['Missing or out-of-scope amounts are not filled with zero.',
            'No full business-validity or full-graph correctness claim follows from this reconciliation.',
            'No OR-Full/OR-D timing or superiority measurement is performed here.']}
    save(a.out/'summary.json', summary)
    import csv
    if details:
        with (a.out/'amount_reconciliation.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(details[0]))
            writer.writeheader()
            writer.writerows(details)
    print('AUDIT_PASS' if passed else 'AUDIT_FAIL')
    print('EXPECTED:', summary['expected_amount_records'], 'MATCHED:', summary['matched_amount_records'])
    print('RESULT:', a.out/'summary.json')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
