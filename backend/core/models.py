"""
Data model for the Smart Customer Follow-up Worker.

Four kinds of data, kept separate on purpose:

* **Operational records** — ``Customer``, ``Purchase``, ``Interaction``. The
  facts about what actually happened.
* **Work items** — ``FollowUp``. What the worker is trying to achieve, and how
  it ended.
* **Execution history** — ``WorkerRun``, ``RunStep``. An audit trail, not a
  narrative: every step records the tool it used, the model it chose, the tokens
  it spent and the money that cost.
* **Retrieval and memory** — ``AgentMemory``. Append-only notes scoped to a
  customer or to the business, so a later run starts with context.

``Approval`` sits beside the work item: it is the human decision that gates any
discount.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Customer(models.Model):
    """A micro-entrepreneur's customer."""

    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        LAPSED = 'LAPSED', 'Lapsed'
        REACTIVATED = 'REACTIVATED', 'Reactivated'
        OPTED_OUT = 'OPTED_OUT', 'Opted out'

    class Channel(models.TextChoices):
        WHATSAPP = 'WHATSAPP', 'WhatsApp'
        SMS = 'SMS', 'SMS'

    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20, unique=True)
    language = models.CharField(max_length=8, default='en')
    city = models.CharField(max_length=80, blank=True)
    preferred_channel = models.CharField(
        max_length=16, choices=Channel.choices, default=Channel.WHATSAPP
    )
    marketing_consent = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    last_order_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.phone})'

    @property
    def days_since_last_order(self):
        if self.last_order_at is None:
            return None
        return (timezone.now() - self.last_order_at).days

    @property
    def order_count(self):
        return self.purchases.count()

    @property
    def lifetime_value(self):
        return self.purchases.aggregate(total=models.Sum('amount'))['total'] or 0

    @property
    def last_purchase(self):
        return self.purchases.order_by('-purchased_at').first()

    def recompute_last_order(self):
        latest = self.purchases.order_by('-purchased_at').first()
        self.last_order_at = latest.purchased_at if latest else None
        self.save(update_fields=['last_order_at'])
        return self.last_order_at


class Purchase(models.Model):
    """An order already on record. Read-only history: it decides who has lapsed
    and what to offer back, but the worker never writes one."""

    customer = models.ForeignKey(Customer, related_name='purchases', on_delete=models.CASCADE)
    product_name = models.CharField(max_length=160)
    category = models.CharField(max_length=80, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    purchased_at = models.DateTimeField()

    class Meta:
        ordering = ['-purchased_at']

    def __str__(self):
        return f'{self.product_name} x{self.quantity} ({self.customer.name})'


class Interaction(models.Model):
    """Every message in and out. The worker's outbound sends are not special —
    they are recorded here like any other interaction, so the conversation is in
    one place."""

    class Direction(models.TextChoices):
        OUTBOUND = 'OUTBOUND', 'Outbound'
        INBOUND = 'INBOUND', 'Inbound'

    class Kind(models.TextChoices):
        FOLLOW_UP = 'FOLLOW_UP', 'Follow-up'
        REPLY = 'REPLY', 'Reply'
        REORDER = 'REORDER', 'Reorder'

    customer = models.ForeignKey(Customer, related_name='interactions', on_delete=models.CASCADE)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    channel = models.CharField(max_length=16, blank=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_direction_display()} {self.kind} → {self.customer.name}'


class FollowUp(models.Model):
    """The unit of work: one attempt to bring one customer back.

    ``status`` is the honest record of what the worker managed to achieve —
    ``CONVERTED`` means the customer replied positively to the follow-up. The
    actual order happens on the shop site, so the worker never records a
    purchase; the win is the reply, not an order it cannot see.
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        AWAITING_APPROVAL = 'AWAITING_APPROVAL', 'Awaiting approval'
        SENT = 'SENT', 'Sent'
        CONVERTED = 'CONVERTED', 'Converted'
        DECLINED = 'DECLINED', 'Declined by customer'
        REJECTED = 'REJECTED', 'Rejected by owner'
        ESCALATED = 'ESCALATED', 'Escalated'
        FAILED = 'FAILED', 'Failed'

    customer = models.ForeignKey(Customer, related_name='follow_ups', on_delete=models.CASCADE)
    run = models.ForeignKey(
        'WorkerRun', related_name='follow_ups', null=True, blank=True, on_delete=models.SET_NULL
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    message = models.TextField(blank=True)
    discount_pct = models.PositiveIntegerField(default=0)
    channel = models.CharField(max_length=16, blank=True)
    product_name = models.CharField(max_length=160, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    outcome_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.customer.name}: {self.get_status_display()}'


class Approval(models.Model):
    """The human decision that gates a discount.

    It carries everything the owner needs to decide: the action, the amount, and
    the findings the worker relied on. The owner should not have to read a trace
    to make a safe call.
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    follow_up = models.OneToOneField(
        FollowUp, related_name='approval', on_delete=models.CASCADE
    )
    action_type = models.CharField(max_length=32, default='DISCOUNT')
    proposed_discount_pct = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    summary = models.CharField(max_length=300)
    findings = models.JSONField(default=dict, blank=True)
    decided_by = models.CharField(max_length=120, blank=True)
    decision_reason = models.CharField(max_length=300, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.action_type} {self.proposed_discount_pct}% ({self.status})'


class AgentMemory(models.Model):
    """Append-only notes the worker carries *between* executions.

    A later run recalls these so it does not re-ask a customer's language
    preference or re-send an offer that was already declined.
    """

    class Scope(models.TextChoices):
        CUSTOMER = 'CUSTOMER', 'Customer'
        BUSINESS = 'BUSINESS', 'Business'

    scope = models.CharField(max_length=12, choices=Scope.choices, default=Scope.CUSTOMER)
    customer = models.ForeignKey(
        Customer, related_name='memories', null=True, blank=True, on_delete=models.CASCADE
    )
    key = models.CharField(max_length=64)
    value = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'agent memories'

    def __str__(self):
        return f'{self.scope}/{self.key}: {self.value[:60]}'


class WorkerRun(models.Model):
    """One execution of the worker.

    ``state_data`` is the worker's own view of the run; ``next_node`` and
    ``resume_node`` are projections of the LangGraph checkpoint so the API can
    answer "where is this run, and can it continue?" with a plain ORM query.
    """

    class Mode(models.TextChoices):
        FOLLOW_UP = 'FOLLOW_UP', 'Follow-up'
        RESPONSE = 'RESPONSE', 'Handle customer reply'

    class Status(models.TextChoices):
        RUNNING = 'RUNNING', 'Running'
        AWAITING_APPROVAL = 'AWAITING_APPROVAL', 'Awaiting approval'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        REJECTED = 'REJECTED', 'Rejected'

    run_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.FOLLOW_UP)
    customer = models.ForeignKey(
        Customer, related_name='runs', null=True, blank=True, on_delete=models.SET_NULL
    )
    goal = models.CharField(max_length=300)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    next_node = models.CharField(max_length=40, blank=True)
    resume_node = models.CharField(max_length=40, blank=True)
    outcome_summary = models.CharField(max_length=400, blank=True)
    resolved_autonomously = models.BooleanField(default=False)
    resume_count = models.PositiveIntegerField(default=0)
    state_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.run_id} {self.status}'


class RunStep(models.Model):
    """One node of the graph, with its tool call and its model call if it made one.

    This is the evidence layer: a step that degraded to the rule engine, retried
    a flaky tool, or was denied by a permission guard is visible here rather than
    hidden inside a log line.
    """

    class Status(models.TextChoices):
        OK = 'OK', 'Ok'
        FAILED = 'FAILED', 'Failed'
        SKIPPED = 'SKIPPED', 'Skipped'
        PAUSED = 'PAUSED', 'Paused for a human'
        DENIED = 'DENIED', 'Denied by a guard'

    run = models.ForeignKey(WorkerRun, related_name='steps', on_delete=models.CASCADE)
    index = models.PositiveIntegerField(default=0)
    node = models.CharField(max_length=40)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OK)
    summary = models.CharField(max_length=400, blank=True)

    tool_name = models.CharField(max_length=64, blank=True)
    tool_status = models.CharField(max_length=16, blank=True)
    tool_attempts = models.PositiveIntegerField(default=0)
    tool_authorized = models.BooleanField(default=True)
    tool_error = models.CharField(max_length=300, blank=True)
    tool_result = models.JSONField(default=dict, blank=True)

    model_used = models.CharField(max_length=64, blank=True)
    strategy = models.CharField(max_length=32, blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=8, default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    output = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=400, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['index']

    def __str__(self):
        return f'{self.index}. {self.node} [{self.status}]'


class SimulationControl(models.Model):
    """Demo controls for the simulated messaging provider.

    Kept in the database rather than in code so an outage can be injected and
    cleared from the UI, and so a failure scenario is reproducible on any machine.
    """

    SINGLETON_PK = 1

    messaging_failures_remaining = models.PositiveIntegerField(default=0)
    messaging_latency_ms = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'simulation control'
        verbose_name_plural = 'simulation control'

    def __str__(self):
        return f'outage remaining={self.messaging_failures_remaining}'

    @classmethod
    def current(cls):
        control, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return control

    def reset(self):
        self.messaging_failures_remaining = 0
        self.messaging_latency_ms = 0
        self.save(update_fields=['messaging_failures_remaining', 'messaging_latency_ms', 'updated_at'])


def business_goal():
    """The one goal this worker owns. Single source of truth for the UI and API."""
    return (
        'Win back customers who have not ordered recently by sending one relevant, '
        'consented follow-up that leads to a repeat purchase.'
    )


def policy_snapshot():
    """The worker's policy, exposed so it can be inspected rather than assumed."""
    return {
        'goal': business_goal(),
        'eligibility': {
            'min_inactive_days': settings.FOLLOW_UP_MIN_INACTIVE_DAYS,
            'max_inactive_days': settings.FOLLOW_UP_MAX_INACTIVE_DAYS,
            'requires_marketing_consent': settings.REQUIRE_MARKETING_CONSENT,
        },
        'approval': {
            'max_autonomous_discount_pct': settings.MAX_AUTONOMOUS_DISCOUNT_PCT,
            'max_proposable_discount_pct': settings.MAX_PROPOSABLE_DISCOUNT_PCT,
            'rule': (
                'Any discount at or below the autonomous limit may be granted by the worker; '
                'anything above it must be approved by the owner before the message is sent.'
            ),
        },
        'guardrails': [
            'One follow-up per customer per cooldown window.',
            'No outreach without marketing consent.',
            'The messaging and reorder tools refuse to run when approval is required and missing.',
        ],
    }
