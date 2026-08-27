import React, { useEffect, useState } from 'react'
import {
  RefreshCw, Search, ExternalLink, CircleDot, CircleDashed, Eye, CheckCircle2, Circle,
  SlidersHorizontal, LayoutGrid,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'

const bucketMeta = {
  'In progress': { icon: CircleDot, color: 'text-sky-400' },
  'In review': { icon: Eye, color: 'text-purple-400' },
  'To do': { icon: CircleDashed, color: 'text-zinc-500' },
  Done: { icon: CheckCircle2, color: 'text-emerald-400' },
  Other: { icon: Circle, color: 'text-zinc-500' },
}
const typeColor = {
  Epic: 'text-orange-300',
  Feature: 'text-purple-300',
  'User Story': 'text-sky-300',
  Bug: 'text-red-300',
  Task: 'text-zinc-400',
}
const order = ['In progress', 'In review', 'To do', 'Done', 'Other']
const TYPE_ORDER = ['Epic', 'Feature', 'User Story', 'Bug', 'Task']
// Optional shortening for long ADO project names (real project name → tab label).
const TAB_LABEL = {}
const tabLabel = (id) => TAB_LABEL[id] || id

export default function AdoPanel() {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('')
  const [project, setProject] = useState(null)
  const [typeFilter, setTypeFilter] = useState(null)
  const [boards, setBoards] = useState([])
  const [boardBusy, setBoardBusy] = useState(null)
  const [showBoards, setShowBoards] = useState(false)
  const [resweeping, setResweeping] = useState(false)

  const load = (refresh) => {
    if (refresh) setBusy(true)
    return fetch(`/api/ado-items${refresh ? '?refresh=1' : ''}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => {})
      .finally(() => setBusy(false))
  }
  const loadBoards = () =>
    fetch('/api/ado-projects')
      .then((r) => r.json())
      .then((d) => setBoards(Array.isArray(d) ? d : []))
      .catch(() => setBoards([]))

  useEffect(() => {
    load()
    loadBoards()
    const t = setInterval(load, 300000)
    return () => clearInterval(t)
  }, [])

  const enabledBoards = boards.filter((b) => b.enabled)
  // Keep the active tab pointed at a watched board (switches away the moment
  // the current one is turned off, and picks the first watched board on load).
  useEffect(() => {
    if (enabledBoards.length === 0) return
    if (!enabledBoards.some((b) => b.name === project)) setProject(enabledBoards[0].name)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boards])

  const toggleBoard = async (b) => {
    setBoardBusy(b.name)
    try {
      await post('/api/ado-projects', { project: b.name, enabled: !b.enabled })
      await loadBoards()
      setResweeping(true)
      await load(true)
    } finally {
      setBoardBusy(null)
      setResweeping(false)
    }
  }

  const all = data?.items || []
  const projectItems = all.filter((i) => i.project === project)
  const typeCounts = TYPE_ORDER.reduce((acc, t) => {
    const n = projectItems.filter((i) => i.type === t).length
    if (n) acc[t] = n
    return acc
  }, {})
  const typeTabs = TYPE_ORDER.filter((t) => typeCounts[t])
  const items = projectItems
    .filter((i) => !typeFilter || i.type === typeFilter)
    .filter(
      (i) => !filter || `${i.id} ${i.title} ${i.state} ${i.project}`.toLowerCase().includes(filter.toLowerCase()),
    )
  const counts = order.reduce((acc, b) => {
    const n = projectItems.filter((i) => i.bucket === b).length
    if (n) acc[b] = n
    return acc
  }, {})
  const buckets = order.filter((b) => items.some((i) => i.bucket === b))

  // Reset the type filter when it no longer applies to the active board
  // (switching boards, or the board's data reloading with different types).
  useEffect(() => {
    if (typeFilter && !typeCounts[typeFilter]) setTypeFilter(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project, data])

  return (
    <Panel
      title="My ADO items"
      badge={data ? projectItems.length : '·'}
      actions={
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowBoards((s) => !s)}
            className={`press flex items-center gap-1 rounded-lg px-2.5 py-1 text-[11px] ${
              showBoards ? 'bg-zinc-700 text-zinc-100' : 'bg-zinc-800 text-zinc-300 hover:bg-zinc-700'
            }`}
          >
            <SlidersHorizontal size={12} /> Boards ({enabledBoards.length}/{boards.length})
          </button>
          <RefreshButton busy={busy} onClick={() => load(true)} title="Refresh from Azure DevOps" />
        </div>
      }
    >
      {showBoards && (
        <div className="animate-fade-in mb-3 rounded-xl border border-zinc-800 bg-zinc-950 p-3">
          <p className="mb-2 text-[11px] text-zinc-500">
            Only boards toggled on here are polled and shown as tabs. Turning one off{' '}
            {resweeping ? '— refreshing…' : 'stops watching it immediately.'}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {boards.map((b) => (
              <button
                key={b.name}
                onClick={() => toggleBoard(b)}
                disabled={boardBusy === b.name}
                title={b.enabled ? 'Watched — click to stop watching' : 'Not watched — click to watch'}
                className={`press flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] disabled:opacity-40 ${
                  b.enabled
                    ? 'border-[var(--accent-strong)] bg-[var(--accent-fill)] text-zinc-100'
                    : 'border-zinc-800 bg-zinc-900 text-zinc-500'
                }`}
              >
                <LayoutGrid size={11} className="shrink-0" />
                {tabLabel(b.name)}
              </button>
            ))}
            {boards.length === 0 && <span className="text-[11px] text-zinc-600">no boards configured</span>}
          </div>
        </div>
      )}

      <div className="mb-3 flex gap-0.5 rounded-lg bg-zinc-950 p-0.5">
        {enabledBoards.map((t) => {
          const n = all.filter((i) => i.project === t.name).length
          const active = project === t.name
          return (
            <button
              key={t.name}
              onClick={() => setProject(t.name)}
              className={`press flex-1 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors ${active ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300'}`}
            >
              {tabLabel(t.name)}
              <span className="ml-1.5 tabular-nums text-zinc-600">{n}</span>
            </button>
          )
        })}
        {enabledBoards.length === 0 && (
          <div className="flex-1 px-2.5 py-1.5 text-center text-xs text-zinc-600">
            No boards watched — enable one above
          </div>
        )}
      </div>

      {typeTabs.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          <button
            onClick={() => setTypeFilter(null)}
            className={`press rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
              !typeFilter ? 'bg-zinc-700 text-zinc-100' : 'bg-zinc-900 text-zinc-500 hover:text-zinc-300'
            }`}
          >
            All <span className="ml-1 tabular-nums opacity-70">{projectItems.length}</span>
          </button>
          {typeTabs.map((t) => (
            <button
              key={t}
              onClick={() => setTypeFilter(typeFilter === t ? null : t)}
              className={`press rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
                typeFilter === t ? 'bg-zinc-700 text-zinc-100' : 'bg-zinc-900 text-zinc-500 hover:text-zinc-300'
              }`}
            >
              <span className={typeFilter === t ? typeColor[t] : ''}>{t}</span>
              <span className="ml-1 tabular-nums opacity-70">{typeCounts[t]}</span>
            </button>
          ))}
        </div>
      )}

      <div className="mb-3 flex items-center gap-2">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
          <input
            className="w-full rounded-lg border border-zinc-800 bg-zinc-950 py-1.5 pl-8 pr-3 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-[var(--accent)]"
            placeholder="Filter items"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <div className="flex gap-1.5">
          {order
            .filter((b) => counts[b])
            .map((b) => {
              const M = bucketMeta[b] || bucketMeta.Other
              return (
                <span key={b} className="flex items-center gap-1 rounded-md bg-zinc-800 px-2 py-1 text-[11px] text-zinc-300">
                  <M.icon size={12} className={M.color} />
                  <span className="tabular-nums">{counts[b]}</span>
                </span>
              )
            })}
        </div>
      </div>

      {data?.error && <Empty text={data.error} />}
      {data &&
        buckets.map((b) => {
          const M = bucketMeta[b] || bucketMeta.Other
          const group = items.filter((i) => i.bucket === b)
          return (
            <div key={b} className="mb-4 last:mb-0">
              <h3 className="mb-1.5 flex items-center gap-1.5 px-1 text-[11px] font-semibold uppercase tracking-[0.1em] text-zinc-500">
                <M.icon size={13} className={M.color} />
                {b}
                <span className="text-zinc-600">{group.length}</span>
              </h3>
              <div className="stagger space-y-1">
                {group.map((i) => (
                  <a
                    key={i.id}
                    href={i.url}
                    target="_blank"
                    rel="noreferrer"
                    className="row-hover group flex items-center gap-2.5 rounded-lg border border-transparent px-2.5 py-2 text-sm hover:border-zinc-800"
                  >
                    <span className={`shrink-0 text-[10px] font-semibold uppercase tracking-wide ${typeColor[i.type] || 'text-zinc-400'}`}>
                      {i.type}
                    </span>
                    <span className="shrink-0 text-[11px] tabular-nums text-zinc-600">#{i.id}</span>
                    <span className="truncate text-zinc-200">{i.title}</span>
                    <span className="ml-auto shrink-0 rounded-md bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">{i.state}</span>
                    <ExternalLink size={13} className="shrink-0 text-zinc-700 opacity-0 transition-opacity group-hover:opacity-100" />
                  </a>
                ))}
              </div>
            </div>
          )
        })}
      {data && items.length === 0 && !data.error && <Empty text="No items" />}
    </Panel>
  )
}

export function RefreshButton({ busy, onClick, title }) {
  return (
    <button
      onClick={onClick}
      disabled={busy}
      title={title}
      className="press grid h-7 w-7 place-items-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
    >
      <RefreshCw size={14} className={busy ? 'animate-spin' : ''} />
    </button>
  )
}

export function Empty({ text }) {
  return <div className="py-8 text-center text-sm text-zinc-600">{text}</div>
}
