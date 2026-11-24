import { useState } from 'react'
import { api } from '../api'
import { Badge, Card, Empty, Note, useAction, useAsync } from '../ui'

export default function Simulation({ onChanged }) {
  const meta = useAsync(() => api.meta(), [])
  const sim = useAsync(() => api.simulation(), [])
  const action = useAction()
  const [failures, setFailures] = useState('')
  const [latency, setLatency] = useState('')

  const config = sim.data

  async function apply(patch, message) {
    const result = await action.run(() => api.setSimulation(patch), message)
    if (result) {
      onChanged(message)
      sim.reload()
    }
  }

  async function seed(reset) {
    const summary = await action.run(
      () => api.seed(reset),
      reset ? 'Demo data reset and reloaded.' : 'Demo data loaded.',
    )
    if (summary) {
      onChanged(
        `${summary.customers_total} customers on record · ${summary.note}`,
      )
    }
  }

  async function reset() {
    const result = await action.run(
      () => api.reset(),
      'All synthetic data, runs and checkpoints deleted.',
    )
    if (result) {
      onChanged(`Reset: ${result.runs ?? 0} runs, ${result.customers ?? 0} customers deleted.`)
    }
  }

  return (
    <div className="stack">
      <Note kind="error" onDismiss={action.clear}>
        {action.error}
      </Note>
      <Note kind="success" onDismiss={action.clear}>
        {action.message}
      </Note>

      <Card
        title="Messaging outage"
        subtitle="Inject a real failure into the simulated provider, then watch the worker retry, fail, and recover from its checkpoint."
      >
        {config ? (
          <>
            <div className="metrics">
              <div className="metric">
                <span className="metric-label">Failures remaining</span>
                <strong className="metric-value">{config.messaging_failures_remaining}</strong>
                <span className="metric-hint">
                  Next {config.messaging_failures_remaining} send(s) raise an outage
                </span>
              </div>
              <div className="metric">
                <span className="metric-label">Provider latency</span>
                <strong className="metric-value">{config.messaging_latency_ms} ms</strong>
                <span className="metric-hint">Delay per send</span>
              </div>
              <div className="metric">
                <span className="metric-label">Tool attempts</span>
                <strong className="metric-value">{config.tool_max_attempts}</strong>
                <span className="metric-hint">
                  Retries before the tool gives up and the run fails
                </span>
              </div>
            </div>

            <div className="row" style={{ marginTop: 14 }}>
              <input
                placeholder="Failures (e.g. 3)"
                style={{ width: 140 }}
                value={failures}
                onChange={(event) => setFailures(event.target.value)}
              />
              <input
                placeholder="Latency ms"
                style={{ width: 140 }}
                value={latency}
                onChange={(event) => setLatency(event.target.value)}
              />
              <button
                type="button"
                className="btn btn-primary"
                disabled={action.busy}
                onClick={() =>
                  apply(
                    {
                      messaging_failures_remaining: Number(failures || 0),
                      messaging_latency_ms: Number(latency || 0),
                    },
                    'Outage injected. Run the worker for an eligible customer and it will fail.',
                  )
                }
              >
                Apply
              </button>
              <button
                type="button"
                className="btn"
                disabled={action.busy}
                onClick={() => apply({ reset: true }, 'Provider restored to normal.')}
              >
                Clear outage
              </button>
              <button
                type="button"
                className="btn"
                disabled={action.busy || !failures}
                onClick={() =>
                  apply(
                    { messaging_failures_remaining: Math.max(0, config.tool_max_attempts) },
                    `Injected ${config.tool_max_attempts} failures — exactly enough to exhaust the retries.`,
                  )
                }
              >
                One full retry cycle
              </button>
            </div>

            <p className="sub" style={{ marginTop: 10 }}>
              Set failures higher than {config.tool_max_attempts} and the run ends FAILED with a
              checkpoint. Clear the outage, open <b>Runs &amp; trace</b>, and hit{' '}
              <b>Retry from the broken step</b> — the resume happens in a fresh request, from
              durable state, with no duplicate message.
            </p>
          </>
        ) : (
          <Empty>Loading…</Empty>
        )}
      </Card>

      <Card
        title="Synthetic dataset"
        subtitle="Nine customers, each one exercising a different rule. No real data anywhere."
      >
        <div className="chips" style={{ marginBottom: 12 }}>
          <button type="button" className="btn" disabled={action.busy} onClick={() => seed(false)}>
            Load demo data
          </button>
          <button
            type="button"
            className="btn btn-bad"
            disabled={action.busy}
            onClick={() => seed(true)}
          >
            Reset &amp; reload
          </button>
          <button type="button" className="btn" disabled={action.busy} onClick={reset}>
            Delete everything
          </button>
        </div>
        <p className="sub">
          Reset deletes runs, approvals, checkpoints and interactions, then rebuilds the same
          dataset — so a scenario can be replayed from the same starting point.
        </p>
      </Card>

      {meta.data && (
        <>
          <div className="split-narrow">
            <Card title="Tools" subtitle="The only ways the worker can cause a side effect.">
              <table className="table">
                <thead>
                  <tr>
                    <th>Tool</th>
                    <th>Read only</th>
                    <th>Retryable</th>
                  </tr>
                </thead>
                <tbody>
                  {meta.data.tools.map((tool) => (
                    <tr key={tool.name}>
                      <td>
                        <span className="mono">{tool.name}</span>
                        <div className="dim">{tool.description}</div>
                      </td>
                      <td>{tool.read_only ? 'yes' : 'no'}</td>
                      <td>{tool.retryable ? 'yes' : 'no'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>

            <Card title="Harness" subtitle="Where durable state lives.">
              <dl className="kv">
                <dt>Checkpointer</dt>
                <dd className="mono">
                  {meta.data.harness.checkpointer.type} · {meta.data.harness.checkpointer.path}
                </dd>
                <dt>Recursion limit</dt>
                <dd>{meta.data.harness.recursion_limit}</dd>
                <dt>Tool attempts</dt>
                <dd>{meta.data.harness.tool_max_attempts}</dd>
                <dt>Provider</dt>
                <dd>{meta.data.routing.provider}</dd>
                <dt>Live inference</dt>
                <dd>
                  <Badge
                    value={meta.data.routing.live ? 'yes' : 'no'}
                    tone={meta.data.routing.live ? 'ok' : 'warn'}
                  />
                </dd>
              </dl>
            </Card>
          </div>

          <Card
            title="Model routing"
            subtitle="Which model handles which task — and why. Policy decisions never reach a model."
          >
            <table className="table">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Tier</th>
                  <th>Model</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(meta.data.routing.tasks).map(([task, spec]) => (
                  <tr key={task}>
                    <td className="mono">{task}</td>
                    <td>
                      <Badge
                        value={spec.tier}
                        tone={spec.tier === 'rules' ? 'warn' : 'info'}
                      />
                    </td>
                    <td className="mono">{spec.model}</td>
                    <td className="dim">{spec.why}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <Card title="Policy the worker must obey" subtitle="Read from settings, not from prose.">
            <dl className="kv">
              <dt>Eligibility</dt>
              <dd>
                Inactive between {meta.data.policy.eligibility.min_inactive_days} and{' '}
                {meta.data.policy.eligibility.max_inactive_days} days
                {meta.data.policy.eligibility.requires_marketing_consent
                  ? ', with marketing consent on record'
                  : ''}
                .
              </dd>
              <dt>Approval</dt>
              <dd>{meta.data.policy.approval.rule}</dd>
              <dt>Autonomous limit</dt>
              <dd>{meta.data.policy.approval.max_autonomous_discount_pct}%</dd>
              <dt>Proposable limit</dt>
              <dd>{meta.data.policy.approval.max_proposable_discount_pct}%</dd>
            </dl>
            <ul className="stack-sm" style={{ marginTop: 10, paddingLeft: 18 }}>
              {meta.data.policy.guardrails.map((rule) => (
                <li key={rule} className="dim">
                  {rule}
                </li>
              ))}
            </ul>
          </Card>
        </>
      )}
    </div>
  )
}
