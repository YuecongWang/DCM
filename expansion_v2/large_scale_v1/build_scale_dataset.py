from pathlib import Path
import csv,gzip,json,random,hashlib
from decimal import Decimal,ROUND_HALF_UP
ROOT=Path(__file__).resolve().parent
src=ROOT/'ted_api'/'ted_api_10k.jsonl.gz'
out=ROOT/'ted_derived_synthetic_1m.csv.gz'
N=1_000_000
SEED=20261105

def main():
    base=[]
    with gzip.open(src,'rt',encoding='utf-8') as f:
        for line in f:
            r=json.loads(line); c=r.get('currency') or []
            if not r.get('total_value') or not c: continue
            currency=c[0] if isinstance(c,list) else c
            cents=int((Decimal(str(r['total_value']))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
            base.append((r['notice_id'],max(cents,0),currency))
    rng=random.Random(SEED)
    with gzip.open(out,'wt',encoding='utf-8',newline='') as f:
        w=csv.writer(f);w.writerow(['record_id','amount_cents','currency','linked','seed_notice_id'])
        for i in range(1,N+1):
            source,amount,currency=base[rng.randrange(len(base))]
            w.writerow([i,amount,currency,1,source])
            if i%100000==0: print(f'generated {i}/{N}',flush=True)
    manifest={'dataset':out.name,'records':N,'generated_from_official_ted_metadata_records':len(base),
      'generation':'uniform bootstrap with replacement over official amount/currency notice records; new synthetic record IDs; amount rounded to cents; no generated row is an additional real TED notice',
      'seed':SEED,'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'compressed_bytes':out.stat().st_size,
      'scaling_subsets':[10000,100000,1000000]}
    (ROOT/'synthetic_dataset_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
