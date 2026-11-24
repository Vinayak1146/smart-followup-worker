import { useEffect, useState } from 'react'
import { api, getOwnerName, setOwnerName } from './api'
import { Badge, Note, useAsync } from './ui'
import Dashboard from './views/Dashboard'
import Customers from './views/Customers'
import Approvals from './views/Approvals'
import Runs from './views/Runs'
import Simulation from './views/Simulation'

const TABS = [
  { id: 'dashboard', label: 'Goal' },
  { id: 'customers', label: 'Customers' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'runs', label: 'Runs & trace' },
  { id: 'simulation', label: 'Simulation' },
]

export default function App() {
  const meta = useAsync(() => api.meta(), [])
  const [tab, setTab] = useState('dashboard')
  const [customerId, setCustomerId] = useState(null)
  const [runId, setRunId] = useState(null)
  const [flash, setFlash] = useState('')
  const [nonce, setNonce] = useState(0)
  const [owner, setOwner] = useState(getOwnerName())

  useEffect(() => {
    setOwnerName(owner)
  }, [owner])

  function notify(message) {
    setFlash(message)
    setNonce((n) => n + 1)
  }

  function openCustomer(id) {
    setCustomerId(id)
    setTab('customers')
  }

  function openRun(id) {
    setRunId(id)
    setTab('runs')
  }

  const view = {
    dashboard: <Dashboard onChanged={notify} onOpenCustomer={openCustomer} />,
    customers: (
      <Customers
        selectedId={customerId}
        onSelect={setCustomerId}
        onChanged={notify}
        onOpenRun={openRun}
      />
    ),
    approvals: <Approvals onChanged={notify} onOpenRun={openRun} />,
    runs: <Runs focusRunId={runId} onChanged={notify} />,
    simulation: <Simulation onChanged={notify} />,
  }[tab]

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <strong>Smart Customer Follow-up Worker</strong>
          <span className="sub">
            {meta.data?.goal || 'One worker, one goal: bring a lapsed customer back.'}
          </span>
        </div>
        <div className="topbar-right">
          {meta.data && (
            <span className={`live ${meta.data.routing.live ? 'live-on' : 'live-off'}`}>
              {meta.data.routing.live
                ? `live · ${meta.data.routing.provider}`
                : 'no provider · rule engine'}
            </span>
          )}
          <label className="owner">
            <span>Acting as</span>
            <input
              value={owner}
              onChange={(event) => setOwner(event.target.value)}
              placeholder="Owner"
              maxLength={120}
            />
          </label>
        </div>
      </header>

      <nav className="tabs">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={item.id === tab ? 'tab tab-active' : 'tab'}
            onClick={() => {
              setTab(item.id)
              if (item.id !== 'customers') setCustomerId(null)
              if (item.id !== 'runs') setRunId(null)
            }}
          >
            {item.label}
          </button>
        ))}
        <span className="tabs-spacer" />
        {meta.data && (
          <>
            <Badge value={`${meta.data.tools.length} tools`} tone="info" />
            <Badge value={meta.data.harness.checkpointer.type} tone="info" />
          </>
        )}
      </nav>

      <main className="content">
        <Note kind="success" onDismiss={() => setFlash('')}>
          {flash}
        </Note>
        <Note kind="error">{meta.error}</Note>
        <div key={`${tab}-${nonce}`} className="view">
          {view}
        </div>
      </main>
    </div>
  )
}
