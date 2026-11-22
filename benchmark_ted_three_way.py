import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import statistics
import sys
import time

import rdflib
from rdflib import Graph
from rdflib.compare import to_isomorphic
import benchmark_ted_contract as existing
from dcm_scheduler import Scheduler, Premise, State

METHODS = ('B0', 'B1', 'B2')
DEFINITIONS = {
    'B0': 'Actual SPARQL execution on every event; full business validation',
    'B1': 'Query footprint reuse; full business validation',
    'B2': 'Same query footprint reuse; versioned incremental business validation',
}
LIMITATIONS = [
    'Controlled TED-derived data maintenance, not natural production evolution.',
    'Scope is explicit application test metadata, not inferred from TED.',
    'Programmatic contract expectations; human_reviewed=false.',
    'Four amount/currency/scope/mapping contracts, not full ePO/SHACL coverage.',
    'Sequential events and repeated timings are not independent samples or datasets.',
    'Post-mapping maintenance only, not end-to-end XML/RML pipeline time.',
    'Trusted complete dependency declarations and immutable versions are required.',
    'No scoped amounts means UNKNOWN even after scope recovery; no zero filling.',
    'Frozen mapping is validated before execution; the existing oracle assumes that mapping.',
    'Small smoke timings cannot establish performance superiority or generalization.',
]


def premises():
    return [Premise('amount', 'amount-v1', ('amount',), existing.amounts, True),
            Premise('currency', 'currency-v1', ('currency',), existing.currencies, True),
            Premise('scope', 'scope-v1', ('scope',), existing.scope, True),
            Premise('mapping', 'mapping-v1', ('mapping',), existing.mapping_definition, True)]


def footprint_and_versions(snapshot, mapping_hash):
    pa, pc = existing.projections(snapshot['graph'])


    ids = sorted((str(s), s.n3()) for s in set(
        snapshot['graph'].subjects(existing.EPO.hasAmountValue, None)))
    footprint = existing.signature([pa, pc, ids, existing.QUERY])
    versions = {'amount': existing.signature([pa, ids]),
                'currency': existing.signature([sorted({s for s, _ in pa}), pc, ids]),
                'scope': existing.signature(snapshot['declared_scope']),
                'mapping': mapping_hash}
    return footprint, versions


def matching_evidence(before, specs, versions):

    hits = []
    for p in specs:
        deps = tuple(sorted(set(p.dependencies)))
        signature = (p.revision, p.dependencies_complete,
                     tuple((d, versions.get(d)) for d in deps))
        old = before.get(p.key)
        known = all(isinstance(versions.get(d), str) and versions[d] for d in deps)
        if (old is not None and p.dependencies_complete and p.revision and known
                and old.signature == signature and old.state != State.UNKNOWN):
            hits.append(p.key)
    return hits


class Runner:
    def __init__(self, method, mapping_hash):
        if method not in METHODS:
            raise ValueError('Unknown strategy: ' + method)
        self.method = method
        self.mapping_hash = mapping_hash
        self.scheduler = Scheduler(incremental=method == 'B2')
        self.specs = premises()
        self.cached = None

    def step(self, snapshot):


        before = dict(self.scheduler.cache)
        start = time.perf_counter()
        footprint, versions = footprint_and_versions(snapshot, self.mapping_hash)
        execute = self.method == 'B0' or self.cached is None or self.cached[0] != footprint
        if execute:
            answer = existing.query_answer(snapshot['graph'])
            self.cached = (footprint, answer)
        else:
            answer = self.cached[1]
        business = self.scheduler.evaluate(self.specs, snapshot, versions)
        decision = 'DELIVER' if business['deliver'] else 'BLOCK'
        milliseconds = (time.perf_counter() - start) * 1000
        allowed = matching_evidence(before, self.specs, versions) if self.method == 'B2' else []
        if business['evidence_hits'] != len(allowed):
            raise AssertionError('Evidence hits do not match same-premise signatures')
        return {'query_executed': execute, 'query_reused': not execute, 'answer': answer,
                'business_state': business['business_state'], 'delivery_decision': decision,
                'premise_checks': business['premise_checks'], 'evidence_hits': business['evidence_hits'],
                'premise_states': business['premise_states'], 'evidence_hit_premises': allowed,
                'evidence_signatures_verified': True, 'query_footprint': footprint,
                'dependency_versions': versions, 'maintenance_ms': milliseconds,
                'strategy_config_id': existing.signature(DEFINITIONS[self.method])}


def run_case(original, mapping, mapping_hash, source, repeat, source_index, rdf_hash):
    runners = {method: Runner(method, mapping_hash) for method in METHODS}
    rows = []
    for event_index, (event, snapshot) in enumerate(existing.replay(original, mapping)):

        oracle = existing.independent_oracle(snapshot)
        event_hash = str(to_isomorphic(snapshot['graph']).graph_digest())
        offset = (source_index + repeat + event_index) % len(METHODS)
        order = METHODS[offset:] + METHODS[:offset]
        for position, method in enumerate(order):
            try:
                row = runners[method].step(snapshot)
            except Exception as exc:
                raise RuntimeError(f'{source} repeat={repeat} event={event} method={method}: {exc}') from exc
            row.update(source=source, repeat=repeat, event=event, method=method,
                       input_rdf_sha256=rdf_hash, snapshot_graph_digest=event_hash,
                       declared_scope=snapshot['declared_scope'], run_position=position,
                       oracle=oracle, cache_instance=f'{source}|{repeat}|{method}')
            row.update(answer_matches_oracle=row['answer'] == oracle['answer'],
                       state_matches_oracle=row['business_state'] == oracle['business_state'],
                       decision_matches_oracle=(row['delivery_decision'] == 'DELIVER') == oracle['deliver'],
                       wrong_query_reuse=row['query_reused'] and row['answer'] != oracle['answer'],
                       wrong_delivery=row['delivery_decision'] == 'DELIVER' and not oracle['deliver'])
            rows.append(row)
    return rows


def validate_records(rows, sources, repeats):
    expected = {(source, repeat, event, method) for source in sources
                for repeat in range(repeats) for event in existing.EVENTS for method in METHODS}
    keys = [(r['source'], r['repeat'], r['event'], r['method']) for r in rows]
    counts = Counter(keys)
    failures = []
    if set(keys) != expected or len(rows) != len(expected) or any(n != 1 for n in counts.values()):
        failures.append({'kind': 'row_coverage', 'missing': sorted(expected-set(keys)),
                         'extra': sorted(set(keys)-expected),
                         'duplicates': [k for k, n in counts.items() if n > 1]})
    index = {k: r for k, r in zip(keys, rows)}
    for key, r in zip(keys, rows):
        problems = []
        if not all(r[k] for k in ('answer_matches_oracle', 'state_matches_oracle',
                                  'decision_matches_oracle', 'evidence_signatures_verified')):
            problems.append('oracle_or_evidence_mismatch')
        if r['wrong_delivery'] or r['wrong_query_reuse']:
            problems.append('wrong_delivery_or_reuse')
        if not isinstance(r['query_executed'], bool) or not isinstance(r['query_reused'], bool) or r['query_executed'] == r['query_reused']:
            problems.append('query_counter_partition')
        if r['method'] == 'B0' and not r['query_executed']:
            problems.append('B0_did_not_execute')
        if r['event'] == 'initial' and (not r['query_executed'] or r['evidence_hits'] or r['premise_checks'] != 4):
            problems.append('initial_cache_not_empty')
        if r['method'] in ('B0', 'B1') and (r['premise_checks'] != 4 or r['evidence_hits']):
            problems.append('full_business_not_full')
        if r['method'] in ('B1', 'B2') and r['event'] != 'initial':
            previous_event = existing.EVENTS[existing.EVENTS.index(r['event'])-1]
            prev = index.get((r['source'], r['repeat'], previous_event, r['method']))
            if prev and r['query_executed'] != (r['query_footprint'] != prev['query_footprint']):
                problems.append('footprint_execution_rule')
        if r['business_state'] in ('INVALID', 'UNKNOWN') and r['delivery_decision'] != 'BLOCK':
            problems.append('unsafe_delivery')
        if r['method'] == 'B1':
            other = index.get((r['source'], r['repeat'], r['event'], 'B2'))
            if other and any(r[k] != other[k] for k in ('answer', 'business_state', 'delivery_decision', 'premise_states', 'query_footprint', 'query_executed', 'dependency_versions')):
                problems.append('B1_B2_disagreement')
        if problems:
            failures.append({'key': key, 'problems': problems})
    return {'passed': not failures, 'expected_rows': len(expected), 'actual_rows': len(rows),
            'unique_rows': len(counts), 'failures': failures}


def distribution(values):
    return {'per_repeat_ms': values, 'median_ms': statistics.median(values),
            'mean_ms': statistics.mean(values), 'stddev_ms': statistics.stdev(values) if len(values)>1 else None,
            'min_ms': min(values), 'max_ms': max(values),
            'sample_definition': 'One sum across selected notices per timing repeat; repeats not independent cases'}


def summarize(rows, sources, repeats, amount_records, validation):
    methods = []
    for method in METHODS:
        selected = [r for r in rows if r['method'] == method]
        per_repeat = []
        for repeat in range(repeats):
            warm = [r for r in selected if r['repeat'] == repeat and r['event'] == 'initial']
            maintained = [r for r in selected if r['repeat'] == repeat and r['event'] != 'initial']
            per_repeat.append({'repeat': repeat, 'initialization_ms': sum(r['maintenance_ms'] for r in warm),
                'maintenance_ms': sum(r['maintenance_ms'] for r in maintained),
                **{k: sum(r[k] for r in maintained) for k in ('query_executed', 'query_reused', 'premise_checks', 'evidence_hits')}})
        methods.append({'method': method, 'per_repeat': per_repeat,
            'maintenance': distribution([r['maintenance_ms'] for r in per_repeat]),
            'initialization': distribution([r['initialization_ms'] for r in per_repeat]),
            'totals_include_initialization': True,
            **{k+'_total': sum(r[k] for r in selected) for k in ('query_executed', 'query_reused', 'premise_checks', 'evidence_hits', 'wrong_delivery', 'wrong_query_reuse')},
            **{k+'_mismatches': sum(not r[k+'_matches_oracle'] for r in selected) for k in ('answer', 'state', 'decision')},
            'all_correctness_checks_passed': all(r['answer_matches_oracle'] and r['state_matches_oracle'] and r['decision_matches_oracle'] and not r['wrong_delivery'] and not r['wrong_query_reuse'] for r in selected)})
    return {'stage': 'controlled_TED_DCM_three_way', 'notices': len(sources), 'sources': sources,
            'amount_records': amount_records, 'events_per_notice': len(existing.EVENTS), 'repeats': repeats,
            'methods': methods, 'B1_B2_state_and_delivery_agree': not any(
                'B1_B2_disagreement' in f.get('problems', []) for f in validation['failures']),
            'validation': validation, 'passed': validation['passed'], 'human_reviewed': False,
            'limitations': LIMITATIONS}


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, tuple)) else v for k, v in row.items()})


def write_report(out, summary):
    table = []
    for m in summary['methods']:
        table.append({'method': m['method'], 'notices': summary['notices'], 'repeats': summary['repeats'],
            'maintenance_median_ms': m['maintenance']['median_ms'],
            'initialization_median_ms': m['initialization']['median_ms'],
            **{k: m[k] for k in ('query_executed_total','query_reused_total','premise_checks_total','evidence_hits_total','answer_mismatches','state_mismatches','decision_mismatches','wrong_delivery_total','wrong_query_reuse_total')}})
    write_csv(out/'comparison.csv', table)
    lines = ['# DCM 三策略实际运行结果', '',
        f"本次为 {'小规模 smoke test' if summary['notices']<128 else '128 公告实验'}：{summary['notices']} 个公告，{summary['amount_records']} 条范围内金额，7 个顺序事件，{summary['repeats']} 次计时重复，共 {summary['validation']['actual_rows']} 条记录。",
        '', '旧 OR-Full 为全量业务验证，同时复用查询结果；新 B0 每个事件实际执行 SPARQL。B1/B2 仅业务验证调度方式不同。', '',
        '|策略|后续维护中位数 ms|初始化中位数 ms|查询执行/复用总数|前提检查/证据命中总数|', '|---|---:|---:|---:|---:|']
    for m in summary['methods']:
        lines.append(f"|{m['method']}|{m['maintenance']['median_ms']:.6f}|{m['initialization']['median_ms']:.6f}|{m['query_executed_total']}/{m['query_reused_total']}|{m['premise_checks_total']}/{m['evidence_hits_total']}|")
    lines += ['', f"正确性验收：{summary['passed']}；B1/B2 逐事件业务状态与交付决定一致：{summary['B1_B2_state_and_delivery_agree']}。详细错误计数见 comparison.csv 和 summary.json。",
        '', '总计数包含初始化；维护耗时排除 initial。统计样本是每次重复中所有选中公告的维护时间之和。单次重复的样本标准差留空，不计算 P95。',
        '', '计时包括查询 footprint、依赖签名、实际查询或缓存复用、业务验证、交付决定；排除解析、事件快照构造、oracle、证据审计、文件 I/O 和 XML/RML 物化。初始化单独计时，缓存对象分配发生在计时之前。B0 同样计算共同依赖签名和 footprint，但从不复用查询答案。',
        '', 'scope 是显式应用测试元数据；业务期望未经独立人工审核（human_reviewed=false）。受控 TED 派生数据维护不代表生产系统自然演化。无范围内金额的公告在 scope 恢复后仍为 UNKNOWN。',
        '', '本次小规模测试仅验证实现与输出，不据此声称性能优越。相同公告的计时重复不构成独立数据集。', '', '## 已知限制', '']
    lines += ['- '+line for line in LIMITATIONS]
    (out/'结果说明.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def parser():
    base = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--adapter', type=Path, default=base/'results/ted_amount_adapter_v2')
    ap.add_argument('--out', type=Path, default=base/'results/dcm_three_way_v1')
    ap.add_argument('--limit', type=int, default=None, help='First N sources in adapter audit order; default 2')
    ap.add_argument('--repeats', type=int, default=None, help='Default 1; with --full default 5')
    ap.add_argument('--full', action='store_true', help='Explicitly permit all 128 notices')
    return ap


def execute(args):
    adapter, out = args.adapter.resolve(), args.out.resolve()
    if out.exists():
        raise FileExistsError('Refusing existing output directory; use a new --out: '+str(out))
    limit = args.limit if args.limit is not None else (0 if args.full else 2)
    repeats = args.repeats if args.repeats is not None else (5 if args.full else 1)
    if limit < 0 or repeats < 1 or (not args.full and (limit == 0 or limit >= 128)):
        raise ValueError('Positive repeats required; all-notice execution requires --full')
    audit = json.loads((adapter/'summary.json').read_text(encoding='utf-8'))
    adapter_manifest = json.loads((adapter/'manifest.json').read_text(encoding='utf-8'))
    if not audit.get('audit_passed') or audit['selected_xml'] != 128 or len(audit['records']) != 128:
        raise ValueError('Existing 128-notice adapter reconciliation must pass')
    selected = audit['records'][:limit] if limit else audit['records']
    sources = [r['source'] for r in selected]
    if len(sources) != len(set(sources)):
        raise ValueError('Duplicate source IDs')
    mapping_path = adapter/'adapted_mapping.ttl'
    mapping = Graph().parse(mapping_path, format='turtle')
    if str(to_isomorphic(mapping).graph_digest()) != adapter_manifest['derived_mapping_sha256']:
        raise ValueError('Frozen mapping differs from adapter manifest')
    if existing.mapping_definition({'mapping': mapping}) != State.VALID:
        raise ValueError('Frozen mapping contract failed')
    mapping_hash = existing.sha(mapping_path)
    paths = {r['source']: adapter/'notices'/existing.hashlib.sha256(r['source'].encode()).hexdigest()[:16]/'output.ttl' for r in selected}
    for r in selected:
        if existing.sha(paths[r['source']]) != r['rdf_sha256']:
            raise ValueError('Frozen RDF hash mismatch: '+r['source'])
    related = [Path(__file__), Path(existing.__file__), Path(sys.modules['dcm_scheduler'].__file__)]
    test_path = Path(__file__).with_name('test_ted_three_way.py')
    if test_path.is_file():
        related.append(test_path)
    base = Path(existing.__file__).parent
    related += [base/'reconcile_ted_amount_mapping.py', base/'prepare_ted_rdf_batch.py']
    manifest = {'started_utc': datetime.now(timezone.utc).isoformat(), 'python_executable': sys.executable,
        'python': sys.version, 'rdflib': rdflib.__version__, 'platform': platform.platform(),
        'code_sha256': {str(p): existing.sha(p) for p in related},
        'mapping_sha256': mapping_hash, 'adapter_manifest_sha256': existing.sha(adapter/'manifest.json'),
        'adapter_summary_sha256': existing.sha(adapter/'summary.json'),
        'input_rdf_sha256': {s: existing.sha(p) for s,p in paths.items()}, 'sources': sources,
        'adapter': str(adapter), 'command_argv': sys.argv, 'arguments': vars(args) | {'adapter': str(adapter), 'out': str(out)},
        'effective_limit': limit, 'effective_repeats': repeats, 'query': existing.QUERY,
        'strategies': DEFINITIONS, 'events': existing.EVENTS,
        'order': 'Rotate methods by source index + repeat + event index; fresh runners for each source/repeat',
        'footprint': 'Existing amount/currency projections + typed focus identities + unchanged query; B1 and B2 identical',
        'timed': ['query footprint and business dependency signatures (also B0)', 'SPARQL or query reuse', 'business scheduler/checks/evidence lookup', 'delivery decision'],
        'excluded': ['RDF parsing', 'event snapshot generation', 'oracle', 'record formatting', 'evidence auditing', 'file I/O', 'XML/RML materialization', 'runner/cache object allocation'],
        'initialization': 'initial event separately timed; fresh empty query/evidence caches; excludes runner object allocation',
        'source': 'Existing reconciled TED-derived RDF; explicit controlled application metadata',
        'human_reviewed': False, 'limitations': LIMITATIONS}
    out.mkdir(parents=True, exist_ok=False)
    existing.save(out/'manifest.json', manifest)
    rows = []
    try:
        for i, record in enumerate(selected):
            original = Graph().parse(paths[record['source']], format='turtle')
            for repeat in range(repeats):
                rows.extend(run_case(original, mapping, mapping_hash, record['source'], repeat, i, record['rdf_sha256']))
            print(f"[{i+1}/{len(selected)}] {record['source']} completed", flush=True)
        validation = validate_records(rows, sources, repeats)
        summary = summarize(rows, sources, repeats, sum(r['scope_records'] for r in selected), validation)
        existing.save(out/'event_records.json', rows)
        write_csv(out/'event_records.csv', rows)
        existing.save(out/'summary.json', summary)
        write_report(out, summary)
        manifest.update(completed_utc=datetime.now(timezone.utc).isoformat(), passed=summary['passed'])
        existing.save(out/'manifest.json', manifest)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        if not summary['passed']:
            raise AssertionError('Acceptance validation failed; see summary.json')
        return summary
    except Exception as exc:
        existing.save(out/'failure.json', {'error': f'{type(exc).__name__}: {exc}', 'completed_rows': len(rows)})
        existing.save(out/'partial_event_records.json', rows)
        raise


def main(argv=None):
    args = parser().parse_args(argv)
    execute(args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
