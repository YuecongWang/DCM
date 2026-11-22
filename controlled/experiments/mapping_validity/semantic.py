import copy
import bootstrap
from fabric.common import digest
from fabric.graph import Graph, NAMESPACES


def uri(term):
    if ':' in term:
        p,s=term.split(':',1)
        return NAMESPACES.get(p,'urn:controlled:'+p+':')+s
    return NAMESPACES['demo']+term


def dependency_ids(premise,state):

    ids=set(premise['dependencies'])
    if premise['dimension']=='unit':ids.add('@transform:'+premise['mapping_id'])
    pending=list(ids)
    while pending:
        rid=pending.pop()
        for dep in state['resources'].get(rid,{}).get('dependencies',[]):
            if dep not in ids:ids.add(dep);pending.append(dep)
    return sorted(ids)


def dependency_bindings(premise,state):
    bindings={}
    for rid in dependency_ids(premise,state):
        if rid.startswith('@transform:'):
            m=rid.split(':',1)[1]
            body={'scale':state['scales'][m],'kind':'decimal','quantum':'0.01','rounding':'HALF_EVEN'}
            bindings[rid]={'version':'content-addressed','sha256':digest(body)}
        else:
            r=state['resources'].get(rid)
            bindings[rid]={'version':r['version'],'sha256':digest(r)} if r else {'version':None,'sha256':'MISSING'}
    bindings['@premise-definition']={'version':premise['version'],'sha256':digest(premise)}
    return bindings


def current_resource(state,rid):
    r=state['resources'].get(rid)
    if r is None:return None,{'code':'MISSING_RESOURCE','resource':rid}
    for dep,expected in r.get('bindings',{}).items():
        actual=state['resources'].get(dep)
        if actual is None or actual['version']!=expected:
            return None,{'code':'STALE_SUPPORT_VERSION','resource':rid,'dependency':dep,
                         'bound_version':expected,'current_version':actual['version'] if actual else None}
    return r,None


def make_graph(state,premise):
    g=Graph(); triples=[]; unknowns=[]
    for rid in [premise['actual_id'],premise['rule_id']]:
        r,problem=current_resource(state,rid)
        if problem:unknowns.append(problem);continue
        payload=r['payload']
        if payload.get('triple'):triples.append((payload['triple'],rid))
        for triple in payload.get('triples',[]):triples.append((triple,rid))
    for triple,rid in triples:
        s,p,o=triple
        for term in [s,o]:
            if uri(term) not in g.nodes:g.node(uri(term),['Resource'],{'name':term},'concept')

        if p=='rdfs:subPropertyOf':
            for term in [s,o]:g.nodes[uri(term)]['properties']['qname']=term
        g.edge(uri(s),p,uri(o),{'source_resource':rid,'source_version':state['resources'][rid]['version']})
    g.infer()
    return g,unknowns


def proof(g,triple,seen=None):
    seen=set(seen or ())
    if triple in seen:return {'triple':list(triple),'cycle_reference':True}
    e=g.edges[triple]
    if e['asserted']:
        return {'triple':list(triple),'asserted':True,'resource':e['properties'].get('source_resource'),
                'version':e['properties'].get('source_version')}
    return {'triple':list(triple),'asserted':False,'rule':e['rule'],
            'premises':[proof(g,tuple(t),seen|{triple}) for t in e['premises']]}


def check_premise(state,premise):

    violations=[]; unknowns=[]; supports=[]
    req,problem=current_resource(state,premise['requirement_id'])
    if problem:unknowns.append(problem)
    required=req['payload']['required'] if req else None
    concept=state['resources'].get('concept:'+str(required)) if required else None
    if required and concept is None:unknowns.append({'code':'MISSING_REQUIRED_CONCEPT','required':required})
    elif concept and concept['payload'].get('permitted') is False:
        violations.append({'code':'EXPLICIT_CONCEPT_PROHIBITION','resource':concept['id'],'version':concept['version']})
    actual,problem=current_resource(state,premise['actual_id'])
    if problem:unknowns.append(problem)
    dim=premise['dimension']
    if req and actual:
        if dim=='unit':
            registry,problem=current_resource(state,premise['rule_id'])
            if problem:unknowns.append(problem)
            from_unit=actual['payload']['value']; found=False
            for rid in registry['payload'].get('conversions',[]) if registry else []:
                record,problem=current_resource(state,rid)
                if problem:unknowns.append(problem);continue
                p=record['payload']
                if (p.get('from'),p.get('to'))!=(from_unit,required):continue
                found=True
                if p.get('approved_version')!=record['version']:
                    unknowns.append({'code':'STALE_CONVERSION_APPROVAL','resource':rid,'approved_version':p.get('approved_version'),
                                     'current_version':record['version']});continue
                if p.get('status')=='revoked':
                    violations.append({'code':'EXPLICIT_CONVERSION_REVOCATION','resource':rid});continue
                if p.get('status')!='approved':
                    unknowns.append({'code':'UNCONFIRMED_CONVERSION','resource':rid});continue
                expected={'scale':state['scales'][premise['mapping_id']],'quantum':'0.01','rounding':'HALF_EVEN'}
                if any(p.get(k)!=v for k,v in expected.items()):
                    violations.append({'code':'APPROVED_TRANSFORM_MISMATCH','resource':rid,'expected':expected,
                                       'approved':{k:p.get(k) for k in expected}})
                else:
                    supports.append({'resource':rid,'version':record['version'],'rule':'explicit-approved-conversion',
                                     'from':from_unit,'to':required,**expected})
            if not found:unknowns.append({'code':'NO_APPROVED_CONVERSION','from':from_unit,'to':required})
        else:
            g,problems=make_graph(state,premise);unknowns.extend(problems)
            subject=uri('demo:'+premise['mapping_id'])
            predicate={'object':'rdf:type','basis':'demo:hasBasis','time':'demo:hasTime'}[dim]
            approved=(subject,predicate,uri('demo:'+required))
            relation={'object':'owl:disjointWith','basis':'demo:incompatibleBasis','time':'demo:incompatibleTime'}[dim]
            for s,p,o in g.edges:
                if s==subject and p==predicate:
                    for incompat in [(o,relation,approved[2]),(approved[2],relation,o)]:
                        if incompat in g.edges:
                            violations.append({'code':'EXPLICIT_INCOMPATIBILITY','observed':o,'required':approved[2],
                                               'evidence':[proof(g,(s,p,o)),proof(g,incompat)]})
            if approved in g.edges:supports.append(proof(g,approved))
            elif not violations:
                unknowns.append({'code':'NO_DERIVABLE_SUPPORT','dimension':dim,'required':required,
                                 'observed':actual['payload'].get('value')})
    if not supports and not unknowns and not violations:unknowns.append({'code':'NO_CURRENT_SUPPORT'})
    state_code='INVALID' if violations else 'UNKNOWN' if unknowns else 'VALID'
    return {'premise_id':premise['id'],'state':state_code,'violations':violations,'unknowns':unknowns,
            'supports':supports,'bindings':dependency_bindings(premise,state)}


def aggregate(premise_results,state):
    mappings={}
    for m in state['mapping_ids']:
        parts=[premise_results[p['id']] for p in state['premises'] if p['mapping_id']==m]
        codes=[p['state'] for p in parts]
        code='INVALID' if 'INVALID' in codes else 'UNKNOWN' if 'UNKNOWN' in codes or len(parts)!=4 else 'VALID'
        mappings[m]={'state':code,'premises':parts,
                     'violations':[v for p in parts for v in p['violations']],
                     'unknowns':[u for p in parts for u in p['unknowns']]}
    codes=[m['state'] for m in mappings.values()]
    case_state='INVALID' if 'INVALID' in codes else 'UNKNOWN' if 'UNKNOWN' in codes else 'VALID'
    return {'state':case_state,'mappings':mappings}


def delivery(business,calculation):
    if business['state']=='NOT_ASSESSED':return {'decision':'NOT_ASSESSED','approved':None}
    if business['state']=='INVALID':return {'decision':'BUSINESS_BLOCKED','approved':False}
    if business['state']=='UNKNOWN':return {'decision':'PENDING_VERIFICATION','approved':False}
    if any(v['errors'] for v in calculation.values()):return {'decision':'COMPUTATION_FAILED','approved':False}
    return {'decision':'APPROVED','approved':True}
