// While the user is editing inline (e.g. daily-note items), pause the global
// board poll so a refresh never re-renders/reverts the field mid-edit.
let _editLocks = 0
export const editLock = {
  acquire: () => { _editLocks += 1 },
  release: () => { _editLocks = Math.max(0, _editLocks - 1) },
  active: () => _editLocks > 0,
}

export async function getBoard() {
  const r = await fetch('/api/board')
  if (!r.ok) throw new Error(`board: ${r.status}`)
  return r.json()
}

export async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`)
  return r.json()
}

export const setSetting = (key, value) => post('/api/settings', { key, value })
export const toggleCheckbox = (date, line, checked) =>
  post('/api/today/checkbox', { date, line, checked })
export const snoozeItem = (id, hours) => post(`/api/items/${id}/snooze`, { hours })
export const bulkStatus = (ids, status) => post('/api/items/bulk-status', { ids, status })
export const wakeAllSnoozed = () => post('/api/items/wake-snoozed', {})
