"""
The worker's nodes, and the graph they form.

One worker, one goal. Ten nodes, and every branch is either a deterministic rule
or a tool call the worker is allowed to make.

    load_context ─┬─▶ handle_response ─────────────────────────▶ finalize
                  └─▶ decide ─┬─▶ request_approval ─┬─▶ draft_message
                              │   (human interrupt) │
                              └─▶ draft_message     └─▶ finalize (rejected)
                                        │
                                        ▼
                          send_followup → create_reorder → verify_outcome → finalize
                                        │
                                        └────────────▶ handle_failure

Notes on two decisions that are easy to get wrong:

* **Everything before ``interrupt()`` must be idempotent.** LangGraph re-executes
  the interrupting node from the top when the run resumes, so the approval row is
  *reused* rather than created a second time.
* **``CONVERTED`` is never set here.** A follow-up is sent, not converted. Only a
  real ``Purchase`` arriving afterwards can mark it converted, which keeps the
  success metric honest instead of optimistically self-reported.
"""
import logging
import time

from django.conf import settings
from django.utils import timezone

from .. import rules
from ..models import Approval, Customer, FollowUp, Interaction, RunStep, WorkerRun, business_goal
from . import reasoner, tools
from .graph import (
    END,
    build_graph,
    interrupt,
    invoke as langgraph_invoke,
    resume_at_node,
    resume_with_decision,
)

logger = logging.getLogger(__name__)

FAILURE_RESUME_NODE = 'load_context'


def _record(run, node, *, status=RunStep.Status.OK, summary='', tool=None,
            model=None, output=None, error=''):
    """Write one row of evidence. Every node does exactly this once."""
    step = RunStep.objects.create(
        run=run,
        index=run.steps.count(),
        node=node,
        status=status,
        summary=summary[:400],
        output=output or {},
        error=(error or '')[:400],
    )
    if tool is not None:
        step.tool_name = tool.name
        step.tool_status = tool.status
        step.tool_attempts = tool.attempts
        step.tool_authorized = tool.authorized
        step.tool_error = tool.error[:300]
        step.tool_result = tool.result
    if model is not None:
        step.model_used = model.model
        step.strategy = model.strategy
        step.prompt_tokens = model.prompt_tokens
        step.completion_tokens = model.completion_tokens
        step.total_tokens = model.total_tokens
        step.estimated_cost = model.estimated_cost
        step.latency_ms = model.latency_ms
        if model.error:
            step.error = model.error[:400]
    step.save()
    return step


def _remember_tool(state, outcome):
    state.setdefault('tools', [])
    state['tools'] = list(state['tools']) + [
        {
            'name': outcome.name,
            'status': outcome.status,
            'attempts': outcome.attempts,
            'authorized': outcome.authorized,
            'error': outcome.error,
            'result': outcome.result,
        }
    ]


def _fail(state, node, message):
    state.setdefault('errors', [])
    state['errors'] = list(state['errors']) + [message]
    state['failure'] = {'node': node, 'message': message}
    state['resume_node'] = node
    state['_next'] = 'handle_failure'
    return state


def node_load_context(state):
    """Gather everything the rest of the run needs, then decide which branch.

    Retrieval is a tool call, not a model question: the worker does not "ask the
    model what it remembers". Memory comes from a scoped query, and the evidence
    handed to the model later is assembled from these facts.
    """
    run = WorkerRun.objects.get(run_id=state['run_id'])
    customer = Customer.objects.get(pk=state['customer_id'])

    outcome = tools.call_tool(tools.TOOL_RETRIEVAL, customer=customer)
    _remember_tool(state, outcome)

    if not outcome.ok:
        _record(
            run, 'load_context', status=RunStep.Status.FAILED,
            summary=f'Could not read the customer record: {outcome.error}',
            tool=outcome, error=outcome.error,
        )
        return _fail(state, 'load_context', outcome.error)

    context = outcome.result
    state['context'] = context
    state['findings'] = [
        f"Inactive {context['customer']['days_since_last_order']} days with "
        f"{context['order_count']} previous order(s), lifetime value "
        f"{context['lifetime_value']}.",
        f"Preferred channel: {context['customer']['preferred_channel']}; "
        f"language: {context['customer']['language']}.",
        f"Suggested product to offer back: {context.get('suggested_product') or 'none on record'}.",
    ]
    if context.get('memory'):
        state['findings'].append(
            f"Recalled {len(context['memory'])} memory note(s) from earlier runs."
        )

    mode = state.get('mode') or WorkerRun.Mode.FOLLOW_UP
    eligible = context['eligibility']

    if mode == WorkerRun.Mode.RESPONSE:
        follow_up = (
            customer.follow_ups.filter(status__in=[FollowUp.Status.SENT, FollowUp.Status.PENDING])
            .order_by('-created_at')
            .first()
        )
        state['follow_up_id'] = follow_up.id if follow_up else None
        _record(
            run, 'load_context',
            summary='Loaded the customer record and the open follow-up to interpret the reply.',
            tool=outcome,
        )
        state['_next'] = 'handle_response'
        return state

    if not eligible['eligible']:
        state['should_follow_up'] = False
        state['decision_reason'] = eligible['reason']
        state['outcome'] = 'NOT_ELIGIBLE'
        state['outcome_summary'] = f'No follow-up sent — {eligible["reason"]}'
        _record(
            run, 'load_context', status=RunStep.Status.SKIPPED,
            summary=f'Not eligible: {eligible["reason"]}',
            tool=outcome, output={'eligibility': eligible},
        )
        state['_next'] = 'finalize'
        return state

    _record(
        run, 'load_context',
        summary=f'Eligible: {eligible["reason"]}',
        tool=outcome,
        output={'eligibility': eligible, 'order_count': context['order_count']},
    )
    state['_next'] = 'decide'
    return state


def node_decide(state):
    """Ask the reasoning tier whether to act, and how large an incentive to propose.

    The model proposes; the policy disposes. Whatever discount the model suggests
    is clamped to the business cap, and whether it needs the owner is decided by
    :mod:`rules`, not by the model.
    """
    run = WorkerRun.objects.get(run_id=state['run_id'])
    customer = Customer.objects.get(pk=state['customer_id'])
    context = state['context']

    analysis_context = {
        **context,
        'days_since_last_order': context['customer']['days_since_last_order'],
        'discount_cap': settings.MAX_PROPOSABLE_DISCOUNT_PCT,
        'autonomous_discount_limit': settings.MAX_AUTONOMOUS_DISCOUNT_PCT,
    }
    result = reasoner.run_task('CUSTOMER_ANALYSIS', {'context': analysis_context})
    output = result.output

    discount = rules.clamp_discount(output.get('discount_pct'))
    needs_approval = rules.discount_needs_approval(discount)
    product_name = context.get('suggested_product') or ''
    reason = str(output.get('reason') or '')[:300]

    state['should_follow_up'] = bool(output.get('should_follow_up', True))
    state['decision_reason'] = reason
    state['discount_pct'] = discount
    state['urgency'] = str(output.get('urgency') or 'normal')
    state['tone'] = str(output.get('tone') or 'warm and personal')
    state['needs_approval'] = needs_approval
    state['product_name'] = product_name

    follow_up, _ = FollowUp.objects.get_or_create(
        run=run,
        defaults={
            'customer': customer,
            'discount_pct': discount,
            'status': FollowUp.Status.PENDING,
            'product_name': product_name,
        },
    )
    state['follow_up_id'] = follow_up.id

    policy_note = (
        f'A {discount}% discount exceeds the autonomous limit of '
        f'{settings.MAX_AUTONOMOUS_DISCOUNT_PCT}%, so the owner must approve it.'
        if needs_approval
        else 'No owner approval needed: the offer is within the autonomous limit.'
    )
    _record(
        run, 'decide',
        summary=f'Proposed a {discount}% incentive. {policy_note}',
        model=result,
        output={
            'decision': output,
            'discount_pct_applied': discount,
            'policy': policy_note,
            'needs_approval': needs_approval,
        },
    )

    if not state['should_follow_up']:
        state['outcome'] = 'NOT_ELIGIBLE'
        state['outcome_summary'] = f'No follow-up sent — {reason}'
        state['_next'] = 'finalize'
        return state

    state['_next'] = 'request_approval' if needs_approval else 'draft_message'
    return state


def node_request_approval(state):
    """Pause for the owner. This is a native LangGraph ``interrupt()``.

    On the first pass the approval is created, the run is marked as awaiting a
    human, and ``interrupt()`` checkpoints and unwinds the run. On resume the whole
    node runs again from the top.

    That replay is why nothing before ``interrupt()`` may depend on this run's own
    writes: LangGraph re-executes the node with the state it had *on entry*, so any
    key this node set before pausing is gone. The approval row is therefore fetched
    with ``get_or_create`` (a database row survives), and the paused step is found
    by querying for it rather than by remembering its id in the state.
    """
    run = WorkerRun.objects.get(run_id=state['run_id'])
    follow_up = FollowUp.objects.get(pk=state['follow_up_id'])

    approval, created = Approval.objects.get_or_create(
        follow_up=follow_up,
        defaults={
            'action_type': 'DISCOUNT',
            'proposed_discount_pct': state['discount_pct'],
            'summary': (
                f'{state["discount_pct"]}% discount for {follow_up.customer.name} '
                f'on {state.get("product_name") or "their usual order"}'
            ),
            'findings': list(state.get('findings') or [])[:6],
        },
    )

    if created:
        follow_up.status = FollowUp.Status.AWAITING_APPROVAL
        follow_up.save(update_fields=['status', 'updated_at'])

        run.status = WorkerRun.Status.AWAITING_APPROVAL
        run.next_node = 'request_approval'
        run.save(update_fields=['status', 'next_node'])

        _record(
            run, 'request_approval', status=RunStep.Status.PAUSED,
            summary=(
                f'Paused for the owner: {state["discount_pct"]}% discount on '
                f'{follow_up.customer.name}. Checkpoint saved; nothing has been sent.'
            ),
            output={'approval_id': approval.id, 'findings': state.get('findings') or []},
        )

    state['approval_id'] = approval.id

    payload = interrupt(
        {
            'approval_id': approval.id,
            'action_type': approval.action_type,
            'proposed_discount_pct': approval.proposed_discount_pct,
            'summary': approval.summary,
            'findings': approval.findings,
            'awaiting': 'owner_decision',
        }
    ) or {}

    decision = str(payload.get('decision') or '').upper()
    reason = str(payload.get('reason') or '')

    approval.status = (
        Approval.Status.APPROVED if decision == 'APPROVED' else Approval.Status.REJECTED
    )
    approval.decision_reason = reason[:300]
    approval.decided_at = timezone.now()
    approval.decided_by = str(payload.get('actor') or 'Owner')[:120]
    approval.save(update_fields=['status', 'decided_by', 'decision_reason', 'decided_at'])

    state['approval_status'] = approval.status
    state['approval_reason'] = reason

    step = (
        RunStep.objects.filter(run=run, node='request_approval', status=RunStep.Status.PAUSED)
        .order_by('index')
        .last()
    )
    if step is not None:
        step.status = RunStep.Status.OK
        step.summary = f'Owner {approval.status.lower()}: {reason or "no reason given"}'[:400]
        step.output = {
            **(step.output or {}),
            'decision': approval.status,
            'decided_by': approval.decided_by,
            'reason': reason,
        }
        step.save(update_fields=['status', 'summary', 'output'])

    if approval.status == Approval.Status.APPROVED:
        run.status = WorkerRun.Status.RUNNING
        run.save(update_fields=['status'])
        state['_next'] = 'draft_message'
        return state

    follow_up.status = FollowUp.Status.REJECTED
    follow_up.outcome_note = f'Owner rejected the discount: {reason}'[:300]
    follow_up.save(update_fields=['status', 'outcome_note', 'updated_at'])

    rules.remember(
        follow_up.customer,
        'offer_rejected',
        f'Owner rejected a {state["discount_pct"]}% discount on {timezone.now():%Y-%m-%d}. '
        f'Do not propose that incentive again without new information.',
    )
    state['outcome'] = 'REJECTED'
    state['outcome_summary'] = (
        f'Owner rejected the {state["discount_pct"]}% discount; no message was sent.'
    )
    state['_next'] = 'finalize'
    return state


def node_draft_message(state):
    """Write the message on the fast tier.

    Deliberately after the approval gate: there is no point paying a model to write
    a message that the owner was going to refuse.
    """
    run = WorkerRun.objects.get(run_id=state['run_id'])
    context = state['context']

    payload = {
        'brief': {
            'customer_name': context['customer']['name'].split()[0],
            'language': context['customer']['language'],
            'product_name': state.get('product_name'),
            'discount_pct': state.get('discount_pct') or 0,
            'tone': state.get('tone'),
            'reason': state.get('decision_reason'),
            'constraints': 'Under 300 characters. No invented facts. One clear call to action.',
        },
        'customer_name': context['customer']['name'].split()[0],
        'language': context['customer']['language'],
        'product_name': state.get('product_name'),
        'discount_pct': state.get('discount_pct') or 0,
    }
    result = reasoner.run_task('MESSAGE_DRAFTING', payload)
    message = str(result.output.get('message') or '').strip()

    if not message:
        return _fail(state, 'draft_message', 'The message drafter returned an empty message.')

    state['message'] = message[:300]
    _record(run, 'draft_message', summary=f'Drafted a {len(message)}-character message.',
            model=result, output={'message': message})
    state['_next'] = 'send_followup'
    return state


def node_send_followup(state):
    """The irreversible step. Guarded inside the tool, not by the caller."""
    run = WorkerRun.objects.get(run_id=state['run_id'])
    customer = Customer.objects.get(pk=state['customer_id'])
    follow_up = FollowUp.objects.get(pk=state['follow_up_id'])

    outcome = tools.call_tool(
        tools.TOOL_SEND, customer=customer, follow_up=follow_up,
        message=state['message'], channel=customer.preferred_channel,
    )
    _remember_tool(state, outcome)

    if not outcome.ok:
        _record(
            run, 'send_followup',
            status=RunStep.Status.DENIED if outcome.status == 'DENIED' else RunStep.Status.FAILED,
            summary=(
                f'Refused to send: {outcome.error}' if outcome.status == 'DENIED'
                else f'Could not send the message: {outcome.error}'
            ),
            tool=outcome, error=outcome.error,
        )
        if outcome.status == 'DENIED':
            state['outcome'] = 'DENIED'
            state['outcome_summary'] = outcome.error
            state['_next'] = 'finalize'
            return state
        return _fail(state, 'send_followup', outcome.error)

    _record(
        run, 'send_followup',
        summary=(
            f'Sent via {outcome.result.get("channel")} '
            f'(message {outcome.result.get("message_id")}).'
        ),
        tool=outcome,
    )
    rules.remember(
        customer,
        'last_outreach',
        f'Sent a follow-up on {timezone.now():%Y-%m-%d} offering '
        f'{state.get("product_name") or "their usual order"}'
        + (f' at {state["discount_pct"]}% off' if state.get('discount_pct') else ' with no discount'),
    )
    state['_next'] = 'create_reorder'
    return state


def node_create_reorder(state):
    """Prepare the one-tap reorder the message points at."""
    run = WorkerRun.objects.get(run_id=state['run_id'])
    customer = Customer.objects.get(pk=state['customer_id'])
    follow_up = FollowUp.objects.get(pk=state['follow_up_id'])

    outcome = tools.call_tool(
        tools.TOOL_REORDER, customer=customer, follow_up=follow_up,
        product_name=state.get('product_name') or '',
        discount_pct=state.get('discount_pct') or 0,
    )
    _remember_tool(state, outcome)

    if not outcome.ok:
        _record(
            run, 'create_reorder',
            status=RunStep.Status.DENIED if outcome.status == 'DENIED' else RunStep.Status.FAILED,
            summary=f'Could not prepare the reorder: {outcome.error}',
            tool=outcome, error=outcome.error,
        )
        if outcome.status == 'DENIED':
            state['outcome'] = 'DENIED'
            state['outcome_summary'] = outcome.error
            state['_next'] = 'finalize'
            return state
        return _fail(state, 'create_reorder', outcome.error)

    _record(
        run, 'create_reorder',
        summary=f'Prepared reorder request {outcome.result.get("reorder_request_id")}.',
        tool=outcome,
    )
    state['_next'] = 'verify_outcome'
    return state


def node_verify_outcome(state):
    """Check what actually happened, from the database rather than from the plan.

    This is the Definition of Done made executable. A run that "believes" it sent a
    message but recorded no interaction, or prepared no reorder, is not done.
    """
    run = WorkerRun.objects.get(run_id=state['run_id'])
    customer = Customer.objects.get(pk=state['customer_id'])
    follow_up = FollowUp.objects.get(pk=state['follow_up_id'])

    checks = {
        'follow_up_sent': follow_up.sent_at is not None,
        'message_recorded': Interaction.objects.filter(
            customer=customer,
            direction=Interaction.Direction.OUTBOUND,
            kind=Interaction.Kind.FOLLOW_UP,
        ).exists(),
        'reorder_prepared': Interaction.objects.filter(
            customer=customer,
            direction=Interaction.Direction.OUTBOUND,
            kind=Interaction.Kind.REORDER,
        ).exists(),
    }
    passed = all(checks.values())

    _record(
        run, 'verify_outcome',
        status=RunStep.Status.OK if passed else RunStep.Status.FAILED,
        summary=(
            'All checks passed: the message was recorded and the reorder is prepared.'
            if passed
            else f'Verification failed: {checks}'
        ),
        output={'checks': checks, 'passed': passed},
    )

    if not passed:
        return _fail(state, 'verify_outcome', f'Post-action verification failed: {checks}')

    state['outcome'] = 'FOLLOW_UP_SENT'
    state['outcome_summary'] = (
        f'Follow-up sent to {customer.name}'
        + (f' with a {state["discount_pct"]}% discount' if state.get('discount_pct') else '')
        + f' offering {follow_up.product_name or "their usual order"}. '
        'Awaiting the customer\'s response.'
    )
    state['_next'] = 'finalize'
    return state


def node_handle_response(state):
    """Interpret a customer reply and update what the worker knows.

    A rejection is a *successful* run here: the worker did the right thing by
    recording it, backing off and remembering not to repeat the offer.
    """
    run = WorkerRun.objects.get(run_id=state['run_id'])
    customer = Customer.objects.get(pk=state['customer_id'])

    result = reasoner.run_task(
        'REPLY_CLASSIFICATION', {'reply': state.get('reply') or ''}
    )
    intent = str(result.output.get('intent') or 'QUESTION').upper()
    wants_no_contact = bool(result.output.get('wants_no_contact'))
    summary = str(result.output.get('summary') or state.get('reply') or '')

    rules.log_interaction(
        customer, Interaction.Direction.INBOUND, Interaction.Kind.REPLY,
        state.get('reply') or '', channel=customer.preferred_channel,
    )
    state['intent'] = intent

    follow_up = FollowUp.objects.filter(pk=state.get('follow_up_id')).first()
    if follow_up is not None:
        follow_up.responded_at = timezone.now()

    if intent == 'NEGATIVE':
        if follow_up is not None:
            follow_up.status = FollowUp.Status.DECLINED
            follow_up.outcome_note = summary[:300]
        rules.remember(
            customer, 'declined_offer',
            f'Declined a follow-up on {timezone.now():%Y-%m-%d}: "{summary[:120]}". '
            f'Do not repeat this offer.',
        )
        state['outcome'] = 'CUSTOMER_DECLINED'
        state['outcome_summary'] = f'Customer declined: {summary[:200]}'
    elif intent == 'POSITIVE':
        if follow_up is not None:
            follow_up.status = FollowUp.Status.SENT
        rules.remember(
            customer, 'positive_signal',
            f'Responded positively on {timezone.now():%Y-%m-%d}: "{summary[:120]}".',
        )
        state['outcome'] = 'CUSTOMER_POSITIVE'
        state['outcome_summary'] = f'Customer is interested: {summary[:200]}'
    else:
        state['outcome'] = 'CUSTOMER_QUESTION'
        state['outcome_summary'] = f'Customer asked something: {summary[:200]}'

    if wants_no_contact:
        customer.status = Customer.Status.OPTED_OUT
        customer.marketing_consent = False
        customer.save(update_fields=['status', 'marketing_consent'])
        rules.remember(
            customer, 'do_not_contact',
            'Customer asked not to be contacted again. Outreach is now blocked at policy level.',
        )
        state['outcome_summary'] += ' Customer asked not to be contacted; outreach is now blocked.'

    if follow_up is not None:
        follow_up.save(update_fields=['status', 'responded_at', 'outcome_note', 'updated_at'])

    _record(
        run, 'handle_response',
        summary=f'Classified the reply as {intent}. {summary[:200]}',
        model=result,
        output={'intent': intent, 'wants_no_contact': wants_no_contact, 'summary': summary},
    )
    state['_next'] = 'finalize'
    return state


def node_handle_failure(state):
    """Stop honestly and leave instructions for a resume.

    The run does not fabricate a success and does not retry forever. It records the
    step it broke on, so a retry re-enters there instead of repeating the run.
    """
    run = WorkerRun.objects.get(run_id=state['run_id'])
    failure = state.get('failure') or {}
    node = failure.get('node') or state.get('resume_node') or FAILURE_RESUME_NODE

    run.resume_node = node
    run.status = WorkerRun.Status.FAILED
    run.next_node = ''
    run.outcome_summary = (
        f'Failed at {node}: {failure.get("message", "unknown error")}. '
        f'Checkpoint saved — a retry resumes from this step instead of repeating the run.'
    )[:400]
    run.finished_at = timezone.now()
    run.save(update_fields=['resume_node', 'status', 'next_node', 'outcome_summary', 'finished_at'])

    _record(
        run, 'handle_failure', status=RunStep.Status.FAILED,
        summary=run.outcome_summary,
        output={'failed_node': node, 'errors': state.get('errors') or []},
    )
    state['outcome'] = 'FAILED'
    state['outcome_summary'] = run.outcome_summary
    state['_next'] = END
    return state


def node_finalize(state):
    """Close the run: record the outcome and the honest autonomy flag."""
    run = WorkerRun.objects.get(run_id=state['run_id'])
    customer = run.customer

    outcome = state.get('outcome') or 'COMPLETED'
    if outcome in ('FAILED',):
        run.status = WorkerRun.Status.FAILED
    elif outcome == 'REJECTED':
        run.status = WorkerRun.Status.REJECTED
    else:
        run.status = WorkerRun.Status.COMPLETED

    run.resolved_autonomously = (
        run.status == WorkerRun.Status.COMPLETED and not state.get('approval_id')
    )
    run.outcome_summary = str(state.get('outcome_summary') or '')[:400]
    run.next_node = ''
    run.resume_node = ''
    run.state_data = _slim(state)
    run.finished_at = timezone.now()
    run.duration_ms = int((run.finished_at - run.created_at).total_seconds() * 1000)
    run.save(
        update_fields=[
            'status', 'resolved_autonomously', 'outcome_summary', 'next_node',
            'resume_node', 'state_data', 'finished_at', 'duration_ms',
        ]
    )

    if run.status == WorkerRun.Status.COMPLETED and outcome == 'FOLLOW_UP_SENT' and customer:
        rules.remember(
            customer, 'last_run',
            f'Win-back attempt completed on {timezone.now():%Y-%m-%d}.',
        )

    _record(
        run, 'finalize',
        status=RunStep.Status.OK if run.status != WorkerRun.Status.FAILED else RunStep.Status.FAILED,
        summary=f'{run.status}: {run.outcome_summary}',
        output={
            'status': run.status,
            'resolved_autonomously': run.resolved_autonomously,
            'outcome': outcome,
            'errors': state.get('errors') or [],
        },
    )
    state['_next'] = END
    return state


NODES = {
    'load_context': node_load_context,
    'decide': node_decide,
    'request_approval': node_request_approval,
    'draft_message': node_draft_message,
    'send_followup': node_send_followup,
    'create_reorder': node_create_reorder,
    'verify_outcome': node_verify_outcome,
    'handle_response': node_handle_response,
    'handle_failure': node_handle_failure,
    'finalize': node_finalize,
}

_DEFAULT_ROUTERS = {
    'request_approval': lambda state: 'finalize',
}


def _slim(state):
    """The part of the state worth persisting on the Django projection."""
    keep = (
        'mode', 'goal', 'should_follow_up', 'decision_reason', 'discount_pct',
        'urgency', 'tone', 'needs_approval', 'approval_status', 'approval_reason',
        'intent', 'outcome', 'outcome_summary', 'errors', 'findings',
    )
    slim = {key: state.get(key) for key in keep if state.get(key) not in (None, '', [], {})}
    context = state.get('context') or {}
    if context:
        slim['customer'] = context.get('customer')
        slim['eligibility'] = context.get('eligibility')
        slim['memory'] = context.get('memory')
    if state.get('message'):
        slim['message'] = state['message']
    return slim


def _project_checkpoint(state, next_node):
    """Mirror the LangGraph checkpoint into the Django row the API reads.

    LangGraph stays the source of truth for *where a run is*; this is a
    denormalised read model so the console can answer with a plain ORM query.
    """
    run = WorkerRun.objects.filter(run_id=state.get('run_id')).first()
    if run is None:
        return
    run.next_node = '' if next_node in (None, END) else str(next_node)
    run.state_data = _slim(state)
    if state.get('resume_node'):
        run.resume_node = state['resume_node']
    run.save(update_fields=['next_node', 'state_data', 'resume_node'])


def _build_graph():
    return build_graph(
        entry='load_context',
        nodes=NODES,
        default_routers=_DEFAULT_ROUTERS,
        on_checkpoint=_project_checkpoint,
    )


GRAPH = _build_graph()


def _drive(run, state, *, start=None, decision=None, reason='', actor='Owner'):
    """Advance the run and keep the Django bookkeeping in step.

    Three modes, one thread per run:

    * fresh run — ``invoke`` seeds the state on a new thread;
    * paused for a human — ``Command(resume=...)`` wakes the ``interrupt()``;
    * failed run retried — ``Command(goto=...)`` re-enters at the broken step.
    """
    try:
        if decision is not None:
            resume_with_decision(
                GRAPH, run_id=run.run_id, decision=decision,
                reason=reason, actor=actor,
            )
        elif start:
            run.resume_count += 1
            run.save(update_fields=['resume_count'])
            resume_at_node(GRAPH, run_id=run.run_id, node=start)
        else:
            langgraph_invoke(GRAPH, state, run_id=run.run_id)
    except Exception as exc:  # noqa: BLE001 — a run must never take down the API
        logger.exception('Worker run crashed for %s', run.run_id)
        run.refresh_from_db()
        run.status = WorkerRun.Status.FAILED
        run.outcome_summary = f'Run crashed: {type(exc).__name__}: {exc}'[:400]
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'outcome_summary', 'finished_at'])

    run.refresh_from_db()
    if run.status in (
        WorkerRun.Status.COMPLETED, WorkerRun.Status.FAILED, WorkerRun.Status.REJECTED
    ) and run.finished_at is None:
        run.finished_at = timezone.now()
        run.duration_ms = int((run.finished_at - run.created_at).total_seconds() * 1000)
        run.save(update_fields=['finished_at', 'duration_ms'])
    return run


def start_follow_up_run(customer, actor='Owner'):
    """Kick off a win-back attempt for one customer."""
    run = WorkerRun.objects.create(
        mode=WorkerRun.Mode.FOLLOW_UP,
        customer=customer,
        goal=business_goal(),
        status=WorkerRun.Status.RUNNING,
    )
    state = {
        'run_id': str(run.run_id),
        'mode': WorkerRun.Mode.FOLLOW_UP,
        'customer_id': customer.id,
        'goal': run.goal,
        'node_history': [],
        'errors': [],
        'tools': [],
    }
    return _drive(run, state, actor=actor)


def start_response_run(customer, reply, actor='Owner'):
    """Handle an inbound reply from a customer."""
    run = WorkerRun.objects.create(
        mode=WorkerRun.Mode.RESPONSE,
        customer=customer,
        goal=f'Interpret {customer.name}\'s reply and update what we know about them.',
        status=WorkerRun.Status.RUNNING,
    )
    state = {
        'run_id': str(run.run_id),
        'mode': WorkerRun.Mode.RESPONSE,
        'customer_id': customer.id,
        'goal': run.goal,
        'reply': reply,
        'node_history': [],
        'errors': [],
        'tools': [],
    }
    return _drive(run, state, actor=actor)


def decide_run(run, decision, reason='', actor='Owner'):
    """Record a human decision and resume the paused run."""
    run.status = WorkerRun.Status.RUNNING if decision == 'APPROVED' else run.status
    run.save(update_fields=['status'])
    return _drive(run, None, decision=decision, reason=reason, actor=actor)


def retry_run(run, actor='Owner'):
    """Re-enter a failed run at the step that broke."""
    node = run.resume_node or run.next_node
    if not node:
        raise ValueError('This run has no recorded resume point to retry from.')
    return _drive(run, None, start=node, actor=actor)
