import csv
import hashlib
import json
import platform
import statistics
import sys
import time
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN
from pathlib import Path
from config import RESULT_ROOT, ROWS, REPEATS, SHARED_QUERIES, DELAY_GRID_MS


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def rule(scale='1', revision='v1'):
    return dict(scale=scale, revision=revision, quantum='0.01', rounding='HALF_EVEN',
                null='None-or-empty', numeric='decimal-string-max28digits', errors='fail-query')


def normalize(snapshot, field, scan):

    out, errors = [], []
    r = snapshot['rules'][field]
    with localcontext() as ctx:
        ctx.prec = 64
        for i, row in enumerate(snapshot['rows']):
            scan[0] += 1
            value = row[field]
            if value is None or value == '':
                out.append(None)
                continue
            try:
                if not isinstance(value, str):
                    raise ValueError('TYPE')
                d = Decimal(value)
                if not d.is_finite():
                    raise ValueError('NONFINITE')
                if len(d.as_tuple().digits) > 28 or abs(d.adjusted()) > 28:
                    raise ValueError('RANGE')
                v = d * Decimal(r['scale'])
                out.append(str(v.quantize(Decimal(r['quantum']), rounding=ROUND_HALF_EVEN)))
            except (InvalidOperation, ValueError) as exc:
                out.append(None)
                code = str(exc) if isinstance(exc, ValueError) else 'INVALID_DECIMAL'
                errors.append([i, field, code])
    return out, errors


def execute(snapshot, query, scan):
    columns, errors = [], []
    for f in query['fields']:
        col, err = normalize(snapshot, f, scan)
        columns.append(col)
        errors.extend(err)
    if errors:
        return {'answer': None, 'errors': errors}

    with localcontext() as ctx:
        ctx.prec = 128
        vals = [Decimal(v) for col in columns for v in col if v is not None]
        op = query['op']
        answer = (str(sum(vals, Decimal(0))) if op == 'sum' else
                  len(vals) if op == 'count' else
                  sorted(str(v) for v in vals) if op == 'sort' else
                  [list(row) for row in zip(*columns)])
    return {'answer': answer, 'errors': []}


def fixtures():
    base = [{'x': str(Decimal(i) / 10), 'y': str(i)} for i in range(ROWS)]
    base[0]['x'] = None
    base[1]['x'] = ''
    base[2]['x'] = '1.005'
    base[3]['x'] = '1.015'
    snapshots = []
    def add(name, rows, rules, version):
        s = {'version': version, 'rows': rows, 'rules': rules}
        snapshots.append({'name': name, 'snapshot': s})
    add('initial', base, {'x': rule(), 'y': rule()}, 'v0')
    scaled = [dict(x=(str(Decimal(r['x']) * 100) if r['x'] not in (None, '') else r['x']), y=r['y']) for r in base]
    add('representation_and_mapping_equivalent', scaled, {'x': rule('0.01'), 'y': rule()}, 'v1')
    changed = [dict(r) for r in scaled]
    changed[4]['x'] = '999'
    add('normalized_changed', changed, {'x': rule('0.01'), 'y': rule()}, 'v2')
    partial = [dict(r, y=str(Decimal(r['y']) + 1)) for r in changed]
    add('partial_fields_changed', partial, {'x': rule('0.01'), 'y': rule()}, 'v3')
    add('version_only_old_evidence_invalid', partial, {'x': rule('0.01'), 'y': rule()}, 'v4')
    add('definition_only_old_evidence_invalid', partial, {'x': rule('0.01', 'v2'), 'y': rule()}, 'v4')
    broken = [dict(r) for r in partial]
    broken[5]['x'] = 'NaN'
    broken[6]['x'] = 'bad'
    add('conversion_failure', broken, {'x': rule('0.01', 'v2'), 'y': rule()}, 'v5')
    add('recover_errors', partial, {'x': rule('0.01', 'v2'), 'y': rule()}, 'v6')
    boundary = [dict(r) for r in partial]
    boundary[2]['x'] = '100.500000000000000000000001'
    boundary[7]['x'] = '1E100'
    add('precision_boundary_and_range_failure', boundary, {'x': rule('0.01', 'v2'), 'y': rule()}, 'v7')
    queries = [{'id': f'x{i}', 'fields': ['x'], 'op': ['sum', 'count', 'sort', 'project'][i % 4]} for i in range(SHARED_QUERIES)]
    queries += [{'id': 'y', 'fields': ['y'], 'op': 'sum'}, {'id': 'xy', 'fields': ['x', 'y'], 'op': 'project'}]
    return snapshots, queries


def bind(snapshot):

    return digest({'version': snapshot['version'], 'rows': snapshot['rows']}), digest(snapshot['rules'])


def evidence_key(old_binding, new_binding, fields):
    return (old_binding, new_binding, tuple(fields), 'ordered-full-column-v1')


def run(group, events, queries, check_delay=0, exec_delay=0):
    old = events[0]['snapshot']
    old_binding = bind(old)
    answers = {q['id']: execute(old, q, [0]) for q in queries}
    cache = {}
    records, audit = [], []
    for event in events[1:]:
        new = event['snapshot']
        reference = {q['id']: execute(new, q, [0]) for q in queries}
        m = dict(group=group, event=event['name'], queries=len(queries), reexecutions=0,
                 checks=0, check_scanned_cells=0, execution_scanned_cells=0, evidence_hits=0,
                 reused=0, wrong_reuse=0, answer_mismatches=0, error_mismatches=0,
                 analysis_seconds=0., check_seconds=0., execution_seconds=0.)
        start = time.perf_counter()
        t = time.perf_counter()
        new_binding = bind(new)
        m['analysis_seconds'] += time.perf_counter() - t
        for q in queries:
            t = time.perf_counter()

            affected = old_binding != new_binding
            key = evidence_key(old_binding, new_binding, q['fields'])
            cached = cache.get(key) if group == 'C' else None
            m['analysis_seconds'] += time.perf_counter() - t
            equivalent = not affected
            if affected and group != 'A':
                t = time.perf_counter()
                if cached is not None:
                    m['evidence_hits'] += 1
                    equivalent = cached['equivalent']
                else:
                    scan = [0]
                    pairs = [(normalize(old, f, scan), normalize(new, f, scan)) for f in q['fields']]
                    equivalent = all(not a[1] and not b[1] and a[0] == b[0] for a, b in pairs)
                    m['checks'] += 1
                    m['check_scanned_cells'] += scan[0]
                    if check_delay:
                        time.sleep(check_delay / 1000)
                    if group == 'C':
                        cache[key] = {'equivalent': equivalent, 'scope': key, 'failed': any(a[1] or b[1] for a,b in pairs)}
                    audit.append({'group': group, 'event': event['name'], 'query': q['id'],
                                  'key': key, 'equivalent': equivalent})
                m['check_seconds'] += time.perf_counter() - t
            if equivalent:
                result = answers[q['id']]
                m['reused'] += 1
                m['wrong_reuse'] += result != reference[q['id']]
            else:
                t = time.perf_counter()
                scan = [0]
                result = execute(new, q, scan)
                if exec_delay:
                    time.sleep(exec_delay / 1000)
                m['execution_seconds'] += time.perf_counter() - t
                m['execution_scanned_cells'] += scan[0]
                m['reexecutions'] += 1
            answers[q['id']] = result

        m['total_seconds'] = time.perf_counter() - start
        for q in queries:
            actual, expected = answers[q['id']], reference[q['id']]
            m['answer_mismatches'] += actual['answer'] != expected['answer']
            m['error_mismatches'] += actual['errors'] != expected['errors']
        records.append(m)
        old, old_binding = new, new_binding
    return records, audit


def self_checks():
    a = {'version': '1', 'rows': [{'x': '1.005'}, {'x': '1.015'}, {'x': None}], 'rules': {'x': rule()}}
    assert normalize(a, 'x', [0]) == (['1.00', '1.02', None], [])
    b = dict(a, version='2')
    c = dict(a, rules={'x': rule(revision='2')})
    key = evidence_key(bind(a), bind(b), ['x'])
    assert key != evidence_key(bind(a), bind(c), ['x'])
    assert key != evidence_key(bind(a), bind(b), ['x', 'y'])
    d = dict(b, rows=[{'x': '2'}])
    assert key != evidence_key(bind(a), bind(d), ['x'])


def main():
    self_checks()
    events, queries = fixtures()
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    (RESULT_ROOT.parent / 'controlled_data.json').write_text(json.dumps({'events': events, 'queries': queries}, ensure_ascii=False, indent=2), encoding='utf-8')
    trials, audits = [], []
    for cd, ed in DELAY_GRID_MS:
        for repeat in range(REPEATS):
            groups = ['A', 'B', 'C']
            groups = groups[repeat % 3:] + groups[:repeat % 3]
            for group in groups:
                rows, audit = run(group, events, queries, cd, ed)
                for r in rows:
                    r.update(repeat=repeat, check_delay_ms=cd, execution_delay_ms=ed,
                             timing_mode='real' if cd == ed == 0 else 'injected_delay')
                trials.extend(rows)
                if repeat == 0 and cd == ed == 0:
                    audits.extend(audit)
    assert all(r['wrong_reuse'] == r['answer_mismatches'] == r['error_mismatches'] == 0 for r in trials)
    summary = []
    for cd, ed in DELAY_GRID_MS:
        for g in ['A', 'B', 'C']:
            rows = [r for r in trials if r['group'] == g and r['check_delay_ms'] == cd and r['execution_delay_ms'] == ed]
            totals = [sum(r['total_seconds'] for r in rows if r['repeat'] == i) for i in range(REPEATS)]
            s = dict(group=g, check_delay_ms=cd, execution_delay_ms=ed,
                     timing_mode='real' if cd == ed == 0 else 'injected_delay',
                     median_total_seconds=statistics.median(totals), min_total_seconds=min(totals), max_total_seconds=max(totals))
            for k in ['checks', 'reexecutions', 'check_scanned_cells', 'execution_scanned_cells', 'evidence_hits', 'reused', 'wrong_reuse', 'answer_mismatches', 'error_mismatches', 'analysis_seconds', 'check_seconds', 'execution_seconds']:
                s[k] = statistics.median(sum(r[k] for r in rows if r['repeat'] == i) for i in range(REPEATS))
            summary.append(s)
    result = {'candidate_vs_C': 'No distinct mechanism verified; D omitted; attribution experiment only',
              'runtime': {'python': sys.version, 'executable': sys.executable, 'platform': platform.platform()},
              'config': {'rows': ROWS, 'queries': len(queries), 'repeats': REPEATS},
              'timing_scope': 'Includes snapshot hashing, evidence lookup/acquisition/insertion, checks and execution; excludes initial answers, oracle and file reporting',
              'summary': summary, 'trials': trials, 'evidence_audit': audits}
    (RESULT_ROOT / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    with (RESULT_ROOT / 'comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        w = csv.DictWriter(stream, fieldnames=list(summary[0])); w.writeheader(); w.writerows(summary)
    print(json.dumps(summary[:3], indent=2))


if __name__ == '__main__':
    main()
