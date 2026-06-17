import React, { useState } from 'react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'

const kindCls = {
  email_unanswered: 'bg-indigo-900 text-indigo-200',
  silent_mention: 'bg-orange-900 text-orange-200',
  pr_awaiting_me: 'bg-purple-900 text-purple-200',
  my_pr_stuck: 'bg-sky-900 text-sky-200',
  carried_checkbox: 'bg-yellow-900 text-yellow-200',
}

const days = (iso) => Math.floor((Date.now() - new Date(iso)) / 86400000)

export default function OpenLoopsPanel({ board }) {
  const loops = board.open_loops || []
  // all checked by default; we only track the ones the user unchecks
  const [unchecked, setUnchecked] = useState(() => new Set())
  const [running, setRunning] = useState(false)
  const [results, setResults] = useState(null)

  const toggle = (id) =>
    setUnchecked((s) => {
      const n = new Set(s)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })
  const selected = loops.filter((l) => !unchecked.has(l.id)).map((l) => l.id)

  const runNow = async () => {
    setRunning(true)
    setResults(null)
    try {
      const r = await post('/api/loops/run', { ids: selected })
      setResults(r.results)
    } catch (e) {
      setResults([{ id: 0, kind: 'error', result: String(e) }])
    }
    setRunning(false)
  }

  const labelFor = (id) => {
    const l = loops.find((x) => x.id === id)
    return l ? l.content.slice(0, 60) : `#${id}`
  }

  return (
    <Panel title="Open loops" badge={loops.length}>
      <div className="space-y-1">
        {loops.length > 0 && (
          <div className="mb-2 flex items-center gap-3">
            <button
              onClick={runNow}
              disabled={running || selected.length === 0}
              className="rounded bg-emerald-800 px-3 py-1 text-sm text-emerald-100 hover:bg-emerald-700 disabled:opacity-40"
            >
              {running ? 'running…' : `Run ${selected.length} now`}
            </button>
            <span className="text-xs text-zinc-500">
              PRs awaiting you get reviewed, stuck PRs get a nudge, silent emails/mentions get a
              draft in Approvals. Uncheck to leave out.
            </span>
          </div>
        )}
        {results && (
          <div className="mb-2 space-y-0.5 rounded bg-zinc-900 p-2 text-xs">
            {results.map((r, i) => (
              <div key={i}>
                <span className="text-zinc-400">{labelFor(r.id)}</span>{' '}
                <span className="text-emerald-300">{r.result}</span>
              </div>
            ))}
          </div>
        )}
        {loops.length === 0 && <div className="text-sm text-zinc-500">no loops open</div>}
        {loops.map((l) => (
          <div key={l.id} className="flex items-center gap-2 rounded bg-zinc-900 px-2 py-1 text-sm">
            <input
              type="checkbox"
              checked={!unchecked.has(l.id)}
              onChange={() => toggle(l.id)}
              disabled={running}
              className="accent-emerald-600"
            />
            <span className={`shrink-0 rounded px-1.5 text-[10px] ${kindCls[l.subject] || 'bg-zinc-800'}`}>
              {(l.subject || 'loop').replaceAll('_', ' ')}
            </span>
            <span className="truncate" title={l.content}>{l.content}</span>
            <span className="ml-auto shrink-0 text-[11px] text-zinc-500">
              {l.received_at ? `${days(l.received_at)}d` : ''}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  )
}
