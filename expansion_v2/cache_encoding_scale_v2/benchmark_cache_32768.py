from pathlib import Path
import csv, sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import cache_encoding_scale_v2 as bench
out=bench.OUT
runs=[]
for seed in bench.SEEDS:
    for rep in (0,):
        events, summaries=bench.run_one(32768,seed,rep)
        by={r['method']:r for r in summaries}
        tup=by['TUPLE_CACHE']; dcm=by['DCM']
        runs.append({'premises':32768,'seed':seed,'repeat':rep,
          'tuple_ms':tup['total_ms'],'dcm_ms':dcm['total_ms'],
          'dcm_saving_pct':(tup['total_ms']-dcm['total_ms'])/tup['total_ms']*100,
          'dcm_overhead_pct':(dcm['total_ms']-tup['total_ms'])/tup['total_ms']*100,
          'tuple_checks':tup['checks'],'dcm_checks':dcm['checks'],
          'tuple_hits':tup['hits'],'dcm_hits':dcm['hits']})
        print(f"completed 32768 seed={seed}, trajectory={seed}",flush=True)
with (out/'extra_32768_paired_runs.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(runs[0]));w.writeheader();w.writerows(runs)
print('completed 3 paired trajectories for 32768 premises')

