import copy
import csv
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
import unittest
from experiment_config import ROOT,PROTOTYPE_ROOT,INPUT_ROOT,OUTPUT_ROOT,WORK_ROOT,REPEATS
import bootstrap
from fabric.common import write_json,read_json,digest
from calculation import events_for_case,full_reference,maintain_computation
from scheduling import BusinessMaintenance
from semantic import delivery


def computation_only_delivery(calculation):

    if any(answer['errors'] for answer in calculation.values()):
        return {'decision':'COMPUTATION_FAILED','approved':False}
    return {'decision':'COMPUTATION_ONLY_DELIVERED','approved':True}


def load_frozen():
    manifest=read_json(INPUT_ROOT/'freeze_manifest.json')
    for name,expected in manifest['sha256'].items():
        if hashlib.sha256((INPUT_ROOT/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('Fixed inputs modified: '+name+'; use an explicitly versioned new experiment.')
    cases=read_json(INPUT_ROOT/'cases.json')
    if len(cases)!=24:raise ValueError('Exactly 24 independent fixed cases required')

    if any('expected' in key for c in cases for key in c):raise ValueError('Labels leaked into evaluator cases')
    return cases,manifest


def tests_before_evaluation():
    WORK_ROOT.mkdir(parents=True,exist_ok=True)

    command=[sys.executable,'-m','unittest','discover','-s',str(PROTOTYPE_ROOT/'tests'),'-v']
    prior=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace')
    (WORK_ROOT/'existing_regression.log').write_text(prior.stdout+prior.stderr,encoding='utf-8')
    if prior.returncode:raise RuntimeError('Existing regressions failed; see work log, new experiment not run')
    print('Existing 18 regression tests: PASS',flush=True)
    suite=unittest.defaultTestLoader.discover(str(ROOT),pattern='test_controlled_cases.py')
    outcome=unittest.TextTestRunner(verbosity=1).run(suite)
    if not outcome.wasSuccessful():raise RuntimeError('New unit tests failed; evaluation not run')
    return {'existing_returncode':prior.returncode,'existing_test_count':18,'new_unit_test_count':outcome.testsRun,
            'cases_are_separate_from_unit_tests':True,'human_review_status':'PENDING'}


def evaluate(cases):
    records=[];references={};semantics_initial=[]
    for case in cases:
        folder=WORK_ROOT/'physical_inputs'/case['id'];events=events_for_case(case,folder)
        references[case['id']]=full_reference(events)
        for repeat in range(REPEATS):
            groups=['G0','G1','G2'];groups=groups[repeat%3:]+groups[:repeat%3]
            for group in groups:
                business_maint=None
                if group!='G0':business_maint=BusinessMaintenance(group,case['before'])
                path=WORK_ROOT/'catalogs'/f'{case["id"]}_{group}_{repeat}_{time.time_ns()}.sqlite'
                path.parent.mkdir(parents=True,exist_ok=True)

                answers,technical,audit=maintain_computation(events,path)
                if group=='G0':
                    business={'state':'NOT_ASSESSED','mappings':{}}
                    semantic={'premises_rechecked':None,'mappings_rechecked':None,'evidence_invalidations':None,
                              'evidence_hits':None,'semantic_total_seconds':None,'analysis_seconds':None,'semantic_check_seconds':None}
                else:business,semantic=business_maint.update(case['after'])
                permit=computation_only_delivery(answers) if group=='G0' else delivery(business,answers)
                oracle=references[case['id']]
                if answers!=oracle['new']:raise AssertionError('Technical result differs from full execution reference')
                record={'case_id':case['id'],'group':group,'repeat':repeat,'business':business,'delivery':permit,
                        'calculation':answers,'old_reference':oracle['old'],'new_reference':oracle['new'],
                        'answers_equal_reference':oracle['answers_equal'],'errors_equal_reference':oracle['errors_equal'],
                        'calculation_success':not any(x['errors'] for x in answers.values()),
                        'technical_metrics':technical,'semantic_metrics':semantic,
                        'total_maintenance_seconds':technical['total_seconds']+(semantic['semantic_total_seconds'] or 0),
                        'analysis_seconds':technical['analysis_seconds']+(semantic['analysis_seconds'] or 0),
                        'verification_seconds':technical['check_seconds']+(semantic['semantic_check_seconds'] or 0),
                        'execution_seconds':technical['execution_seconds'],'technical_audit':audit}
                records.append(record)
    return records,references


def score(records,expectations):
    gold={e['case_id']:e for e in expectations};first=[r for r in records if r['repeat']==0]
    classes=['VALID','INVALID','UNKNOWN'];metrics={};matrices={}
    for group in ['G0','G1','G2']:
        selected=[r for r in first if r['group']==group]
        if group=='G0':
            metrics[group]={'business_assessment':'NOT_ASSESSED','confusion_matrix':None,
                'invalid_as_valid':None,'valid_as_invalid':None,'unknown_as_valid':None,
                'invalid_but_same_correct':None,'wrong_business_delivery':None,'case_state_accuracy':None,
                'counterfactual_basis':'相对于预先声明的受控规范期望的反事实统计；非真实业务标注或人工审核真值',
                'counterfactual_computation_success_delivered':sum(r['calculation_success'] and r['delivery']['approved'] is True for r in selected),
                'counterfactual_invalid_same_answer_delivered':sum(gold[r['case_id']]['expected_case_state']=='INVALID' and r['answers_equal_reference'] and r['delivery']['approved'] is True for r in selected),
                'counterfactual_invalid_or_unknown_delivered':sum(gold[r['case_id']]['expected_case_state'] in ('INVALID','UNKNOWN') and r['delivery']['approved'] is True for r in selected)}
            matrices[group]=None;continue
        matrix={s:{p:0 for p in classes} for s in classes}
        for r in selected:matrix[gold[r['case_id']]['expected_case_state']][r['business']['state']]+=1
        wrongdelivery=sum(r['delivery']['approved'] is True and gold[r['case_id']]['expected_case_state']!='VALID' for r in selected)
        identified=sum(gold[r['case_id']]['expected_case_state']=='INVALID' and r['answers_equal_reference'] and r['business']['state']=='INVALID' for r in selected)
        matrices[group]=matrix
        metrics[group]={'confusion_matrix':matrix,'invalid_as_valid':matrix['INVALID']['VALID'],
            'valid_as_invalid':matrix['VALID']['INVALID'],'unknown_as_valid':matrix['UNKNOWN']['VALID'],
            'invalid_but_same_correct':identified,'wrong_business_delivery':wrongdelivery,
            'case_state_accuracy':sum(matrix[c][c] for c in classes)/24}
    paired={}
    for r in records:paired.setdefault((r['case_id'],r['repeat']),{})[r['group']]=r
    same=0
    for key,p in paired.items():
        if p['G1']['business']!=p['G2']['business'] or p['G1']['delivery']!=p['G2']['delivery']:
            raise AssertionError('G1/G2 disagree on '+str(key))
        same+=1
    for r in first:
        e=gold[r['case_id']]
        if r['answers_equal_reference']!=e['expected_answers_equal']:raise AssertionError('Frozen arithmetic expectation mismatch')
        actual_errors=[error for answer in r['new_reference'].values() for error in answer['errors']]
        if actual_errors!=e['expected_execution_errors']:raise AssertionError('Frozen execution error expectation mismatch')
        if r['group']!='G0':
            if r['business']['state']!=e['expected_case_state']:raise AssertionError('Frozen aggregate state mismatch')
            actual={m:d['state'] for m,d in r['business']['mappings'].items()}
            if actual!=e['expected_mapping_states']:raise AssertionError('Frozen mapping expectation mismatch: '+r['case_id']+' '+str(actual))
    return {'groups':metrics,'g1_g2_state_and_delivery_agreement':same/len(paired),
            'independent_case_count':24,'case_categories':{cat:sum(e['category']==cat for e in expectations) for cat in ['harmless','legal_data','business_change','unknown']},
            'human_review_status':'PENDING; metrics are against predeclared specification expectations, not human-signed labels'},matrices


def summaries(records):
    results=[]
    for group in ['G0','G1','G2']:
        selected=[r for r in records if r['group']==group]
        s={'group':group,'independent_cases':24,'timing_repeats':REPEATS}
        for key in ['total_maintenance_seconds','analysis_seconds','verification_seconds','execution_seconds']:
            totals=[sum(r[key] for r in selected if r['repeat']==i) for i in range(REPEATS)]
            s[key]=statistics.median(totals)
            if key=='total_maintenance_seconds':s.update(min_total_seconds=min(totals),max_total_seconds=max(totals))
        for key in ['checks','reexecutions','reused','evidence_hits','wrong_reuse','answer_mismatches','error_mismatches','check_scanned_cells','capture_scanned_cells','execution_scanned_cells']:
            s['technical_'+key]=statistics.median(sum(r['technical_metrics'][key] for r in selected if r['repeat']==i) for i in range(REPEATS))
        for key in ['premises_rechecked','mappings_rechecked','evidence_invalidations','evidence_hits']:
            s['business_'+key]=None if group=='G0' else statistics.median(sum(r['semantic_metrics'][key] for r in selected if r['repeat']==i) for i in range(REPEATS))
        results.append(s)
    return results


def export(records,references,test_report,manifest):
    global OUTPUT_ROOT

    OUTPUT_ROOT=OUTPUT_ROOT/('g0_computation_only_'+time.strftime('%Y%m%d_%H%M%S')+'_'+str(time.time_ns()))

    expectations=read_json(INPUT_ROOT/'expectations.json')
    metrics,matrices=score(records,expectations);summary=summaries(records)
    counterfactual=metrics['groups']['G0']
    for row in summary:
        for key in ['counterfactual_basis','counterfactual_computation_success_delivered',
                    'counterfactual_invalid_same_answer_delivered','counterfactual_invalid_or_unknown_delivered']:
            row[key]=counterfactual[key] if row['group']=='G0' else None
    OUTPUT_ROOT.mkdir(parents=True,exist_ok=True)
    write_json(OUTPUT_ROOT/'results.json',{'experiment':'Mapping validity vs numeric answer reuse separation',
        'runtime':{'executable':sys.executable,'python':sys.version,'platform':platform.platform()},
        'fixed_input_manifest':manifest,'tests':test_report,'metrics':metrics,'timing_summary':summary,
        'calculation_references':references,'records':records,
        'scope':'No injected delay. Existing C is the common independent calculation channel; business semantics never enter numeric execution.',
        'novel_mechanism_claim':False,'human_review_completed':False})
    write_json(OUTPUT_ROOT/'confusion_matrices.json',{'orientation':'rows=predeclared truth, columns=prediction','matrices':matrices})
    with (OUTPUT_ROOT/'confusion_matrices.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        w=csv.writer(stream);w.writerow(['group','truth','predicted','count','assessment'])
        w.writerow(['G0','','','','NOT_ASSESSED'])
        for g in ['G1','G2']:
            for t,row in matrices[g].items():
                for p,n in row.items():w.writerow([g,t,p,n,'ASSESSED'])
    with (OUTPUT_ROOT/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
    gold={e['case_id']:e for e in expectations}
    case_rows=[]
    for r in records:
        if r['repeat']!=0:continue
        e=gold[r['case_id']];technical=r['technical_metrics'];semantic=r['semantic_metrics']
        case_rows.append({'case_id':r['case_id'],'category':e['category'],'group':r['group'],
            'expected_state':e['expected_case_state'],'actual_state':r['business']['state'],
            'old_answers':json.dumps(r['old_reference'],ensure_ascii=False),'new_answers':json.dumps(r['new_reference'],ensure_ascii=False),
            'answers_equal':r['answers_equal_reference'],'calculation_success':r['calculation_success'],
            'delivery':r['delivery']['decision'],'approved':r['delivery']['approved'],
            'premises_rechecked':semantic['premises_rechecked'],'mappings_rechecked':semantic['mappings_rechecked'],
            'business_evidence_invalidations':semantic['evidence_invalidations'],'business_evidence_hits':semantic['evidence_hits'],
            'technical_checks':technical['checks'],'query_reexecutions':technical['reexecutions'],'query_reused':technical['reused'],
            'violations':json.dumps({m:v['violations'] for m,v in r['business']['mappings'].items()},ensure_ascii=False),
            'unknowns':json.dumps({m:v['unknowns'] for m,v in r['business']['mappings'].items()},ensure_ascii=False),
            'review_status':e['review_status'],'total_seconds_first_repeat':r['total_maintenance_seconds']})
    with (OUTPUT_ROOT/'per_case.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=list(case_rows[0]));w.writeheader();w.writerows(case_rows)
    with (OUTPUT_ROOT/'人工审核表.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        w=csv.writer(stream);w.writerow(['case_id','category','spec_expected_state','spec_answers_equal','rationale','review_status','reviewer','human_confirmed_state','comment'])
        for e in expectations:w.writerow([e['case_id'],e['category'],e['expected_case_state'],e['expected_answers_equal'],e['rationale'],e['review_status'],'','',''])
    summarize_version_cache=['# 本轮真实结果','',f'已有18项回归、新增{test_report["new_unit_test_count"]}项单元测试通过。固定案例24例，计时重复{REPEATS}次，不新增独立样本。',
            '', 'G0 是计算成功即交付的基线：无计算错误即 COMPUTATION_ONLY_DELIVERED，有计算错误即 COMPUTATION_FAILED；业务状态仍为 NOT_ASSESSED，业务混淆矩阵和状态判定指标留空，不代表零错误。G1/G2 状态与交付决定逐事件一致。',
            '', '预先固定的案例级期望分布：VALID 12、INVALID 6、UNKNOWN 6。G1/G2 对角混淆计数均为12/6/6，失效但答案相同识别6/6，错误业务交付0。',
            '', '|组|实际重检前提|涉及映射累计|业务证据失效|业务证据命中|总维护中位数 ms|','|---|---:|---:|---:|---:|---:|']
    for s in summary:summarize_version_cache.append(f"|{s['group']}|{s['business_premises_rechecked'] if s['business_premises_rechecked'] is not None else '未评估'}|{s['business_mappings_rechecked'] if s['business_mappings_rechecked'] is not None else '未评估'}|{s['business_evidence_invalidations'] if s['business_evidence_invalidations'] is not None else '未评估'}|{s['business_evidence_hits'] if s['business_evidence_hits'] is not None else '未评估'}|{s['total_maintenance_seconds']*1000:.2f}|")
    summarize_version_cache += ['', 'G0：相对于预先声明的受控规范期望的反事实统计（24例各计一次，不累计5次计时重复；非真实业务标注或人工审核真值）：',
               f"计算成功并交付：{counterfactual['counterfactual_computation_success_delivered']}；INVALID 且新旧答案相同仍交付：{counterfactual['counterfactual_invalid_same_answer_delivered']}；INVALID 或 UNKNOWN 仍交付：{counterfactual['counterfactual_invalid_or_unknown_delivered']}。期望仅在评分阶段读取，不传入被测判定器。",
               '', '维护行为：业务前提与数值结果独立；缺失信息为UNKNOWN；明确违反优先并保留缺失原因；版本过期重检；共享前提变化传播到多个映射；无关概念变化不触发业务前提重检。',
               '', '知识与规则收益：显式口径/对象/时间不相容及批准转换使G1/G2能区分业务阻断、待核验和可交付；这是相同知识与规则共同提供的能力。',
               '', '增量调度收益：G2减少重检前提次数，代价包括图构建与证据签名维护；真实耗时必须看本表及范围，不能只按重检次数声称更快。',
               '', '新机制：未发现超出同知识普通方案的语义判定能力；G1/G2判断相同，不宣称SEKG独有能力或创新成立。',
               '', '人工审核尚未完成：期望表在测试前已独立固定，但当前无人工审核人签字。这里评价的是预先声明的受控规范期望；人工审核表提供后续复核入口，不把它声称为人工真值。',
               '', '未实现：真实银行/Valentine业务标注、完整OWL/SHACL/SPARQL、Qwen/训练/自动匹配、分布式快照/生产审批系统。']
    (OUTPUT_ROOT/'结果解释.md').write_text('\n'.join(summarize_version_cache),encoding='utf-8')
    write_json(OUTPUT_ROOT/'code_manifest.json',{'new_code_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob('*.py')},
             'reused_module_sha256':{str(p.relative_to(PROTOTYPE_ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (PROTOTYPE_ROOT/'fabric').glob('*.py')}})
    print(json.dumps({'metrics':metrics,'summary':summary},ensure_ascii=False,indent=2))


def main():
    cases,manifest=load_frozen()
    test_report=tests_before_evaluation()
    records,references=evaluate(cases)
    export(records,references,test_report,manifest)


if __name__=='__main__':main()
