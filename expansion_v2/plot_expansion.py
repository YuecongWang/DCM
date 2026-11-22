from pathlib import Path
import csv,statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
P=Path(__file__).resolve().parent
def read(p):return list(csv.DictReader(p.open(encoding='utf-8-sig')))
effects=read(P/'paired_effects.csv');scale=read(P/'results_v1/rdf_scale.csv')
fig,axes=plt.subplots(1,2,figsize=(11,4.3),layout='constrained')
for i,f in enumerate(('0.1','1.0')):
    rr=[r for r in effects if r['fraction']==f];values=[float(r['time_saving_pct']) for r in rr]
    axes[0].scatter([i]*len(values),values,s=35,color='tab:blue')
    axes[0].plot([i-.17,i+.17],[statistics.median(values)]*2,color='black')
axes[0].axhline(0,color='gray',linewidth=.7);axes[0].set_xticks([0,1],['10% targets','100% targets']);axes[0].set_ylabel('Paired DCM time saving vs full checks (%)');axes[0].set_title('124 real-derived sources; 3 seeds')
for method in ('FULL','QUERY_CACHE_FULL','VERSION_CACHE','DCM'):
    ys=[statistics.median(float(r['total_ms']) for r in scale if r['objects']==str(n) and r['fraction']=='0.1' and r['method']==method) for n in (32,128,512)]
    axes[1].plot([32,128,512],ys,marker='o',label=method)
axes[1].set_xscale('log',base=2);axes[1].set_xlabel('Amount objects (synthetic RDF)');axes[1].set_ylabel('16-event maintenance median (ms)');axes[1].set_title('6 SPARQL templates; 10% targets');axes[1].legend(fontsize=7)
fig.savefig(P/'expanded_results.png',dpi=160)
print('Figure saved')
