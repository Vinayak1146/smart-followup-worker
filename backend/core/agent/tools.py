"""
Tool layer — the only way the worker can cause a side effect.

Three tools, deliberately. Each one is a real boundary: it validates its own
inputs, enforces its own permissions, retries transient failures, and reports what
it did. Permissions are enforced **here**, not by the caller, so a routing mistake
in the graph cannot turn into a message the business did not authorise. The model
never gets to decide whether it is allowed to act.

The dispatcher returns a :class:`ToolOutcome` rather than raising, so a refused or
failed call is still recordable evidence instead of a lost stack trace.
"""
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from .. import gateway, rules
from ..models import Approval, FollowUp, Interaction

TOOL_RETRIEVAL = 'retrieve_customer_history'
TOOL_SEND = 'send_followup'
TOOL_REORDER = 'create_reorder_request'


class ToolPermissionDenied(Exception):
    """The call was refused by a guard. Not a failure — a correct refusal."""


class ToolUnavailable(Exception):
    """Transient: the dependency is down. Safe to retry."""


@dataclass
class ToolOutcome:
    name: str
    status: str
    attempts: int = 1
    authorized: bool = True
    result: dict = field(default_factory=dict)
    error: str = ''
    latency_ms: int = 0

    @property
    def ok(self):
        return self.status in ('SUCCESS', 'RETRIED')


def _approved_approval_for(follow_up):
    if follow_up is None:
        return None
    approval = getattr(follow_up, 'approval', None)
    if approval is not None and approval.status == Approval.Status.APPROVED:
        return approval
    return None


def require_discount_authorization(follow_up, discount_pct):
    """Refuse to act on a discount the owner has not approved.

    This is the rule that makes "discounts require owner approval" true even if
    the graph, the policy check and the prompt all somehow agreed to skip it.
    """
    if not rules.discount_needs_approval(discount_pct):
        return

    if _approved_approval_for(follow_up) is None:
        raise ToolPermissionDenied(
            f'A {discount_pct}% discount exceeds the autonomous limit of '
            f'{settings.MAX_AUTONOMOUS_DISCOUNT_PCT}% and has no approved approval. '
            f'Refusing to send.'
        )


def _retrieve_customer_history(customer, **_):
    """Read-only: assemble the evidence the rest of the run reasons over."""
    purchases = [
        {
            'product_name': purchase.product_name,
            'category': purchase.category,
            'quantity': purchase.quantity,
            'amount': str(purchase.amount),
            'purchased_at': purchase.purchased_at.isoformat(),
        }
        for purchase in customer.purchases.order_by('-purchased_at')[:10]
    ]
    return {
        'customer': {
            'id': customer.id,
            'name': customer.name,
            'language': customer.language,
            'city': customer.city,
            'preferred_channel': customer.preferred_channel,
            'marketing_consent': customer.marketing_consent,
            'status': customer.status,
            'days_since_last_order': customer.days_since_last_order,
        },
        'order_count': customer.order_count,
        'lifetime_value': str(customer.lifetime_value),
        'purchases': purchases,
        'conversation': rules.conversation(customer),
        'memory': rules.recall(customer),
        'eligibility': rules.eligibility(customer),
        'suggested_product': rules.suggested_product(customer),
    }


def _send_followup(customer, follow_up, message, channel='', **_):
    """Irreversible: once this lands, the customer has been contacted. Hence the
    guard runs before the provider is touched."""
    require_discount_authorization(follow_up, follow_up.discount_pct if follow_up else 0)

    channel = channel or customer.preferred_channel
    receipt = gateway.send_message(customer, message, channel)

    rules.log_interaction(
        customer,
        Interaction.Direction.OUTBOUND,
        Interaction.Kind.FOLLOW_UP,
        message,
        channel=channel,
    )
    if follow_up is not None:
        follow_up.message = message
        follow_up.channel = channel
        follow_up.sent_at = timezone.now()
        follow_up.status = FollowUp.Status.SENT
        follow_up.save(update_fields=['message', 'channel', 'sent_at', 'status', 'updated_at'])

    return receipt


def _create_reorder_request(customer, product_name, discount_pct=0, follow_up=None, **_):
    """Create the pre-filled reorder the customer can accept in one tap.

    Guarded for the same reason as the send: a discount is a commercial commitment.
    """
    discount_pct = rules.clamp_discount(discount_pct)
    require_discount_authorization(follow_up, discount_pct)

    if not product_name:
        raise ValueError('A reorder request needs a product to order.')

    offer = f'{discount_pct}% off' if discount_pct else 'no discount'
    content = f'Reorder request prepared for {product_name} ({offer}).'
    rules.log_interaction(
        customer,
        Interaction.Direction.OUTBOUND,
        Interaction.Kind.REORDER,
        content,
        channel=customer.preferred_channel,
    )

    if follow_up is not None and not follow_up.product_name:
        follow_up.product_name = product_name
        follow_up.save(update_fields=['product_name', 'updated_at'])

    return {
        'reorder_request_id': f'ro-{customer.id}-{int(time.time())}',
        'product_name': product_name,
        'discount_pct': discount_pct,
        'channel': customer.preferred_channel,
        'status': 'PREPARED',
    }


REGISTRY = {
    TOOL_RETRIEVAL: {
        'fn': _retrieve_customer_history,
        'read_only': True,
        'retryable': False,
        'description': 'Read the customer\'s purchases, conversation and memory.',
    },
    TOOL_SEND: {
        'fn': _send_followup,
        'read_only': False,
        'retryable': True,
        'description': 'Send one follow-up message through the messaging provider.',
    },
    TOOL_REORDER: {
        'fn': _create_reorder_request,
        'read_only': False,
        'retryable': True,
        'description': 'Prepare a pre-filled reorder request for the customer.',
    },
}


def describe_tools():
    return [
        {
            'name': name,
            'read_only': spec['read_only'],
            'retryable': spec['retryable'],
            'description': spec['description'],
        }
        for name, spec in REGISTRY.items()
    ]


def call_tool(name, *, customer=None, follow_up=None, **kwargs):
    """Invoke a tool. Always returns a :class:`ToolOutcome`; never raises.

    Retries only transient failures, and only for tools that are safe to repeat.
    A refused call (``DENIED``) is not retried: retrying a permission decision
    would be a way to get a different answer by asking twice.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolOutcome(
            name=name, status='FAILED', authorized=False, error=f'Unknown tool {name!r}.'
        )

    attempts_allowed = settings.TOOL_MAX_ATTEMPTS if spec['retryable'] else 1
    started = time.perf_counter()
    last_error = ''

    for attempt in range(1, attempts_allowed + 1):
        try:
            result = spec['fn'](customer=customer, follow_up=follow_up, **kwargs)
            return ToolOutcome(
                name=name,
                status='SUCCESS' if attempt == 1 else 'RETRIED',
                attempts=attempt,
                result=result,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except ToolPermissionDenied as exc:
            return ToolOutcome(
                name=name,
                status='DENIED',
                attempts=attempt,
                authorized=False,
                error=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except gateway.MessagingUnavailable as exc:
            last_error = str(exc)
        except gateway.MessagingPermanentError as exc:
            return ToolOutcome(
                name=name,
                status='FAILED',
                attempts=attempt,
                error=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 — a tool must not crash the run
            return ToolOutcome(
                name=name,
                status='FAILED',
                attempts=attempt,
                error=f'{type(exc).__name__}: {exc}'[:300],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

    return ToolOutcome(
        name=name,
        status='FAILED',
        attempts=attempts_allowed,
        error=f'{last_error} (gave up after {attempts_allowed} attempts)'[:300],
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
