import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
def read(path):return json.loads(path.read_text(encoding='utf-8'))
def compact(value):return json.dumps(value,ensure_ascii=False,separators=(',',':')).replace('|','\\|')
NAMES={'Employee':'员工','StaffMember':'工作人员子类','Vendor':'供应商','Payable':'应付','Paid':'实付',
       'Pretax':'税前','Net':'实发','PayableAlias':'应付别名概念','CalendarMonth':'自然月','FiscalMonth':'财务月',
       'Year':'年','CNY':'元','CNY_CENT':'分','USD':'美元','CNY_PER_MONTH':'元/月','CNY_PER_YEAR':'元/年',
       'UnrelatedCar':'无关汽车概念'}
DIMS={'object':'业务对象','basis':'业务口径','unit':'单位','time':'时间口径'}
DECISIONS={'APPROVED':'应批准','BUSINESS_BLOCKED':'应业务阻断','PENDING_VERIFICATION':'应待核验'}
NOTES={
 'H01':'原始元金额100、200改写为分金额10000、20000，两映射因子由1改为0.01，目标仍为元。这是物理表示/映射同步变化，并非将目标单位改为分。m_a 的工作人员子类与 hasSalaryBasis 子属性在变化前后均已存在，不是本事件新增的推理知识。',
 'H02':'变化前原始数为10000、20000分，已用0.01转换为100、200元；变化后原始数为100、200元，因子恢复1。原始要求始终为元，两个版本均有对应的明确批准。',
 'H05':'m_a 变化前为1.005、1.015元，逐项半偶舍入到1.00、1.02；变化后为100.500、101.500分，乘0.01后再逐项舍入仍为1.00、1.02，求和2.02。m_b 同步元转分。相等指逐项规范化后查询答案，不指未舍入总和；舍入规则是本案例固定计算约定。',
 'D03':'固定数据确实删除了第二行，两个映射由两行变成一行；不是删除空行或重复行。只检查四类映射前提，案例没有行数守恒、禁止删除或完整性业务要求，因此在当前有限规范中仍VALID。这不表示现实业务删除总是合法。',
 'D06':'仅 m_a 第一项由-10改为-11；m_b不变。没有非负金额、退款分类或取值范围前提，因此本案例允许负数。VALID仅针对已建模四类要求，不是现实负薪资合法性的结论。',
 'B03':'员工与供应商名称不同本身不足以判失效。此例要求Employee，事实改为Vendor，并引用 rules:object 中明确声明的 disjointWith，才构成确认违反。互斥仅是受控案例局部设定；现实中同一人可能具有多个角色。m_b仍满足要求。',
 'B04':'自然月与财务月不相容仅由本案例 rules:time 显式指定，不是普遍业务事实。本例没有证明两个实际日期区间不同，也没有实现日期区间计算；验证的是已注入的局部时间口径约束。',
 'B05':'变化方向是共享业务要求由应付改为实付，两个映射实际事实仍为应付；不是实际数据或映射事实改为实付。两个映射共同引用 requirement:basis，显式应付/实付不相容使二者均INVALID。仅要求版本变化或依赖可达不会自动导致失效。',
 'B06':'共享Employee概念由版本1/permitted=true变为版本2/permitted=false。这里permitted=false是被测规范明确建模的局部禁用约束，不是说员工不存在或员工概念天然非法。两映射对象支持仍绑定Employee版本1，因此又有旧证据问题；m_a单位支持记录也被删除。显式禁用产生INVALID，同时保留对象证据过期和单位缺失的UNKNOWN原因。不能把缺失本身说成确认违反。',
 'U02':'PayableAlias与Payable的概念不同仅意味着无法由本规则确认同口径；新增closeMatch不批准等价，也没有显式不相容，所以UNKNOWN而非INVALID。',
 'U03':'approval:cny资源版本变为2，approved_version仍为1；status仍写approved但版本不匹配，不能继续批准，产生UNKNOWN，不是已确认换算因子错误。',
 'U05':'变化前并未标注月薪：单位事实为CNY、时间事实为CalendarMonth。变化后才把实际单位事实改为CNY_PER_MONTH，要求改为CNY_PER_YEAR和Year，并把数值因子改为12。缺少月到年的批准，且CalendarMonth到Year没有可推导支持；未声明二者不相容，故UNKNOWN。标题是机制场景简称，不能理解为完整月薪业务模型。',
 'U06':'exactMatch仅联系口径概念；本规则不授予全前提批准。USD到CNY又没有批准转换，两个不足分别保留；不是根据美元与元名称不同断言违反。'}

def table(lines,headers,rows):
    lines.append('|'+ '|'.join(headers)+'|');lines.append('|'+ '|'.join(['---']*len(headers))+'|')
    for row in rows:lines.append('|'+ '|'.join(str(v).replace('\n',' ') for v in row)+'|')
    lines.append('')

def main():
    paths=list((ROOT/'fixed_inputs').glob('*.json'))+list((ROOT/'results').glob('*'))
    paths=[p for p in paths if p.is_file()]
    original={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    cases=read(ROOT/'fixed_inputs'/'cases.json');rules=read(ROOT/'fixed_inputs'/'rules.json')
    gold={e['case_id']:e for e in read(ROOT/'fixed_inputs'/'expectations.json')}
    results=read(ROOT/'results'/'results.json')
    records={r['case_id']:r for r in results['records'] if r['group']=='G1' and r['repeat']==0}
    lines=['# 完整中文人工审核表（待审核）','',
      '本表只读取固定 cases.json、rules.json、expectations.json 及已保存 results.json，不运行或修改判定器，不修改案例、规则、标签或原结果。所有人工栏留空；预设标签不代表人工确认。',
      '', '## 阅读约定与来源', '',
      f"全局规则来源：`{rules['source']['id']}`，类型 `{rules['source']['kind']}`，版本 `{rules['source']['version']}`。全部业务事实、互斥关系和批准是受控注入，不来自Valentine或银行标注。", '',
      'm_a 对应 core.amount_a 求和；m_b 对应 core.amount_b 有序投影。行键为字符串0、1、2，按数组位置对应；SQL原值为文本或NULL。所有案例交付均要求两映射有效。', '',
      '数值定义：Decimal字符串解析，缩放后逐项quantum=0.01、ROUND_HALF_EVEN；None/空串为空。求和沿既有引擎跳过空值，投影保留空值；异常保存分类错误。本表答案直接取已保存独立全量计算参考，不由业务期望推导。', '',
      '概念不同：不同标识不自动推导不相容。明确违反：存在当前显式不相容、禁用或批准转换与实际定义冲突。证据不足：缺失、过期或无法推导支持，且无确认违反时为UNKNOWN。INVALID优先，但须保留缺失/过期原因。', '',
      '所有必要前提VALID且计算成功才应批准；INVALID应业务阻断；UNKNOWN应待核验。G0未评估业务有效性，不由本审核表补造业务批准。', '',
      '规则适用范围仅限各案例的有限受控词汇与四类业务要求；没有完整OWL含义，也没有现实世界的普遍互斥、完整性或薪资法规断言。', '',
      '### 全局固定规则中文内容', '',
      '- 必要前提全部获得当前支持才VALID；确认违反优先于UNKNOWN。',
      '- 只支持有限子类、子属性、类型继承和显式不相容。',
      '- SKOS closeMatch、related、exactMatch不自动批准业务前提。',
      '- 单位只按明确批准的from/to/scale/quantum/rounding、当前approved_version判定；没有默认月年乘12批准。','',
      '### 原始证据代码中文释义', '',
      'MISSING_RESOURCE=支持资源缺失；STALE_SUPPORT_VERSION=支持记录绑定旧版本；STALE_CONVERSION_APPROVAL=批准版本过期；NO_DERIVABLE_SUPPORT=有限规则不能推导所需支持；NO_APPROVED_CONVERSION=未找到批准转换；EXPLICIT_INCOMPATIBILITY=显式局部不相容；EXPLICIT_CONCEPT_PROHIBITION=显式局部概念禁用；APPROVED_TRANSFORM_MISMATCH=实际转换与批准定义冲突。原始记录保留代码以便对照。','',
      '### 概念译名（标识才是判定依据）','']
    table(lines,['固定标识','中文辅助释义'],NAMES.items())
    lines+=['## 逐例材料','']
    for c in cases:
        cid=c['id'];e=gold[cid];r=records[cid];b=c['before'];a=c['after']
        lines +=[f"## {cid}：{c['description']}",'',f"预设案例状态：**{e['expected_case_state']}**；理由：{e['rationale']}。预设映射状态：`{compact(e['expected_mapping_states'])}`。人工状态仍为 `{e['review_status']}`。",'']
        if cid in NOTES:lines +=['**重点澄清：** '+NOTES[cid],'']
        lines +=['### 1. 原业务要求与变后要求','']
        table(lines,['维度','变化前要求（共享于两个映射）','变化后要求','要求记录版本 前→后'],[
            [DIMS[d],NAMES[b['resources']['requirement:'+d]['payload']['required']],NAMES[a['resources']['requirement:'+d]['payload']['required']],b['resources']['requirement:'+d]['version']+'→'+a['resources']['requirement:'+d]['version']] for d in DIMS])
        lines +=['### 2. 具体数据与实际转换定义','']
        table(lines,['映射/查询','变化前原始数据','变化后原始数据','因子 前→后'],[[m+(' / sum_a' if m=='m_a' else ' / project_b'), '`'+compact(b['values'][m])+'`','`'+compact(a['values'][m])+'`',b['scales'][m]+'→'+a['scales'][m]] for m in b['mapping_ids']])
        lines +=['两映射两个版本均为decimal、quantum=0.01、HALF_EVEN、输入数字位数及指数上限28、空值策略None-or-empty、错误策略fail-query、revision=1。差异列中的因子为实际执行定义；批准记录见下文，二者不能混为一谈。','', '### 3. 实际语义事实与支持版本','']
        factrows=[]
        for m in b['mapping_ids']:
            for d in DIMS:
                rid=f'fact:{m}:{d}'
                def show(s):
                    x=s['resources'].get(rid)
                    return '缺失记录（证据不足）' if x is None else '`'+compact({'payload':x['payload'],'version':x['version'],'bindings':x.get('bindings',{}),'dependencies':x['dependencies']})+'`'
                factrows.append([rid,DIMS[d],show(b),show(a)])
        table(lines,['事实ID','维度','变化前','变化后'],factrows)
        lines +=['### 4. 引用规则：内容、版本、来源和局部范围','']
        ruletext={
          'object':'StaffMember为Employee子类，类型可向Employee继承；Employee与Vendor显式互斥。互斥仅是本受控案例局部设定。',
          'basis':'hasSalaryBasis为hasBasis子属性；Payable/Paid及Pretax/Net显式口径不相容，仅限本受控规范。SKOS匹配边不提供业务批准。',
          'time':'CalendarMonth与FiscalMonth显式不相容，仅限本受控规范；没有CalendarMonth/Year显式不相容或等价规则。',
          'unit':'注册批准conversion清单；仅匹配当前版本、from/to和实际因子及舍入定义才支持单位前提。'}
        for d in DIMS:
            rid='rules:'+d;x=b['resources'][rid];y=a['resources'][rid]
            lines +=[f"- `{rid}`：{ruletext[d]} 来源 `{y['source']['id']}`（受控注入，来源版本{y['source']['version']}）；规则版本{x['version']}→{y['version']}。前内容 `{compact(x['payload'])}`；后内容 `{compact(y['payload'])}`。依赖 `{compact(y['dependencies'])}`。"]
        lines +=['']
        table(lines,['批准记录','变化前完整批准内容','变化后完整批准内容'],[[rid,'`'+compact({'version':b['resources'][rid]['version'],**b['resources'][rid]['payload']})+'`','`'+compact({'version':a['resources'][rid]['version'],**a['resources'][rid]['payload']})+'`'] for rid in ['approval:cny','approval:cent']])
        lines +=['批准记录来源同为controlled://mapping-validity/spec-v1，适用范围限该案例明确列出的单位、因子及舍入定义；不授予对象、口径或时间批准。','', '### 5. 概念/约束与前提依赖清单','']
        table(lines,['概念记录','前版本及内容','后版本及内容'],[[rid,'`'+compact({'version':b['resources'][rid]['version'],**b['resources'][rid]['payload']})+'`','`'+compact({'version':a['resources'][rid]['version'],**a['resources'][rid]['payload']})+'`'] for rid in b['resources'] if rid.startswith('concept:')])
        table(lines,['前提ID（映射）','要求 / 实际事实 / 规则族','版本 前→后','声明依赖'],[[p['id'],p['requirement_id']+' / '+p['actual_id']+' / '+p['rule_id'],p['version']+'→'+a['premises'][i]['version'],'`'+compact(p['dependencies'])+'`'] for i,p in enumerate(b['premises'])])
        lines +=['本例以上资源与前提的来源均为受控来源v1。要求依赖其目标concept；事实依赖及绑定见第3节；规则依赖见第4节；批准依赖源/目标单位concept。单位前提还运行时绑定对应实际转换定义。概念记录的dependencies为空。依赖关系用于确定重检范围，不作为语义失效的真值。','', '### 6. 保存的计算参考与交付判断','']
        table(lines,['固定查询','旧全量答案及分类错误','新全量答案及分类错误'],[[q,'`'+compact(r['old_reference'][q])+'`','`'+compact(r['new_reference'][q])+'`'] for q in ['sum_a','project_b']])
        expected_decision={'VALID':'应批准' if r['calculation_success'] else '应因计算失败停止交付','INVALID':'应业务阻断','UNKNOWN':'应待核验'}[e['expected_case_state']]
        lines +=[f"答案相同：**{'是' if r['answers_equal_reference'] else '否'}**；分类错误状态相同：{'是' if r['errors_equal_reference'] else '否'}；计算成功：{'是' if r['calculation_success'] else '否'}。预设答案相同：{'是' if e['expected_answers_equal'] else '否'}。",'',f"按预设状态和已保存计算成功状态，应：**{expected_decision}**。保存的G1结果为{r['business']['state']}、{r['delivery']['decision']}；这只是被测结果，不能代替独立人工审核。",'']
        table(lines,['映射','预设状态','已保存状态','已保存前提状态'],[[m,e['expected_mapping_states'][m],v['state'],'；'.join(p['premise_id']+'='+p['state'] for p in v['premises'])] for m,v in r['business']['mappings'].items()])
        for m,v in r['business']['mappings'].items():
            lines +=[f"- {m} 确认违反记录：`{compact(v['violations'])}`",f"- {m} 证据不足/过期记录：`{compact(v['unknowns'])}`"]
        lines +=['','### 7. 人工审核（全部留空）','', '- 审核人：________________', '- 审核日期：________________', '- 是否接受局部业务设定及其来源：________________', '- 独立确认 m_a 状态及理由：________________', '- 独立确认 m_b 状态及理由：________________', '- 独立确认案例状态：________________', '- 独立确认计算答案/错误与是否相同：________________', '- 独立确认应批准／阻断／待核验：________________', '- 标签或规则疑点、所需补充证据：________________', '- 审核结论／签名：________________','']
    lines +=['## 疑点与限制（未修改案例、规则或标签）','',
      '1. 预设标签尚无人工签审；本表提供材料，不代表审核通过。',
      '2. Employee/Vendor互斥、CalendarMonth/FiscalMonth不相容以及Employee禁用均为受控局部规定。若审核人不接受，须记录规范争议，不能将其冒充现实普遍事实。',
      '3. D03合法删除和D06允许负值仅因为当前四类前提没有完整性/数值域约束；此处的VALID不是全面业务合法性。',
      '4. H05先逐项舍入再求和，不能用未舍入总和替换计算语义；H01/H02目标单位一直为元。',
      '5. U05变化前没有月薪单位事实，变化后才注入元/月事实及年要求；没有实际时间区间/12个月完整性数据，不足以评价真实月年薪资等价。',
      '6. B04不检查真实日期范围；B06的permitted=False作为显式禁止的含义是实验判定器约定，全局rules.json没有完整列举此字段语义。cases.json含事实，semantic.py实施约定；该表达完整性问题应供人工审核，不在本轮改规则。',
      '7. 四类业务要求以共享资源表示；m_a、m_b为两个列映射，没有建立真实员工ID、薪资支付主体或业务事件对象。这些名字仅是受控语义标识。',
      '8. rules.json是规则范围摘要，具体三元组和批准存放于cases.json。依赖完整性、证据新鲜度及优先级还依赖现有实现；审核者可同时查看semantic.py及scheduling.py。本表未将旧判定函数用作生成期望真值。','',
      '## 生成依据与不变性核验','', '以下为生成前的固定输入及原结果SHA-256；生成后逐一核对，全部保持一致。','']
    table(lines,['文件（相对实验目录）','SHA-256'],[[str(Path(p).relative_to(ROOT)),h] for p,h in original.items()])
    output=ROOT/'完整中文审核表.md';output.write_text('\n'.join(lines),encoding='utf-8')
    for p,h in original.items():
        if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h:raise AssertionError('Source changed: '+p)
    assert len(cases)==24 and sum(x.startswith('### 7.') for x in lines)==24
    print(json.dumps({'output':str(output),'case_count':24,'source_files_unchanged':len(original),'human_review_completed':False,'bytes':output.stat().st_size},ensure_ascii=False))

if __name__=='__main__':main()
