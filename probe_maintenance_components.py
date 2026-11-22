from pathlib import Path
import random,time,hashlib,json,csv
from collections import defaultdict
from dcm_scheduler import Premise,Scheduler,State
from benchmark_ted_contract import signature
ROOT=Path(__file__).resolve().parent
def main():
    rows=[]
    for nq in (64,256,1024):
        nm=nq//2; nd=nm//2
        pd={f'p{i}':tuple(f'b{4*(i//4%nd)+k}' for k in range(4)) for i in range(nm*4)}
        qd={f'q{i}':f'd{i%nm}' for i in range(nq)}; reverse=defaultdict(set)
        for p,deps in pd.items():
            for d in deps: reverse[d].add(p)
        for q,d in qd.items(): reverse[d].add(q)
        for seed in (11,29,47):
            rng=random.Random(seed); values={d:1 for d in reverse}; versions=dict.fromkeys(values,'0'); meter=[0]
            def checker(deps):
                def evaluate(s):
                    start=time.perf_counter_ns(); vv=[s[d] for d in deps]
                    for _ in range(8): hashlib.sha256(bytes(vv)).digest()
                    state=State.VALID if all(vv) else State.INVALID
                    meter[0]+=time.perf_counter_ns()-start
                    return state
                return evaluate
            specs=[Premise(p,'r1',deps,checker(deps),True) for p,deps in pd.items()]
            sch=Scheduler(True); cached={}
            for ei in range(129):
                changed=rng.sample(sorted(values),max(1,round(len(values)*.05))) if ei and ei%5 else []
                for d in changed: values[d]=rng.choice([0,1])
                meter[0]=0; start=time.perf_counter_ns()
                for d in changed: versions[d]=str(ei)
                t1=time.perf_counter_ns(); affected=set()
                for d in changed: affected.update(reverse[d])
                t2=time.perf_counter_ns(); signature(sorted(versions.items())); t3=time.perf_counter_ns()
                result=sch.evaluate(specs,values,versions); t4=time.perf_counter_ns()
                actual={}
                for q,d in qd.items():
                    if q not in cached or cached[q][0]!=versions[d]: cached[q]=(versions[d],sum(values[d] for _ in range(32)))
                    actual[q]=cached[q][1]
                end=time.perf_counter_ns()
                expected={p:'VALID' if all(values[d] for d in ds) else 'INVALID' for p,ds in pd.items()}
                assert expected==result['premise_states'] and actual=={q:32*values[d] for q,d in qd.items()}
                rows.append(dict(queries=nq,seed=seed,event_index=ei,version_update_ms=(t1-start)/1e6,traversal_ms=(t2-t1)/1e6,digest_ms=(t3-t2)/1e6,actual_checker_ms=meter[0]/1e6,scheduler_other_ms=(t4-t3-meter[0])/1e6,query_ms=(end-t4)/1e6,total_ms=(end-start)/1e6,checks=result['premise_checks']))
    with (ROOT/'component_probe.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (ROOT/'component_probe_note.md').write_text('# 分项插桩测量\n\n本表是单独运行的 DCM 合成工作负载剖析，64/256/1024 查询，每规模三种子、128 后续事件。初始事件 index=0 单列。规则和 CPU 工作与主规模实验一致。actual_checker_ms 为每次生产检查器内部计时之和；scheduler_other_ms 为调度总时间扣除此和。额外计时器调用影响短操作，不应将这些值与未插桩主实验直接拼接。version_update_ms 只含生产者版本号更新，不含上游事实写入或外部获取成本。所有当前事实重新判定和查询答案交叉核对均通过。\n',encoding='utf-8')
    print('component probe passed',len(rows))
if __name__=='__main__': main()
