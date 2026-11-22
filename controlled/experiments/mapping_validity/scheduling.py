import copy
import time
import bootstrap
from fabric.graph import Graph
from fabric.common import canonical
from semantic import check_premise,dependency_bindings,dependency_ids,aggregate,uri


class BusinessMaintenance:
    def __init__(self,group,initial):
        if group not in ('G1','G2'):raise ValueError(group)
        self.group=group;self.old=copy.deepcopy(initial)

        t=time.perf_counter()
        self.evidence={p['id']:check_premise(initial,p) for p in initial['premises']}
        self.initialization_seconds=time.perf_counter()-t

    def update(self,new):
        start=time.perf_counter();analysis_start=start
        old_p={p['id']:p for p in self.old['premises']};new_p={p['id']:p for p in new['premises']}
        all_resources=set(self.old['resources'])|set(new['resources'])
        changed={rid for rid in all_resources if self.old['resources'].get(rid)!=new['resources'].get(rid)}
        for m in new['mapping_ids']:
            if self.old['scales'].get(m)!=new['scales'].get(m):changed.add('@transform:'+m)
        changed_defs={pid for pid,p in new_p.items() if old_p.get(pid)!=p}
        impacted=set(new_p) if self.group=='G1' else set()
        graph=None
        if self.group=='G2':
            graph=Graph()

            for state in [self.old,new]:
                for p in state['premises']:
                    pn=uri('demo:premise:'+p['id'])
                    if pn not in graph.nodes:graph.node(pn,['dataJob'],{'query_id':p['id']})
                    for rid in dependency_ids(p,state):
                        rnode=uri('demo:resource:'+rid)
                        if rnode not in graph.nodes:graph.node(rnode,['Resource'])
                        graph.edge(rnode,'demo:feeds',pn)
            impacted=set(graph.impacted_queries([uri('demo:resource:'+r) for r in changed])) & set(new_p)
            impacted.update(changed_defs)

            for pid,p in new_p.items():
                cached=self.evidence.get(pid)
                if cached is None or cached['bindings']!=dependency_bindings(p,new):impacted.add(pid)
        m={'premises_rechecked':0,'mappings_rechecked':len({new_p[p]['mapping_id'] for p in impacted}),
           'evidence_invalidations':0,'evidence_hits':0,'changed_resources':sorted(changed),
           'affected_premises':sorted(impacted),'analysis_seconds':time.perf_counter()-analysis_start,'semantic_check_seconds':0.}
        updated={}
        for pid,p in new_p.items():
            t=time.perf_counter()
            cached=self.evidence.get(pid)
            binding=dependency_bindings(p,new)
            if cached is not None and cached['bindings']!=binding:m['evidence_invalidations']+=1
            if pid in impacted:
                updated[pid]=check_premise(new,p);m['premises_rechecked']+=1
            else:
                if cached is None or cached['bindings']!=binding:raise AssertionError('Stale evidence cannot authorize')
                updated[pid]=copy.deepcopy(cached);m['evidence_hits']+=1
            m['semantic_check_seconds']+=time.perf_counter()-t
        business=aggregate(updated,new)
        m['initialization_seconds']=self.initialization_seconds
        self.evidence=updated;self.old=copy.deepcopy(new)
        m['semantic_total_seconds']=time.perf_counter()-start
        return business,m
