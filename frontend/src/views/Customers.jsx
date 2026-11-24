import { useState } from 'react'
import { api } from '../api'
import { Badge, Card, Empty, Note, ago, fmtMoney, fmtTime, useAction, useAsync } from '../ui'

export default function Customers({ selectedId, onSelect, onChanged, onOpenRun }) {
  const list = useAsync(() => api.customers(), [])

  if (selectedId) {
    return (
      <CustomerDetail
        id={selectedId}
        onBack={() => onSelect(null)}
        onChanged={onChanged}
        onOpenRun={onOpenRun}
      />
    )
  }

  const rows = list.data?.results || []

  return (
    <div className="stack">
      <Note kind="error">{list.error}</Note>
      <Card
        title="All customers"
        subtitle={`${list.data?.eligible ?? 0} of ${rows.length} currently pass the eligibility rules.`}
        right={
          <button type="button" className="btn" onClick={list.reload}>
            Refresh
          </button>
        }
      >
        {list.loading ? (
          <Empty>Loading…</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Customer</th>
                <th>Status</th>
                <th>Last order</th>
                <th>Orders</th>
                <th>Lifetime</th>
                <th>Consent</th>
                <th>Eligible</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <button type="button" className="link" onClick={() => onSelect(row.id)}>
                      {row.name}
                    </button>
                    <div className="dim">
                      {row.city} · {row.preferred_channel} · {row.language}
                    </div>
                  </td>
                  <td>
                    <Badge value={row.status} />
                  </td>
                  <td>
                    {row.days_since_last_order === null ? 'never' : `${row.days_since_last_order}d`}
                    <div className="dim">{row.last_product || '—'}</div>
                  </td>
                  <td>{row.order_count}</td>
                  <td>{fmtMoney(row.lifetime_value)}</td>
                  <td>
                    <Badge
                      value={row.marketing_consent ? 'yes' : 'no'}
                      tone={row.marketing_consent ? 'ok' : 'bad'}
                    />
                  </td>
                  <td>
                    <Badge
                      value={row.eligibility.eligible ? 'eligible' : 'not eligible'}
                      tone={row.eligibility.eligible ? 'ok' : 'warn'}
                    />
                    <div className="dim">{row.eligibility.reason}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}

function CustomerDetail({ id, onBack, onChanged, onOpenRun }) {
  const detail = useAsync(() => api.customer(id), [id])
  const action = useAction()
  const [reply, setReply] = useState('')
  const [order, setOrder] = useState({ product_name: '', amount: '', quantity: 1 })

  const customer = detail.data

  async function runWorker() {
    const run = await action.run(
      () => api.startFollowUp(id),
      'The worker ran. Open the trace to see each step.',
    )
    if (run) {
      onChanged(`${customer.name} → ${run.status.replace(/_/g, ' ').toLowerCase()}. ${run.outcome_summary}`)
      detail.reload()
    }
  }

  async function sendReply() {
    const run = await action.run(
      () => api.sendReply(id, reply),
      'Reply handled by the worker.',
    )
    if (run) {
      onChanged(`${customer.name} replied → ${run.outcome_summary}`)
      setReply('')
      detail.reload()
    }
  }

  async function recordPurchase() {
    const result = await action.run(
      () => api.recordPurchase(id, {
        product_name: order.product_name,
        amount: Number(order.amount || 0),
        quantity: Number(order.quantity || 1),
      }),
      'Order recorded.',
    )
    if (result) {
      onChanged(
        result.converted_follow_up_id
          ? `Order recorded and follow-up #${result.converted_follow_up_id} marked converted by the purchase, not by the worker.`
          : 'Order recorded.',
      )
      setOrder({ product_name: '', amount: '', quantity: 1 })
      detail.reload()
    }
  }

  if (detail.loading) return <Empty>Loading customer…</Empty>
  if (detail.error) return <Note kind="error">{detail.error}</Note>
  if (!customer) return <Empty>Customer not found.</Empty>

  const sentFollowUp = customer.follow_ups.find((f) => f.status === 'SENT')

  return (
    <div className="stack">
      <div className="row">
        <button type="button" className="btn" onClick={onBack}>
          ← All customers
        </button>
        <Badge value={customer.status} />
        <Badge
          value={customer.eligibility.eligible ? 'eligible now' : 'not eligible'}
          tone={customer.eligibility.eligible ? 'ok' : 'warn'}
        />
      </div>

      <Note kind="error" onDismiss={action.clear}>
        {action.error}
      </Note>
      <Note kind="success" onDismiss={action.clear}>
        {action.message}
      </Note>

      <Card title={customer.name} subtitle={customer.eligibility.reason}>
        <div className="row" style={{ gap: 18, alignItems: 'flex-start' }}>
          <dl className="kv">
            <dt>Phone</dt>
            <dd>{customer.phone}</dd>
            <dt>City</dt>
            <dd>{customer.city || '—'}</dd>
            <dt>Language</dt>
            <dd>{customer.language}</dd>
            <dt>Channel</dt>
            <dd>{customer.preferred_channel}</dd>
            <dt>Marketing consent</dt>
            <dd>{customer.marketing_consent ? 'yes' : 'no'}</dd>
          </dl>
          <dl className="kv">
            <dt>Last order</dt>
            <dd>
              {customer.days_since_last_order === null
                ? 'never'
                : `${customer.days_since_last_order} days ago`}
            </dd>
            <dt>Orders</dt>
            <dd>{customer.order_count}</dd>
            <dt>Lifetime value</dt>
            <dd>{fmtMoney(customer.lifetime_value)}</dd>
            <dt>Suggested product</dt>
            <dd>{customer.suggested_product || '—'}</dd>
            <dt>Last product</dt>
            <dd>{customer.last_product || '—'}</dd>
          </dl>
        </div>

        <div className="row" style={{ marginTop: 14 }}>
          <button type="button" className="btn btn-primary" onClick={runWorker} disabled={action.busy}>
            Run the worker for this customer
          </button>
          {!customer.eligibility.eligible && (
            <span className="dim">
              The worker still runs — and must decide not to contact this customer.
            </span>
          )}
        </div>
      </Card>

      <div className="split-narrow">
        <Card title="Purchase history" subtitle="What the retrieve-history tool reads.">
          {customer.purchases.length === 0 ? (
            <Empty>No orders on record.</Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Product</th>
                  <th>Qty</th>
                  <th>Amount</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {customer.purchases.map((p) => (
                  <tr key={p.id}>
                    <td>
                      {p.product_name}
                      <div className="dim">{p.category}</div>
                    </td>
                    <td>{p.quantity}</td>
                    <td>{fmtMoney(p.amount)}</td>
                    <td className="dim">{fmtTime(p.purchased_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card
          title="Memory"
          subtitle="Append-only notes the worker carries between runs."
        >
          {customer.memories.length === 0 ? (
            <Empty>Nothing remembered about this customer yet.</Empty>
          ) : (
            <dl className="kv">
              {customer.memories.map((m) => (
                <div key={m.id} style={{ display: 'contents' }}>
                  <dt>{m.key}</dt>
                  <dd>
                    {m.value}
                    <div className="dim">
                      {m.scope} · {ago(m.created_at)}
                    </div>
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </Card>
      </div>

      <Card
        title="Conversation"
        subtitle="Follow-ups the worker sent, and replies it received."
      >
        {customer.interactions.length === 0 ? (
          <Empty>No messages exchanged yet.</Empty>
        ) : (
          <div className="thread">
            {[...customer.interactions].reverse().map((i) => (
              <div
                key={i.id}
                className={i.direction === 'INBOUND' ? 'bubble bubble-in' : 'bubble bubble-out'}
              >
                <div>{i.content}</div>
                <div className="dim">
                  {i.direction === 'INBOUND' ? 'customer' : 'worker'} · {i.kind} ·{' '}
                  {fmtTime(i.created_at)}
                </div>
              </div>
            ))}
          </div>
        )}

        {sentFollowUp && (
          <div className="stack-sm" style={{ marginTop: 14 }}>
            <p className="sub">
              Simulate the customer replying to follow-up #{sentFollowUp.id}.
            </p>
            <textarea
              rows={2}
              value={reply}
              onChange={(event) => setReply(event.target.value)}
              placeholder="No thanks, not interested"
            />
            <div className="chips">
              <button
                type="button"
                className="chip"
                onClick={() => setReply('Yes please, send it over')}
              >
                Positive
              </button>
              <button
                type="button"
                className="chip"
                onClick={() => setReply('No thanks, not interested')}
              >
                Negative
              </button>
              <button
                type="button"
                className="chip"
                onClick={() => setReply('Please stop messaging me')}
              >
                Opt-out
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!reply.trim() || action.busy}
                onClick={sendReply}
              >
                Send reply
              </button>
            </div>
          </div>
        )}
      </Card>

      <Card
        title="Record an order"
        subtitle="The only path that can mark a follow-up converted — never the worker itself."
      >
        <div className="row">
          <input
            placeholder="Product"
            value={order.product_name}
            onChange={(event) => setOrder({ ...order, product_name: event.target.value })}
          />
          <input
            placeholder="Amount"
            style={{ width: 110 }}
            value={order.amount}
            onChange={(event) => setOrder({ ...order, amount: event.target.value })}
          />
          <input
            placeholder="Qty"
            style={{ width: 70 }}
            value={order.quantity}
            onChange={(event) => setOrder({ ...order, quantity: event.target.value })}
          />
          <button
            type="button"
            className="btn"
            disabled={!order.product_name.trim() || action.busy}
            onClick={recordPurchase}
          >
            Record order
          </button>
        </div>
      </Card>

      <Card title="Follow-ups" subtitle="Every attempt to bring this customer back.">
        {customer.follow_ups.length === 0 ? (
          <Empty>No follow-up has been attempted.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>Status</th>
                <th>Discount</th>
                <th>Product</th>
                <th>Outcome</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {customer.follow_ups.map((f) => (
                <tr key={f.id}>
                  <td>{f.id}</td>
                  <td>
                    <Badge value={f.status} />
                  </td>
                  <td>{f.discount_pct ? `${f.discount_pct}%` : '—'}</td>
                  <td>{f.product_name || '—'}</td>
                  <td className="dim">{f.outcome_note || '—'}</td>
                  <td className="dim">{fmtTime(f.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Runs" subtitle="One execution of the worker, with its trace.">
        {customer.runs.length === 0 ? (
          <Empty>The worker has not run for this customer.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Mode</th>
                <th>Status</th>
                <th>Autonomous</th>
                <th>Outcome</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {customer.runs.map((run) => (
                <tr key={run.run_id}>
                  <td className="dim">{fmtTime(run.created_at)}</td>
                  <td>{run.mode}</td>
                  <td>
                    <Badge value={run.status} />
                  </td>
                  <td>{run.resolved_autonomously ? 'yes' : 'no'}</td>
                  <td className="dim">{run.outcome_summary}</td>
                  <td className="right">
                    <button type="button" className="link" onClick={() => onOpenRun(run.run_id)}>
                      Trace
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
