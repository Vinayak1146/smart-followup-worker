"""HTTP surface for the worker console.

Thin on purpose. Every endpoint does three things: read a row, call one entry
point from :mod:`core.agent.workflows`, and hand back a read model. There is no
business logic here — if a rule matters, it lives in :mod:`core.rules`, and if a
permission matters, it is enforced inside the tool.

The worker itself is the subject of this prototype, so the API is unauthenticated
and there is no user model. ``X-Owner-Name`` is used purely as a label on a human
decision so the trace can say *who* approved a discount.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import exception_handler

from . import seed as seed_module
from . import serializers as ser
from .agent import reasoner, tools, workflows
from .models import (
    Approval,
    Customer,
    FollowUp,
    Purchase,
    SimulationControl,
    WorkerRun,
    business_goal,
    policy_snapshot,
)


def plain_error_handler(exc, context):
    """Return a JSON body for every failure.

    The default handler re-raises unhandled exceptions, which Django renders as
    an HTML error page. A JSON-only API should never answer with HTML, so this
    converts anything unexpected into a 500 with a message the console can show.
    """
    response = exception_handler(exc, context)
    if response is None:
        return Response(
            {'error': f'{type(exc).__name__}: {exc}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    if isinstance(response.data, dict) and 'detail' in response.data:
        response.data = {'error': str(response.data['detail'])}
    elif not isinstance(response.data, dict):
        response.data = {'error': response.data}
    return response


def _actor(request):
    """Who is making this call. A label for the audit trail, not authentication."""
    return (request.headers.get('X-Owner-Name') or 'Owner').strip()[:120] or 'Owner'


def _body(request):
    if isinstance(request.data, dict):
        return request.data
    return {}


@api_view(['GET'])
def meta(request):
    """What this worker is, what it may do, and how it is configured.

    Exposed rather than documented so the console can show the real policy
    instead of a description of the policy that could drift from it.
    """
    from .agent import graph

    return Response(
        {
            'goal': business_goal(),
            'policy': policy_snapshot(),
            'tools': tools.describe_tools(),
            'routing': reasoner.describe_routing(),
            'harness': {
                'checkpointer': graph.checkpoint_backend(),
                'recursion_limit': settings.WORKER_RECURSION_LIMIT,
                'tool_max_attempts': settings.TOOL_MAX_ATTEMPTS,
            },
            'simulation': {
                'messaging_failures_remaining': SimulationControl.current().messaging_failures_remaining,
                'messaging_latency_ms': SimulationControl.current().messaging_latency_ms,
            },
        }
    )


@api_view(['GET'])
def dashboard(request):
    return Response(ser.dashboard())


@api_view(['GET'])
def customer_list(request):
    only_eligible = request.query_params.get('eligible') == '1'
    customers = Customer.objects.all()
    rows = [ser.customer_summary(c, include_eligibility=True) for c in customers]
    eligible_count = 0
    for row in rows:
        if row['eligibility']['eligible']:
            eligible_count += 1
    if only_eligible:
        rows = [row for row in rows if row['eligibility']['eligible']]

    return Response(
        {
            'count': len(rows),
            'eligible': eligible_count,
            'results': rows,
        }
    )


@api_view(['GET'])
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    return Response(
        {
            **ser.customer_summary(customer, include_eligibility=True),
            'purchases': [
                ser.purchase_row(p) for p in customer.purchases.order_by('-purchased_at')[:20]
            ],
            'interactions': [
                ser.interaction_row(i) for i in customer.interactions.order_by('-created_at')[:30]
            ],
            'memories': [ser.memory_row(m) for m in customer.memories.order_by('-created_at')[:20]],
            'follow_ups': [ser.follow_up_row(f) for f in customer.follow_ups.order_by('-created_at')[:20]],
            'runs': [ser.run_summary(r) for r in customer.runs.order_by('-created_at')[:20]],
        }
    )


@api_view(['POST'])
def start_follow_up(request, pk):
    """Start one win-back attempt for this customer."""
    customer = get_object_or_404(Customer, pk=pk)
    run = workflows.start_follow_up_run(customer, actor=_actor(request))
    return Response(ser.run_detail(run), status=status.HTTP_201_CREATED)


@api_view(['POST'])
def customer_reply(request, pk):
    """Simulate an inbound reply and let the worker interpret it."""
    customer = get_object_or_404(Customer, pk=pk)
    body = _body(request)
    reply = str(body.get('reply') or '').strip()
    if not reply:
        return Response({'error': 'A reply is required.'}, status=status.HTTP_400_BAD_REQUEST)
    run = workflows.start_response_run(customer, reply, actor=_actor(request))
    return Response(ser.run_detail(run), status=status.HTTP_201_CREATED)


@api_view(['POST'])
def record_purchase(request, pk):
    """Record a real order, and let it — not the worker — mark a follow-up
    converted.

    This exists so the success metric can be demonstrated honestly: the worker
    cannot award itself a conversion. Only a ``Purchase`` row can.
    """
    customer = get_object_or_404(Customer, pk=pk)
    body = _body(request)

    product_name = str(body.get('product_name') or '').strip()
    if not product_name:
        return Response({'error': 'product_name is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        amount = float(body.get('amount') or 0)
        quantity = int(body.get('quantity') or 1)
    except (TypeError, ValueError):
        return Response({'error': 'amount and quantity must be numeric.'}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        purchase = Purchase.objects.create(
            customer=customer,
            product_name=product_name,
            category=str(body.get('category') or ''),
            quantity=max(1, quantity),
            amount=amount,
            purchased_at=timezone.now(),
        )
        customer.recompute_last_order()

        window_start = timezone.now() - timedelta(days=30)
        converted = (
            customer.follow_ups.filter(
                status=FollowUp.Status.SENT, sent_at__gte=window_start
            )
            .order_by('-sent_at')
            .first()
        )
        if converted is not None:
            converted.status = FollowUp.Status.CONVERTED
            converted.outcome_note = f'Converted: {product_name} ordered {timezone.now():%Y-%m-%d}.'
            converted.save(update_fields=['status', 'outcome_note', 'updated_at'])
            customer.status = Customer.Status.REACTIVATED
            customer.save(update_fields=['status'])

    return Response(
        {
            'purchase': ser.purchase_row(purchase),
            'converted_follow_up_id': converted.id if converted is not None else None,
            'customer': ser.customer_summary(customer),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET'])
def run_list(request):
    runs = WorkerRun.objects.all()
    status_filter = request.query_params.get('status')
    if status_filter:
        runs = runs.filter(status=status_filter)
    mode = request.query_params.get('mode')
    if mode:
        runs = runs.filter(mode=mode)
    limit = min(int(request.query_params.get('limit') or 50), 200)
    results = [ser.run_summary(r) for r in runs[:limit]]
    return Response({'count': len(results), 'results': results})


@api_view(['GET'])
def run_detail(request, run_id):
    run = get_object_or_404(WorkerRun, run_id=run_id)
    return Response(ser.run_detail(run))


@api_view(['POST'])
def decide_run(request, run_id):
    """Approve or reject the discount a paused run is waiting on."""
    run = get_object_or_404(WorkerRun, run_id=run_id)
    body = _body(request)
    decision = str(body.get('decision') or '').upper()
    if decision not in ('APPROVED', 'REJECTED'):
        return Response(
            {'error': 'decision must be APPROVED or REJECTED.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if run.status != WorkerRun.Status.AWAITING_APPROVAL:
        return Response(
            {'error': f'This run is {run.status}, so there is nothing to decide.'},
            status=status.HTTP_409_CONFLICT,
        )

    run = workflows.decide_run(
        run, decision, reason=str(body.get('reason') or ''), actor=_actor(request)
    )
    return Response(ser.run_detail(run))


@api_view(['POST'])
def retry_run(request, run_id):
    """Resume a failed run from the step that broke, reusing its checkpoint.

    This is the recovery path, and it works from a *different process*: the
    checkpoint lives in SQLite on disk, not in memory, so restarting the server
    does not lose a paused or failed run.
    """
    run = get_object_or_404(WorkerRun, run_id=run_id)
    if run.status != WorkerRun.Status.FAILED:
        return Response(
            {'error': f'This run is {run.status}; only a failed run can be retried.'},
            status=status.HTTP_409_CONFLICT,
        )
    try:
        run = workflows.retry_run(run, actor=_actor(request))
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
    return Response(ser.run_detail(run))


@api_view(['GET'])
def approval_list(request):
    approvals = Approval.objects.select_related('follow_up__customer', 'follow_up__run')
    status_filter = request.query_params.get('status')
    if status_filter:
        approvals = approvals.filter(status=status_filter.upper())
    results = [ser.approval_row(a) for a in approvals[:100]]
    return Response({'count': len(results), 'results': results})


@api_view(['POST'])
def decide_approval(request, pk):
    """Decide by approval id — the same action as ``decide_run``, for the queue view."""
    approval = get_object_or_404(Approval, pk=pk)
    body = _body(request)
    decision = str(body.get('decision') or '').upper()
    if decision not in ('APPROVED', 'REJECTED'):
        return Response(
            {'error': 'decision must be APPROVED or REJECTED.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    run = approval.follow_up.run
    if run is None:
        return Response(
            {'error': 'This approval is not attached to a run.'},
            status=status.HTTP_409_CONFLICT,
        )
    if run.status != WorkerRun.Status.AWAITING_APPROVAL:
        return Response(
            {'error': f'The run behind this approval is {run.status}.'},
            status=status.HTTP_409_CONFLICT,
        )
    run = workflows.decide_run(
        run, decision, reason=str(body.get('reason') or ''), actor=_actor(request)
    )
    return Response(ser.run_detail(run))


@api_view(['GET', 'POST'])
def simulation(request):
    """Inject and clear failures in the simulated messaging provider.

    Kept as an API rather than a settings file so a failure scenario is
    reproducible on any machine, in any order, from the browser.
    """
    control = SimulationControl.current()
    if request.method == 'POST':
        body = _body(request)
        if body.get('reset'):
            control.reset()
        else:
            if 'messaging_failures_remaining' in body:
                control.messaging_failures_remaining = max(
                    0, int(body['messaging_failures_remaining'])
                )
            if 'messaging_latency_ms' in body:
                control.messaging_latency_ms = max(0, int(body['messaging_latency_ms']))
            control.save(
                update_fields=[
                    'messaging_failures_remaining', 'messaging_latency_ms', 'updated_at'
                ]
            )
        control.refresh_from_db()

    return Response(
        {
            'messaging_failures_remaining': control.messaging_failures_remaining,
            'messaging_latency_ms': control.messaging_latency_ms,
            'tool_max_attempts': settings.TOOL_MAX_ATTEMPTS,
        }
    )


@api_view(['POST'])
def seed_demo(request):
    """Load the synthetic dataset. Safe to call repeatedly."""
    body = _body(request)
    summary = seed_module.seed_demo(reset=bool(body.get('reset')))
    return Response(summary, status=status.HTTP_201_CREATED)


@api_view(['DELETE', 'POST'])
def reset_demo(request):
    """Delete all synthetic data and the durable checkpoints."""
    return Response(seed_module.reset_demo())
