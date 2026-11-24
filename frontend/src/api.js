const OWNER_KEY = 'fw_owner'
const BASE = '/api'

export function getOwnerName() {
  try {
    return localStorage.getItem(OWNER_KEY) || 'Owner'
  } catch {
    return 'Owner'
  }
}

export function setOwnerName(name) {
  try {
    localStorage.setItem(OWNER_KEY, name)
  } catch {}
}

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.data = data
  }
}

function messageFrom(data, status) {
  if (data && typeof data === 'object') {
    if (typeof data.error === 'string' && data.error) return data.error
    if (typeof data.detail === 'string' && data.detail) return data.detail
  }
  if (typeof data === 'string' && data.trim()) return data
  return `Request failed (${status})`
}

async function request(path, { method = 'GET', body } = {}) {
  const options = { method, headers: { 'X-Owner-Name': getOwnerName() } }
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(body)
  }

  let response
  try {
    response = await fetch(BASE + path, options)
  } catch {
    throw new ApiError('Cannot reach the backend on :8000. Is it running?', 0, null)
  }

  const contentType = response.headers.get('content-type') || ''
  const data = contentType.includes('application/json')
    ? await response.json().catch(() => null)
    : await response.text().catch(() => null)

  if (!response.ok) throw new ApiError(messageFrom(data, response.status), response.status, data)
  return data
}

export const api = {
  meta: () => request('/meta/'),
  dashboard: () => request('/dashboard/'),

  customers: (eligibleOnly = false) =>
    request(`/customers/${eligibleOnly ? '?eligible=1' : ''}`),
  customer: (id) => request(`/customers/${id}/`),
  startFollowUp: (id) => request(`/customers/${id}/follow-up/`, { method: 'POST', body: {} }),
  sendReply: (id, reply) =>
    request(`/customers/${id}/reply/`, { method: 'POST', body: { reply } }),
  recordPurchase: (id, payload) =>
    request(`/customers/${id}/purchase/`, { method: 'POST', body: payload }),

  runs: (params = {}) => {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== '' && v !== null),
    ).toString()
    return request(`/runs/${query ? `?${query}` : ''}`)
  },
  run: (runId) => request(`/runs/${runId}/`),
  decideRun: (runId, decision, reason) =>
    request(`/runs/${runId}/decision/`, { method: 'POST', body: { decision, reason } }),
  retryRun: (runId) => request(`/runs/${runId}/retry/`, { method: 'POST', body: {} }),

  approvals: (status) => request(`/approvals/${status ? `?status=${status}` : ''}`),
  decideApproval: (id, decision, reason) =>
    request(`/approvals/${id}/decide/`, { method: 'POST', body: { decision, reason } }),

  simulation: () => request('/simulation/'),
  setSimulation: (payload) => request('/simulation/', { method: 'POST', body: payload }),
  seed: (reset = false) => request('/demo/seed/', { method: 'POST', body: { reset } }),
  reset: () => request('/demo/reset/', { method: 'POST', body: {} }),
}
