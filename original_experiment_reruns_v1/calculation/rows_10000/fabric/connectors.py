import csv
import sqlite3
from .common import digest


def identifier(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Invalid identifier')
    return '"' + value.replace('"', '""') + '"'


def capture_source(spec):
    from pathlib import Path
    path = Path(spec['path']).resolve()
    if spec['kind'] == 'csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            if len(set(fields)) != len(fields) or not fields:
                raise ValueError('CSV_SCHEMA')
            rows = list(reader)
        if any(None in r or any(v is None for v in r.values()) for r in rows):
            raise ValueError('CSV_ROW_WIDTH')
        schema = [{'name':f, 'type':'TEXT'} for f in fields]
    elif spec['kind'] == 'sqlite':
        con = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        try:
            con.execute('BEGIN')
            schema = [{'name':r[1], 'type':r[2]} for r in con.execute('PRAGMA table_info('+identifier(spec['table'])+')')]
            if not schema:
                raise ValueError('SQLITE_SCHEMA')
            fields = [s['name'] for s in schema]
            rows = [dict(zip(fields, row)) for row in con.execute('SELECT * FROM '+identifier(spec['table']))]
        finally:
            con.close()
    else:
        raise ValueError('Unsupported connector '+str(spec['kind']))
    key = spec.get('key', 'row_id')
    if key not in fields:
        raise ValueError('MISSING_KEY')

    keys = []
    for r in rows:
        raw = r[key]
        if raw is None or not str(raw).isascii() or not str(raw).isdigit():
            raise ValueError('KEY_FORMAT')
        keys.append(str(raw))
        r[key] = str(raw)
    if len(keys) != len(set(keys)):
        raise ValueError('DUPLICATE_KEY')
    rows.sort(key=lambda r:int(r[key]))
    identity = {'source_id':spec['id'], 'kind':spec['kind'], 'path':str(path),
                'table':spec.get('table'), 'version':spec['version'], 'key':key}
    snapshot = {'identity':identity, 'schema':schema, 'rows':rows}
    snapshot['content_id'] = digest(snapshot)
    snapshot['scanned_cells'] = len(rows)*len(fields)
    return snapshot


def capture_event(event):
    return {'name':event['name'], 'sources':{s['id']:capture_source(s) for s in event['sources']},
            'views':event['views'], 'revision':event.get('revision','1')}
