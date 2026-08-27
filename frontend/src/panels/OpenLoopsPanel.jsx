import React, { useState } from 'react'
import { Play, Search, CheckCircle2 } from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'
import { useToast , decodeEntities } from '../ui.jsx'

const kindCls = {
  email_unanswered: { cls: 'bg-indigo-900 text-indigo-200', priority: 2 },
  silent_mention: { cls: 'bg-orange-900 text-orange-200', priority: 2 },
  pr_awaiting_me: { cls: 'bg-purple-900 text-purple-200', priority: 1 },
  my_pr_stuck: { cls: 'bg-sky-900 text-sky-200', priority: 3 },
  carried_checkbox: { cls: 'bg-yellow-900 text-yellow-200', priority: 4 },
}

const kindAction = {
  email_unanswered: 'Creates a follow-up draft',
  silent_mention: 'Creates a follow-up draft',
  pr_awaiting_me: 'Reviews the PR and posts findings',
  my_pr_stuck: 'Posts a gentle nudge comment on the PR',
  carried_checkbox: 'Opens as a reminder only',
}

const daysOld = (iso) => Math.floor((Date.now() - new Date(iso)) / 86400000)

const urgency = (l) => {
  const d = daysOld(l.received_at)
  const p = (kindCls[l.subject] || {}).priority || 5
  return p * 10 + Math.min(d, 30) // lower = more urgent
}

export default function OpenLoopsPanel({ board, refresh }) {
  const allLoops = [...(board.open_loops || [])].sort((a, b) => urgency(a) - urgency(b))
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('urgency')
  const filteredLoops = q
    ? allLoops.filter((l) => {
        const needle = q.toLowerCase()
        return (l.content || '').toLowerCase().includes(needle)
          || (l.subject || '').toLowerCase().includes(needle)
      })
    : allLoops
  const loops = [...filteredLoops].sort((a, b) => {
    if (sort === 'age') return daysOld(b.received_at) - daysOld(a.received_at)
    if (sort === 'type') return (a.subject || '').localeCompare(b.subject || '')
    return urgency(a) - urgency(b)
  })
  const [unchecked, setUnchecked] = useState(() => new Set())
  const [running, setRunning] = useState(false)
  const [runningId, setRunningId] = useState(null)
  const [doneId, setDoneId] = useState(null)
  const [results, setResults] = useState(null)
  const toast = useToast()

  const toggle = (id) =>
    setUnchecked((s) => {
      const n = new Set(s)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })
  const selected = loops.filter((l) => !unchecked.has(l.id)).map((l) => l.id)

  const toggleAll = () => {
    if (unchecked.size === 0) {
      setUnchecked(new Set(loops.map((l) => l.id)))
    } else {
      setUnchecked(new Set())
    }
  }

  const runNow = async () => {
    setRunning(true)
    setResults(null)
    try {
      const r = await post('/api/loops/run', { ids: selected })
      setResults(r.results)
      refresh?.()
    } catch (e) {
      setResults([{ id: 0, kind: 'error', result: String(e) }])
    }
    setRunning(false)
  }

  const runOne = async (id, label) => {
    setRunningId(id)
    try {
      const r = await post('/api/loops/run', { ids: [id] })
      const res = r.results?.[0]
      if (res) {
        const ok = !res.result.startsWith('failed') && !res.result.startsWith('blocked')
        toast(`${label}: ${res.result.slice(0, 100)}`, ok ? 'success' : 'error')
      }
      refresh?.()
    } catch (e) {
      toast(`Run failed: ${String(e).slice(0, 100)}`, 'error')
    }
    setRunningId(null)
  }

  const markDone = async (id) => {
    setDoneId(id)
    try {
      await post(`/api/items/${id}/status`, { status: 'done' })
      refresh?.()
    } catch (e) {
      toast(`Could not mark done: ${String(e).slice(0, 100)}`, 'error')
    }
    setDoneId(null)
  }

  const labelFor = (id) => {
    const l = allLoops.find((x) => x.id === id)
    return l ? l.content.slice(0, 60) : `#${id}`
  }

  return (
    <Panel
      title="Open loops"
      badge={allLoops.length}
      actions={
        allLoops.length > 0 && (
          <div className="flex items-center gap-2">
            <div className="flex gap-0.5">
              {['urgency', 'age', 'type'].map(s => (
                <button
                  key={s}
                  onClick={() => setSort(s)}
                  className={`rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
                    sort === s ? 'bg-zinc-600 text-zinc-100' : 'bg-zinc-800 text-zinc-500 hover:text-zinc-300'
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>
            <div className="relative">
              <Search size={11} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-zinc-500" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="filter…"
                className="h-6 w-28 rounded bg-zinc-800 pl-6 pr-2 text-[11px] text-zinc-300 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
              />
            </div>
            <button
              onClick={toggleAll}
              className="press rounded bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400 hover:text-zinc-200"
            >
              {unchecked.size === 0 ? 'none' : 'all'}
            </button>
            <button
              onClick={runNow}
              disabled={running || selected.length === 0}
              className="press rounded bg-emerald-800 px-3 py-1 text-sm text-emerald-100 hover:bg-emerald-700 disabled:opacity-40"
            >
              {running ? 'running…' : `Act on ${selected.length}`}
            </button>
          </div>
        )
      }
    >
      <div className="space-y-1">
        {allLoops.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1">
            {Object.entries(
              allLoops.reduce((acc, l) => {
                const k = (l.subject || 'other').replaceAll('_', ' ')
                acc[k] = (acc[k] || 0) + 1
                return acc
              }, {})
            ).map(([type, count]) => (
              <span key={type} className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">
                {count} {type}
              </span>
            ))}
          </div>
        )}
        {allLoops.length === 0 && <div className="text-sm text-zinc-500">no loops open — all clear</div>}
        {allLoops.length > 0 && loops.length === 0 && (
          <div className="text-sm text-zinc-500">no loops match "{q}"</div>
        )}
        {results && (
          <div className="mb-2 space-y-0.5 rounded bg-zinc-950 p-2 text-xs">
            {results.map((r, i) => (
              <div key={i}>
                <span className="text-zinc-400">{labelFor(r.id)}</span>{' '}
                <span className={r.result.startsWith('failed') || r.result.startsWith('blocked')
                  ? 'text-red-300' : 'text-emerald-300'}>{r.result}</span>
              </div>
            ))}
          </div>
        )}
        {(() => {
          let prevType = null
          return loops.map((l) => {
          const d = daysOld(l.received_at)
          const k = kindCls[l.subject] || { cls: 'bg-zinc-800 text-zinc-400' }
          const action = kindAction[l.subject] || 'Handled manually'
          const borderCls = d >= 7 ? 'border-red-900/50' : d >= 3 ? 'border-amber-900/40' : 'border-zinc-800'
          const ageCls = d >= 7 ? 'text-red-400 font-bold' : d >= 3 ? 'text-amber-400' : 'text-zinc-500'
          const ageIndicator = d >= 7 ? '🔴' : d >= 3 ? '⚠' : null
          const showGroupHeader = sort === 'type' && prevType !== l.subject
          prevType = l.subject
          return (
            <React.Fragment key={l.id}>
              {showGroupHeader && (
                <div className="mt-2 mb-0.5 text-[9px] font-semibold uppercase tracking-widest text-zinc-600">
                  {(l.subject || 'loop').replaceAll('_', ' ')}
                </div>
              )}
            <div
              className={`flex items-start gap-2 rounded-lg border px-2 py-1.5 text-sm ${borderCls} bg-zinc-900`}
            >
              <input
                type="checkbox"
                checked={!unchecked.has(l.id)}
                onChange={() => toggle(l.id)}
                disabled={running}
                className="mt-0.5 accent-emerald-600"
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className={`shrink-0 rounded px-1.5 text-[10px] ${k.cls}`}>
                    {(l.subject || 'loop').replaceAll('_', ' ')}
                  </span>
                  <span className="truncate text-zinc-300" title={decodeEntities(l.content)}>
                    {decodeEntities(l.content)}
                  </span>
                </div>
                <div className="mt-0.5 text-[10px] text-zinc-600">{action}</div>
              </div>
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                <button
                  title="Mark this loop as done manually"
                  disabled={running || doneId === l.id}
                  onClick={(e) => { e.stopPropagation(); markDone(l.id) }}
                  className="press grid h-5 w-5 place-items-center rounded bg-zinc-800 text-zinc-500 hover:bg-emerald-900/60 hover:text-emerald-300 disabled:opacity-40"
                >
                  {doneId === l.id
                    ? <span className="h-2.5 w-2.5 animate-spin rounded-full border border-zinc-500 border-t-transparent" />
                    : <CheckCircle2 size={12} />
                  }
                </button>
                <span
                  className={`text-[11px] ${ageCls}`}
                  title={`Opened ${l.received_at}`}
                >
                  {ageIndicator && <span className="mr-0.5 text-[9px]">{ageIndicator}</span>}{d}d
                </span>
                <button
                  title={`${action} (run just this one)`}
                  disabled={running || runningId === l.id}
                  onClick={(e) => { e.stopPropagation(); runOne(l.id, l.subject) }}
                  className="press grid h-5 w-5 place-items-center rounded bg-emerald-900/40 text-emerald-400 hover:bg-emerald-800/60 hover:text-emerald-200 disabled:opacity-40"
                >
                  {runningId === l.id
                    ? <span className="h-2.5 w-2.5 animate-spin rounded-full border border-emerald-400 border-t-transparent" />
                    : <Play size={10} />
                  }
                </button>
              </div>
            </div>
            </React.Fragment>
          )
          })
        })()}
        {loops.length > 0 && (
          <p className="pt-1 text-[10px] text-zinc-600">
            PR reviews post findings to GitHub + Teams chat. Email/mention drafts go to Approvals.
          </p>
        )}
      </div>
    </Panel>
  )
}
