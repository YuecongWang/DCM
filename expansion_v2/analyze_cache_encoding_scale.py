from pathlib import Path
import csv, json, statistics, re
import matplotlib.pyplot as plt

root=Path(__file__).resolve().parent
out=root/'cache_encoding_scale_v2'
rows=list(csv.DictReader((out/'summary.csv').open(encoding='utf-8-sig')))
lines=[]
lines += ['# 业务前提规模对照：元组普通缓存与 DCM','',
'## 实验设置','',
'本实验将业务前提数设为 8、64、512、2,048、8,192、16,384。每项前提有4个登记依赖；每条轨迹包含初始状态和128个后续事件，每个事件两种缓存策略都遍历全部前提。对每个规模使用3个种子，每个种子运行5条不同的事件轨迹，共15条配对轨迹。除定时外，每一步逐前提状态、检查数、证据命中数和聚合结果均断言相同。','',
'每个变更事件随机改变约5%的依赖版本，每第五个事件不改变依赖。计时累加129次 `evaluate()` 调用；策略执行顺序按事件交替。计时仅覆盖业务证据调度器，不含 SPARQL、查询缓存、RDF 投影/摘要、磁盘I/O及系统初始化。','',
'## 结果','',
'“DCM节省率”定义为 `(元组普通缓存时间 − DCM时间) / 元组普通缓存时间 × 100%`；负数表示 DCM 较慢。四分位范围基于每个规模15条配对轨迹。','',
'| 业务前提数 | 元组普通缓存中位时间/ms | DCM中位时间/ms | DCM节省率中位数 | 节省率Q1–Q3 | 检查数/命中数一致 |',
'|---:|---:|---:|---:|---:|:---:|']
for r in rows:
    lines.append(f"| {int(r['premises']):,} | {float(r['tuple_ms_median']):,.2f} | {float(r['dcm_ms_median']):,.2f} | {float(r['dcm_saving_pct_median']):.2f}% | {float(r['dcm_saving_pct_q1']):.2f}%–{float(r['dcm_saving_pct_q3']):.2f}% | {r['checks_equal']}/{r['hits_equal']} |")
lines += ['', '![不同业务前提数下 DCM 相对元组普通缓存的节省率](cache_encoding_scale.png)', '',
'## 解释','',
'在全部规模点，DCM的中位时间均高于元组普通缓存，因而本实验没有测到DCM相对元组缓存的正时间收益。8项前提下两种实现绝对时间很短，调度和计时开销占比较高；64至2,048项之间，DCM中位相对开销约由13.39%升至14.28%；8,192和16,384项时约为12.85%和12.90%。因此，观测结果更接近“较大规模时维持约13%的相对开销”，而非“DCM的正收益随业务数变小后趋于稳定”。最大两个规模的四分位范围有明显重叠；结合当前实验环境和规模点，不宜据此宣称统计意义上的渐近极限。','',
'从绝对时间看，16,384项规模的129次调度调用中，元组普通缓存中位数为11.04秒，DCM为12.32秒。两者执行相同数量的检查和证据命中；可见差异来自当前调度实现的开销，而不是减少了更多业务检查。','',
'该实验刻意扩大前提数以观察调度器规模行为，不代表一个真实应用会有16,384条独立业务前提。依赖结构规则化（每项4个循环登记依赖），每次变更约影响一部分前提；不能由此推断任意高共享或高度稀疏业务依赖图的性能。若论文讨论渐近趋势，应把结论限定为本实现和本配置下的实测范围。','',
'## 可加入第五章的文字','',
'为观察业务证据调度开销随业务前提数增加的变化，我们将前提数扩展为8、64、512、2,048、8,192和16,384项。每项前提登记4个依赖，每种规模采用3个种子、每个种子5条配对轨迹，每条轨迹包含初始状态和128个后续事件。元组普通缓存与DCM逐步得到相同的前提状态、检查数和证据命中数。按15条轨迹配对计算，DCM相对元组普通缓存的中位节省率在所有规模均为负：8至2,048项时约为−9.56%至−14.28%，在8,192和16,384项时分别为−12.85%和−12.90%。这表明在当前调度器实现中，前提数增加并未带来相对元组缓存的时间收益；较大规模下观测到约13%的稳定相对开销。由于最大两个规模的四分位范围重叠，且实验只改变了受控合成前提规模，该结果仅说明当前微基准范围内的趋势，不构成渐近性能保证。','']
(out/'结果说明.md').write_text('\n'.join(lines),encoding='utf-8')


xs=[int(r['premises']) for r in rows]
med=[float(r['dcm_saving_pct_median']) for r in rows]
lo=[m-float(r['dcm_saving_pct_q1']) for m,r in zip(med,rows)]
hi=[float(r['dcm_saving_pct_q3'])-m for m,r in zip(med,rows)]
fig,ax=plt.subplots(figsize=(8.8,4.8),dpi=180)
ax.errorbar(xs,med,yerr=[lo,hi],fmt='o-',capsize=4,lw=2,markersize=5,color='#285f9e',ecolor='#6a8caf')
ax.axhline(0,color='#555555',lw=1,ls='--')
ax.set_xscale('log',base=2)
ax.set_xticks(xs,[f'{x:,}' for x in xs])
ax.set_xlabel('Number of business premises (log scale)')
ax.set_ylabel('DCM time saving vs tuple cache (%)')
ax.set_title('Scheduler-only paired benchmark (15 trajectories per scale)')
ax.grid(axis='y',alpha=.25)
ax.text(.01,.02,'Negative values mean DCM is slower; bars show Q1–Q3.',transform=ax.transAxes,fontsize=8,color='#444444')
fig.tight_layout()
fig.savefig(out/'cache_encoding_scale.png',bbox_inches='tight')
plt.close(fig)


paper=root/'第四章第五章完整修订稿.md'
s=paper.read_text(encoding='utf-8-sig')
pattern=r'\| 前提数 \| JSON普通缓存/ms \| 元组普通缓存/ms \| DCM/ms \| DCM相对元组缓存收益 \|\n\|[-:| ]+\|\n(?:\|.*\n)+?\n数据为128个后续事件的调度时间总和跨三个种子的中位数；负收益表示DCM更慢。普通元组缓存在三种规模下均快于当前DCM实现。'
replacement='''为观察业务前提数扩大后的相对时间变化，进一步使用六个前提规模，每项前提登记4个依赖；每个规模有3个种子、每个种子5条配对轨迹，每条轨迹包含初始状态和128个后续事件。下表给出每个规模15条轨迹的中位时间与配对节省率四分位范围。负节省率表示DCM较慢。

| 前提数 | 元组普通缓存中位时间/ms | DCM中位时间/ms | DCM节省率中位数 | 节省率Q1–Q3 |
|---:|---:|---:|---:|---:|
| 8 | 9.45 | 10.32 | -9.56% | -12.02%–-8.19% |
| 64 | 38.72 | 43.70 | -13.39% | -14.35%–-11.88% |
| 512 | 301.23 | 344.13 | -13.92% | -14.81%–-13.35% |
| 2,048 | 1,316.60 | 1,495.24 | -14.28% | -14.92%–-12.03% |
| 8,192 | 5,543.28 | 6,222.22 | -12.85% | -13.69%–-11.93% |
| 16,384 | 11,044.84 | 12,316.67 | -12.90% | -13.39%–-11.45% |

三种缓存的检查状态、检查数和命中数逐事件相同。DCM在所有规模均未显示相对元组普通缓存的正时间收益；在8,192和16,384项时，中位相对开销约为13%，两个规模的四分位范围有重叠。因而只能说当前实现的相对开销在已测较大规模下大致稳定，不能据此声称存在渐近极限。'''
s2,n=re.subn(pattern,replacement,s,flags=re.S)
if n!=1: raise SystemExit(f'Expected one table block to replace, found {n}')
paper.write_text(s2,encoding='utf-8')
print('wrote summarize_version_cache, plot, and updated chapter draft')
