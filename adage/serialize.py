import networkx as nx
import json
import adage.adageobject

class DefaultAdageEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, adage.adageobject):
            return obj_to_json(obj)
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)

def noop_taskserializer(task):
    return 'unserializable_task'

def noop_proxyserializer(proxy):
    return 'unserializable_proxy'

def noop_ruleserializer(rule):
    return 'unserializable_rule'

def obj_to_json(adageobj, ruleserializer = noop_ruleserializer, taskserializer = noop_taskserializer, proxyserializer = noop_proxyserializer):
    dag, rules, applied = adageobj.dag, adageobj.rules, adageobj.applied_rules
    data = {'dag':None, 'rules':None, 'applied':None}
    
    data['dag'] = {'nodes':[], 'edges': []}
    for node in nx.topological_sort(dag):
        nodeobj = dag.getNode(node)
        data['dag']['nodes']+=[node_to_json(nodeobj,taskserializer,proxyserializer)]
    
    data['dag']['edges'] += dag.edges()

    data['rules'] = []
    for rule in rules:
        data['rules'] += [ruleserializer(rule)]

    data['applied'] = []
    for rule in applied:
        data['applied'] += [ruleserializer(rule)]
        
    return data


def node_to_json(nodeobj,taskserializer, proxyserializer):
    nodeinfo = {
        'id':nodeobj.identifier,
        'name':nodeobj.name,
        'task':taskserializer(nodeobj.task),
        'timestamps':{
            'defined': nodeobj.define_time,
            'submit': nodeobj.submit_time,
            'ready by': nodeobj.ready_by_time
        },
        'state':str(nodeobj.state),
        'proxy':proxyserializer(nodeobj.resultproxy)
    }
    return nodeinfo