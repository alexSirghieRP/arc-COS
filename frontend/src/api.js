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
