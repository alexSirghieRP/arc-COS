import React from 'react'
import { Panel } from '../App.jsx'

const tierCls = {
  A: 'bg-zinc-700 text-zinc-200',
  B: 'bg-sky-900 text-sky-200',
  C: 'bg-red-900 text-red-200',
}

const statusText = {
  new: 'new',
  classified: 'surfaced',
  auto_replied: 'auto-replied',
  drafted: 'draft ready',
  held: 'holding sent',
  answered: 'answered',
  dismissed: 'dismissed',
}

const ago = (iso) => {
  const m = Math.round((Date.now() - new Date(iso)) / 60000)
  if (m < 60) return `${m}m`
  if (m < 1440) return `${Math.round(m / 60)}h`
  return `${Math.round(m / 1440)}d`
}

export default function PingsPanel({ board }) {
  const pings = board.pings || []
  return (
    <Panel title="Pings" badge={pings.length}>
      <div className="space-y-1.5">
        {pings.length === 0 && <div className="text-sm text-zinc-500">quiet so far</div>}
        {pings.map((p) => (
          <div
            key={p.id}
            className={`rounded-lg border px-2.5 py-1.5 text-sm ${
              p.urgent ? 'border-red-700 bg-red-950/40' : 'border-zinc-800 bg-zinc-900'
            }`}
          >
            <div className="flex items-center gap-2">
              <span className="font-medium text-zinc-100">{p.sender}</span>
              {p.type === 'mention' && (
                <span className="rounded bg-orange-900 px-1 text-[10px] text-orange-200">@you</span>
              )}
              {p.tier && (
                <span className={`rounded px-1.5 text-[10px] font-bold ${tierCls[p.tier]}`}>
                  {p.tier}
                </span>
              )}
              <span className="text-[11px] text-zinc-500">{statusText[p.status] || p.status}</span>
              <span className="ml-auto shrink-0 text-[11px] text-zinc-500">{ago(p.received_at)}</span>
            </div>
            <div className="truncate text-zinc-400">{p.content}</div>
            {p.tier_reasoning && (
              <div className="mt-0.5 truncate text-[11px] italic text-zinc-600" title={p.tier_reasoning}>
                {p.tier_reasoning}
              </div>
            )}
          </div>
        ))}
      </div>
    </Panel>
  )
}
