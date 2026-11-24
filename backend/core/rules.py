"""
Deterministic business rules.

Deciding *whether* a customer may be contacted, and *whether* a discount needs a
human, are not LLM jobs. They are explicit, reproducible and auditable rules over
database rows. The model reasons about what to say; it does not get a vote on
what is permitted.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import AgentMemory, FollowUp, Interaction

COOLDOWN_DAYS = 30


def eligibility(customer):
    """Return ``{'eligible': bool, 'reason': str}`` for one customer.

    The reason is always populated, including on success, so the trace can say
    *why* a customer was chosen rather than just that they were.
    """
    if customer.status == customer.Status.OPTED_OUT:
        return _no('Customer has opted out of outreach.')

    if settings.REQUIRE_MARKETING_CONSENT and not customer.marketing_consent:
        return _no('Customer has not consented to marketing messages.')

    days = customer.days_since_last_order
    if days is None:
        return _no('Customer has never ordered, so there is nothing to win back.')

    if days < settings.FOLLOW_UP_MIN_INACTIVE_DAYS:
        return _no(
            f'Customer ordered {days} day(s) ago — below the '
            f'{settings.FOLLOW_UP_MIN_INACTIVE_DAYS}-day inactivity threshold.'
        )

    if days > settings.FOLLOW_UP_MAX_INACTIVE_DAYS:
        return _no(
            f'Customer has been inactive for {days} days, beyond the '
            f'{settings.FOLLOW_UP_MAX_INACTIVE_DAYS}-day window.'
        )

    recent = recent_follow_up(customer)
    if recent is not None:
        if recent.status in (FollowUp.Status.DECLINED, FollowUp.Status.REJECTED):
            return _no(
                f'Customer already declined or was not approved for a follow-up '
                f'{_days_ago(recent.created_at)} day(s) ago.'
            )
        return _no(
            f'A follow-up was already sent {_days_ago(recent.created_at)} day(s) ago; '
            f'still inside the {COOLDOWN_DAYS}-day cooldown.'
        )

    return {'eligible': True, 'reason': f'Inactive for {days} days with consent on record.'}


def _no(reason):
    return {'eligible': False, 'reason': reason}


def _days_ago(moment):
    return (timezone.now() - moment).days


def recent_follow_up(customer):
    cutoff = timezone.now() - timedelta(days=COOLDOWN_DAYS)
    return (
        customer.follow_ups.filter(created_at__gte=cutoff)
        .exclude(status=FollowUp.Status.FAILED)
        .order_by('-created_at')
        .first()
    )


def clamp_discount(pct):
    """Never propose an incentive above what the business allows."""
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        return 0
    return max(0, min(pct, settings.MAX_PROPOSABLE_DISCOUNT_PCT))


def discount_needs_approval(pct):
    """Any discount above the autonomous limit moves the owner's margin, so it
    needs the owner's decision before a message goes out."""
    return clamp_discount(pct) > settings.MAX_AUTONOMOUS_DISCOUNT_PCT


def suggested_product(customer):
    """Deterministically suggest the product to offer back.

    The most frequently bought item is a defensible, explainable default — and it
    keeps this decision out of the model, so a weird suggestion can never be
    blamed on sampling.
    """
    last = customer.last_purchase
    if last is not None:
        return last.product_name
    counts = {}
    for purchase in customer.purchases.all():
        counts[purchase.product_name] = counts.get(purchase.product_name, 0) + 1
    if not counts:
        return ''
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def recall(customer, limit=5):
    """Retrieve memory scoped to this customer, plus business-wide notes.

    Reliable retrieval is a query, not a question to the model.
    """
    from django.db.models import Q

    rows = (
        AgentMemory.objects.filter(Q(customer=customer) | Q(scope=AgentMemory.Scope.BUSINESS))
        .order_by('-created_at')[:limit]
    )
    return [
        {'scope': row.scope, 'key': row.key, 'value': row.value, 'at': row.created_at.isoformat()}
        for row in rows
    ]


def remember(customer, key, value, scope=AgentMemory.Scope.CUSTOMER):
    """Append a durable note. Append-only on purpose: memory is a log of what was
    learned and when, not a mutable field that quietly overwrites history."""
    return AgentMemory.objects.create(scope=scope, customer=customer, key=key, value=value)


def conversation(customer, limit=10):
    """The recent two-way conversation, oldest first, for context."""
    rows = customer.interactions.order_by('-created_at')[:limit]
    return [
        {
            'direction': row.direction,
            'kind': row.kind,
            'content': row.content,
            'at': row.created_at.isoformat(),
        }
        for row in reversed(list(rows))
    ]


def log_interaction(customer, direction, kind, content, channel=''):
    return Interaction.objects.create(
        customer=customer,
        direction=direction,
        kind=kind,
        content=content,
        channel=channel,
    )
