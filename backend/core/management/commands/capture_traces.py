"""Replay the evaluation scenarios over the real HTTP API and save the evidence.

The traces this writes are not hand-written examples: the command resets the
synthetic dataset, drives the same endpoints the console calls, and dumps the
JSON the API returned. If the worker's behaviour changes, re-running this command
changes the traces — which is the point.

    python manage.py capture_traces
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from rest_framework.test import APIClient

from core import seed as seed_module
from core.models import Customer, FollowUp, WorkerRun

OWNER = 'Ananya'
TRACE_DIR = Path(settings.BASE_DIR).parent / 'docs' / 'traces'


class Command(BaseCommand):
    help = 'Replay the evaluation scenarios and write docs/traces/*.json.'

    def handle(self, *args, **options):
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        if 'testserver' not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.append('testserver')

        seed_module.reset_demo()
        seeded = seed_module.seed_demo()
        self.stdout.write(f'Seeded {seeded["customers_total"]} customers.')

        client = APIClient()
        client.credentials(HTTP_X_OWNER_NAME=OWNER)

        written = [
            self.scenario_1_success(client),
            self.scenario_2_rejection(client),
            self.scenario_3_approval(client),
            self.scenario_4_recovery(client),
        ]
        seed_module.reset_demo()
        seed_module.seed_demo()
        self.dump('00_baseline.json', self.baseline(client, seeded))

        for name in written:
            self.stdout.write(self.style.SUCCESS(f'wrote {name}'))

    def fresh(self):
        """Every scenario starts from the same dataset.

        Scenarios change the world — one converts a customer, one declines a
        customer, one burns the cooldown — so replaying them in order would make
        each later trace depend on the earlier ones. Resetting first keeps each
        trace independently reproducible.
        """
        seed_module.reset_demo()
        seed_module.seed_demo()

    def dump(self, name, payload):
        path = TRACE_DIR / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding='utf-8')
        return name

    def customer(self, name):
        return Customer.objects.get(name=name)

    def baseline(self, client, seeded):
        return {
            'note': seeded['note'],
            'goal': client.get('/api/meta/').data['goal'],
            'policy': client.get('/api/meta/').data['policy'],
            'tools': [t['name'] for t in client.get('/api/meta/').data['tools']],
            'dataset': seeded['scenarios'],
            'dashboard': client.get('/api/dashboard/').data,
        }

    def outbound_count(self, customer):
        return customer.interactions.filter(
            direction='OUTBOUND', kind='FOLLOW_UP'
        ).count()

    def scenario_1_success(self, client):
        """A customer is won back with no human involved, and a real order
        — not the worker — marks the follow-up converted."""
        self.fresh()
        customer = self.customer('Priya Nair')

        run = client.post(f'/api/customers/{customer.id}/follow-up/', {}, format='json')
        assert run.status_code == 201, run.data
        run_id = run.data['run_id']

        order = client.post(
            f'/api/customers/{customer.id}/purchase/',
            {'product_name': 'Muslin Cotton Kurta', 'category': 'Apparel', 'quantity': 1,
             'amount': 1299},
            format='json',
        )
        customer.refresh_from_db()
        follow_up = FollowUp.objects.get(customer=customer, status=FollowUp.Status.CONVERTED)

        return self.dump('01_success_autonomous_reorder.json', {
            'scenario': 'Successful reorder, entirely autonomous',
            'expectation': (
                'The worker finds an eligible customer, proposes no discount (so no approval '
                'is needed), sends one message, and completes. The conversion is written by '
                'the recorded purchase, not by the worker.'
            ),
            'step_1_worker_run': run.data,
            'step_2_recorded_order': order.data,
            'step_3_follow_up_state': {
                'id': follow_up.id,
                'status': follow_up.status,
                'sent_at': follow_up.sent_at.isoformat() if follow_up.sent_at else None,
                'outcome_note': follow_up.outcome_note,
                'message': follow_up.message,
            },
            'checks': {
                'run_completed': run.data['status'] == WorkerRun.Status.COMPLETED,
                'resolved_autonomously': run.data['resolved_autonomously'],
                'no_approval_recorded': run.data['approval'] is None,
                'exactly_one_message_sent': self.outbound_count(customer) == 1,
                'converted_after_purchase': order.data['converted_follow_up_id'] == follow_up.id,
                'customer_status': customer.status,
            },
            'final_trace': client.get(f'/api/runs/{run_id}/').data,
        })

    def scenario_2_rejection(self, client):
        """A customer says no. The worker records it, and never asks again while
        the cooldown is running."""
        self.fresh()
        customer = self.customer('Meera Joshi')

        run = client.post(f'/api/customers/{customer.id}/follow-up/', {}, format='json')
        reply = client.post(
            f'/api/customers/{customer.id}/reply/',
            {'reply': 'No thanks, not interested'},
            format='json',
        )
        follow_up = FollowUp.objects.filter(customer=customer).order_by('-created_at').first()

        second = client.post(f'/api/customers/{customer.id}/follow-up/', {}, format='json')
        after = self.outbound_count(customer)

        return self.dump('02_adverse_customer_rejection.json', {
            'scenario': 'Adverse — the customer declines',
            'expectation': (
                'The reply is classified as a decline, the follow-up is recorded as declined, '
                'no purchase is invented, and an immediate second attempt is refused by the '
                'cooldown rule rather than by the model.'
            ),
            'step_1_worker_run': run.data,
            'step_2_customer_reply': reply.data,
            'step_3_follow_up_state': {
                'id': follow_up.id,
                'status': follow_up.status,
                'discount_pct': follow_up.discount_pct,
                'outcome_note': follow_up.outcome_note,
                'responded_at': (
                    follow_up.responded_at.isoformat() if follow_up.responded_at else None
                ),
            },
            'step_4_second_attempt': second.data,
            'checks': {
                'follow_up_declined': follow_up.status == FollowUp.Status.DECLINED,
                'not_marked_converted': not FollowUp.objects.filter(
                    customer=customer, status=FollowUp.Status.CONVERTED
                ).exists(),
                'second_attempt_sent_nothing': after == 1
                and 'no follow-up sent' in (second.data.get('outcome_summary') or '').lower(),
                'outbound_messages_total': after,
                'eligibility_now': client.get(f'/api/customers/{customer.id}/').data['eligibility'],
            },
        })

    def scenario_3_approval(self, client):
        """A discount the worker is not allowed to grant alone: it stops and asks."""
        self.fresh()
        customer = self.customer('Rahul Verma')

        run = client.post(f'/api/customers/{customer.id}/follow-up/', {}, format='json')
        run_id = run.data['run_id']
        queue = client.get('/api/approvals/', {'status': 'PENDING'}).data

        sent_while_pending = self.outbound_count(customer)

        decided = client.post(
            f'/api/runs/{run_id}/decision/',
            {'decision': 'APPROVED', 'reason': 'Repeat buyer, worth the margin.'},
            format='json',
        )

        return self.dump('03_approval_discount_gate.json', {
            'scenario': 'A discount pauses the worker for owner approval',
            'expectation': (
                'The proposed discount exceeds the autonomous limit, so the run stops before '
                'anything is sent, an approval is raised with the findings the owner needs, '
                'and the decision resumes the paused run in place.'
            ),
            'step_1_worker_run': run.data,
            'step_2_approval_queue': queue,
            'step_3_owner_decision': decided.data,
            'checks': {
                'paused_for_approval': run.data['status'] == WorkerRun.Status.AWAITING_APPROVAL,
                'approval_visible_in_queue': queue['count'] == 1,
                'nothing_sent_while_pending': sent_while_pending == 0,
                'proposed_discount_pct': (
                    run.data['approval']['proposed_discount_pct'] if run.data['approval'] else None
                ),
                'resumed_after_decision': decided.data['status'] == WorkerRun.Status.COMPLETED,
                'decided_by': decided.data['approval']['decided_by'],
                'message_sent_once': self.outbound_count(customer) == 1,
                'discount_offered': (
                    FollowUp.objects.filter(customer=customer)
                    .order_by('-created_at')
                    .first()
                    .discount_pct
                ),
            },
            'final_trace': client.get(f'/api/runs/{run_id}/').data,
        })

    def scenario_4_recovery(self, client):
        """The provider goes down mid-run. The run fails with a checkpoint, and
        resumes from the broken step after the outage — without double-sending.

        The subject must be a customer whose follow-up needs no approval:
        an approval pause happens *before* the send, so it would never reach the
        outage this scenario is about.
        """
        self.fresh()
        customer = self.customer('Priya Nair')

        attempts = client.get('/api/simulation/').data['tool_max_attempts']
        outage = client.post(
            '/api/simulation/', {'messaging_failures_remaining': attempts}, format='json'
        ).data
        run = client.post(f'/api/customers/{customer.id}/follow-up/', {}, format='json')
        run_id = run.data['run_id']
        sent_during_outage = self.outbound_count(customer)

        restored = client.post('/api/simulation/', {'reset': True}, format='json').data
        retried = client.post(f'/api/runs/{run_id}/retry/', {}, format='json')
        sent_after_retry = self.outbound_count(customer)

        return self.dump('04_recovery_after_outage.json', {
            'scenario': 'Recovery — resume a failed run from its checkpoint',
            'expectation': (
                'The send tool exhausts its retries against a simulated outage, the run ends '
                'FAILED with the node that broke recorded, no message reached the customer, and '
                'retrying re-enters at that node — sending exactly one message, not two.'
            ),
            'step_1_outage_injected': outage,
            'step_2_failed_run': run.data,
            'step_3_outage_cleared': restored,
            'step_4_retry': retried.data,
            'checks': {
                'run_failed': run.data['status'] == WorkerRun.Status.FAILED,
                'resume_point_recorded': run.data['resume_node'],
                'nothing_sent_during_outage': sent_during_outage == 0,
                'retry_completed': retried.data['status'] == WorkerRun.Status.COMPLETED,
                'resume_count': retried.data['resume_count'],
                'exactly_one_message_after_retry': sent_after_retry == 1,
            },
            'final_trace': client.get(f'/api/runs/{run_id}/').data,
        })
