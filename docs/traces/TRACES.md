# Captured traces

The JSON files in this folder are **not hand-written examples**. They are produced by

```bash
cd backend
python manage.py capture_traces
```

which resets the synthetic dataset, drives the same HTTP endpoints the console calls,
and dumps the responses. If the worker's behaviour changes, re-running the command
changes these files.

| File | Scenario | Outcome |
|---|---|---|
| [`00_baseline.json`](00_baseline.json) | The dataset and policy before anything runs | 9 customers, 13 purchases, 0 runs |
| [`01_success_autonomous_reorder.json`](01_success_autonomous_reorder.json) | A lapsed customer is won back with no human involved | `COMPLETED`, `resolved_autonomously: true`, then `CONVERTED` by a real order |
| [`02_adverse_customer_rejection.json`](02_adverse_customer_rejection.json) | The customer says no | Follow-up `DECLINED`, no purchase invented, a second attempt refused by the cooldown |
| [`03_approval_discount_gate.json`](03_approval_discount_gate.json) | A discount the worker may not grant alone | `AWAITING_APPROVAL` → owner approves → `COMPLETED` |
| [`04_recovery_after_outage.json`](04_recovery_after_outage.json) | The provider fails mid-run | `FAILED` with `resume_node: send_followup` → retry → `COMPLETED`, one message, not two |

Each file has the same shape: the scenario and its expectation, the API responses for
every step, a `checks` block, and the final trace including every graph step.

---

## 00 — Baseline

```
customers    9 total · 1 opted out · 8 with consent
purchases    13 orders · ₹20,615 lifetime
follow-ups   1 (a prior decline, back-dated to seed the cooldown rule)
runs         0
```

Three tools are registered (`retrieve_customer_history`, `send_followup`,
`create_reorder_request`) and the checkpointer is a `SqliteSaver` writing to
`agent_checkpoints.sqlite`.

---

## 01 — Successful reorder, entirely autonomous

`Priya Nair`, inactive 45 days, one previous order, consent on record.

```
load_context     tool retrieve_customer_history     "Eligible: Inactive for 45 days with consent on record."
decide           rule engine                        "Proposed a 0% incentive. No owner approval needed."
draft_message                                       "Drafted a 144-character message."
send_followup    tool send_followup                 "Sent via WHATSAPP (message sim-91f38775f8f0)."
create_reorder   tool create_reorder_request        "Prepared reorder request ro-100-1795004935."
verify_outcome                                      "All checks passed: the message was recorded and the reorder is prepared."
finalize                                            "COMPLETED: Follow-up sent to Priya Nair offering Muslin Cotton Kurta."
```

The message that went out:

> Hi Priya, hope you are doing well. Your last order (Muslin Cotton Kurta) is something
> we can get ready for you again. Would you like to reorder?

```json
"checks": {
  "run_completed": true,
  "resolved_autonomously": true,
  "no_approval_recorded": true,
  "exactly_one_message_sent": true,
  "converted_after_purchase": true,
  "customer_status": "REACTIVATED"
}
```

The important part is `converted_after_purchase`. The worker never writes `CONVERTED`
itself — the order that arrives afterwards does, and the follow-up is matched to it
inside the same transaction.

---

## 02 — Adverse: the customer declines

`Meera Joshi` is contacted, replies "No thanks, not interested", and the worker is then
asked to try again immediately.

```json
"checks": {
  "follow_up_declined": true,
  "not_marked_converted": true,
  "second_attempt_sent_nothing": true,
  "outbound_messages_total": 1,
  "eligibility_now": {
    "eligible": false,
    "reason": "Customer already declined or was not approved for a follow-up 0 day(s) ago."
  }
}
```

The second attempt returned `COMPLETED` with the outcome *"No follow-up sent — Customer
already declined or was not approved for a follow-up 0 day(s) ago."* The refusal came
from a database rule read in `load_context`, not from the model, and the customer
received one message in total.

---

## 03 — A discount pauses the worker for the owner

`Rahul Verma`, inactive 75 days with 3 orders and a lifetime value of ₹6,897.

The run stops with `AWAITING_APPROVAL`, and the queue shows what the owner needs to
decide — without reading a trace:

```json
{
  "action_type": "DISCOUNT",
  "proposed_discount_pct": 15,
  "summary": "15% discount for Rahul Verma on Blue Denim Jacket",
  "findings": [
    "Inactive 75 days with 3 previous order(s), lifetime value 6897.",
    "Preferred channel: WHATSAPP; language: en.",
    "Suggested product to offer back: Blue Denim Jacket.",
    "Recalled 3 memory note(s) from earlier runs."
  ]
}
```

```json
"checks": {
  "paused_for_approval": true,
  "approval_visible_in_queue": true,
  "nothing_sent_while_pending": true,
  "proposed_discount_pct": 15,
  "resumed_after_decision": true,
  "decided_by": "Ananya",
  "message_sent_once": true,
  "discount_offered": 15
}
```

`nothing_sent_while_pending` is checked by counting outbound interactions *before* the
decision. A paused run has sent nothing, so a pending request cannot leak a message.

---

## 04 — Recovery: resume a failed run from its checkpoint

The messaging provider is given three injected failures (`TOOL_MAX_ATTEMPTS = 3`), so
the send tool exhausts its retries and the run ends `FAILED`.

```
send_followup    FAILED   attempts: 3
handle_failure   FAILED   resume_node: send_followup
```

The outage is then cleared and `POST /api/runs/:id/retry/` is called — a **new HTTP
request**, into a graph the process has continued running.

```json
"checks": {
  "run_failed": true,
  "resume_point_recorded": "send_followup",
  "nothing_sent_during_outage": true,
  "retry_completed": true,
  "resume_count": 1,
  "exactly_one_message_after_retry": true
}
```

`exactly_one_message_after_retry` is the check that matters. Resuming re-executes the
node from the top — that is how LangGraph replays an interrupted node — so a
non-idempotent send would have delivered the message twice. It did not, because the
follow-up row is fetched with `get_or_create` and the send is guarded by the state of
that row rather than by a flag held in memory.

A cross-process version of this was also run by hand: the run was failed in one server
process, that process was stopped, and the retry was issued against a freshly started
process. It still resumed, because the checkpoint is on disk.
