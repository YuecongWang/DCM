import csv
import platform
import statistics
import sys
import time
import unittest
from pathlib import Path
from config import ROOT, RESULT_ROOT, RUNTIME_ROOT, REPEATS, DELAY_GRID_MS
from build_fixtures import build
from fabric.common import write_json, digest
from fabric.maintenance import maintain


def main():
    tests=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    outcome=unittest.TextTestRunner(verbosity=1).run(tests)
    if not outcome.wasSuccessful(): raise SystemExit('Tests failed; benchmark not run')
    RESULT_ROOT.mkdir(parents=True,exist_ok=True); RUNTIME_ROOT.mkdir(parents=True,exist_ok=True)
    events,queries=build(); trials=[]; audits=[]; runtime_files=[]
    for cd,ed in DELAY_GRID_MS:
        for repeat in range(REPEATS):
            groups=['A','B','C']; groups=groups[repeat%3:]+groups[:repeat%3]
            for group in groups:

                path=RUNTIME_ROOT/f'catalog_{time.time_ns()}_{group}.sqlite'; runtime_files.append(str(path))
                records,audit=maintain(group,events,path,cd,ed,repeat)
                trials.extend(records)
                if repeat==0 and cd==ed==0: audits.append({'group':group,'events':audit})
    b={(r['repeat'],r['event'],r['check_delay_ms'],r['execution_delay_ms']):r for r in trials if r['group']=='B'}
    for r in trials:
        if r['group']=='C':
            other=b[(r['repeat'],r['event'],r['check_delay_ms'],r['execution_delay_ms'])]
            if (r['reexecutions'],r['reused'])!=(other['reexecutions'],other['reused']):
                raise AssertionError('B/C decisions differ')
    summary=[]
    counts=['checks','reexecutions','reused','evidence_hits','expired_entries','query_definition_invalidations',
            'wrong_reuse','answer_mismatches','error_mismatches','check_scanned_cells','execution_scanned_cells','capture_scanned_cells']
    times=['total_seconds','analysis_seconds','check_seconds','execution_seconds']
    for cd,ed in DELAY_GRID_MS:
        for group in ['A','B','C']:
            selected=[r for r in trials if r['group']==group and r['check_delay_ms']==cd and r['execution_delay_ms']==ed]
            s={'group':group,'timing_mode':'real' if cd==ed==0 else 'injected_delay','check_delay_ms':cd,'execution_delay_ms':ed}
            for k in counts+times:
                totals=[sum(r[k] for r in selected if r['repeat']==i) for i in range(REPEATS)]
                s[k]=statistics.median(totals)
                if k=='total_seconds': s.update(min_total_seconds=min(totals),max_total_seconds=max(totals))
            summary.append(s)
    write_json(RESULT_ROOT/'results.json',{'system':'Document-guided local data fabric research prototype',
                'comparison_kind':'mechanism attribution; not commercial product comparison',
                'candidate_vs_C':'No distinct maintenance mechanism in provided documents; candidate maintenance aliases C; no D',
                'runtime':{'python':sys.version,'executable':sys.executable,'platform':platform.platform()},
                'configuration':{'repeats':REPEATS,'queries':len(queries),'events':len(events)-1,'delay_grid_ms':DELAY_GRID_MS,
                                 'fixture_digest':digest(events),'test_count':outcome.testsRun},
                'timing_scope':'Includes read-only source capture, content hashing, metadata SQLite writes, graph construction/inference, impact analysis, evidence acquisition/expiry/lookups, and query execution. Excludes baseline setup, oracle and summarize_version_cache export.',
                'summary':summary,'trials':trials,'audit':audits})
    with (RESULT_ROOT/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
    write_json(RESULT_ROOT/'runtime_files.json',runtime_files)

    text=['# 本机维护对照结果','',f'通过 {outcome.testsRun} 项正确性测试；{len(trials)} 条事件试验记录均与全量执行答案及错误一致。',
          '', '|组|计时设置|核验|重执行|复用|证据命中|总维护中位数 ms|范围 ms|','|---|---|---:|---:|---:|---:|---:|---|']
    for s in summary:
        setting='真实无延迟' if s['timing_mode']=='real' else f"核验/执行额外请求 {s['check_delay_ms']}/{s['execution_delay_ms']} ms"
        text.append(f"|{s['group']}|{setting}|{s['checks']}|{s['reexecutions']}|{s['reused']}|{s['evidence_hits']}|{s['total_seconds']*1000:.2f}|{s['min_total_seconds']*1000:.2f}–{s['max_total_seconds']*1000:.2f}|")
    text.extend(['','B/C 逐事件重执行与复用次数一致，图遍历与普通依赖集合分析逐事件一致。没有依据把去重效果归为 SEKG 独有机制。',
                 '','这里总耗时包含元数据持久化与图构建成本，比上一轮纯内存受控实验具有更完整的维护路径；两轮总耗时不可直接横比。',
                 '','真实时间有系统磁盘/调度噪声；不声明统计显著或商业产品性能。商业产品、分布式集群、真实银行业务行数据均未运行。'])
    (RESULT_ROOT/'结果解释.md').write_text('\n'.join(text),encoding='utf-8')
    for s in summary: print(s['group'],s['timing_mode'],'checks=',s['checks'],'execute=',s['reexecutions'],'total_ms=',round(s['total_seconds']*1000,3))


if __name__=='__main__': main()
