import networkx as nx
import logging
import os
import dagstate
import time
from adage import trackers
from adage import nodestate
from adage.decorators import adageop, adagetask, Rule
from adage.adageobject import adageobject

#silence pyflakes
assert adageop
assert adagetask
assert Rule
assert adageobject

__all__ = ['decorators','trackers']

log = logging.getLogger(__name__)

def validate_finished_dag(dag):
    for node in dag:
        nodeobj = dag.getNode(node)
        if nodeobj.submit_time:
            sanity = all([nodeobj.submit_time > dag.getNode(x).ready_by_time for x in dag.predecessors(node)])
            if not sanity:
                return False
    return True

def nodes_left_or_rule_applicable(adageobj):
    dag,rules = adageobj.dag, adageobj.rules
    nodes_we_could_run = [node for node in dag.nodes() if not dagstate.upstream_failure(dag,dag.getNode(node))]
    nodes_running_or_defined = [x for x in nodes_we_could_run if dagstate.node_defined_or_waiting(dag.getNode(x))]

    if any(rule.applicable(adageobj) for rule in rules):
        return True

    log.debug('nodes we could run: %s',nodes_we_could_run)
    if nodes_running_or_defined:
        log.debug('%s nodes that could be run or are running are left.',len(nodes_running_or_defined))
        log.debug('nodes are: %s',[dag.node[n] for n in nodes_running_or_defined])
        return True
    else:
        log.info('no nodes can be run anymore')
        return False

def update_coroutine(adageobj):
    for i,rule in reversed([x for x in enumerate(adageobj.rules)]):
        if rule.applicable(adageobj):
            do_apply = yield rule
            if do_apply:
                log.info('extending graph.')
                rule.apply(adageobj)
                adageobj.applied_rules.append(adageobj.rules.pop(i))
            yield
        else:
            log.debug('rule not ready yet')

def update_dag(adageobj,decider):
    #iterate rules in reverse so we can safely pop items
    anyapplied = False
    update_loop = update_coroutine(adageobj)
    for possible_rule in update_loop:
        log.info('we could update this with rule: %s',possible_rule)
        command = decider.send((possible_rule,adageobj))
        if command:
            log.debug('we are in fact updating this..')
            anyapplied = True
        update_loop.send(command)
    #we changed the state so let's just recurse
    if anyapplied:
        log.info('we applied a change, so we will recurse to see if we can apply anything else give updated state')
        update_dag(adageobj,decider)

def submit_node(nodeobj,backend):
    nodeobj.resultproxy = backend.submit(nodeobj.task)
    nodeobj.submit_time = time.time()
    if not nodeobj.backend:
        nodeobj.backend = backend

def process_dag(backend,adageobj,decider):
    dag = adageobj.dag
    for node in nx.topological_sort(dag):
        nodeobj = dag.getNode(node)
        log.debug("working on node: %s with obj %s",node,nodeobj)
        if nodeobj.submit_time:
            log.debug("node already submitted. continue")
            continue;
        if dagstate.upstream_ok(dag,nodeobj):
            log.info('Node %s upstream is OK and has not been submitted yet. deciding whether to submit',nodeobj)
            do_submit = decider.send((dag,nodeobj))
            if do_submit:
                log.info('submitting %s job',nodeobj)
                submit_node(nodeobj,backend)
        if dagstate.upstream_failure(dag,nodeobj):
            log.debug('not submitting node: %s due to upstream failure',node)

def update_state(adageobj):
    for node in adageobj.dag.nodes():
        #check node status one last time so we pick up the finishing times
        dagstate.node_status(adageobj.dag.getNode(node))

def trackprogress(trackerlist,adageobj):
    map(lambda t: t.track(adageobj), trackerlist)

def adage_coroutine(backend,extend_decider,submit_decider):
    """the main loop as a coroutine, for sequential stepping"""
    # after priming the coroutin, we yield right away until we get send a state object
    state = yield

    # after receiving the state object, we yield and will start the loop once we regain controls
    yield

    #starting the loop
    while nodes_left_or_rule_applicable(state):
        update_dag(state,extend_decider)
        process_dag(backend,state,submit_decider)
        update_state(state)
        #we're done for this tick, let others proceed
        yield state

def yes_man():
    # we yield until we receive some data via send()
    data = yield
    while True:
        rule, state = data
        log.info('received rule: %s state: %s',rule,state)
        #we yield True and wait again to receive some data
        value = True
        data = yield value

def rundag(adageobj,
           track = False,
           backend = None,
           extend_decider = None,
           submit_decider = None,
           loggername = None,
           workdir = None,
           trackevery = 1,
           update_interval = 0.01,
           additional_trackers = None
           ):

    if loggername:
        global log
        log = logging.getLogger(loggername)

    if not workdir:
        workdir = os.getcwd()

    ## funny behavior of multiprocessing Pools means that
    ## we can not have backendsubmit = multiprocsetup(2)    in the function sig
    ## so we only initialize them here
    if not backend:
        from backends import MultiProcBackend
        backend = MultiProcBackend(2)
    if not extend_decider:
        extend_decider = yes_man()
        extend_decider.next() #prime it..

    if not submit_decider:
        submit_decider = yes_man()
        submit_decider.next() #prime it..

    trackerlist = [trackers.SimpleReportTracker(log,trackevery)]
    if track:
        trackerlist += [trackers.GifTracker(gifname = '{}/workflow.gif'.format(workdir), workdir = '{}/track'.format(workdir))]
        trackerlist += [trackers.TextSnapShotTracker(logfilename = '{}/adagesnap.txt'.format(workdir), mindelta = trackevery)]
        # trackerlist += [trackers.JSONDumpTracker(dumpname = '{}/adage.json'.format(workdir))]

    if additional_trackers:
        trackerlist += additional_trackers

    map(lambda t: t.initialize(adageobj), trackerlist)

    log.info('preparing adage coroutine.')
    coroutine = adage_coroutine(backend,extend_decider,submit_decider)
    coroutine.next() #prime the coroutine....
    coroutine.send(adageobj)


    log.info('starting state loop.')

    try:
        for state in coroutine:
            trackprogress(trackerlist,state)
            time.sleep(update_interval)
    except:
        log.exception('some weird exception caught in adage process loop')
        raise

    if adageobj.rules:
        log.warning('some rules were not applied.')

    log.info('all running jobs are finished.')
    log.info('track last time')

    map(lambda t: t.finalize(adageobj), trackerlist)

    log.info('validating execution')

    if not validate_finished_dag(adageobj.dag):
        log.error('DAG execution not validating')
        raise RuntimeError('DAG execution not validating')
    log.info('execution valid. (in terms of execution order)')

    if any(adageobj.dag.getNode(x).state == nodestate.FAILED for x in adageobj.dag.nodes()):
        log.error('raising RunTimeError due to failed jobs')
        raise RuntimeError('DAG execution failed')
