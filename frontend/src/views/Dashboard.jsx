import { useState } from 'react'
import { api } from '../api'
import { Badge, Card, Empty, Metric, Note, fmtMoney, fmtTime, useAction, useAsync } from '../ui'

export default function Dashboard({ onChanged, onOpenCustomer }) {
  const dash = useAsync(() => api.dashboard(), [])
  const customers = useAsync(() => api.customers(), [])
  const action = useAction()
  const [replyFor, setReplyFor] = useState(null)
  const [replyText, setReplyText] = useState('')

  async function runFollowUp(customer) {
    const run = await action.run(
      () => api.startFollowUp(customer.id),
      `Ran the worker for ${customer.name}: ${''}`,
    )
    if (run) {
      action.clear()
      onChanged(
        `${customer.name} → ${run.status.replace(/_/g, ' ').toLowerCase()}. ${run.outcome_summary}`,
      )
    }
    dash.reload()
    customers.reload()
  }

  async function sendReply(customer, text) {
    const run = await action.run(() => api.sendReply(customer.id, text))
    if (run) {
      onChanged(`${customer.name} replied → ${run.outcome_summary}`)
      setReplyFor(null)
      setReplyText('')
    }
    dash.reload()
    customers.reload()
  }

  const metrics = dash.data
  const rows = customers.data?.results || []
  const eligible = rows.filter((row) => row.eligibility?.eligible)

  return (
    <div className="stack">
      <Note kind="error" onDismiss={action.clear}>
        {action.error}
      </Note>
      <Note kind="success" onDismiss={action.clear}>
        {action.message}
      </Note>
      <Note kind="error">{dash.error || customers.error}</Note>

      {metrics && (
        <>
          <Card title="The one goal" subtitle={metrics.goal}>
            <div className="metrics">
              <Metric
                label="Win-back rate"
                value={`${metrics.follow_ups.win_back_rate_pct}%`}
                hint={`${metrics.follow_ups.converted} converted of ${metrics.follow_ups.sent} sent`}
                tone={metrics.follow_ups.converted > 0 ? 'ok' : undefined}
              />
              <Metric
                label="Follow-ups sent"
                value={metrics.follow_ups.sent}
                hint={`${metrics.follow_ups.total} prepared in total`}
              />
              <Metric
                label="Awaiting your decision"
                value={metrics.approvals.pending}
                hint="Discount requests above the autonomous limit"
                tone={metrics.approvals.pending > 0 ? 'warn' : undefined}
              />
              <Metric
                label="Resolved with no human"
                value={metrics.runs.autonomous}
                hint={`of ${metrics.runs.total} runs`}
              />
              <Metric
                label="Inference spend"
                value={fmtMoney(metrics.efficiency.total_cost_usd)}
                hint={`${metrics.efficiency.total_tokens} tokens over ${metrics.efficiency.steps} steps`}
              />
              <Metric
                label="Runs failed"
                value={metrics.runs.failed}
                hint="Each keeps a checkpoint so it can be retried"
                tone={metrics.runs.failed > 0 ? 'bad' : undefined}
              />
            </div>
          </Card>

          <Card
            title="Customers who can be contacted now"
            subtitle="Eligibility is a rule over stored facts, not a model's opinion."
            right={
              <button
                type="button"
                className="btn"
                onClick={() => {
                  dash.reload()
                  customers.reload()
                }}
              >
                Refresh
              </button>
            }
          >
            {customers.loading ? (
              <Empty>Loading…</Empty>
            ) : eligible.length === 0 ? (
              <Empty>No customer currently passes the eligibility rules.</Empty>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Customer</th>
                    <th>Inactive</th>
                    <th>Orders</th>
                    <th>Why eligible</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {eligible.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <button
                          type="button"
                          className="link"
                          onClick={() => onOpenCustomer(row.id)}
                        >
                          {row.name}
                        </button>
                        <div className="dim">
                          {row.city} · {row.preferred_channel}
                          {row.language !== 'en' ? ` · ${row.language}` : ''}
                        </div>
                      </td>
                      <td>{row.days_since_last_order}d</td>
                      <td>{row.order_count}</td>
                      <td className="dim">{row.eligibility.reason}</td>
                      <td className="right">
                        <button
                          type="button"
                          className="btn btn-primary"
                          disabled={action.busy}
                          onClick={() => runFollowUp(row)}
                        >
                          Run worker
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <Card
            title="Simulate a customer reply"
            subtitle="Replies are handled the same way a real inbound message would be."
          >
            {replyFor === null ? (
              <div className="chips">
                {rows
                  .filter((row) => row.open_follow_up?.status === 'SENT')
                  .map((row) => (
                    <button
                      key={row.id}
                      type="button"
                      className="chip"
                      onClick={() => setReplyFor(row)}
                    >
                      Reply as {row.name}
                    </button>
                  ))}
                {!rows.some((row) => row.open_follow_up?.status === 'SENT') && (
                  <Empty>No customer is waiting on a reply yet.</Empty>
                )}
              </div>
            ) : (
              <div className="stack-sm">
                <p className="sub">Replying as {replyFor.name}</p>
                <textarea
                  rows={2}
                  value={replyText}
                  onChange={(event) => setReplyText(event.target.value)}
                  placeholder="No thanks, not interested"
                />
                <div className="chips">
                  <button
                    type="button"
                    className="chip"
                    onClick={() => setReplyText('Yes please, send it over')}
                  >
                    Positive reply
                  </button>
                  <button
                    type="button"
                    className="chip"
                    onClick={() => setReplyText('No thanks, not interested')}
                  >
                    Negative reply
                  </button>
                  <button
                    type="button"
                    className="chip"
                    onClick={() => setReplyText('Please stop messaging me')}
                  >
                    Opt-out reply
                  </button>
                </div>
                <div className="chips">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={!replyText.trim() || action.busy}
                    onClick={() => sendReply(replyFor, replyText)}
                  >
                    Send reply
                  </button>
                  <button type="button" className="btn" onClick={() => setReplyFor(null)}>
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </Card>

          <Card title="Recent runs">
            {metrics.runs.total === 0 ? (
              <Empty>No runs yet. Run the worker for a customer above.</Empty>
            ) : (
              <RecentRuns />
            )}
          </Card>
        </>
      )}
    </div>
  )
}

function RecentRuns() {
  const runs = useAsync(() => api.runs({ limit: 8 }), [])
  if (runs.loading) return <Empty>Loading…</Empty>
  if (runs.error) return <Note kind="error">{runs.error}</Note>
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Started</th>
          <th>Customer</th>
          <th>Status</th>
          <th>Autonomous</th>
          <th>Outcome</th>
        </tr>
      </thead>
      <tbody>
        {(runs.data?.results || []).map((run) => (
          <tr key={run.run_id}>
            <td className="dim">{fmtTime(run.created_at)}</td>
            <td>{run.customer_name || '—'}</td>
            <td>
              <Badge value={run.status} />
            </td>
            <td>{run.resolved_autonomously ? 'yes' : 'no'}</td>
            <td className="dim">{run.outcome_summary}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
