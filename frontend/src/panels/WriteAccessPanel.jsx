import React, { useEffect, useState } from 'react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'

const typeLabel = { oneOnOne: '1:1', group: 'group', meeting: 'meeting', channel: 'channel' }

export default function WriteAccessPanel() {
  const [entities, setEntities] = useState([])
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState(null)

  const load = () =>
    fetch('/api/write-access')
      .then((r) => r.json())
      .then(setEntities)
      .catch(() => {})

  useEffect(() => {
    load()
  }, [])

  const toggle = async (e, dimension) => {
    const key = `${e.id}:${dimension}`
    setBusy(key)
    try {
      const allow = dimension === 'read' ? !e.readable : !e.writable
      await post('/api/write-access', { id: e.id, kind: e.kind, dimension, allow, label: e.label })
      await load()
    } finally {
      setBusy(null)
    }
  }

  const shown = entities.filter(
    (e) => !filter || e.label.toLowerCase().includes(filter.toLowerCase()),
  )
  const channels = shown.filter((e) => e.kind === 'channel')
  const chats = shown.filter((e) => e.kind !== 'channel')

  return (
    <Panel
      title="Write access"
      badge={`${entities.filter((e) => e.writable).length} writable`}
    >
      <p className="mb-2 text-xs text-zinc-500">
        Two independent switches per chat/channel: <b>read</b> (CoS can see messages there) and{' '}
        <b>write</b> (CoS can post/react there - auto-replies, PR reviews, holding messages,
        approved drafts). Any combination is valid, including neither. Base rules come from
        policy.yaml; toggles here override per entity.
      </p>
      <input
        className="mb-2 w-full rounded border border-transparent bg-zinc-800 px-2 py-1 text-sm outline-none transition-colors focus:border-[var(--accent)]"
        placeholder="filter…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      {channels.length > 0 && (
        <>
          <h3 className="mb-1 mt-2 text-xs font-semibold uppercase text-zinc-500">
            Channels ({channels.length})
          </h3>
          <div className="mb-3 space-y-1">
            {channels.map((e) => (
              <EntityRow key={e.id} e={e} busy={busy} toggle={toggle} />
            ))}
          </div>
        </>
      )}
      <h3 className="mb-1 mt-2 text-xs font-semibold uppercase text-zinc-500">
        Chats ({chats.length})
      </h3>
      <div className="space-y-1">
        {chats.map((e) => (
          <EntityRow key={e.id} e={e} busy={busy} toggle={toggle} />
        ))}
        {chats.length === 0 && <div className="text-sm text-zinc-500">none</div>}
      </div>
    </Panel>
  )
}

function MiniToggle({ on, disabled, onClick, label, tone }) {
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      title={`${label}: click to ${on ? 'disable' : 'enable'}`}
      className={`press flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase transition-colors disabled:opacity-50 ${
        on ? tone : 'bg-zinc-800 text-zinc-500'
      }`}
    >
      <span
        className={`relative h-2.5 w-2.5 rounded-full transition-colors ${on ? 'bg-white' : 'bg-zinc-600'}`}
      />
      {label}
    </button>
  )
}

function EntityRow({ e, busy, toggle }) {
  const active = e.readable || e.writable
  return (
    <div
      className={`row-hover flex items-center gap-2 rounded px-2 py-1.5 text-sm transition-colors ${
        active ? 'bg-zinc-900' : 'bg-zinc-900/40'
      }`}
    >
      <span className="shrink-0 rounded bg-zinc-800 px-1.5 text-[10px] text-zinc-400">
        {typeLabel[e.type] || e.type}
      </span>
      <span className="truncate">{e.label}</span>
      <span className="ml-auto flex shrink-0 items-center gap-1.5">
        <MiniToggle
          on={e.readable}
          disabled={busy === `${e.id}:read`}
          onClick={() => toggle(e, 'read')}
          label="read"
          tone="bg-sky-700 text-white"
        />
        <MiniToggle
          on={e.writable}
          disabled={busy === `${e.id}:write`}
          onClick={() => toggle(e, 'write')}
          label="write"
          tone="bg-green-700 text-white"
        />
      </span>
    </div>
  )
}
