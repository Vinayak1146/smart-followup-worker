"""Read models for the API.

These are plain dict builders rather than DRF ``Serializer`` classes. Nothing in
this prototype accepts a model instance as request input — every mutation is a
named action (start a run, decide an approval, record a purchase) — so a
serializer's main job, validating incoming fields, has no work to do here. What
does have work to do is *shaping outgoing rows*, and that is easier to read as
explicit functions that state exactly which fields the console sees.
"""
from django.conf import settings
from django.db.models import Sum

from . import rules
from .agent.reasoner import RULES_MODEL
from .models import Approval, Customer, FollowUp, Purchase, RunStep, WorkerRun, business_goal


def money(value):
    return str(value or 0)


def purchase_row(purchase):
    return {
        'id': purchase.id,
        'product_name': purchase.product_name,
        'category': purchase.category,
        'quantity': purchase.quantity,
        'amount': money(purchase.amount),
        'purchased_at': purchase.purchased_at.isoformat(),
    }


def interaction_row(interaction):
    return {
        'id': interaction.id,
        'direction': interaction.direction,
        'kind': interaction.kind,
        'channel': interaction.channel,
        'content': interaction.content,
        'created_at': interaction.created_at.isoformat(),
    }


def memory_row(memory):
    return {
        'id': memory.id,
        'scope': memory.scope,
        'key': memory.key,
        'value': memory.value,
        'created_at': memory.created_at.isoformat(),
    }


def approval_row(approval):
    if approval is None:
        return None
    return {
        'id': approval.id,
        'follow_up_id': approval.follow_up_id,
        'customer_id': approval.follow_up.customer_id,
        'customer_name': approval.follow_up.customer.name,
        'action_type': approval.action_type,
        'proposed_discount_pct': approval.proposed_discount_pct,
        'status': approval.status,
        'summary': approval.summary,
        'findings': approval.findings,
        'decided_by': approval.decided_by,
        'decision_reason': approval.decision_reason,
        'decided_at': approval.decided_at.isoformat() if approval.decided_at else None,
        'created_at': approval.created_at.isoformat(),
        'run_id': str(approval.follow_up.run.run_id) if approval.follow_up.run_id else None,
    }


def follow_up_row(follow_up, *, with_approval=True):
    row = {
        'id': follow_up.id,
        'customer_id': follow_up.customer_id,
        'customer_name': follow_up.customer.name,
        'status': follow_up.status,
        'discount_pct': follow_up.discount_pct,
        'product_name': follow_up.product_name,
        'channel': follow_up.channel,
        'message': follow_up.message,
        'sent_at': follow_up.sent_at.isoformat() if follow_up.sent_at else None,
        'responded_at': follow_up.responded_at.isoformat() if follow_up.responded_at else None,
        'outcome_note': follow_up.outcome_note,
        'created_at': follow_up.created_at.isoformat(),
        'run_id': str(follow_up.run.run_id) if follow_up.run_id else None,
    }
    if with_approval:
        row['approval'] = approval_row(getattr(follow_up, 'approval', None))
    return row


def customer_summary(customer, *, include_eligibility=False):
    row = {
        'id': customer.id,
        'name': customer.name,
        'phone': customer.phone,
        'city': customer.city,
        'language': customer.language,
        'preferred_channel': customer.preferred_channel,
        'marketing_consent': customer.marketing_consent,
        'status': customer.status,
        'days_since_last_order': customer.days_since_last_order,
        'order_count': customer.order_count,
        'lifetime_value': money(customer.lifetime_value),
        'last_order_at': customer.last_order_at.isoformat() if customer.last_order_at else None,
        'last_product': customer.last_purchase.product_name if customer.last_purchase else None,
    }
    if include_eligibility:
        row['eligibility'] = rules.eligibility(customer)
        row['suggested_product'] = rules.suggested_product(customer)
        row['open_follow_up'] = None
        open_follow_up = (
            customer.follow_ups.exclude(status=FollowUp.Status.FAILED)
            .order_by('-created_at')
            .first()
        )
        if open_follow_up is not None:
            row['open_follow_up'] = follow_up_row(open_follow_up)
    return row


def _provider_label(step):
    """Who actually answered this step.

    The rule engine runs in-process, so a step that used it is labelled local.
    Anything else came back from the configured provider — ``model_used`` carries
    the model id that was asked, and ``strategy`` says whether it was the primary
    model or a fallback.
    """
    if step.model_used == RULES_MODEL:
        return 'Local (rule engine)'
    return str(settings.LLM_PROVIDER or 'unknown').title()


def step_row(step):
    return {
        'id': step.id,
        'index': step.index,
        'node': step.node,
        'status': step.status,
        'summary': step.summary,
        'created_at': step.created_at.isoformat(),
        'tool': {
            'name': step.tool_name,
            'status': step.tool_status,
            'attempts': step.tool_attempts,
            'authorized': step.tool_authorized,
            'error': step.tool_error,
            'result': step.tool_result,
        } if step.tool_name else None,
        'model': {
            'used': step.model_used,
            'provider': _provider_label(step),
            'strategy': step.strategy,
            'prompt_tokens': step.prompt_tokens,
            'completion_tokens': step.completion_tokens,
            'total_tokens': step.total_tokens,
            'estimated_cost': str(step.estimated_cost),
            'latency_ms': step.latency_ms,
        } if step.model_used else None,
        'output': step.output,
        'error': step.error,
    }


def run_summary(run):
    return {
        'run_id': str(run.run_id),
        'mode': run.mode,
        'customer_id': run.customer_id,
        'customer_name': run.customer.name if run.customer else None,
        'goal': run.goal,
        'status': run.status,
        'next_node': run.next_node,
        'resume_node': run.resume_node,
        'outcome_summary': run.outcome_summary,
        'resolved_autonomously': run.resolved_autonomously,
        'resume_count': run.resume_count,
        'created_at': run.created_at.isoformat(),
        'finished_at': run.finished_at.isoformat() if run.finished_at else None,
        'duration_ms': run.duration_ms,
        'step_count': run.steps.count(),
    }


def run_detail(run):
    steps = list(run.steps.order_by('index'))
    return {
        **run_summary(run),
        'state': run.state_data,
        'steps': [step_row(step) for step in steps],
        'approval': approval_row(
            Approval.objects.filter(follow_up__run=run).first()
        ),
        'tokens': sum(step.total_tokens for step in steps),
        'cost_usd': str(sum(step.estimated_cost for step in steps)),
    }


def dashboard():
    """The numbers the owner actually cares about, computed from rows.

    Nothing here is self-reported by the worker: the win-back rate counts
    follow-ups the customer replied positively to, and cost comes from recorded
    token usage.
    """
    sent = FollowUp.objects.filter(sent_at__isnull=False)
    converted = FollowUp.objects.filter(status=FollowUp.Status.CONVERTED)
    sent_count = sent.count()
    converted_count = converted.count()

    runs = WorkerRun.objects.all()

    return {
        'goal': business_goal(),
        'customers': {
            'total': Customer.objects.count(),
            'opted_out': Customer.objects.filter(status=Customer.Status.OPTED_OUT).count(),
            'with_consent': Customer.objects.filter(marketing_consent=True).count(),
        },
        'purchases': {
            'count': Purchase.objects.count(),
            'revenue': money(Purchase.objects.aggregate(total=Sum('amount'))['total']),
        },
        'follow_ups': {
            'total': FollowUp.objects.count(),
            'sent': sent_count,
            'awaiting_approval': FollowUp.objects.filter(
                status=FollowUp.Status.AWAITING_APPROVAL
            ).count(),
            'declined': FollowUp.objects.filter(status=FollowUp.Status.DECLINED).count(),
            'rejected_by_owner': FollowUp.objects.filter(
                status=FollowUp.Status.REJECTED
            ).count(),
            'converted': converted_count,
            'win_back_rate_pct': (
                round(100 * converted_count / sent_count, 1) if sent_count else 0.0
            ),
        },
        'runs': {
            'total': runs.count(),
            'completed': runs.filter(status=WorkerRun.Status.COMPLETED).count(),
            'awaiting_approval': runs.filter(
                status=WorkerRun.Status.AWAITING_APPROVAL
            ).count(),
            'failed': runs.filter(status=WorkerRun.Status.FAILED).count(),
            'rejected': runs.filter(status=WorkerRun.Status.REJECTED).count(),
            'autonomous': runs.filter(resolved_autonomously=True).count(),
        },
        'efficiency': {
            'total_tokens': RunStep.objects.aggregate(t=Sum('total_tokens'))['t'] or 0,
            'total_cost_usd': money(RunStep.objects.aggregate(c=Sum('estimated_cost'))['c']),
            'steps': RunStep.objects.count(),
        },
        'approvals': {
            'pending': Approval.objects.filter(status=Approval.Status.PENDING).count(),
            'approved': Approval.objects.filter(status=Approval.Status.APPROVED).count(),
            'rejected': Approval.objects.filter(status=Approval.Status.REJECTED).count(),
        },
    }
