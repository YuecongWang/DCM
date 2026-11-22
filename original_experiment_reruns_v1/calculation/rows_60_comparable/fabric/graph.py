import re
import sqlite3
import uuid
from collections import deque
from .common import canonical, digest, write_json

NAMESPACES = {
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'rdfs': 'http://www.w3.org/2000/01/rdf-schema#',
    'skos': 'http://www.w3.org/2004/02/skos/core#',
    'owl': 'http://www.w3.org/2002/07/owl#',
    'xsd': 'http://www.w3.org/2001/XMLSchema#',
    'tkos': 'http://taifabric.cn/tkos#',
    'zysemg': 'http://taifabric.cn/zysemg#',
    'demo': 'urn:fabric-reproduction:demo:',
}
LAYERS = ['system', 'namespace', 'bilingual', 'w3c', 'reference', 'concept', 'fact']


class Graph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}

    def node(self, uri, labels, properties=None, layer='fact'):
        if layer not in LAYERS:
            raise ValueError('Unknown graph layer')
        props = dict(properties or {})
        prefix = next((p for p, ns in NAMESPACES.items() if uri.startswith(ns)), 'demo')
        props.setdefault('ID', uri.rsplit('#', 1)[-1].rsplit(':', 1)[-1])
        props.setdefault('name', props['ID'])
        props.setdefault('namespace', prefix)
        props.setdefault('nodeMapping', 'DATA' if layer == 'fact' else 'W3C' if layer == 'w3c' else 'TAIFABRIC')
        props.setdefault('timestatus', 'ST')
        props.setdefault('TID', prefix + '.' + labels[0] + '.' + uuid.uuid5(uuid.NAMESPACE_URL, uri).hex if layer == 'fact' else '0')
        self.nodes[uri] = {'uri': uri, 'labels': sorted(set(labels)), 'layer': layer, 'properties': props}
        return uri

    def edge(self, source, relation, target, properties=None):
        if source not in self.nodes or target not in self.nodes:
            raise ValueError('Dangling graph edge')
        key = (source, relation, target)
        props=dict(properties or {})
        props.setdefault('RID','demo.'+relation.replace(':','.')+'.'+uuid.uuid5(uuid.NAMESPACE_URL,canonical(key)).hex
                         if self.nodes[source]['layer']=='fact' or self.nodes[target]['layer']=='fact' else '0')
        props.setdefault('timestatus','ST')
        self.edges[key] = {'source': source, 'relation': relation, 'target': target,
                           'properties': props, 'asserted': True}

    def infer(self):

        inverse = {'skos:broader': 'skos:narrower', 'skos:narrower': 'skos:broader',
                   'skos:hasTopConcept': 'skos:topConceptOf', 'skos:topConceptOf': 'skos:hasTopConcept',
                   'skos:related': 'skos:related', 'skos:exactMatch': 'skos:exactMatch',
                   'skos:closeMatch': 'skos:closeMatch', 'owl:disjointWith': 'owl:disjointWith'}
        transitive = {'rdfs:subClassOf', 'rdfs:subPropertyOf', 'skos:broaderTransitive',
                      'skos:narrowerTransitive', 'skos:exactMatch'}
        def derive(key, rule, premises):
            if key in self.edges:
                return False
            self.edges[key] = {'source': key[0], 'relation': key[1], 'target': key[2],
                               'properties': {}, 'asserted': False, 'rule': rule,
                               'premises': [list(x) for x in premises]}
            return True
        changed = True
        while changed:
            changed = False
            current = list(self.edges)
            by_relation, by_source = {}, {}
            for triple in current:
                by_relation.setdefault(triple[1], []).append(triple)
                by_source.setdefault(triple[0], []).append(triple)
            for s, p, o in current:
                e = (s, p, o)
                if p in inverse:
                    changed |= derive((o, inverse[p], s), 'inverse/symmetric', [e])
                if p in {'skos:broader', 'skos:narrower'}:
                    changed |= derive((s, p + 'Transitive', o), 'hierarchy-superproperty', [e])
                if p in transitive:
                    for _, pp, z in by_source.get(o, []):
                        if pp == p:
                            changed |= derive((s, p, z), 'transitive', [e, (o, pp, z)])
                if p == 'rdf:type':
                    for _, pp, parent in by_source.get(o, []):
                        if pp == 'rdfs:subClassOf':
                            changed |= derive((s, 'rdf:type', parent), 'rdfs-type-inheritance', [e, (o, pp, parent)])

                prop_uri = NAMESPACES.get(p.split(':')[0], '') + p.split(':')[-1]
                for _, pp, value in by_source.get(prop_uri, []):
                    if pp == 'rdfs:domain':
                        changed |= derive((s, 'rdf:type', value), 'rdfs-domain', [e, (prop_uri, pp, value)])
                    elif pp == 'rdfs:range':
                        changed |= derive((o, 'rdf:type', value), 'rdfs-range', [e, (prop_uri, pp, value)])
                    elif pp == 'rdfs:subPropertyOf':
                        target_prop = self.nodes[value]['properties'].get('qname')
                        if target_prop:
                            changed |= derive((s, target_prop, o), 'rdfs-subproperty', [e, (prop_uri, pp, value)])
        return sum(not e['asserted'] for e in self.edges.values())

    def consistency(self):
        violations = []
        types = {}
        for s, p, o in self.edges:
            if p == 'rdf:type':
                types.setdefault(s, set()).add(o)
            if p == 'rdfs:subClassOf' and s == o:
                violations.append({'code': 'CLASS_CYCLE', 'node': s})
        for s, classes in types.items():
            for a in classes:
                for b in classes:
                    if (a, 'owl:disjointWith', b) in self.edges:
                        violations.append({'code': 'DISJOINT_TYPES', 'node': s, 'classes': sorted([a, b])})
        return list({canonical(v): v for v in violations}.values())

    def impacted_queries(self, changed_uris):

        links = {}
        for s, p, o in self.edges:
            if p == 'demo:feeds':
                links.setdefault(s, []).append(o)
        seen, queue = set(changed_uris), deque(changed_uris)
        while queue:
            for n in links.get(queue.popleft(), []):
                if n not in seen:
                    seen.add(n); queue.append(n)
        return sorted(self.nodes[n]['properties']['query_id'] for n in seen
                      if n in self.nodes and 'query_id' in self.nodes[n]['properties'])

    def semantic_search(self,text):

        needle=text.casefold().strip()
        if not needle: return {'term_nodes':[],'assets':[],'evidence':[]}
        matches=set()
        for u,n in self.nodes.items():
            if 'Concept' not in n['labels']: continue
            p=n['properties']; labels=[p.get('name',''),p.get('prefLabel','')]
            alt=p.get('altLabel',[]); labels.extend(alt if isinstance(alt,list) else [alt])
            if any(needle==str(x).casefold() for x in labels): matches.add(u)
        expanded=set(matches)
        for s,p,o in self.edges:
            if p=='skos:broaderTransitive' and o in matches: expanded.add(s)
        evidence=[]; assets=set()
        for s,p,o in self.edges:
            if p=='zysemg:AssociatedWith' and o in expanded:
                assets.add(s); evidence.append({'asset':s,'concept':o,'relation':p})
        return {'term_nodes':sorted(matches),'assets':[self.nodes[u] for u in sorted(assets)],'evidence':evidence}

    def export(self):
        return {'layers': LAYERS, 'namespaces': NAMESPACES,
                'nodes': sorted(self.nodes.values(), key=lambda n: n['uri']),
                'edges': sorted(self.edges.values(), key=lambda e: (e['source'], e['relation'], e['target']))}

    def persist(self, path):
        con = sqlite3.connect(path)
        with con:
            con.execute('CREATE TABLE IF NOT EXISTS graph_nodes(uri TEXT PRIMARY KEY, document TEXT NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS graph_edges(identity TEXT PRIMARY KEY, document TEXT NOT NULL)')
            con.execute('DELETE FROM graph_nodes'); con.execute('DELETE FROM graph_edges')
            con.executemany('INSERT INTO graph_nodes VALUES (?, ?)', [(u, canonical(n)) for u,n in self.nodes.items()])
            con.executemany('INSERT INTO graph_edges VALUES (?, ?)', [(digest(k), canonical(v)) for k,v in self.edges.items()])
        con.close()

    @classmethod
    def load(cls, path):
        import json
        g = cls(); con = sqlite3.connect(path)
        g.nodes = {u: json.loads(d) for u,d in con.execute('SELECT uri,document FROM graph_nodes')}
        for document, in con.execute('SELECT document FROM graph_edges'):
            e=json.loads(document); g.edges[(e['source'],e['relation'],e['target'])]=e
        con.close(); return g

    def export_neo4j(self, path):

        def literal(v):
            if isinstance(v, str):
                return "'" + v.replace('\\', '\\\\').replace("'", "\\'").replace('\n','\\n').replace('\r','\\r') + "'"
            if isinstance(v, bool): return 'true' if v else 'false'
            if v is None: return 'null'
            if isinstance(v, (int,float)): return str(v)
            if isinstance(v,list) and all(isinstance(x,(str,int,float,bool)) for x in v): return '['+', '.join(literal(x) for x in v)+']'
            return literal(canonical(v))
        safe=lambda x: re.sub('[^A-Za-z0-9_]', '_', x)
        lines=['// Research prototype export. Validate with your own Neo4j version before use.']
        for n in self.export()['nodes']:
            props = dict(n['properties'], uri=n['uri'], layer=n['layer'])
            pairs=', '.join(safe(k)+': '+literal(v) for k,v in props.items())
            lines.append('MERGE (n:FabricResource {uri: '+literal(n['uri'])+'}) SET n += {'+pairs+'} SET n:'+':'.join(safe(l) for l in n['labels'])+';')
        for e in self.export()['edges']:
            props=dict(e['properties'], relation_uri=e['relation'], asserted=e['asserted'])
            lines.append('MATCH (s:FabricResource {uri: '+literal(e['source'])+'}), (o:FabricResource {uri: '+literal(e['target'])+'}) MERGE (s)-[r:'+safe(e['relation'])+']->(o) SET r += {'+', '.join(safe(k)+': '+literal(v) for k,v in props.items())+'};')
        path.write_text('\n'.join(lines), encoding='utf-8')


def vocabulary_graph():
    g = Graph()
    for name in ['_GraphConfig', '_MapDef', '_MapNs', '_NsPrefDef']:
        g.node(NAMESPACES['tkos']+name, [name], {'name': name}, 'system')
    for prefix, uri in NAMESPACES.items():
        g.node(uri, ['_NsPrefDef'], {'ID':prefix,'name':prefix,'prefix':prefix}, 'namespace')
    classes={'skos:ConceptScheme':'词汇体系','skos:Collection':'词汇集合','skos:OrderedCollection':'有序集合',
             'skos:Concept':'主题词汇','rdfs:Resource':'资源','rdfs:Class':'类','rdf:Property':'属性',
             'owl:DatatypeProperty':'数据类型属性','owl:ObjectProperty':'关系对象','owl:Ontology':'本体'}
    for qname, zh in classes.items():
        p, local=qname.split(':'); uri=NAMESPACES[p]+local
        g.node(uri, [local], {'name':zh,'qname':qname}, 'w3c')
        g.node('urn:fabric-reproduction:bilingual:'+qname, ['_MapDef'], {'name':zh,'qname':qname,'english':local}, 'bilingual')
    for name in ['string','integer','boolean','date','float','time','datetime','long','byte','char','short','double']:
        g.node(NAMESPACES['xsd']+name, ['Resource'], {'name':name}, 'w3c')
    props=['altLabel','hiddenLabel','prefLabel','notation','changeNote','definition','editorialNote','example','historyNote','note','scopeNote']
    base=g.node(NAMESPACES['tkos']+'BaseProperty',['Property'],{'name':'基础节点标签属性'},'w3c')
    for name in props:
        u=g.node(NAMESPACES['skos']+name,['DatatypeProperty'],{'name':name,'qname':'skos:'+name},'w3c')
        g.edge(u,'rdfs:range',NAMESPACES['xsd']+'string')
        g.edge(u,'rdfs:subPropertyOf',base)
    relations=['skos:inScheme','skos:hasTopConcept','skos:topConceptOf','skos:broader','skos:broaderTransitive',
               'skos:narrower','skos:narrowerTransitive','skos:related','skos:exactMatch','skos:closeMatch',
               'rdfs:subClassOf','rdfs:subPropertyOf','rdfs:domain','rdfs:range','rdf:type','owl:disjointWith']
    for qname in relations:
        p,name=qname.split(':'); g.node(NAMESPACES[p]+name,['ObjectProperty'],{'name':name,'qname':qname},'w3c')

    for name in ['broader','narrower','related','exactMatch','closeMatch']:
        prop=NAMESPACES['skos']+name
        g.edge(prop,'rdfs:domain',NAMESPACES['skos']+'Concept')
        g.edge(prop,'rdfs:range',NAMESPACES['skos']+'Concept')
    for name,zh in [('dataset','数据集'),('dataPlatform','数据平台'),('dataProduct','数据产品'),
                    ('dataJob','数据作业'),('dataFlow','管道'),('dataProcess','处理过程'),
                    ('SchemaField','模式字段'),('Corpuser','企业用户'),('CorpGroup','用户组'),('role','角色'),('tag','标签')]:
        g.node(NAMESPACES['zysemg']+name, ['Resource','Concept','Class',name],
               {'name':zh,'prefLabel':zh,'altLabel':[name], 'notation':'Concept.zysemg.SemanticEnrichedMetadataGraph.'+name},'concept')

    for name,zh,alts in [('amount','金额',['交易金额','amount']),('counter','计数',['counter']),('measure','度量',['measure'])]:
        g.node(NAMESPACES['demo']+name,['Concept'],{'name':zh,'prefLabel':zh,'altLabel':alts},'reference')
    g.edge(NAMESPACES['demo']+'amount','skos:broader',NAMESPACES['demo']+'measure')
    return g
