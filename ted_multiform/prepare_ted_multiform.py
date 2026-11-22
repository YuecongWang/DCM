import os
import csv, hashlib, json, os, re, zipfile
from pathlib import Path

ZIP = Path(os.environ.get('DCM_TED_ARCHIVE', Path(__file__).resolve().parents[1]/'datasets/ted-rdf-mapping.zip'))
BASE = Path(__file__).resolve().parent
DATA = BASE / 'dataset'
PACKAGES = ['F03','F06','F13','F21','F22','F23','F25']
ROOT_EXPECT = 'ted-rdf-mapping-2004fef480fe38706a5f15dc8db89806d005077d/'

def sha_bytes(b): return hashlib.sha256(b).hexdigest()

with zipfile.ZipFile(ZIP) as z:
    prefix = z.namelist()[0].split('/')[0] + '/'
    allnames=set(z.namelist())
    raw=[]; package_counts={}
    for form in PACKAGES:
        pkg=f'package_{form}'
        test_prefix=prefix+f'mappings/{pkg}/test_data/'
        xmls=sorted(n for n in z.namelist() if n.startswith(test_prefix) and n.lower().endswith('.xml'))
        package_counts[form]={'xml_count':len(xmls)}
        for name in xmls:
            rel=name[len(prefix):]
            data=z.read(name)
            parts=rel.split('/')
            try: idx=parts.index('test_data')
            except ValueError: raise RuntimeError(rel)
            folder=parts[idx+1]
            file=parts[-1]
            notice=Path(file).stem
            out_rel='/'.join(parts[:idx]+['output',folder,notice,notice+'.ttl'])
            out_name=prefix+out_rel
            if out_name not in allnames:
                raise FileNotFoundError(f'matching official mapped RDF missing for {rel}: {out_rel}')
            out_data=z.read(out_name)

            for member in [rel,out_rel]:
                target=DATA.joinpath(*member.split('/'))
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists(): target.write_bytes(z.read(prefix+member))
            m_prefix=prefix+f'mappings/{pkg}/transformation/mappings/'
            mapping_names=sorted(n for n in z.namelist() if n.startswith(m_prefix) and n.endswith('.ttl'))
            for mn in mapping_names:
                mrel=mn[len(prefix):]
                target=DATA.joinpath(*mrel.split('/'))
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists(): target.write_bytes(z.read(mn))
            meta_rel=f'mappings/{pkg}/metadata.json'
            target=DATA.joinpath(*meta_rel.split('/'))
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists(): target.write_bytes(z.read(prefix+meta_rel))
            raw.append({'form':form,'package':pkg,'notice_id':notice,'schema_test_folder':folder,
                'source_rel':rel,'source_sha256':sha_bytes(data),'source_bytes':len(data),
                'rdf_rel':out_rel,'rdf_sha256':sha_bytes(out_data),'rdf_bytes':len(out_data),
                'mapping_files':[n[len(prefix):] for n in mapping_names]})


    by_id={}
    duplicate_rows=[]
    for r in raw:
        by_id.setdefault(r['notice_id'],[]).append(r)
    selected=[]
    for notice, group in sorted(by_id.items()):
        forms={r['form'] for r in group}
        shas={r['source_sha256'] for r in group}
        if len(forms)>1:
            duplicate_rows.append({'notice_id':notice,'kind':'cross_form_collision','count':len(group),'forms':','.join(sorted(forms)),'source_hashes':','.join(sorted(shas))})
            raise RuntimeError('Notice ID occurs in multiple form packages; manual resolution needed: '+notice)
        if len(shas)>1:
            duplicate_rows.append({'notice_id':notice,'kind':'conflicting_same_form_source','count':len(group),'forms':','.join(sorted(forms)),'source_hashes':','.join(sorted(shas))})
            raise RuntimeError('Different XML bytes share a notice ID within same form: '+notice)
        chosen=sorted(group,key=lambda r:(r['schema_test_folder'],r['source_rel']))[0]
        selected.append(chosen)
        for dup in group:
            if dup is not chosen:
                duplicate_rows.append({'notice_id':notice,'kind':'identical_duplicate_test_fixture','count':len(group),'forms':dup['form'],'duplicate_source':dup['source_rel'],'kept_source':chosen['source_rel'],'same_xml_hash':dup['source_sha256']==chosen['source_sha256']})

    fields=['form','package','notice_id','schema_test_folder','source_rel','source_sha256','source_bytes','rdf_rel','rdf_sha256','rdf_bytes']
    with (BASE/'source_manifest.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows({k:r[k] for k in fields} for r in selected)
    with (BASE/'dedup_audit.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields2=list(dict.fromkeys(k for r in duplicate_rows for k in r))
        w=csv.DictWriter(f,fieldnames=fields2); w.writeheader(); w.writerows(duplicate_rows)
    manifest={'source_archive':str(ZIP),'archive_bytes':ZIP.stat().st_size,'archive_root':prefix.rstrip('/'),
      'op_ted_commit':'2004fef480fe38706a5f15dc8db89806d005077d','included_packages':PACKAGES,
      'package_raw_xml_counts':package_counts,'raw_xml_total':len(raw),'unique_notice_ids':len(selected),
      'deduplicated_xml_fixtures':len(raw)-len(selected),'dedup_rule':'publication notice ID from filename; repeated same-package XML fixtures retained once only after byte-identical SHA-256 confirmation; cross-package ID collisions are fatal.',
      'rdf_source':'Official pre-generated RDF/Turtle output files shipped in each matching mapping package; no new RML execution performed in this stage.',
      'rows':len(selected)}
    (BASE/'dataset_manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False,indent=2))
