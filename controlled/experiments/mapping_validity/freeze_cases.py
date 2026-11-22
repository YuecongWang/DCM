import copy
import json
import hashlib
from decimal import Decimal
from experiment_config import INPUT_ROOT

SOURCE = {'id':'controlled://mapping-validity/spec-v1', 'kind':'CONTROLLED_INJECTION',
          'version':'1', 'description':'Researcher-authored controlled premises; not Valentine or real bank annotations.'}


def resource(identifier, kind, payload, dependencies=(), version='1'):
    return {'id':identifier,'kind':kind,'version':version,'source':SOURCE,
            'dependencies':list(dependencies),'payload':payload}


def base_state():
    resources={}
    def put(r): resources[r['id']]=r
    terms=['Employee','StaffMember','Vendor','Payable','Paid','Pretax','Net','PayableAlias',
           'CalendarMonth','FiscalMonth','Year','CNY','CNY_CENT','USD','CNY_PER_MONTH','CNY_PER_YEAR','UnrelatedCar']
    for term in terms: put(resource('concept:'+term,'concept',{'term':term,'permitted':True}))
    requirements={'object':'Employee','basis':'Payable','unit':'CNY','time':'CalendarMonth'}
    for dim,target in requirements.items():
        put(resource('requirement:'+dim,'requirement',{'dimension':dim,'required':target},['concept:'+target]))
    put(resource('rules:object','rules',{'triples':[
        ['demo:StaffMember','rdfs:subClassOf','demo:Employee'],
        ['demo:Employee','owl:disjointWith','demo:Vendor']]},['concept:StaffMember','concept:Employee','concept:Vendor']))
    put(resource('rules:basis','rules',{'triples':[
        ['demo:hasSalaryBasis','rdfs:subPropertyOf','demo:hasBasis'],
        ['demo:Payable','demo:incompatibleBasis','demo:Paid'],
        ['demo:Pretax','demo:incompatibleBasis','demo:Net']]},['concept:Payable','concept:Paid','concept:Pretax','concept:Net']))
    put(resource('rules:time','rules',{'triples':[
        ['demo:CalendarMonth','demo:incompatibleTime','demo:FiscalMonth']]},['concept:CalendarMonth','concept:FiscalMonth']))
    put(resource('rules:unit','rules',{'conversions':['approval:cny','approval:cent']}))
    for rid,unit,scale in [('approval:cny','CNY','1'),('approval:cent','CNY_CENT','0.01')]:
        put(resource(rid,'conversion_approval',{'from':unit,'to':'CNY','scale':scale,'quantum':'0.01',
                    'rounding':'HALF_EVEN','status':'approved','approved_version':'1'},['concept:'+unit,'concept:CNY']))
    premises=[]
    for mapping in ['m_a','m_b']:
        for dim,term in requirements.items():
            payload={'value':term}
            if dim!='unit': payload['triple']=['demo:'+mapping,{'object':'rdf:type','basis':'demo:hasBasis','time':'demo:hasTime'}[dim],'demo:'+term]
            put(resource('fact:'+mapping+':'+dim,'support_statement',payload,['concept:'+term]))

            resources['fact:'+mapping+':'+dim]['bindings']={'concept:'+term:'1'}
            deps=['requirement:'+dim,'fact:'+mapping+':'+dim,'rules:'+dim]
            if dim=='unit': deps+=['approval:cny','approval:cent']

            relevant={'object':['Employee','StaffMember','Vendor'], 'basis':['Payable','Paid','Pretax','Net','PayableAlias'],
                      'unit':['CNY','CNY_CENT','USD','CNY_PER_MONTH','CNY_PER_YEAR'], 'time':['CalendarMonth','FiscalMonth','Year']}[dim]
            deps+=['concept:'+x for x in relevant]
            premises.append({'id':mapping+':'+dim,'mapping_id':mapping,'dimension':dim,'version':'1',
                             'source':SOURCE,'requirement_id':'requirement:'+dim,
                             'actual_id':'fact:'+mapping+':'+dim,'rule_id':'rules:'+dim,'dependencies':sorted(set(deps))})
    return {'resources':resources,'premises':premises,'mapping_ids':['m_a','m_b'],
            'values':{'m_a':['100','200'],'m_b':['100','200']},'scales':{'m_a':'1','m_b':'1'}}


def fact(state,mapping,dim,value,predicate=None):
    r=state['resources']['fact:'+mapping+':'+dim]
    r['version']=str(int(r['version'])+1); r['payload']['value']=value
    if dim!='unit': r['payload']['triple']=['demo:'+mapping,predicate or {'object':'rdf:type','basis':'demo:hasBasis','time':'demo:hasTime'}[dim],'demo:'+value]
    r['dependencies']=['concept:'+value]; r['bindings']={'concept:'+value:state['resources']['concept:'+value]['version']}


def target(state,dim,value):
    r=state['resources']['requirement:'+dim]
    r['version']=str(int(r['version'])+1);r['payload']['required']=value;r['dependencies']=['concept:'+value]


def build_cases():
    cases=[]; gold=[]
    descriptions=[
        ('H01','harmless','元转分且采用批准的0.01转换','VALID',True),
        ('H02','harmless','分转元并恢复恒等转换','VALID',True),
        ('H03','harmless','十进制尾零表示变化','VALID',True),
        ('H04','harmless','科学计数法表示变化','VALID',True),
        ('H05','harmless','半偶舍入边界表示变化','VALID',True),
        ('H06','harmless','表示变化及无关概念描述变化','VALID',True),
        ('D01','legal_data','金额合法增加','VALID',False),
        ('D02','legal_data','合法新增行','VALID',False),
        ('D03','legal_data','合法删除行','VALID',False),
        ('D04','legal_data','空值变为合法金额','VALID',False),
        ('D05','legal_data','Decimal精度边界越过舍入点','VALID',False),
        ('D06','legal_data','允许负值的合法金额变化','VALID',False),
        ('B01','business_change','应付改为实付，显式口径不相容','INVALID',True),
        ('B02','business_change','税前改为实发，显式口径不相容','INVALID',True),
        ('B03','business_change','员工对象改为供应商，显式互斥','INVALID',True),
        ('B04','business_change','自然月改为财务月，显式时间不相容','INVALID',True),
        ('B05','business_change','共享约束要求改为实付，影响两个映射','INVALID',True),
        ('B06','business_change','共享参考概念明确禁用并且缺少单位证据','INVALID',True),
        ('U01','unknown','对象证据缺失','UNKNOWN',True),
        ('U02','unknown','只有closeMatch口径关系，不能批准','UNKNOWN',True),
        ('U03','unknown','转换批准的版本过期，旧批准拒用','UNKNOWN',True),
        ('U04','unknown','单位信息缺失且数值变化','UNKNOWN',False),
        ('U05','unknown','月薪到年薪计算乘12，但没有业务批准','UNKNOWN',False),
        ('U06','unknown','exactMatch不能覆盖口径和单位前提，数值变化','UNKNOWN',False),
    ]
    for index,(cid,category,description,expected,same) in enumerate(descriptions):
        before=base_state()
        if cid=='H02':
            for m in ['m_a','m_b']:
                before['values'][m]=['10000','20000'];before['scales'][m]='0.01';fact(before,m,'unit','CNY_CENT')
        if cid=='H05': before['values']['m_a']=['1.005','1.015']
        if cid=='D04': before['values']['m_a']=[None,'200']
        if cid=='D05': before['values']['m_a']=['1.005','200']
        if cid=='D06': before['values']['m_a']=['-10','200']
        if cid=='B02':
            target(before,'basis','Pretax')
            for m in ['m_a','m_b']:fact(before,m,'basis','Pretax')

        if cid=='H01':
            fact(before,'m_a','object','StaffMember')
            fact(before,'m_a','basis','Payable','demo:hasSalaryBasis')
        after=copy.deepcopy(before)
        if cid in ('H01','H05'):
            for m in ['m_a','m_b']:
                after['values'][m]=[str(Decimal(v)*100) if v is not None else None for v in before['values'][m]]
                after['scales'][m]='0.01';fact(after,m,'unit','CNY_CENT')
        elif cid=='H02':
            for m in ['m_a','m_b']:
                after['values'][m]=['100','200'];after['scales'][m]='1';fact(after,m,'unit','CNY')
        elif cid=='H03': after['values']['m_a']=['100.0000','200.000000']
        elif cid=='H04': after['values']['m_a']=['1E2','2E2']
        elif cid=='H06':
            after['values']['m_a']=['0100.0','0200.000']
            after['resources']['concept:UnrelatedCar']['version']='2'
            after['resources']['concept:UnrelatedCar']['payload']['description']='Unrelated metadata edit'
        elif cid=='D01': after['values']['m_a']=['101','200']
        elif cid=='D02':
            after['values']['m_a'].append('50');after['values']['m_b'].append('25')
        elif cid=='D03':
            after['values']['m_a']=after['values']['m_a'][:1];after['values']['m_b']=after['values']['m_b'][:1]
        elif cid=='D04': after['values']['m_a']=['100','200']
        elif cid=='D05': after['values']['m_a']=['1.005000000000000000000001','200']
        elif cid=='D06': after['values']['m_a']=['-11','200']
        elif cid=='B01':fact(after,'m_a','basis','Paid')
        elif cid=='B02':fact(after,'m_a','basis','Net')
        elif cid=='B03':fact(after,'m_a','object','Vendor')
        elif cid=='B04':fact(after,'m_a','time','FiscalMonth')
        elif cid=='B05':target(after,'basis','Paid')
        elif cid=='B06':
            after['resources']['concept:Employee']['version']='2'
            after['resources']['concept:Employee']['payload']['permitted']=False
            del after['resources']['fact:m_a:unit']
        elif cid=='U01':del after['resources']['fact:m_a:object']
        elif cid in ('U02','U06'):
            fact(after,'m_a','basis','PayableAlias')
            after['resources']['rules:basis']['version']='2'
            after['resources']['rules:basis']['payload']['triples'].append(
                ['demo:PayableAlias','skos:closeMatch' if cid=='U02' else 'skos:exactMatch','demo:Payable'])
            if cid=='U06':
                fact(after,'m_a','unit','USD');after['values']['m_a']=['101','200']
        elif cid=='U03':
            after['resources']['approval:cny']['version']='2'
        elif cid=='U04':
            del after['resources']['fact:m_a:unit'];after['values']['m_a']=['101','200']
        elif cid=='U05':
            target(after,'unit','CNY_PER_YEAR');target(after,'time','Year')
            for m in ['m_a','m_b']:
                fact(after,m,'unit','CNY_PER_MONTH');after['scales'][m]='12'
        case={'id':cid,'description':description,'source':SOURCE,'before':before,'after':after}
        cases.append(case)
        both=cid in ('B05','B06','U03','U05')
        maps={'m_a':expected,'m_b':expected if both else 'VALID'}
        gold.append({'case_id':cid,'category':category,'expected_case_state':expected,
                     'expected_mapping_states':maps,'expected_answers_equal':same,'expected_execution_errors':[],
                     'rationale':description,'review_status':'HUMAN_REVIEW_PENDING',
                     'reviewer':None,'note':'Predeclared specification expectations authored before testing; no human signature supplied.'})
    return cases,gold


def main():
    INPUT_ROOT.mkdir(parents=True,exist_ok=True)
    cases,gold=build_cases()
    rules={'source':SOURCE,'state_logic':'Violation dominates UNKNOWN; VALID requires current support for each premise.',
           'allowed_inference':['rdfs:subClassOf','rdfs:subPropertyOf','rdf:type inheritance','explicit incompatibility'],
           'skos_policy':'closeMatch/related/exactMatch do not approve business prerequisites.',
           'unit_policy':'Only explicit current approved from/to/scale/quantum/rounding records approve conversion.'}
    files={'cases.json':cases,'expectations.json':gold,'rules.json':rules}
    frozen={}
    for name,value in files.items():
        path=INPUT_ROOT/name
        if path.exists():raise RuntimeError('Fixed cases already exist; preserve them and use a new experiment version.')
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        frozen[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    (INPUT_ROOT/'freeze_manifest.json').write_text(json.dumps({'version':'1','case_count':24,'sha256':frozen,
        'human_review_status':'PENDING','labels_not_passed_to_evaluators':True},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Fixed 24 cases and independent expectations before evaluation.')


if __name__=='__main__':main()
