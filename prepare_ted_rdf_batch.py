import argparse
import csv
import hashlib
import json
import subprocess
import time
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET
from rdflib import Graph, Namespace


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def main():
    base = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-root', type=Path, default=base/'datasets/ted-rdf-mapping/mappings/package_F03/test_data')
    ap.add_argument('--mapping', type=Path, default=base/'results/official_api_20261016_180019/updated_mappings.ttl')
    ap.add_argument('--jar', type=Path, default=base/'tools/rmlmapper-6.2.2-r369-all.jar')
    ap.add_argument('--out', type=Path, default=base/'results/ted_rdf_batch_v1')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--timeout', type=int, default=180)
    a = ap.parse_args()
    if a.limit < 0 or a.timeout < 1:
        ap.error('Invalid limit/timeout')
    a.data_root = a.data_root.resolve()
    a.out = a.out.resolve()
    a.jar = a.jar.resolve()
    a.mapping = a.mapping.resolve()
    files = sorted(a.data_root.rglob('*.xml'))
    if not files:
        ap.error('No XML files in data root')
    total = len(files)
    if a.limit:
        files = files[:a.limit]
    for path in (a.mapping, a.jar):
        if not path.is_file():
            ap.error('Missing file: ' + str(path))
    subprocess.run(['java', '-version'], check=True, capture_output=True)
    mapping_text = a.mapping.read_text(encoding='utf-8-sig')
    Graph().parse(data=mapping_text, format='turtle')
    config = {
        'version': 1, 'script_sha256': sha(Path(__file__)),
        'jar_sha256': sha(a.jar), 'mapping_sha256': sha(a.mapping),
        'timeout': a.timeout,
        'source_hashes': {p.relative_to(a.data_root).as_posix(): sha(p) for p in files},
        'selection': 'ORIGINAL; prefer EN, then language lexicographic, then XML order',
        'normalization': 'selected F03_2014 as root; strip element namespaces; retain attributes',
        'publisher_binding': 'https://ted.europa.eu/',
    }
    a.out.mkdir(parents=True, exist_ok=True)
    manifest = a.out/'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != config:
        raise RuntimeError('Inputs/configuration changed. Use a new --out directory.')
    write_json(manifest, config)
    rows = []
    epo = Namespace('http://data.europa.eu/a4g/ontology#')
    for index, source in enumerate(files, 1):
        rel = source.relative_to(a.data_root).as_posix()
        job = a.out/'notices'/hashlib.sha256(rel.encode()).hexdigest()[:16]
        job.mkdir(parents=True, exist_ok=True)
        record = job/'result.json'
        if record.exists():
            old = json.loads(record.read_text(encoding='utf-8'))
            if old.get('status') == 'ok' and (job/'output.ttl').is_file():
                if sha(job/'output.ttl') == old['rdf_sha256']:
                    rows.append(old)
                    print(f'[{index}/{len(files)}] SKIP {rel}', flush=True)
                    continue
        started = time.perf_counter()
        row = {'source': rel, 'source_sha256': config['source_hashes'][rel],
               'job': str(job), 'status': 'failed'}
        try:
            root = ET.parse(source).getroot()
            row['source_namespace'] = root.tag.rsplit('}', 1)[0].lstrip('{') if '}' in root.tag else ''
            forms = [e for e in root.iter() if e.tag.split('}')[-1] == 'F03_2014' and e.get('CATEGORY') == 'ORIGINAL']
            if not forms:
                raise ValueError('No ORIGINAL F03_2014')
            selected = sorted(enumerate(forms), key=lambda t: (t[1].get('LG') != 'EN', t[1].get('LG', ''), t[0]))[0]
            form = selected[1]
            row.update(selected_original_index=selected[0], original_forms_available=len(forms), language=form.get('LG'))
            for node in form.iter():
                node.tag = node.tag.split('}')[-1]
            row['xml_currency_elements'] = sum('CURRENCY' in e.attrib for e in form.iter())
            row['xml_currency_counts'] = dict(Counter(e.attrib['CURRENCY'] for e in form.iter() if 'CURRENCY' in e.attrib))
            ET.ElementTree(form).write(job/'source.xml', encoding='utf-8', xml_declaration=True)
            text = mapping_text.replace('%1$s', 'source.xml').replace('%2$s', 'F03_2014').replace('%3$s', 'https://ted.europa.eu/')
            (job/'mapping.ttl').write_text(text, encoding='utf-8')

            (job/'output.ttl').unlink(missing_ok=True)
            start_mapper = time.perf_counter()
            with (job/'mapper.log').open('w', encoding='utf-8') as log:
                completed = subprocess.run(['java', '-Xmx2g', '-jar', str(a.jar), '-m', 'mapping.ttl',
                    '-o', 'output.ttl', '-s', 'turtle'], cwd=job, stdout=log,
                    stderr=subprocess.STDOUT, timeout=a.timeout)
            row['mapper_seconds'] = time.perf_counter() - start_mapper
            if completed.returncode:
                raise RuntimeError(f'Java exit={completed.returncode}; see mapper.log')
            g = Graph().parse(job/'output.ttl', format='turtle')
            if not len(g):
                raise ValueError('Empty RDF; not accepted as a successful conversion')
            row.update(status='ok', rdf_triples=len(g), rdf_sha256=sha(job/'output.ttl'),
                       rdf_amount_triples=sum(1 for _ in g.triples((None, epo.hasAmountValue, None))),
                       rdf_currency_triples=sum(1 for _ in g.triples((None, epo.hasCurrency, None))))
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
        row['preparation_seconds'] = time.perf_counter() - started
        write_json(record, row)
        rows.append(row)
        print(f'[{index}/{len(files)}] {row["status"]} {rel} triples={row.get("rdf_triples", 0)}', flush=True)
    successful = [r for r in rows if r['status'] == 'ok']
    summary = {
        'stage': 'real_TED_mapping_preparation', 'available_xml': total, 'selected_xml': len(files),
        'successful': len(successful), 'failed': len(rows)-len(successful),
        'sum_per_notice_rdf_triples': sum(r['rdf_triples'] for r in successful),
        'notices_with_rdf_amount': sum(r['rdf_amount_triples'] > 0 for r in successful),
        'notices_with_rdf_currency': sum(r['rdf_currency_triples'] > 0 for r in successful),
        'comparison_experiment_completed': False,
        'notes': ['Timings include Java startup and are preparation costs, not D speed measurements.',
                  'No merged RDF is produced: cross-notice generated-IRI collisions have not been resolved.',
                  'XML/RDF counts alone do not establish complete or semantically correct mapping coverage.',
                  'Original-language selection discards other original forms when several exist; see per-file records.'],
        'records': rows,
    }
    write_json(a.out/'summary.json', summary)
    fields = ['source', 'status', 'language', 'original_forms_available', 'rdf_triples',
              'xml_currency_elements', 'rdf_amount_triples', 'rdf_currency_triples', 'mapper_seconds', 'error']
    with (a.out/'conversion.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    print('DONE', 'successful=', summary['successful'], 'failed=', summary['failed'])
    print('RESULT:', a.out/'summary.json')
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
