"""
LangGraph orchestration for the follow-up worker.

This module owns the *engine*: a real ``langgraph.graph.StateGraph`` compiled with
a durable ``SqliteSaver`` checkpointer. The node functions live in
:mod:`workflows`.

Three things this engine is responsible for:

* **One shared typed state** (:class:`WorkflowState`) is threaded between nodes.
  Every node returns the keys it wants merged back; LangGraph owns the merge.
* **Conditional edges** drive every branch. A node names its successor in
  ``state['_next']``, or falls back to a per-node default router.
* **A durable checkpoint** on disk makes "pause for a human, then resume — even
  after a restart" real rather than aspirational. ``Django`` mirrors a
  denormalised copy of the checkpoint so the API and trace view can answer "where
  is this run?" with a plain ORM query. LangGraph remains the source of truth for
  *where a run is*.
"""
import copy
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from django.conf import settings
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

__all__ = [
    'END',
    'START',
    'Command',
    'interrupt',
    'WorkflowState',
    'build_graph',
    'thread_config',
    'invoke',
    'resume_with_decision',
    'resume_at_node',
    'checkpoint_backend',
]


class WorkflowState(TypedDict, total=False):
    """The contract every node reads from and writes to.

    ``run_id`` is also the LangGraph ``thread_id``, so a run and its checkpoint
    always line up.
    """

    run_id: str
    mode: str
    customer_id: int
    goal: str
    node_history: List[str]

    context: Dict[str, Any]
    findings: List[str]
    follow_up_id: Optional[int]

    should_follow_up: bool
    decision_reason: str
    discount_pct: int
    urgency: str
    tone: str

    message: str
    product_name: str
    needs_approval: bool
    approval_id: Optional[int]
    approval_status: str
    approval_reason: str

    reply: str
    intent: str
    wants_no_contact: bool

    tools: List[Dict[str, Any]]
    outcome: str
    outcome_summary: str
    errors: List[str]
    failure: Dict[str, Any]
    resume_node: str

    _next: Optional[str]


_lock = threading.RLock()
_saver: Optional[SqliteSaver] = None
_connection: Optional[sqlite3.Connection] = None


def _checkpoint_path() -> Path:
    path = Path(getattr(settings, 'LANGGRAPH_CHECKPOINT_PATH', 'agent_checkpoints.sqlite'))
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    return path


def checkpointer() -> SqliteSaver:
    """A process-wide :class:`SqliteSaver` writing to disk.

    A disk-backed saver — not ``MemorySaver`` — is what makes "resume after a
    restart" real: stop the server mid-approval or mid-outage, start it again,
    retry, and the graph continues from its checkpoint instead of from the top.
    """
    global _saver, _connection
    if _saver is None:
        target = _checkpoint_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        _connection = sqlite3.connect(str(target), check_same_thread=False)
        _saver = SqliteSaver(_connection)
        _saver.setup()
    return _saver


def checkpoint_backend() -> Dict[str, str]:
    return {'type': 'SqliteSaver', 'path': str(_checkpoint_path())}


def _adapt(name, node_fn, on_checkpoint):
    """Wrap a node function so it can run inside a LangGraph.

    1. Hand the node a private copy of the state with stale routing keys removed.
    2. Clear the previous hop's ``failure`` marker. The write (rather than a
       ``pop``) matters: LangGraph merges returned keys, so an absent key would
       leave the old value in the channel. ``handle_failure`` is exempt because it
       is the one consumer.
    3. Record the hop in ``node_history``.
    4. Mirror the resulting checkpoint into Django for the API and trace view.
    """

    def wrapper(state):
        working = copy.deepcopy(dict(state))
        working.pop('_next', None)
        if name != 'handle_failure':
            working['failure'] = {}

        working['node_history'] = list(working.get('node_history') or []) + [name]

        result = dict(node_fn(working) or working)

        if on_checkpoint is not None:
            on_checkpoint(result, result.get('_next'))

        return result

    wrapper.__name__ = f'langgraph_node_{name}'
    return wrapper


def _router_for(name, default_router):
    """Resolve the successor a node asked for, else its default branch."""

    def route(state):
        chosen = state.get('_next')
        if chosen:
            return chosen
        if default_router is not None:
            return default_router(state)
        return END

    return route


def build_graph(*, entry, nodes, default_routers=None, on_checkpoint=None,
                recursion_limit=None):
    """Compile a :class:`~langgraph.graph.StateGraph` with a durable saver.

    ``nodes``          name -> ``fn(state) -> state``
    ``default_routers`` name -> ``fn(state) -> next node``
    ``on_checkpoint``  ``fn(state, next_node)`` after every node
    """
    default_routers = default_routers or {}
    graph = StateGraph(WorkflowState)

    for node_name, node_fn in nodes.items():
        graph.add_node(node_name, _adapt(node_name, node_fn, on_checkpoint))

    graph.add_edge(START, entry)

    path_map = {node_name: node_name for node_name in nodes}
    path_map[END] = END
    for node_name in nodes:
        graph.add_conditional_edges(
            node_name, _router_for(node_name, default_routers.get(node_name)), path_map
        )

    limit = recursion_limit or getattr(settings, 'WORKER_RECURSION_LIMIT', 40)
    return graph.compile(checkpointer=checkpointer()).with_config({'recursion_limit': limit})


def thread_config(run_id) -> Dict[str, Any]:
    """LangGraph address for one run: the run id *is* the thread id."""
    return {'configurable': {'thread_id': str(run_id)}}


def invoke(graph, state, *, run_id):
    """Start a run on a fresh thread."""
    with _lock:
        return graph.invoke(state, thread_config(run_id))


def resume_with_decision(graph, *, run_id, decision, reason='', actor='Owner'):
    """Resume a run that paused on ``interrupt()``.

    LangGraph replays the interrupting node, which returns this payload instead of
    raising, so the graph continues from the approval gate — not from the top. The
    actor travels with the payload so the audit trail records who decided.
    """
    with _lock:
        return graph.invoke(
            Command(resume={'decision': decision, 'reason': reason, 'actor': actor}),
            thread_config(run_id),
        )


def resume_at_node(graph, *, run_id, node):
    """Re-enter a finished run at a specific node, reusing its saved state.

    This is the recovery path: a run that ended in ``handle_failure`` restarts at
    the step that broke rather than repeating the whole run.
    """
    with _lock:
        return graph.invoke(Command(goto=node), thread_config(run_id))
