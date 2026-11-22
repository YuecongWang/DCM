from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN
from .catalog import scopes_for_query
from .common import digest


def validate_definition(definition):
    if definition.get('kind') not in ('text','decimal') or not isinstance(definition.get('column'),str):
        raise ValueError('UNSUPPORTED_RULE')
    if definition.get('null_policy','None-or-empty')!='None-or-empty' or definition.get('error_policy','fail-query')!='fail-query':
        raise ValueError('UNSUPPORTED_RULE')
    if definition['kind']=='decimal':
        if definition.get('rounding')!='HALF_EVEN' or definition.get('input_digits',28)!=28 or definition.get('input_adjusted_exponent_limit',28)!=28:
            raise ValueError('UNSUPPORTED_RULE')
        try:
            scale=Decimal(definition['scale']); quantum=Decimal(definition['quantum'])
            if not scale.is_finite() or not quantum.is_finite() or quantum<=0 or quantum.as_tuple().digits!=(1,):
                raise ValueError('RULE_DEFINITION')
            if len(scale.as_tuple().digits)>28 or abs(scale.adjusted())>28 or abs(quantum.adjusted())>28:
                raise ValueError('RULE_RANGE')
        except (InvalidOperation,KeyError):
            raise ValueError('RULE_DEFINITION') from None


def convert(value, definition):
    validate_definition(definition)
    kind=definition['kind']
    if value is None or value == '':
        return None
    if not isinstance(value,str):
        raise ValueError('TYPE')
    if kind == 'text':
        return value
    if kind != 'decimal' or definition.get('rounding')!='HALF_EVEN':
        raise ValueError('UNSUPPORTED_RULE')
    try:
        d=Decimal(value)
        if not d.is_finite(): raise ValueError('NONFINITE')
        if len(d.as_tuple().digits)>28 or abs(d.adjusted())>28: raise ValueError('RANGE')
        scale=Decimal(definition['scale']); quantum=Decimal(definition['quantum'])
        if not scale.is_finite() or not quantum.is_finite() or quantum <= 0 or quantum.as_tuple().digits != (1,):
            raise ValueError('RULE_DEFINITION')
        if len(scale.as_tuple().digits)>28 or abs(scale.adjusted())>28 or abs(quantum.adjusted())>28:
            raise ValueError('RULE_RANGE')
        with localcontext() as ctx:
            ctx.prec=96
            result=(d*scale).quantize(quantum,rounding=ROUND_HALF_EVEN)
        if result == 0: result = abs(result)
        return format(result,'f')
    except InvalidOperation:
        raise ValueError('INVALID_DECIMAL') from None


def normalized_column(snapshot, logical_field, scan):
    view_name,field=logical_field.split('.')
    view=snapshot['views'].get(view_name)
    if view is None or field not in view['fields']:
        return [], [{'field':logical_field,'code':'MISSING_MAPPING'}]
    definition=view['fields'][field]
    try:
        validate_definition(definition)
    except ValueError as exc:
        return [], [{'field':logical_field,'code':str(exc)}]
    source=snapshot['sources'].get(view['source'])
    if source is None:
        return [], [{'field':logical_field,'code':'MISSING_SOURCE'}]
    schema={s['name'] for s in source['schema']}
    if definition['column'] not in schema:
        return [], [{'field':logical_field,'code':'MISSING_COLUMN'}]
    out,errors=[],[]
    for index,row in enumerate(source['rows']):
        scan[0]+=1
        try:
            value=convert(row[definition['column']],definition)
            out.append([row[view['key']],value])
        except (ValueError, KeyError) as exc:
            out.append([row[view['key']],None])
            errors.append({'field':logical_field,'row_id':row[view['key']],'row_index':index,'code':str(exc) if isinstance(exc,ValueError) else 'RULE_DEFINITION'})
    return out,errors


def execute(snapshot, query, scan):
    scopes=scopes_for_query(query)
    fields={}; errors=[]
    for field in scopes:
        vals,errs=normalized_column(snapshot,field,scan)
        fields[field]=dict(vals); errors.extend(errs)
    if errors:
        return {'answer':None,'errors':errors}
    numeric_fields=[]
    if query['op']=='sum': numeric_fields.append(query['columns'][0])
    if query.get('where'): numeric_fields.append(query['where']['field'])
    for field in numeric_fields:
        view,col=field.split('.')
        if snapshot['views'][view]['fields'][col]['kind']!='decimal':
            return {'answer':None,'errors':[{'field':field,'code':'QUERY_NUMERIC_TYPE'}]}
    views={f.split('.')[0] for f in scopes}
    if len(views)>2 or (len(views)>1 and not query.get('join')):
        return {'answer':None,'errors':[{'code':'UNSUPPORTED_JOIN'}]}
    if not views:
        return {'answer':None,'errors':[{'code':'EMPTY_SCOPE'}]}

    join=query.get('join'); rows=[]
    if join:
        left,right=join['left'],join['right']; lv,rv=left.split('.')[0],right.split('.')[0]
        index={}
        for rid,val in fields[right].items():
            if val is not None: index.setdefault(val,[]).append(rid)
        for lid,val in fields[left].items():
            if val is None: continue
            for rid in index.get(val,[]):
                rows.append({f:column[lid if f.split('.')[0]==lv else rid] for f,column in fields.items()})
    else:
        first=next(iter(fields.values()))
        rows=[{f:column[rid] for f,column in fields.items()} for rid in first]
    where=query.get('where')
    if where:
        with localcontext() as ctx:
            ctx.prec=128
            threshold=Decimal(where['value'])
            rows=[r for r in rows if r[where['field']] is not None and Decimal(r[where['field']])>=threshold]
    op=query['op']; columns=query['columns']
    if op=='project': answer=[[r[f] for f in columns] for r in rows]
    elif op=='count': answer=sum(r[columns[0]] is not None for r in rows)
    elif op=='sort': answer=sorted([r[columns[0]] for r in rows if r[columns[0]] is not None])
    elif op=='sum':
        with localcontext() as ctx:
            ctx.prec=128
            values=[Decimal(r[columns[0]]) for r in rows if r[columns[0]] is not None]
            answer=format(sum(values,Decimal(0)),'f') if values else None
    else:
        return {'answer':None,'errors':[{'code':'UNSUPPORTED_OPERATION'}]}
    return {'answer':answer,'errors':[]}


def scope_identity(snapshot, scope):

    identities=[]
    for field in scope:
        v,c=field.split('.'); view=snapshot['views'].get(v)
        if view is None:
            identities.append({'field':field,'missing_view':True}); continue
        source=snapshot['sources'].get(view['source'])
        identities.append({'field':field,'source_id':source['content_id'] if source else None,
                           'definition':view['fields'].get(c),'key':view['key'],'source':view['source']})
    return digest({'scope':list(scope),'identity':identities,'semantics':'full-ordered-keyed-column-v1'})


def check_equivalence(old,new,scope,scan):
    pairs=[(normalized_column(old,f,scan),normalized_column(new,f,scan)) for f in scope]
    failed=any(a[1] or b[1] for a,b in pairs)


    def output_kind(snapshot,field):
        v,c=field.split('.')
        return snapshot['views'].get(v,{}).get('fields',{}).get(c,{}).get('kind')
    compatible_types=all(output_kind(old,f)==output_kind(new,f) for f in scope)
    return {'equivalent':not failed and compatible_types and all(a[0]==b[0] for a,b in pairs),
            'failed':bool(failed),'compatible_output_types':compatible_types}


def quality_report(snapshot):
    summarize_version_cache=[]
    for v,view in snapshot['views'].items():
        for c in view['fields']:
            values,errors=normalized_column(snapshot,v+'.'+c,[0])
            summarize_version_cache.append({'field':v+'.'+c,'rows':len(values),'nulls':sum(x[1] is None for x in values),
                           'conversion_errors':len(errors),'errors':errors})
    return summarize_version_cache
