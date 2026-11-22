import json
import sqlite3
import time
from .common import canonical, digest
from .graph import NAMESPACES, vocabulary_graph


def field_uri(view, field):
    return NAMESPACES['demo']+'field:'+view+'.'+field


def physical_uri(source, field):
    return NAMESPACES['demo']+'physical:'+source+'.'+field


def scopes_for_query(query):
    scopes = set(query.get('columns', []))
    if query.get('join'):
        scopes.update(query['join'].values())
    if query.get('where'):
        scopes.add(query['where']['field'])
    return sorted(scopes)


def metadata_graph(snapshot, queries):
    g = vocabulary_graph()
    for source_id, source in snapshot['sources'].items():
        platform = g.node(NAMESPACES['demo']+'platform:'+source_id,['dataPlatform'],
                          {'name':source_id,'connector':source['identity']['kind']},'fact')
        ds = g.node(NAMESPACES['demo']+'source:'+source_id,['dataset'],
                    {'name':source_id,'location':source['identity']['path'],'version':source['identity']['version'],
                     'content_id':source['content_id']},'fact')
        g.edge(ds,'zysemg:SourcePlatform',platform)
        for column in source['schema']:
            f=g.node(physical_uri(source_id,column['name']),['SchemaField'],column,'fact')
            g.edge(ds,'zysemg:Contains',f)
    for name, view in snapshot['views'].items():
        ds=g.node(NAMESPACES['demo']+'view:'+name,['dataset'],{'name':name,'virtual':True},'fact')
        for name2, mapping in view['fields'].items():
            f=g.node(field_uri(name,name2),['SchemaField'],{'name':name2,'mapping':mapping},'fact')
            job=g.node(NAMESPACES['demo']+'mapping:'+name+'.'+name2,['dataProcess'],
                       {'name':name+'.'+name2,'definition':mapping,'digest':digest(mapping)},'fact')
            g.edge(ds,'zysemg:Contains',f)

            p=physical_uri(view['source'],mapping['column'])
            if p not in g.nodes:
                g.node(p,['SchemaField'],{'name':mapping['column'],'missing':True},'fact')
            g.edge(p,'demo:feeds',job)
            g.edge(job,'demo:feeds',f)
            g.edge(f,'zysemg:DerivedFrom',p)

            k=physical_uri(view['source'],view['key'])
            g.edge(k,'demo:feeds',job)
            if mapping.get('concept'):
                target=NAMESPACES['demo']+mapping['concept']
                if target not in g.nodes:
                    g.node(target,['Concept'],{'name':mapping['concept']},'reference')
                g.edge(f,'zysemg:AssociatedWith',target)
    for q in queries:
        u=g.node(NAMESPACES['demo']+'query:'+q['id'],['dataJob'],{'name':q['id'],'query_id':q['id'],'definition':q},'fact')
        for field in scopes_for_query(q):
            view, col = field.split('.')
            f=field_uri(view,col)
            if f not in g.nodes:
                g.node(f,['SchemaField'],{'name':col,'missing':True},'fact')
            g.edge(f,'demo:feeds',u)
            g.edge(u,'zysemg:DerivedFrom',f)
    return g


def changed_fields(old, new):

    changed=[]
    names=set(old['views']) | set(new['views'])
    for v in sorted(names):
        a,b=old['views'].get(v),new['views'].get(v)
        cols=set(a['fields'] if a else []) | set(b['fields'] if b else [])
        for col in sorted(cols):
            if a is None or b is None or a['source']!=b['source'] or a['key']!=b['key']:
                changed.append(v+'.'+col); continue
            ma,mb=a['fields'].get(col),b['fields'].get(col)
            if ma is None or mb is None or ma != mb:
                changed.append(v+'.'+col); continue
            sa,sb=old['sources'][a['source']],new['sources'][b['source']]

            def scoped(s, m, key):
                names={m['column'],key}
                return {'version':s['identity']['version'],
                        'schema':[x for x in s['schema'] if x['name'] in names],
                        'values':[[r.get(key),r.get(m['column'])] for r in s['rows']]}
            if scoped(sa,ma,a['key'])!=scoped(sb,mb,b['key']):
                changed.append(v+'.'+col)
    return changed


class Catalog:
    def __init__(self, path):
        self.path=path
        con=sqlite3.connect(path)
        with con:
            con.execute('CREATE TABLE IF NOT EXISTS aspects(urn TEXT, version TEXT, aspect TEXT, body TEXT, PRIMARY KEY(urn,version,aspect))')
            con.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, event TEXT, body TEXT)')
        con.close()

    def ingest(self, snapshot):
        con=sqlite3.connect(self.path)
        with con:
            for sid, s in snapshot['sources'].items():
                urn='urn:fabric-reproduction:dataset:'+sid
                for aspect, body in [('schema',s['schema']),('location',s['identity']),('profile',{'rows':len(s['rows']),'content_id':s['content_id']})]:
                    con.execute('INSERT OR REPLACE INTO aspects VALUES (?,?,?,?)',(urn,s['content_id'],aspect,canonical(body)))
            for name,v in snapshot['views'].items():
                urn='urn:fabric-reproduction:view:'+name
                con.execute('INSERT OR REPLACE INTO aspects VALUES (?,?,?,?)',(urn,digest(v),'mapping',canonical(v)))
            con.execute('INSERT INTO events(event,body) VALUES (?,?)',(snapshot['name'],canonical({'sources':{s:v['content_id'] for s,v in snapshot['sources'].items()},'views':digest(snapshot['views'])})))
        con.close()

    def entities(self):
        con=sqlite3.connect(self.path)
        out=[{'urn':u,'version':v,'aspect':a,'body':json.loads(b)} for u,v,a,b in con.execute('SELECT urn,version,aspect,body FROM aspects ORDER BY urn,version,aspect')]
        con.close(); return out

    def search(self, text):
        return [e for e in self.entities() if text.casefold() in canonical(e).casefold()]
