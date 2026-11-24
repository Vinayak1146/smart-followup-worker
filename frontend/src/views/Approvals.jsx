import { useState } from 'react'
import { api } from '../api'
import { Badge, Card, Empty, Note, ago, useAction, useAsync } from '../ui'

export default function Approvals({ onChanged, onOpenRun }) {
  const pending = useAsync(() => api.approvals('PENDING'), [])
  const history = useAsync(() => api.approvals(), [])

  const pendingRows = pending.data?.results || []
  const decided = (history.data?.results || []).filter((row) => row.status !== 'PENDING')

  return (
    <div className="stack">
      <Note kind="error">{pending.error || history.error}</Note>

      <Card
        title="Discount requests waiting on you"
        subtitle="The worker proposes; the owner decides. Nothing is sent while a request is pending."
        right={
          <button
            type="button"
            className="btn"
            onClick={() => {
              pending.reload()
              history.reload()
            }}
          >
            Refresh
          </button>
        }
      >
        {pending.loading ? (
          <Empty>Loading…</Empty>
        ) : pendingRows.length === 0 ? (
          <Empty>Nothing is waiting. Every autonomous decision has already been made.</Empty>
        ) : (
          <div className="stack">
            {pendingRows.map((row) => (
              <ApprovalCard
                key={row.id}
                approval={row}
                onOpenRun={onOpenRun}
                onDecided={(message) => {
                  onChanged(message)
                  pending.reload()
                  history.reload()
                }}
              />
            ))}
          </div>
        )}
      </Card>

      <Card title="Decision history" subtitle="Who decided what, and when.">
        {decided.length === 0 ? (
          <Empty>No decision has been made yet.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Customer</th>
                <th>Proposed</th>
                <th>Decision</th>
                <th>By</th>
                <th>Reason</th>
                <th>When</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {decided.map((row) => (
                <tr key={row.id}>
                  <td>{row.customer_name}</td>
                  <td>{row.proposed_discount_pct}% off</td>
                  <td>
                    <Badge value={row.status} />
                  </td>
                  <td>{row.decided_by || '—'}</td>
                  <td className="dim">{row.decision_reason || '—'}</td>
                  <td className="dim">{ago(row.decided_at || row.created_at)}</td>
                  <td className="right">
                    {row.run_id && (
                      <button type="button" className="link" onClick={() => onOpenRun(row.run_id)}>
                        Trace
                      </button>
                    )}
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

function ApprovalCard({ approval, onDecided, onOpenRun }) {
  const action = useAction()
  const [reason, setReason] = useState('')

  async function decide(decision) {
    const run = await action.run(
      () => api.decideApproval(approval.id, decision, reason),
      decision === 'APPROVED' ? 'Approved.' : 'Rejected.',
    )
    if (run) {
      onDecided(
        `${approval.customer_name}: ${decision.toLowerCase()} a ${approval.proposed_discount_pct}% discount → ${run.outcome_summary}`,
      )
    }
  }

  const findings = approval.findings
  const findingRows = Array.isArray(findings)
    ? findings.map((value, index) => [String(index + 1), value])
    : Object.entries(findings || {})

  return (
    <div className="step step-paused">
      <div className="step-head">
        <strong>{approval.customer_name}</strong>
        <Badge value={approval.action_type} tone="info" />
        <Badge value={`${approval.proposed_discount_pct}% off`} tone="warn" />
        <Badge value="blocked until decided" tone="warn" />
        <span className="dim">asked {ago(approval.created_at)}</span>
      </div>

      <div className="step-summary">{approval.summary}</div>

      <div className="stack-sm" style={{ marginTop: 8 }}>
        {findingRows.map(([key, value]) => (
          <div key={key} className="dim">
            · {typeof value === 'string' ? value : JSON.stringify(value)}
          </div>
        ))}
      </div>

      <Note kind="error" onDismiss={action.clear}>
        {action.error}
      </Note>

      <div className="row" style={{ marginTop: 10 }}>
        <input
          placeholder="Reason (recorded on the approval and in the trace)"
          style={{ flex: 1, minWidth: 220 }}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
        <button
          type="button"
          className="btn btn-ok"
          disabled={action.busy}
          onClick={() => decide('APPROVED')}
        >
          Approve & send
        </button>
        <button
          type="button"
          className="btn btn-bad"
          disabled={action.busy}
          onClick={() => decide('REJECTED')}
        >
          Reject
        </button>
        {approval.run_id && (
          <button type="button" className="btn" onClick={() => onOpenRun(approval.run_id)}>
            Trace
          </button>
        )}
      </div>
    </div>
  )
}
