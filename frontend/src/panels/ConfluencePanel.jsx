import React, { useEffect, useState } from 'react'
import { Eye, PenLine, ExternalLink, FileText } from 'lucide-react'
import { Panel } from '../App.jsx'
import { RefreshButton, Empty } from './AdoPanel.jsx'

const scopes = [
  { id: 'all', label: 'All' },
  { id: 'following', label: 'Following' },
  { id: 'mine', label: 'Mine' },
]

export default function ConfluencePanel() {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [scope, setScope] = useState('all')

  const load = (refresh) => {
    if (refresh) setBusy(true)
    return fetch(`/api/confluence-updates${refresh ? '?refresh=1' : ''}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => {})
      .finally(() => setBusy(false))
  }
  useEffect(() => {
    load()
    const t = setInterval(load, 300000)
    return () => clearInterval(t)
  }, [])

  const items = (data?.items || []).filter((i) => scope === 'all' || i.relations.includes(scope))

  return (
    <Panel
      title="Confluence updates"
      badge={data?.total ?? '·'}
      actions={<RefreshButton busy={busy} onClick={() => load(true)} title="Refresh from Confluence" />}
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-xs text-zinc-500">Recent changes to pages you follow or publish.</p>
        <div className="flex gap-0.5 rounded-lg bg-zinc-950 p-0.5">
          {scopes.map((s) => {
            const count = s.id === 'all' ? data?.total : data?.[s.id]
            return (
              <button
                key={s.id}
                onClick={() => setScope(s.id)}
                className={`press rounded-md px-2.5 py-1 text-xs font-medium ${
                  scope === s.id ? 'bg-zinc-800 text-zinc-100 shadow-sm' : 'text-zinc-500 hover:text-zinc-300'
                }`}
              >
                {s.label}
                {count != null && <span className="ml-1 tabular-nums text-zinc-600">{count}</span>}
              </button>
            )
          })}
        </div>
      </div>

      {data?.error && <Empty text={data.error} />}
      <div className="stagger space-y-1">
        {items.map((i) => (
          <a
            key={i.id}
            href={i.url}
            target="_blank"
            rel="noreferrer"
            className="row-hover group flex items-center gap-2.5 rounded-lg border border-transparent px-2.5 py-2 text-sm hover:border-zinc-800"
          >
            <FileText size={15} className="shrink-0 text-zinc-600" />
            <span className="flex shrink-0 gap-1">
              {i.relations.includes('following') && (
                <span className="flex items-center gap-1 rounded bg-sky-900 px-1.5 py-0.5 text-[10px] text-sky-200">
                  <Eye size={10} /> follow
                </span>
              )}
              {i.relations.includes('mine') && (
                <span className="flex items-center gap-1 rounded bg-purple-900 px-1.5 py-0.5 text-[10px] text-purple-200">
                  <PenLine size={10} /> mine
                </span>
              )}
            </span>
            <span className="truncate text-zinc-200">{i.title}</span>
            <span className="ml-auto shrink-0 text-[11px] text-zinc-500">{i.space}</span>
            <span className="shrink-0 text-[10px] tabular-nums text-zinc-600">
              v{i.version} · {i.by} · {i.when?.slice(0, 10)}
            </span>
            <ExternalLink size={13} className="shrink-0 text-zinc-700 opacity-0 transition-opacity group-hover:opacity-100" />
          </a>
        ))}
        {data && items.length === 0 && !data.error && <Empty text="No recent updates" />}
      </div>
    </Panel>
  )
}
