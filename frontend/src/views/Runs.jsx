import { useState } from 'react'
import { api } from '../api'
import { Badge, Card, Empty, Note, Row, ago, fmtMoney, fmtTime, useAction, useAsync } from '../ui'

const STATUS_FILTERS = ['', 'COMPLETED', 'AWAITING_APPROVAL', 'FAILED', 'REJECTED']

const RULES_MODEL = 'rule-engine-v1'

const STRATEGY_LABEL = {
  llm: 'LLM',
  'llm-model-fallback': 'LLM (fallback model)',
  deterministic: 'Deterministic',
  'deterministic-fallback': 'Deterministic (provider degraded)',
}

export default function Runs({ focusRunId, onChanged }) {
  const [status, setStatus] = useState('')
  const [selected, setSelected] = useState(focusRunId || null)
  const list = useAsync(() => api.runs({ status, limit: 100 }), [status])

  const rows = list.data?.results || []
  const failed = rows.filter((row) => row.status === 'FAILED').length

  return (
    <div className="split">
      <Card
        title="Execution history"
        subtitle={`${rows.length} run(s) shown${failed ? ` · ${failed} failed and retryable` : ''}`}
        right={
          <div className="chips">
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              {STATUS_FILTERS.map((value) => (
                <option key={value || 'all'} value={value}>
                  {value ? value.replace(/_/g, ' ') : 'all statuses'}
                </option>
              ))}
            </select>
            <button type="button" className="btn" onClick={list.reload}>
              Refresh
            </button>
          </div>
        }
      >
        <Note kind="error">{list.error}</Note>
        {list.loading ? (
          <Empty>Loading…</Empty>
        ) : rows.length === 0 ? (
          <Empty>No runs match this filter.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Customer</th>
                <th>Status</th>
                <th>Steps</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((run) => (
                <tr
                  key={run.run_id}
                  className={run.run_id === selected ? 'row-active' : ''}
                  onClick={() => setSelected(run.run_id)}
                  style={{ cursor: 'pointer' }}
                >
                  <td>
                    {fmtTime(run.created_at)}
                    <div className="dim">{ago(run.created_at)}</div>
                  </td>
                  <td>
                    {run.customer_name || '—'}
                    <div className="dim">{run.mode}</div>
                  </td>
                  <td>
                    <Badge value={run.status} />
                    {run.resume_count > 0 && (
                      <div className="dim">resumed {run.resume_count}×</div>
                    )}
                  </td>
                  <td>{run.step_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {selected ? (
        <RunTrace
          key={selected}
          runId={selected}
          onChanged={(message) => {
            onChanged(message)
            list.reload()
          }}
        />
      ) : (
        <Card title="Trace" subtitle="Pick a run to see every step it took.">
          <Empty>
            Each step records the tool it called, the model it chose, the tokens it spent and
            whether a guard refused it.
          </Empty>
        </Card>
      )}
    </div>
  )
}

function RunTrace({ runId, onChanged }) {
  const run = useAsync(() => api.run(runId), [runId])
  const action = useAction()
  const [reason, setReason] = useState('')

  const data = run.data

  async function decide(decision) {
    const result = await action.run(
      () => api.decideRun(runId, decision, reason),
      decision === 'APPROVED' ? 'Approved and resumed.' : 'Rejected.',
    )
    if (result) {
      onChanged(`${data.customer_name}: ${decision.toLowerCase()} → ${result.outcome_summary}`)
      run.reload()
    }
  }

  async function retry() {
    const result = await action.run(
      () => api.retryRun(runId),
      'Resumed from the checkpoint.',
    )
    if (result) {
      onChanged(`Retried ${data.customer_name || 'run'} → ${result.outcome_summary}`)
      run.reload()
    }
  }

  if (run.loading) return <Card title="Trace"><Empty>Loading…</Empty></Card>
  if (run.error) return <Card title="Trace"><Note kind="error">{run.error}</Note></Card>
  if (!data) return null

  const canDecide = data.status === 'AWAITING_APPROVAL'
  const canRetry = data.status === 'FAILED'

  return (
    <div className="stack">
      <Note kind="error" onDismiss={action.clear}>
        {action.error}
      </Note>
      <Note kind="success" onDismiss={action.clear}>
        {action.message}
      </Note>

      <Card
        title={`Run · ${data.customer_name || 'unknown customer'}`}
        subtitle={data.goal}
        right={<Badge value={data.status} />}
      >
        <div className="row" style={{ gap: 18 }}>
          <dl className="kv">
            <dt>Mode</dt>
            <dd>{data.mode}</dd>
            <dt>Started</dt>
            <dd>{fmtTime(data.created_at)}</dd>
            <dt>Duration</dt>
            <dd>{data.duration_ms} ms</dd>
            <dt>Steps</dt>
            <dd>{data.step_count}</dd>
          </dl>
          <dl className="kv">
            <dt>Autonomous</dt>
            <dd>{data.resolved_autonomously ? 'yes — no human needed' : 'no'}</dd>
            <dt>Tokens</dt>
            <dd>{data.tokens}</dd>
            <dt>Cost</dt>
            <dd>{fmtMoney(data.cost_usd)}</dd>
            <dt>Resume count</dt>
            <dd>{data.resume_count}</dd>
          </dl>
          <dl className="kv">
            <dt>Next node</dt>
            <dd className="mono">{data.next_node || '—'}</dd>
            <dt>Resume node</dt>
            <dd className="mono">{data.resume_node || '—'}</dd>
            <dt>run_id</dt>
            <dd className="mono">{data.run_id}</dd>
          </dl>
        </div>

        <p className="sub" style={{ marginTop: 12 }}>
          {data.outcome_summary}
        </p>

        {canRetry && data.resume_node && (
          <Note kind="info">
            This run broke at <span className="mono">{data.resume_node}</span>. Its checkpoint
            survives a restart, so it can continue from that step — not from the beginning.
          </Note>
        )}

        {canDecide && (
          <div className="row" style={{ marginTop: 10 }}>
            <input
              placeholder="Reason"
              style={{ flex: 1, minWidth: 180 }}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
            <button
              type="button"
              className="btn btn-ok"
              disabled={action.busy}
              onClick={() => decide('APPROVED')}
            >
              Approve
            </button>
            <button
              type="button"
              className="btn btn-bad"
              disabled={action.busy}
              onClick={() => decide('REJECTED')}
            >
              Reject
            </button>
          </div>
        )}

        {canRetry && (
          <div className="row" style={{ marginTop: 10 }}>
            <button type="button" className="btn btn-primary" disabled={action.busy} onClick={retry}>
              Retry from the broken step
            </button>
          </div>
        )}
      </Card>

      {data.approval && (
        <Card title="Approval behind this run">
          <Row>
            <Badge value={data.approval.action_type} tone="info" />
            <Badge value={`${data.approval.proposed_discount_pct}% off`} tone="warn" />
            <Badge value={data.approval.status} />
            {data.approval.decided_by && (
              <span className="dim">
                decided by {data.approval.decided_by}
                {data.approval.decision_reason ? ` — ${data.approval.decision_reason}` : ''}
              </span>
            )}
          </Row>
        </Card>
      )}

      <Card title="Steps" subtitle="In order. A refused or retried call is evidence, not a log line.">
        <div className="steps">
          {data.steps.map((step) => (
            <Step key={step.id} step={step} />
          ))}
        </div>
      </Card>

      <Card title="Worker state" subtitle="The state the graph carried, as it stands now.">
        <pre>{JSON.stringify(data.state, null, 2)}</pre>
      </Card>
    </div>
  )
}

function Step({ step }) {
  const tone = {
    OK: 'ok',
    FAILED: 'failed',
    PAUSED: 'paused',
    DENIED: 'denied',
    SKIPPED: 'skipped',
  }[step.status]

  const model = step.model
  const tool = step.tool

  return (
    <div className={`step step-${tone || 'ok'}`}>
      <div className="step-head">
        <span className="dim">{step.index}.</span>
        <span className="step-node">{step.node}</span>
        <Badge value={step.status} />
        {tool && <Badge value={`tool: ${tool.name}`} tone={tool.status === 'DENIED' ? 'bad' : 'info'} />}
        {tool && tool.status !== 'SUCCESS' && <Badge value={tool.status} tone="warn" />}
        {model && (
          <Badge
            value={model.used === RULES_MODEL ? 'rule engine' : model.used}
            tone={model.used === RULES_MODEL ? 'mute' : 'info'}
          />
        )}
      </div>

      {step.summary && <div className="step-summary">{step.summary}</div>}

      <div className="step-facts">
        {tool && (
          <>
            <span>
              attempts <b>{tool.attempts}</b>
            </span>
            <span>
              authorized <b>{tool.authorized ? 'yes' : 'no'}</b>
            </span>
          </>
        )}
        {model && (
          <>
            <span>
              provider <b>{model.provider}</b>
            </span>
            <span>
              model <b>{model.used}</b>
            </span>
            <span>
              strategy <b>{STRATEGY_LABEL[model.strategy] || model.strategy}</b>
            </span>
            <span>
              tokens <b>{model.total_tokens}</b>
            </span>
            <span>
              cost <b>{fmtMoney(model.estimated_cost)}</b>
            </span>
            <span>
              latency <b>{model.latency_ms} ms</b>
            </span>
          </>
        )}
        <span>{fmtTime(step.created_at)}</span>
      </div>

      {step.error && (
        <Note kind="error" >
          {step.error}
        </Note>
      )}
      {tool?.error && <Note kind="error">{tool.error}</Note>}

      {(step.output || tool?.result) && (
        <details>
          <summary>Evidence</summary>
          <pre>{JSON.stringify({ output: step.output, tool_result: tool?.result }, null, 2)}</pre>
        </details>
      )}
    </div>
  )
}
