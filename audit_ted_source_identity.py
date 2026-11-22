import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys
from xml.parsers import expat
from xml.etree.ElementTree import TreeBuilder


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_xml(raw):
    if len(raw) > 50_000_000:
        raise ValueError('XML exceeds 50 MB audit limit')
    builder = TreeBuilder()
    parser = expat.ParserCreate(namespace_separator='}')
    def expanded(name):
        return '{'+name if '}' in name else name
    def reject(*args):
        raise ValueError('DTD/entities/external references are forbidden')
    parser.StartElementHandler = lambda name, attrs: builder.start(expanded(name), {expanded(k):v for k,v in attrs.items()})
    parser.EndElementHandler = lambda name: builder.end(expanded(name))
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.Parse(raw, True)
    return builder.close()


def local(tag):
    return tag.split('}')[-1]


def extract(root):
    fields = {k: [] for k in ('notice_identifiers','form_identifiers','dates','versions','related_references')}
    namespaces = set()
    def record(category, path, value, kind='text', attributes=None):
        if value:
            fields[category].append({'xpath':path,'value':value,'kind':kind,'attributes':attributes or {}})
    def visit(el, path, ancestors):
        tag = local(el.tag)
        if el.tag.startswith('{'):
            namespaces.add(el.tag[1:].split('}')[0])
        text = (el.text or '').strip()
        if path.count('/') == 1:
            for key in ('DOC_ID',):
                record('notice_identifiers', path+'/@'+key, el.get(key))
            for key in ('VERSION','EDITION'):
                record('versions', path+'/@'+key, el.get(key))
        if tag == 'NO_DOC_OJS' and ancestors[-1:] == ['NOTICE_DATA']:
            record('notice_identifiers', path, text)
        if tag == 'TD_DOCUMENT_TYPE':
            record('form_identifiers', path, text, attributes=el.attrib)
        if ancestors[-1:] == ['FORM_SECTION']:
            record('form_identifiers', path, tag, 'element_name_and_attributes', el.attrib)
        if not list(el) and ('DATE' in tag or tag in ('DATE_PUB','DS_DATE_DISPATCH')):
            record('dates', path, text, attributes=el.attrib)
        if any('REF' in a or 'PREVIOUS' in a for a in ancestors) and not list(el) and ('DOC' in tag or 'NOTICE' in tag):
            record('related_references', path, text, attributes=el.attrib)
        if ('VERSION' in tag or 'REVISION' in tag) and not list(el):
            record('versions', path, text, attributes=el.attrib)
        for key,value in el.attrib.items():
            if key in ('VERSION','REVISION') and path.count('/')>1:
                record('versions', path+'/@'+key, value)
        counts = defaultdict(int)
        for child in el:
            counts[child.tag] += 1
            visit(child, path+'/'+local(child.tag)+'['+str(counts[child.tag])+']', ancestors+[tag])
    visit(root, '/'+local(root.tag), [])
    return fields, sorted(namespaces)


def csv_write(path, rows):
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader()
        writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()} for row in rows)


def run(full_manifest, source_root, out):
    if out.exists():
        raise FileExistsError('Refusing existing output; use a new --out: '+str(out))
    m=json.loads(full_manifest.read_text(encoding='utf-8'))
    adapter=Path(m['adapter'])
    adapter_manifest=adapter/'manifest.json'
    am=json.loads(adapter_manifest.read_text(encoding='utf-8'))
    if sha(adapter_manifest)!=m['adapter_manifest_sha256']:
        raise ValueError('Adapter manifest hash differs from completed run')
    sources=m['sources']
    if len(sources)!=128 or len(set(sources))!=128:
        raise ValueError('Expected exactly 128 manifest source records')
    rows=[];logs=[]
    for i,rel in enumerate(sources):
        path=(source_root/rel).resolve()
        if not path.is_relative_to(source_root.resolve()):
            raise ValueError('Source path escapes declared root')
        row={'source_index':i,'source_path':str(path),'source_relative_path':rel,'file_name':path.name,
             'sha256':'','file_size_bytes':'','expected_sha256':am['source_hashes'][rel],
             'hash_matches_adapter':False,'parse_ok':False,'parse_error':'','xml_root':'','root_namespace':'',
             'notice_id':'','publication_id':'','namespace_bindings':{},
             **{k:[] for k in ('notice_identifiers','form_identifiers','dates','versions','related_references')}}
        try:
            raw=path.read_bytes(); row.update(sha256=hashlib.sha256(raw).hexdigest(),file_size_bytes=len(raw))
            row['hash_matches_adapter']=row['sha256']==row['expected_sha256']
            root=safe_xml(raw);fields,namespaces=extract(root)
            row.update(fields);row.update(parse_ok=True,xml_root=local(root.tag),
                root_namespace=root.tag[1:].split('}')[0] if root.tag.startswith('{') else '',
                namespace_bindings={f'ns{j}':uri for j,uri in enumerate(namespaces)},notice_id=root.get('DOC_ID',''))
            row['publication_id']=';'.join(f['value'] for f in fields['notice_identifiers'] if '/@DOC_ID' not in f['xpath'])
        except Exception as exc:
            row['parse_error']=f'{type(exc).__name__}: {exc}'
        row['field_presence']={k:'present' if row[k] else 'not_present' for k in ('notice_identifiers','form_identifiers','dates','versions','related_references')}
        rows.append(row);logs.append(f"[{i+1}/128] parse={row['parse_ok']} hash={row['hash_matches_adapter']} {rel} {row['parse_error']}")
    candidates={}
    def group_by(kind, keyfunc):
        index=defaultdict(list)
        for r in rows:
            for key in set(keyfunc(r)):
                if key:index[key].append(r)
        for key,group in index.items():
            if len(group)>1:
                ids=tuple(sorted(r['source_index'] for r in group))
                candidates.setdefault(ids,[]).append({'reason':kind,'value':key})
        return sum(len(v)>1 for v in index.values())
    byte_groups=group_by('same_sha256',lambda r:[r['sha256']])
    basename_groups=group_by('same_basename',lambda r:[r['file_name']])
    id_groups=group_by('same_internal_DOC_ID',lambda r:[r['notice_id']])
    group_by('same_internal_publication_identifier',lambda r:[r['publication_id']])
    group_by('shared_related_notice_reference',lambda r:[v['value'] for v in r['related_references']])
    groups=[]
    for member_ids,reasons in sorted(candidates.items()):
        members=[rows[i] for i in member_ids];ids={r['notice_id'] for r in members};pubs={r['publication_id'] for r in members}
        same_sha=len({r['sha256'] for r in members})==1
        same_notice=len(ids)==1 and '' not in ids and len(pubs)==1 and '' not in pubs
        distinct=len(ids)==len(members) and '' not in ids and len(pubs)==len(members) and '' not in pubs
        status='confirmed_same_notice' if same_notice else 'confirmed_distinct' if distinct else 'ambiguous'
        relation='byte_identical_representation' if same_sha and same_notice else 'same_notice_nonidentical_representation' if same_notice else 'distinct_publications_shared_reference' if distinct else 'unresolved'
        groups.append({'group_id':f'group_{len(groups)+1:03d}','grouping_evidence':reasons,
            'record_paths':[r['source_relative_path'] for r in members], 'member_count':len(members),
            'same_sha256':same_sha,'classification':status,'relationship':relation,
            'confidence':'high' if status!='ambiguous' else 'insufficient',
            'reason':'Compare actual DOC_ID and direct NOTICE_DATA/NO_DOC_OJS; shared prior references alone do not establish notice identity.',
            'metadata':[{k:r[k] for k in ('notice_id','publication_id','sha256','form_identifiers','dates','versions','related_references')} for r in members],
            'human_review_needed':'Schema/export VERSION is not a notice revision; related procurement/version lineage needs human review.' if not same_sha else 'No human business-label verification implied.'})
    duplicate_records=sum(len([r for r in rows if r['sha256']==h])-1 for h in {r['sha256'] for r in rows} if h)
    focus={name:[g['classification']+': '+g['relationship'] for g in groups if any(Path(p).name==name for p in g['record_paths']) and any(e['reason']=='same_basename' and e['value']==name for e in g['grouping_evidence'])] for name in ('234762-2021.xml','337360-2021.xml','366836-2021.xml','661127-2021.xml')}
    unique_ids=len({r['notice_id'] for r in rows if r['notice_id']})
    summary={'source_records':128,'parsed':sum(r['parse_ok'] for r in rows),
        'all_source_hashes_match_adapter':all(r['hash_matches_adapter'] for r in rows),
        'byte_duplicate_groups':byte_groups,'byte_duplicate_excess_records':duplicate_records,
        'basename_duplicate_groups':basename_groups,'same_internal_notice_id_groups':id_groups,
        'unique_internal_notice_ids':unique_ids,
        'candidate_group_classifications':dict(__import__('collections').Counter(g['classification'] for g in groups)),
        'confirmed_notice_revision_groups':0,'revision_group_count_status':'No notice-revision chronology established; export VERSION is not interpreted as notice revision.',
        'focus_file_conclusions':focus,
        'paper_wording':f'{len(rows)} XML source records, representing {unique_ids} distinct internal publication identifiers ({byte_groups} byte-identical duplicate groups); do not call these {len(rows)} independent notices.'}
    out.mkdir(parents=True,exist_ok=False)
    csv_write(out/'source_identity_audit.csv',rows)
    csv_write(out/'duplicate_candidate_groups.csv',groups)
    manifest={'created_utc':datetime.now(timezone.utc).isoformat(),'python':sys.version,'platform':platform.platform(),
        'full_manifest':str(full_manifest),'full_manifest_sha256':sha(full_manifest),
        'adapter_manifest_sha256':sha(adapter_manifest),'source_root':str(source_root.resolve()),
        'source_resolution':'Completed manifest sources, relative to adapter script declared --data-root default; every source verified against adapter manifest source_hashes.',
        'audit_script_sha256':sha(Path(__file__)),'source_sha256':{r['source_relative_path']:r['sha256'] for r in rows},
        'xml_security':'Expat rejects all DTD/entity declarations and external references, never loads external resources; size capped at 50 MB.',
        'field_provenance':'Actual XML fields and indexed local-name XPath with per-record namespace URIs. Missing fields marked not_present; these paths are audit locators, not namespace-free executable XPath expressions.',
        'version_caveat':'TED_EXPORT/@VERSION is schema/export version; EDITION is recorded without assuming notice revision.',
        'summary':summary}
    (out/'source_identity_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 源记录身份审计','',summary['paper_wording'],'',
        f"解析 {summary['parsed']}/128；源文件哈希全部匹配：{summary['all_source_hashes_match_adapter']}。字节重复 {byte_groups} 组，多余表示 {duplicate_records} 条；basename 重复 {basename_groups} 组。",'',
        '依据完成实验 manifest 的 sources 确定样本，再按现有 adapter 的声明 data-root 解析原始 XML。逐个匹配 adapter manifest 的原始 source SHA-256，不通过全盘扫描推断样本。', '',
        '身份主要依据 TED_EXPORT/@DOC_ID 与直接 CODED_DATA_SECTION/NOTICE_DATA/NO_DOC_OJS，两者与字节哈希、表单属性、日期一起核对。REF_NOTICE 仅作为相关公告引用，不作为当前公告ID。字段来自实际 XML，CSV 内保留带索引路径与命名空间；缺失标为 not_present。','',
        'TED_EXPORT/@VERSION 是导出/模式版本，不能据此声称发现同一公告的修订历史。不同哈希也不自动视为不同公告；共享前序引用可对应不同发布公告，不等于同一采购流程或版本。', '',
        '## 四个重点文件名','']
    lines += [f'- {name}: '+ '; '.join(conclusions) for name,conclusions in focus.items()]
    lines += ['', '## 限制与论文表述','', f'建议使用“{len(rows)} 条 XML 源记录，覆盖 {unique_ids} 个不同内部公告标识”，避免“{len(rows)} 个独立公告”。未删除、去重或重算旧基准；既有结果仍对应原始{len(rows)}条记录。金额对象身份包含源路径，因此即使XML相同，不同路径的派生RDF也可能不同。', '',
        '同一标识的字节相同表示可确认重复输入；对自然历史版本、采购项目归属、相关引用链的业务解释仍需人工复核。未联网补充资料，不将本次元数据身份核对视为业务标签人工审核。', '',
        '完整候选分类统计：'+json.dumps(summary['candidate_group_classifications'],ensure_ascii=False), '',
        '未建立可确认的公告修订版本组；不能将计数0解释为证明不存在版本关系。所有源记录保留，未进行XML/RML转换或全量维护实验。']
    (out/'source_identity_report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    logs.append(json.dumps(summary,ensure_ascii=False,indent=2))
    (out/'source_audit.log').write_text('\n'.join(logs)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    return summary


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest',type=Path,required=True);ap.add_argument('--source-root',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args();run(args.manifest,args.source_root,args.out)
