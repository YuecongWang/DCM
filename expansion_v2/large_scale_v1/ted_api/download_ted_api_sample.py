from pathlib import Path
from urllib.request import Request, urlopen
import gzip, hashlib, json, time, datetime
ROOT=Path(__file__).resolve().parent
URL='https://api.ted.europa.eu/v3/notices/search'
FIELDS=['publication-number','publication-date','total-value','total-value-cur','notice-subtype','main-classification-proc']
BUCKETS=[('2025H1','20250101','20250630',2500),('2025H2','20250701','20251231',2500),('2026H1','20260101','20260630',2500),('2026H2','20260701','20261104',2500)]

def post(body):
    payload=json.dumps(body).encode('utf-8')
    req=Request(URL,data=payload,headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'CodexAcademicScaleStudy/1.0'},method='POST')
    for attempt in range(6):
        try:
            with urlopen(req,timeout=60) as r:return json.loads(r.read().decode('utf-8'))
        except Exception:
            if attempt==5:raise
            time.sleep(min(2**attempt,20))

def main():
    records=[]; seen=set(); bucket_counts=[]
    for label,start,end,target in BUCKETS:
        q=f'publication-date>={start} AND publication-date<={end} AND total-value>=0'
        page=1; picked=0; total=None
        while picked<target:
            req={'query':q,'fields':FIELDS,'limit':250,'scope':'ALL','paginationMode':'PAGE_NUMBER','page':page,'checkQuerySyntax':False}
            result=post(req); total=result.get('totalNoticeCount'); notices=result.get('notices') or []
            if not notices:break
            before=picked
            for n in notices:
                ident=n.get('publication-number')
                if not ident or ident in seen:continue
                seen.add(ident)
                records.append({'notice_id':ident,'publication_date':n.get('publication-date'),'total_value':n.get('total-value'),
                    'currency':n.get('total-value-cur'),'notice_subtype':n.get('notice-subtype'),
                    'main_classification':n.get('main-classification-proc'),'source':'TED_Search_API_v3','bucket':label})
                picked+=1
                if picked>=target:break
            print(f'{label}: page={page}, unique={picked}/{target}, API total={total}',flush=True)
            page+=1
            if picked==before:raise RuntimeError(f'no new notices in {label} page {page-1}')
            if result.get('timedOut'):raise RuntimeError(f'TED API timed out on {label} page {page-1}')
            time.sleep(.1)
        bucket_counts.append({'bucket':label,'query':q,'target':target,'unique_retrieved':picked,'api_total':total,'pages_requested':page-1})
        if picked<target:raise RuntimeError(f'{label} only had {picked} unique records')
    dest=ROOT/'ted_api_10k.jsonl.gz'
    with gzip.open(dest,'wt',encoding='utf-8',newline='\n',compresslevel=6) as f:
        for r in records:f.write(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n')
    valid=[r for r in records if isinstance(r['total_value'],(int,float)) and r['currency']]
    counts={}
    for r in records:
        cs=r.get('currency') or []
        for c in (cs if isinstance(cs,list) else [cs]):counts[c]=counts.get(c,0)+1
    manifest={'api':'https://api.ted.europa.eu/v3/notices/search','retrieved_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
      'official_documentation':['https://docs.ted.europa.eu/api/latest/search.html','https://docs.ted.europa.eu/ODS/latest/reuse/search-api.html'],
      'query_buckets':bucket_counts,'requested_fields':FIELDS,'unique_notice_records':len(records),'valid_amount_currency_records':len(valid),
      'currency_field_counts':counts,'deduplication_key':'publication-number','synthetic_records':0,
      'compressed_sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'compressed_bytes':dest.stat().st_size,
      'caveat':'Search API JSON metadata sample; not full XML, not RML-mapped RDF, and not an extension of the F03 mapping experiment.'}
    (ROOT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print('DONE',json.dumps({k:manifest[k] for k in ('unique_notice_records','valid_amount_currency_records','compressed_bytes','currency_field_counts')},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
