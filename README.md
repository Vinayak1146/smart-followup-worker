# Smart Customer Follow-up Worker

An autonomous AI worker that helps a micro-entrepreneur bring back inactive customers and increase repeat purchases. It pursues **one measurable business goal**:

> Win back customers who have not ordered recently by sending one relevant, consented follow-up that leads to a repeat purchase.

It is **one** worker, not a swarm of agents. It runs on a real **LangGraph** `StateGraph` with a durable SQLite checkpointer, gathers evidence with three tools, remembers what it learned across runs, stops for the owner whenever money would move, resumes a failed run from the point that broke, and measures whether the goal was actually achieved.

All data is **synthetic**. There are no real customer records anywhere in this repository.

---

## 1. What the worker actually does

The worker is handed one customer and decides, end to end, whether and how to bring them back. The seeded customers each exercise a different rule.

| Customer | Situation | What the worker does | Human needed |
|---|---|---|---|
| Priya Nair | Inactive 45 days, one order, consent on record | Sends one follow-up offering her last product back | No |
| Meera Joshi | Same, but replies "not interested" | Records the decline, and refuses to ask again during the cooldown | No |
| Rahul Verma | Inactive 75 days, 3 orders, ₹6,897 lifetime | Proposes a 15% discount, then **stops** | **Yes** |
| Vikram Mehta | Hindi speaker with a stored language preference | Proposes a discount, then **stops**; writes in Hindi | **Yes** |
| Nisha Kulkarni | Declined a follow-up 10 days ago | Refuses — still inside the 30-day cooldown | — |
| Arjun Singh | Ordered 12 days ago | Refuses — below the 30-day inactivity floor | — |
| Fatima Sheikh | Never consented to marketing | Refuses — no consent on record | — |
| Deepak Rao | Inactive 240 days | Refuses — beyond the 180-day win-back window | — |
| Sneha Iyer | Opted out | Refuses at policy level | — |

**Goal, metric and Definition of Done**

- **Business goal** — one relevant, consented follow-up per lapsed customer, leading to a repeat purchase.
- **Success metric** — `win_back_rate_pct` = converted follow-ups ÷ follow-ups sent. A conversion is only ever written by a real `Purchase` row, never by the worker.
- **Autonomy metric** — `resolved_autonomously` is true only when the run completed **and** no approval was involved.
- **Definition of Done** — `verify_outcome` checks three things against the database: the follow-up is marked sent, the message is recorded as an interaction, and the reorder is prepared. Anything else ends `FAILED` (with a checkpoint) or `REJECTED` (a human said no).

---

## 2. Running it

Two processes. The backend is Django on **8010**; the console is Vite on **5175** and proxies `/api` to the backend.

### Backend

```bash
cd backend
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 8010
```

The seed command creates the synthetic demo dataset.

No API key is required. With no `.env`, every model call degrades to the deterministic rule engine and the console reports zero tokens and `$0.00` of inference spend — the whole thing still works end to end.

To use a real model, copy `.env.example` to `.env` and set `LLM_PROVIDER` and `LLM_API_KEY`. To use a different backend port, change `server.proxy['/api'].target` in `frontend/vite.config.js` to match.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5175`.

### Tests

```bash
cd backend
python manage.py test core
```

---

## 3. The console

Five tabs, each answering one question.

| Tab | Question it answers |
|---|---|
| **Goal** | Is the business goal being met? Win-back rate, messages sent, what is waiting on me, how much inference cost |
| **Customers** | Who is eligible and why; one customer's orders, conversation, memory and run history |
| **Approvals** | What discount am I being asked to allow, on what evidence, and who decided |
| **Runs & trace** | What did the worker actually do — every node, tool call, model choice, token count and refusal |
| **Simulation** | Inject an outage, reset the dataset, and read the live policy and routing table |

`X-Owner-Name` (the "Acting as" box in the header) is not authentication — it is the label recorded on a human decision so the trace can say *who* approved a discount.

---

## 4. Project structure

```text
smart-followup-worker/
├── backend/
│   ├── config/                  Django settings, URLs (minimal: no auth, no admin)
│   └── core/
│       ├── models.py            Customer · Purchase · Interaction · FollowUp · Approval
│       │                        AgentMemory · WorkerRun · RunStep · SimulationControl
│       ├── rules.py             Deterministic policy — eligibility, clamping, memory
│       ├── gateway.py           Simulated messaging provider (injectable failures)
│       ├── seed.py              The synthetic dataset and the reset path
│       ├── serializers.py       Read models for the API
│       ├── views.py             Thin REST endpoints
│       ├── tests.py             Automated tests
│       ├── management/commands/ seed_demo
│       └── agent/
│           ├── graph.py         StateGraph, SqliteSaver checkpointer, resume helpers
│           ├── workflows.py     The workflow nodes and entry points
│           ├── tools.py         The three tools and permission guards
│           └── reasoner.py      Model routing, retries, fallback, rule-engine floor
├── frontend/                    React + Vite console (port 5175)
└── README.md
```
