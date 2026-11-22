from pathlib import Path
import csv, statistics, re
import matplotlib.pyplot as plt
root=Path(__file__).resolve().parent; out=root/'cache_encoding_scale_v2'
summary=list(csv.DictReader((out/'summary.csv').open(encoding='utf-8-sig')))
extra=list(csv.DictReader((out/'extra_32768_paired_runs.csv').open(encoding='utf-8-sig')))
vals=[float(r['dcm_saving_pct']) for r in extra]
med=statistics.median(vals); lo=min(vals); hi=max(vals)

summarize_version_cache=out/'结果说明.md'; txt=summarize_version_cache.read_text(encoding='utf-8-sig')
txt=txt.replace('对每个规模使用3个种子，每个种子运行5条不同的事件轨迹，共15条配对轨迹。','对8至16,384项规模使用3个种子、每个种子5条不同轨迹，共15条配对轨迹；另对32,768项规模做了3个种子的探索性补充，每个种子1条轨迹。')
txt=txt.replace('四分位范围基于每个规模15条配对轨迹。','8至16,384项的四分位范围基于每个规模15条配对轨迹；32,768项仅有3条轨迹，单独报告观测范围。')
needle='![不同业务前提数下 DCM 相对元组普通缓存的节省率](cache_encoding_scale.png)'
add=f"""另对32,768项做了3条配对轨迹的探索性测量：元组普通缓存中位时间为{statistics.median(float(r['tuple_ms']) for r in extra):,.2f} ms，DCM为{statistics.median(float(r['dcm_ms']) for r in extra):,.2f} ms；DCM节省率中位数为{med:.2f}%，三条轨迹范围为{lo:.2f}%至{hi:.2f}%。因只有3条轨迹，该点只作趋势补充，不与前六个规模相同强度地解释。\n\n"""+needle
txt=txt.replace(needle,add)
txt=txt.replace('在当前调度器实现中，前提数增加并未带来相对元组缓存的时间收益；较大规模下观测到约13%的稳定相对开销。由于最大两个规模的四分位范围重叠，且实验只改变了受控合成前提规模，该结果仅说明当前微基准范围内的趋势，不构成渐近性能保证。','在当前调度器实现中，前提数增加并未带来相对元组缓存的时间收益；8,192和16,384项的中位相对开销约为13%，32,768项的三条探索性轨迹中位相对开销为14.10%。这一结果提示较大规模时开销可能处于相近区间，但32,768项的三条轨迹范围较宽，不能证明其渐近稳定。实验只改变受控合成前提规模，结论限于本实现和本次计时环境。')
summarize_version_cache.write_text(txt,encoding='utf-8')

xs=[int(r['premises']) for r in summary]; medians=[float(r['dcm_saving_pct_median']) for r in summary]
loerr=[m-float(r['dcm_saving_pct_q1']) for m,r in zip(medians,summary)]
hierr=[float(r['dcm_saving_pct_q3'])-m for m,r in zip(medians,summary)]
fig,ax=plt.subplots(figsize=(9.2,4.9),dpi=180)
ax.errorbar(xs,medians,yerr=[loerr,hierr],fmt='o-',capsize=4,lw=2,markersize=5,color='#285f9e',ecolor='#6a8caf',label='15 paired runs; Q1–Q3')
ax.errorbar([32768],[med],yerr=[[med-lo],[hi-med]],fmt='D',capsize=5,markersize=6,color='#b3502b',ecolor='#b3502b',label='3 exploratory runs; min–max')
ax.plot([xs[-1],32768],[medians[-1],med],ls='--',lw=1,color='#777777')
ax.axhline(0,color='#555555',lw=1,ls='--')
allx=xs+[32768]; ax.set_xscale('log',base=2); ax.set_xticks(allx,[f'{x:,}' for x in allx])
ax.set_xlabel('Number of business premises (log scale)'); ax.set_ylabel('DCM time saving vs tuple cache (%)')
ax.set_title('Scheduler-only paired benchmark')
ax.grid(axis='y',alpha=.25); ax.legend(frameon=False,fontsize=8,loc='lower right')
ax.text(.01,.02,'Negative values mean DCM is slower.',transform=ax.transAxes,fontsize=8,color='#444444')
fig.tight_layout(); fig.savefig(out/'cache_encoding_scale.png',bbox_inches='tight'); plt.close(fig)

paper=root/'第四章第五章完整修订稿.md'; s=paper.read_text(encoding='utf-8-sig')
needle='三种缓存的检查状态、检查数和命中数逐事件相同。DCM在所有规模均未显示相对元组普通缓存的正时间收益；在8,192和16,384项时，中位相对开销约为13%，两个规模的四分位范围有重叠。因而只能说当前实现的相对开销在已测较大规模下大致稳定，不能据此声称存在渐近极限。'
repl=needle+f' 另对32,768项做了3个种子的探索性补充，每个种子1条配对轨迹：DCM节省率中位数为{med:.2f}%，观测范围为{lo:.2f}%至{hi:.2f}%。该点的样本少于主规模组，结果支持“较大规模下约为13%至14%的相对开销”这一描述，但不能证明渐近稳定。'
if needle not in s: raise SystemExit('chapter insertion point not found')
paper.write_text(s.replace(needle,repl),encoding='utf-8')

runner=out/'benchmark_cache_32768.py'; rt=runner.read_text(encoding='utf-8-sig').replace('repeat={rep+1}/{bench.REPEATS}','trajectory={seed}').replace("completed 15 paired trajectories for 32768 premises","completed 3 paired trajectories for 32768 premises")
runner.write_text(rt,encoding='utf-8')
(out/'extra_32768_manifest.json').write_text('{\n  "premises": 32768,\n  "seeds": [11, 29, 47],\n  "paired_trajectories": 3,\n  "events_per_trajectory": 129,\n  "scope": "exploratory scheduler-only benchmark; same implementation and assertions as primary scale study"\n}\n',encoding='utf-8')
print('summarize_version_cache and chapter updated; extra median saving',round(med,2),'range',round(lo,2),round(hi,2))
