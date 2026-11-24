import { useCallback, useEffect, useState } from 'react'

const TONES = {
  ok: 'badge badge-ok',
  warn: 'badge badge-warn',
  bad: 'badge badge-bad',
  info: 'badge badge-info',
  mute: 'badge badge-mute',
}

const STATUS_TONE = {
  COMPLETED: 'ok',
  SENT: 'ok',
  CONVERTED: 'ok',
  APPROVED: 'ok',
  SUCCESS: 'ok',
  OK: 'ok',
  ACTIVE: 'ok',
  POSITIVE: 'ok',

  RUNNING: 'info',
  PENDING: 'info',
  REACTIVATED: 'info',
  RETRIED: 'info',
  PAUSED: 'info',
  RETRYING: 'info',

  AWAITING_APPROVAL: 'warn',
  ESCALATED: 'warn',
  QUESTION: 'warn',
  LAPSED: 'warn',
  SKIPPED: 'warn',
  DENIED: 'warn',
  NOT_ELIGIBLE: 'warn',

  FAILED: 'bad',
  REJECTED: 'bad',
  DECLINED: 'bad',
  NEGATIVE: 'bad',
  OPTED_OUT: 'bad',
  OUTAGE: 'bad',

  OPTED_IN: 'mute',
}

export function Badge({ value, tone }) {
  if (value === null || value === undefined || value === '') return null
  const resolved = tone || STATUS_TONE[value] || 'mute'
  return <span className={TONES[resolved]}>{String(value).replace(/_/g, ' ')}</span>
}

export function Card({ title, subtitle, right, children, className = '' }) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <header className="card-head">
          <div>
            {title && <h3>{title}</h3>}
            {subtitle && <p className="sub">{subtitle}</p>}
          </div>
          {right}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  )
}

export function Metric({ label, value, hint, tone }) {
  return (
    <div className={`metric ${tone ? `metric-${tone}` : ''}`}>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {hint && <span className="metric-hint">{hint}</span>}
    </div>
  )
}

export function Note({ kind = 'info', children, onDismiss }) {
  if (!children) return null
  return (
    <div className={`note note-${kind}`}>
      <span>{children}</span>
      {onDismiss && (
        <button type="button" className="note-close" onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      )}
    </div>
  )
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>
}

export function Row({ children }) {
  return <div className="row">{children}</div>
}

export function useAsync(load, deps = []) {
  const [state, setState] = useState({ data: null, error: '', loading: true })
  const [nonce, setNonce] = useState(0)

  const run = useCallback(() => {
    let cancelled = false
    setState((prev) => ({ ...prev, loading: true, error: '' }))
    Promise.resolve()
      .then(load)
      .then((data) => {
        if (!cancelled) setState({ data, error: '', loading: false })
      })
      .catch((error) => {
        if (!cancelled) setState({ data: null, error: error.message, loading: false })
      })
    return () => {
      cancelled = true
    }
  }, deps)

  useEffect(run, [run, nonce])
  return { ...state, reload: () => setNonce((n) => n + 1) }
}

export function useAction() {
  const [state, setState] = useState({ busy: false, error: '', message: '' })

  const run = useCallback(async (fn, successMessage) => {
    setState({ busy: true, error: '', message: '' })
    try {
      const result = await fn()
      setState({ busy: false, error: '', message: successMessage || '' })
      return result
    } catch (error) {
      setState({ busy: false, error: error.message, message: '' })
      return null
    }
  }, [])

  return { ...state, run, clear: () => setState({ busy: false, error: '', message: '' }) }
}

export function fmtMoney(value) {
  const number = Number(value || 0)
  if (number === 0) return '$0.00'
  if (number < 0.01) return `$${number.toFixed(6)}`
  return `$${number.toFixed(4)}`
}

export function fmtTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function ago(iso) {
  if (!iso) return '—'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}
