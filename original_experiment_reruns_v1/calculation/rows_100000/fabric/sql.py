import re


def parse_sql(sql, query_id='sql'):
    ident=r'[A-Za-z_][A-Za-z0-9_]*'
    field=ident+r'\.'+ident
    pattern=(r'\s*SELECT\s+(?P<select>.+?)\s+FROM\s+(?P<view>'+ident+r')'
             r'(?:\s+JOIN\s+(?P<right>'+ident+r')\s+ON\s+(?P<leftkey>'+field+r')\s*=\s*(?P<rightkey>'+field+r'))?'
             r'(?:\s+WHERE\s+(?P<where>'+field+r')\s*>=\s*(?P<value>[+-]?\d+(?:\.\d+)?))?\s*;?\s*')
    m=re.fullmatch(pattern,sql,flags=re.IGNORECASE)
    if not m: raise ValueError('SQL_SUBSET_ONLY')
    selection=m['select'].strip()
    agg=re.fullmatch(r'(SUM|COUNT)\s*\(\s*('+field+r')\s*\)',selection,flags=re.IGNORECASE)
    if agg:
        columns=[agg[2]]; op=agg[1].lower()
    else:
        columns=[x.strip() for x in selection.split(',')]; op='project'
        if not columns or not all(re.fullmatch(field,x) for x in columns): raise ValueError('SQL_SELECT_SUBSET')
    allowed={m['view'],m['right']}-{None}
    refs=columns+[x for x in [m['leftkey'],m['rightkey'],m['where']] if x]
    if any(x.split('.')[0] not in allowed for x in refs): raise ValueError('SQL_SCOPE')
    q={'id':query_id,'op':op,'columns':columns}
    if m['right']:
        if m['leftkey'].split('.')[0]!=m['view'] or m['rightkey'].split('.')[0]!=m['right']:
            raise ValueError('SQL_JOIN_DIRECTION')
        q['join']={'left':m['leftkey'],'right':m['rightkey']}
    if m['where']: q['where']={'field':m['where'],'value':m['value']}
    return q
