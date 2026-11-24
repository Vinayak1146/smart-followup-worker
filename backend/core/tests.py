"""Tests for the follow-up worker.

The first block is the evaluation report made executable: one success scenario,
two adverse scenarios, and the two mechanisms the brief calls out (approval and
recovery after a restart). The second block covers the edges that are easy to get
wrong — the permission guard, the cooldown rule, and the dishonest-conversion
trap — because those are the failure modes a reviewer would look for.

Run with ``python manage.py test core``.
"""
from django.conf import settings
from django.test import TestCase
from rest_framework.test import APIClient

from . import rules, seed
from .agent import reasoner, tools, workflows
from .models import (
    Approval,
    Customer,
    FollowUp,
    Interaction,
    SimulationControl,
    WorkerRun,
)


def customer_by_name(name):
    return Customer.objects.get(name=name)


class EvaluationScenarios(TestCase):
    """The three scenarios named in the brief, plus the recovery mechanism."""

    def setUp(self):
        seed.seed_demo(reset=True)
        SimulationControl.current().reset()

    def test_successful_follow_up_completes_autonomously(self):
        priya = customer_by_name('Priya Nair')

        run = workflows.start_follow_up_run(priya)

        self.assertEqual(run.status, WorkerRun.Status.COMPLETED)
        self.assertTrue(run.resolved_autonomously)
        self.assertIn('Follow-up sent', run.outcome_summary)

        follow_up = priya.follow_ups.latest('created_at')
        self.assertEqual(follow_up.status, FollowUp.Status.SENT)
        self.assertEqual(follow_up.discount_pct, 0)
        self.assertIsNotNone(follow_up.sent_at)
        self.assertTrue(follow_up.message)

        kinds = set(
            priya.interactions.filter(direction=Interaction.Direction.OUTBOUND)
            .values_list('kind', flat=True)
        )
        self.assertEqual(kinds, {Interaction.Kind.FOLLOW_UP, Interaction.Kind.REORDER})

        self.assertTrue(
            priya.memories.filter(key='last_outreach').exists()
        )

        self.assertGreaterEqual(run.steps.count(), 6)
        self.assertTrue(run.steps.filter(node='verify_outcome', status='OK').exists())

    def test_customer_rejection_is_recorded_not_repeated(self):
        meera = customer_by_name('Meera Joshi')

        sent_run = workflows.start_follow_up_run(meera)
        self.assertEqual(sent_run.status, WorkerRun.Status.COMPLETED)

        reply_run = workflows.start_response_run(meera, 'No thanks, not interested')

        self.assertEqual(reply_run.status, WorkerRun.Status.COMPLETED)
        self.assertIn('declined', reply_run.outcome_summary.lower())

        follow_up = meera.follow_ups.latest('created_at')
        self.assertEqual(follow_up.status, FollowUp.Status.DECLINED)
        self.assertIsNotNone(follow_up.responded_at)

        self.assertTrue(meera.memories.filter(key='declined_offer').exists())

        eligible = rules.eligibility(meera)
        self.assertFalse(eligible['eligible'])
        self.assertIn('declined', eligible['reason'].lower())

        blocked_run = workflows.start_follow_up_run(meera)
        self.assertEqual(blocked_run.status, WorkerRun.Status.COMPLETED)
        self.assertIn('No follow-up sent', blocked_run.outcome_summary)
        self.assertEqual(meera.follow_ups.count(), 1)

    def test_discount_requires_and_gets_owner_approval(self):
        rahul = customer_by_name('Rahul Verma')

        run = workflows.start_follow_up_run(rahul)

        self.assertEqual(run.status, WorkerRun.Status.AWAITING_APPROVAL)
        approval = Approval.objects.get(follow_up__run=run)
        self.assertEqual(approval.proposed_discount_pct, 15)
        self.assertFalse(
            rahul.interactions.filter(kind=Interaction.Kind.FOLLOW_UP).exists(),
            'Nothing may be sent before the owner decides.',
        )

        run = workflows.decide_run(run, 'APPROVED', reason='Loyal repeat buyer.', actor='Ananya')

        self.assertEqual(run.status, WorkerRun.Status.COMPLETED)
        self.assertFalse(run.resolved_autonomously, 'A human was in the loop.')

        approval.refresh_from_db()
        self.assertEqual(approval.status, Approval.Status.APPROVED)
        self.assertEqual(approval.decided_by, 'Ananya')

        follow_up = rahul.follow_ups.latest('created_at')
        self.assertEqual(follow_up.status, FollowUp.Status.SENT)
        self.assertEqual(follow_up.discount_pct, 15)
        self.assertIn('15%', follow_up.message)

    def test_owner_can_reject_a_discount(self):
        vikram = customer_by_name('Vikram Mehta')

        run = workflows.start_follow_up_run(vikram)
        self.assertEqual(run.status, WorkerRun.Status.AWAITING_APPROVAL)

        run = workflows.decide_run(run, 'REJECTED', reason='Margin too thin this month.')

        self.assertEqual(run.status, WorkerRun.Status.REJECTED)
        self.assertEqual(vikram.follow_ups.latest('created_at').status, FollowUp.Status.REJECTED)
        self.assertFalse(
            vikram.interactions.filter(kind=Interaction.Kind.FOLLOW_UP).exists(),
            'A rejected offer must not have been sent anyway.',
        )
        self.assertTrue(vikram.memories.filter(key='offer_rejected').exists())

    def test_run_fails_then_resumes_from_the_broken_step(self):
        priya = customer_by_name('Priya Nair')
        attempts = settings.TOOL_MAX_ATTEMPTS
        control = SimulationControl.current()
        control.messaging_failures_remaining = attempts
        control.save(update_fields=['messaging_failures_remaining'])

        run = workflows.start_follow_up_run(priya)

        self.assertEqual(run.status, WorkerRun.Status.FAILED)
        self.assertEqual(run.resume_node, 'send_followup')
        self.assertFalse(
            priya.interactions.filter(kind=Interaction.Kind.FOLLOW_UP).exists(),
            'A failed send must not be recorded as a delivery.',
        )

        follower = run.steps.filter(node='send_followup').first()
        self.assertEqual(follower.tool_status, 'FAILED')
        self.assertEqual(follower.tool_attempts, attempts)

        run = workflows.retry_run(run)

        self.assertEqual(run.status, WorkerRun.Status.COMPLETED)
        self.assertEqual(run.resume_count, 1)
        self.assertEqual(
            priya.interactions.filter(kind=Interaction.Kind.FOLLOW_UP).count(), 1,
            'Retrying must not send the message twice.',
        )
        self.assertIsNotNone(priya.follow_ups.latest('created_at').sent_at)


class Guardrails(TestCase):
    """Rules that must hold regardless of what the model or the graph decide."""

    def setUp(self):
        seed.seed_demo(reset=True)

    def test_tool_refuses_an_unapproved_discount(self):
        rahul = customer_by_name('Rahul Verma')
        follow_up = FollowUp.objects.create(
            customer=rahul, status=FollowUp.Status.PENDING, discount_pct=20
        )

        outcome = tools.call_tool(
            tools.TOOL_SEND, customer=rahul, follow_up=follow_up, message='Hello'
        )

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, 'DENIED')
        self.assertFalse(outcome.authorized)
        self.assertEqual(outcome.attempts, 1, 'A refusal must not be retried.')
        self.assertFalse(rahul.interactions.exists())

    def test_discount_is_clamped_to_the_business_cap(self):
        self.assertEqual(rules.clamp_discount(90), settings.MAX_PROPOSABLE_DISCOUNT_PCT)
        self.assertEqual(rules.clamp_discount(-5), 0)
        self.assertEqual(rules.clamp_discount('not a number'), 0)

    def test_ineligible_customers_are_never_contacted(self):
        cases = [
            ('Arjun Singh', 'below'),
            ('Fatima Sheikh', 'consented'),
            ('Deepak Rao', 'beyond'),
            ('Sneha Iyer', 'opted out'),
            ('Nisha Kulkarni', 'declined'),
        ]
        for name, phrase in cases:
            customer = customer_by_name(name)
            run = workflows.start_follow_up_run(customer)
            self.assertEqual(run.status, WorkerRun.Status.COMPLETED, name)
            self.assertIn('No follow-up sent', run.outcome_summary, name)
            self.assertIn(phrase, run.outcome_summary.lower(), f'{name}: {run.outcome_summary}')
            self.assertFalse(
                Interaction.objects.filter(
                    customer=customer, kind=Interaction.Kind.FOLLOW_UP
                ).exists(),
                f'{name} must not have been messaged.',
            )
            self.assertFalse(
                customer.follow_ups.filter(status=FollowUp.Status.SENT).exists(), name
            )

    def test_a_follow_up_is_never_self_reported_as_converted(self):
        priya = customer_by_name('Priya Nair')
        run = workflows.start_follow_up_run(priya)
        self.assertEqual(run.status, WorkerRun.Status.COMPLETED)

        follow_up = priya.follow_ups.latest('created_at')
        self.assertEqual(follow_up.status, FollowUp.Status.SENT)
        self.assertNotEqual(follow_up.status, FollowUp.Status.CONVERTED)

    def test_outreach_is_blocked_without_consent_even_if_status_looks_eligible(self):
        fatima = customer_by_name('Fatima Sheikh')
        self.assertTrue(fatima.marketing_consent is False)
        fatima.status = Customer.Status.LAPSED
        fatima.save(update_fields=['status'])

        eligible = rules.eligibility(fatima)
        self.assertFalse(eligible['eligible'])
        self.assertIn('consent', eligible['reason'].lower())


class RoutingPolicy(TestCase):
    """The model policy is explicit and inspectable, not implicit in call sites."""

    def test_eligibility_and_discount_policy_never_use_a_model(self):
        routing = reasoner.describe_routing()
        for task in ('ELIGIBILITY', 'DISCOUNT_POLICY'):
            self.assertEqual(routing['tasks'][task]['tier'], reasoner.TIER_RULES, task)
            self.assertEqual(routing['tasks'][task]['model'], reasoner.RULES_MODEL, task)

    def test_every_task_declares_why_it_is_routed_that_way(self):
        for task, spec in reasoner.describe_routing()['tasks'].items():
            self.assertTrue(spec['why'], task)
            self.assertTrue(spec['candidate_chain'], task)

    def test_analysis_degrades_to_a_usable_answer_without_a_provider(self):
        result = reasoner.run_task(
            'CUSTOMER_ANALYSIS',
            {'context': {'days_since_last_order': 75, 'order_count': 3, 'discount_cap': 25}},
        )
        self.assertEqual(result.model, reasoner.RULES_MODEL)
        self.assertTrue(result.output['should_follow_up'])
        self.assertEqual(result.output['discount_pct'], 15)
        self.assertEqual(result.prompt_tokens, 0)
        self.assertEqual(result.estimated_cost, 0.0)

    def test_reply_classification_detects_an_opt_out(self):
        result = reasoner.run_task(
            'REPLY_CLASSIFICATION', {'reply': 'Please stop messaging me'}
        )
        self.assertEqual(result.output['intent'], 'NEGATIVE')
        self.assertTrue(result.output['wants_no_contact'])


class ApiContract(TestCase):
    """The console talks to real endpoints, so the endpoints are tested as such."""

    def setUp(self):
        seed.seed_demo(reset=True)
        self.client = APIClient()

    def test_meta_exposes_goal_policy_tools_and_routing(self):
        response = self.client.get('/api/meta/')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['tools']), 3, 'Exactly three tools, as designed.')
        tool_names = {tool['name'] for tool in body['tools']}
        self.assertEqual(
            tool_names,
            {'retrieve_customer_history', 'send_followup', 'create_reorder_request'},
        )
        self.assertIn('goal', body['policy'])
        self.assertIn('checkpointer', body['harness'])
        self.assertEqual(body['harness']['checkpointer']['type'], 'SqliteSaver')

    def test_dashboard_reports_measured_not_claimed_numbers(self):
        response = self.client.get('/api/dashboard/')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['customers']['total'], 50)
        self.assertEqual(body['follow_ups']['win_back_rate_pct'], 0.0)
        self.assertEqual(body['follow_ups']['converted'], 0)

    def test_full_flow_over_http_including_approval(self):
        rahul = customer_by_name('Rahul Verma')

        started = self.client.post(f'/api/customers/{rahul.id}/follow-up/')
        self.assertEqual(started.status_code, 201)
        run = started.json()
        self.assertEqual(run['status'], 'AWAITING_APPROVAL')

        pending = self.client.get('/api/approvals/?status=PENDING').json()
        self.assertEqual(pending['count'], 1)

        decided = self.client.post(
            f"/api/approvals/{pending['results'][0]['id']}/decide/",
            {'decision': 'APPROVED', 'reason': 'Approved in the console.'},
            format='json',
            HTTP_X_OWNER_NAME='Ananya',
        )
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()['status'], 'COMPLETED')

        detail = self.client.get(f"/api/runs/{run['run_id']}/").json()
        nodes = [step['node'] for step in detail['steps']]
        self.assertIn('request_approval', nodes)
        self.assertIn('verify_outcome', nodes)
        self.assertEqual(detail['approval']['status'], 'APPROVED')
        self.assertEqual(detail['approval']['decided_by'], 'Ananya')

    def test_conversion_requires_a_real_purchase(self):
        priya = customer_by_name('Priya Nair')
        self.client.post(f'/api/customers/{priya.id}/follow-up/')

        follow_up = priya.follow_ups.latest('created_at')
        self.assertEqual(follow_up.status, FollowUp.Status.SENT)

        recorded = self.client.post(
            f'/api/customers/{priya.id}/purchase/',
            {'product_name': 'Muslin Cotton Kurta', 'amount': 1299, 'quantity': 1},
            format='json',
        )
        self.assertEqual(recorded.status_code, 201)
        self.assertEqual(recorded.json()['converted_follow_up_id'], follow_up.id)

        follow_up.refresh_from_db()
        priya.refresh_from_db()
        self.assertEqual(follow_up.status, FollowUp.Status.CONVERTED)
        self.assertEqual(priya.status, Customer.Status.REACTIVATED)

        dashboard = self.client.get('/api/dashboard/').json()
        self.assertEqual(dashboard['follow_ups']['converted'], 1)
        self.assertEqual(dashboard['follow_ups']['win_back_rate_pct'], 100.0)

    def test_outage_injection_is_controllable_over_http(self):
        priya = customer_by_name('Priya Nair')

        self.client.post('/api/simulation/', {'messaging_failures_remaining': 3}, format='json')
        run = self.client.post(f'/api/customers/{priya.id}/follow-up/').json()
        self.assertEqual(run['status'], 'FAILED')
        self.assertEqual(run['resume_node'], 'send_followup')

        self.client.post('/api/simulation/', {'reset': True}, format='json')
        retried = self.client.post(f"/api/runs/{run['run_id']}/retry/")
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()['status'], 'COMPLETED')

    def test_errors_are_json_not_html(self):
        response = self.client.get('/api/customers/9999/')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Content-Type'].split(';')[0], 'application/json')
        self.assertIn('error', response.json())

    def test_a_decision_is_rejected_when_there_is_nothing_to_decide(self):
        priya = customer_by_name('Priya Nair')
        run = self.client.post(f'/api/customers/{priya.id}/follow-up/').json()
        response = self.client.post(
            f"/api/runs/{run['run_id']}/decision/", {'decision': 'APPROVED'}, format='json'
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn('error', response.json())

    def test_simulation_reset_clears_failures(self):
        self.client.post('/api/simulation/', {'messaging_failures_remaining': 2}, format='json')
        self.assertEqual(
            self.client.get('/api/simulation/').json()['messaging_failures_remaining'], 2
        )
        self.client.post('/api/simulation/', {'reset': True}, format='json')
        self.assertEqual(
            self.client.get('/api/simulation/').json()['messaging_failures_remaining'], 0
        )
