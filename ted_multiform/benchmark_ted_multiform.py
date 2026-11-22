import os, csv, hashlib, json, sys, time, statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from rdflib import Graph, Namespace

BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE.parent))
import benchmark_ted_contract as existing
import benchmark_ted_three_way as core
from dcm_scheduler import State

DATA=BASE/'dataset'
MANIFEST=BASE/'source_manifest.csv'
OUT=BASE/'comparison_all_forms_20261106_v2'
RR=Namespace('http://www.w3.org/ns/r2rml#')
EPO=Namespace('http://data.europa.eu/a4g/ontology#')
XSD=Namespace('http://www.w3.org/2001/XMLSchema#')



def mapping_contract(snapshot):
    g=snapshot['mapping']
    amount_poms=set(g.subjects(RR.predicate,EPO.hasAmountValue))
    currency_poms=set(g.subjects(RR.predicate,EPO.hasCurrency))
    if not amount_poms or not currency_poms:
        return State.INVALID
    valid=0
    for ap in amount_poms:
        decimal=any((om,RR.datatype,XSD.decimal) in g for om in g.objects(ap,RR.objectMap))
        if not decimal: continue
        for tm in g.subjects(RR.predicateObjectMap,ap):
            if any((tm,RR.predicateObjectMap,cp) in g for cp in currency_poms):
                valid+=1; break
    return State.VALID if valid else State.INVALID



existing.mapping_definition=mapping_contract

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def stable_hash(items):
    h=hashlib.sha256()
    for name,digest in sorted(items): h.update(name.encode()); h.update(b'\0'); h.update(digest.encode()); h.update(b'\n')
    return h.hexdigest()

def write_csv(path,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else [])
        if rows: w.writeheader(); w.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list,tuple)) else v for k,v in r.items()} for r in rows)

def main():
    import argparse
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=739)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args()
    if not 1 <= args.limit <= 739 or args.repeats < 1:
        parser.error('require 1 <= limit <= 739 and positive repeats')
    OUT = args.out
    if OUT.exists(): raise FileExistsError('Refusing to overwrite: '+str(OUT))
    records=list(csv.DictReader(MANIFEST.open(encoding='utf-8-sig')))
    if len(records) != 739:
        raise ValueError('Expected the complete 739-notice source manifest')
    records = records[:args.limit]

    package_graphs={}; package_hashes={}; package_file_hashes={}
    for form in sorted({r['form'] for r in records}):
        pkg='package_'+form
        folder=DATA/'mappings'/pkg/'transformation'/'mappings'
        files=sorted(folder.glob('*.ttl'))
        if not files: raise FileNotFoundError('No transformation mappings for '+pkg)
        g=Graph()
        for p in files: g.parse(p,format='turtle')
        if mapping_contract({'mapping':g}) != State.VALID: raise ValueError('Generic amount/currency mapping contract failed for '+pkg)
        hashes=[(p.relative_to(DATA).as_posix(),sha(p)) for p in files]
        package_graphs[pkg]=g; package_hashes[pkg]=stable_hash(hashes); package_file_hashes[pkg]=dict(hashes)


    selected=[]; coverage=defaultdict(lambda:Counter()); input_hashes={}; total_triples=0; total_amount_nodes=0
    for i,r in enumerate(records,1):
        source=r['source_rel']; path=DATA.joinpath(*r['rdf_rel'].split('/'))
        if sha(path)!=r['rdf_sha256']: raise ValueError('RDF hash mismatch: '+source)
        g=Graph().parse(path,format='turtle')
        if not len(g): raise ValueError('Empty official RDF: '+source)
        mapping=package_graphs[r['package']]
        if mapping_contract({'mapping':mapping})!=State.VALID: raise ValueError('Mapping check failed: '+r['package'])
        amount_nodes=len(set(g.subjects(EPO.hasAmountValue,None)))
        record={**r,'source':source,'path':path,'rdf_sha256':r['rdf_sha256'],'scope_records':amount_nodes,
                'rdf_triples':len(g),'schema_year':r['notice_id'].rsplit('-',1)[-1]}
        selected.append(record); input_hashes[source]=r['rdf_sha256']
        c=coverage[r['form']]; c['notices']+=1; c['rdf_triples']+=len(g); c['amount_subjects']+=amount_nodes; c['with_amount']+=amount_nodes>0
        total_triples+=len(g); total_amount_nodes+=amount_nodes
        if i%100==0: print(f'Preflight {i}/{len(records)}',flush=True)
    mapping=Graph()
    repeats=args.repeats
    sources=[r['source'] for r in selected]
    mapping_by_form=package_graphs
    mapping_hash_by_form=package_hashes
    related=[Path(__file__),Path(core.__file__),Path(existing.__file__),Path(sys.modules['dcm_scheduler'].__file__)]
    manifest={'stage':'TED seven-form full-vs-incremental maintenance','started_utc':datetime.now(timezone.utc).isoformat(),
      'source_archive':'ted-rdf-mapping.zip; pinned commit 2004fef480fe38706a5f15dc8db89806d005077d',
      'archive_sha256':sha(Path(os.environ.get('DCM_TED_ARCHIVE', Path(__file__).resolve().parents[1]/'datasets/ted-rdf-mapping.zip'))),
      'packages':sorted(package_graphs),'full_archive_raw_xml':800,'selected_unique_notice_xml':len(selected),'full_archive_identical_duplicate_test_fixtures_removed':61,
      'deduplication':'same publication ID; duplicate XMLs had matching SHA-256 within their package; cross-package collisions were rejected',
      'mapped_rdf_source':'official pre-generated Turtle outputs shipped with each matching form package; not newly converted in this run',
      'package_mapping_hashes':package_hashes,'package_mapping_file_hashes':package_file_hashes,
      'code_sha256':{str(p):sha(p) for p in related},'input_rdf_sha256':input_hashes,'events':existing.EVENTS,
      'strategies':core.DEFINITIONS,'repeats':repeats,'timed':'same DCM/B0/B1/B2 timed post-mapping maintenance path as the 128-source experiment',
      'excluded':['XML/RML conversion','initial RDF parsing','event snapshot construction','independent oracle','record serialization','filesystem I/O'],
      'per_form_coverage':{k:dict(v) for k,v in coverage.items()},'total_mapped_rdf_triples':total_triples,
      'total_amount_subjects_across_notices':total_amount_nodes,'human_reviewed':False,
      'limitations':['Curated mapping-suite test fixtures are not random TED annual samples.','Events are controlled synthetic updates applied independently to each source notice.','Post-mapping maintenance only; mapping conversion time is not measured.','Per-form mapping package is held fixed through each notice event stream.']}
    OUT.mkdir(parents=True)
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    coverage_rows=[{'form':k,**dict(v)} for k,v in sorted(coverage.items())]
    write_csv(OUT/'form_coverage.csv',coverage_rows)
    rows=[]; start=time.time()
    try:
        for i,r in enumerate(selected):
            pkg=r['package']; original=Graph().parse(r['path'],format='turtle')
            for repeat in range(repeats):
                rows.extend(core.run_case(original,mapping_by_form[pkg],mapping_hash_by_form[pkg],r['source'],repeat,i,r['rdf_sha256']))
            print(f"[{i+1}/{len(selected)}] {r['form']} {r['notice_id']} completed",flush=True)
        validation=core.validate_records(rows,sources,repeats)
        summary=core.summarize(rows,sources,repeats,total_amount_nodes,validation)
        summary.update({'raw_xml_fixtures':800,'identical_duplicates_removed':61,'form_count':7,'per_form_coverage':{k:dict(v) for k,v in coverage.items()},
            'mapped_rdf_triples':total_triples,'mapping_contract_valid_for_all_seven_packages':True,
            'experiment_type':'all 739 unique mapped TED test notices; controlled seven-event maintenance, three repeats',
            'duration_seconds':time.time()-start,'official_precomputed_rdf_used':True,'rml_conversion_timed':False})
        (OUT/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
        (OUT/'event_records.json').write_text(json.dumps(rows,ensure_ascii=False),encoding='utf-8')
        write_csv(OUT/'event_records.csv',rows)
        table=[]
        for m in summary['methods']:
            table.append({'method':m['method'],'unique_notices':summary['notices'],'forms':len(package_graphs),'repeats':repeats,
              'maintenance_ms_median':m['maintenance']['median_ms'],'initialization_ms_median':m['initialization']['median_ms'],
              'query_executed_total':m['query_executed_total'],'query_reused_total':m['query_reused_total'],
              'premise_checks_total':m['premise_checks_total'],'evidence_hits_total':m['evidence_hits_total'],
              'answer_mismatches':m['answer_mismatches'],'state_mismatches':m['state_mismatches'],'decision_mismatches':m['decision_mismatches'],
              'wrong_delivery_total':m['wrong_delivery_total'],'wrong_query_reuse_total':m['wrong_query_reuse_total']})
        write_csv(OUT/'comparison.csv',table)
        summarize_version_cache=['# TED 七表单全量与增量维护对比','',
          f"使用 {summary['notices']} 条去重后的公告映射结果（原始测试 XML 800 份，去除 61 份内容完全相同的测试副本），覆盖 7 个标准表单映射包；每条公告 7 个顺序事件，3 轮重复。",
          '', '输入为各自映射包仓库提供的预生成 RDF/Turtle；本实验计量映射后的查询与业务核验维护，不包含 XML/RML 转换耗时。各表单按自己的映射包处理，没有用 F03 规则跨表单转换。', '',
          '|策略|维护耗时中位数（ms）|初始化耗时中位数（ms）|查询执行/复用|前提检查/证据命中|状态/交付错误|','|---|---:|---:|---:|---:|---:|']
        for row,m in zip(table,summary['methods']): summarize_version_cache.append(f"|{row['method']}|{row['maintenance_ms_median']:.3f}|{row['initialization_ms_median']:.3f}|{row['query_executed_total']}/{row['query_reused_total']}|{row['premise_checks_total']}/{row['evidence_hits_total']}|{row['answer_mismatches']}/{row['state_mismatches']}/{row['decision_mismatches']}|")
        summarize_version_cache += ['',f"正确性检查通过：{summary['passed']}。每条公告事件流都使用当前完整图遍历生成独立 oracle；B1 与 B2 采用相同业务规则，差异仅在证据调度。",'', '## 各表单覆盖']
        for form,c in sorted(coverage.items()): summarize_version_cache.append(f"- {form}: {c['notices']} 条唯一公告，{c['amount_subjects']} 个金额对象，{c['rdf_triples']} 条 RDF 三元组。")
        summarize_version_cache += ['', '样例来自 OP-TED 映射包的人工挑选测试集，不能视为 TED 年度公告的随机样本。重复计时轮次作用于相同公告，不构成独立来源样本。', '']
        (OUT/'结果说明.md').write_text('\n'.join(summarize_version_cache),encoding='utf-8')
        manifest.update(completed_utc=datetime.now(timezone.utc).isoformat(),passed=summary['passed'],duration_seconds=time.time()-start)
        (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        print('SUMMARY',json.dumps({'passed':summary['passed'],'notices':summary['notices'],'rows':summary['validation']['actual_rows'],'methods':table,'duration_seconds':summary['duration_seconds']},ensure_ascii=False),flush=True)
        if not summary['passed']: raise AssertionError('Full vs incremental validation failed; see summary.json')
    except Exception as e:
        (OUT/'failure.json').write_text(json.dumps({'error':type(e).__name__+': '+str(e),'rows_so_far':len(rows)},indent=2,ensure_ascii=False),encoding='utf-8')
        if rows: write_csv(OUT/'partial_event_records.csv',rows)
        raise

if __name__=='__main__': main()

