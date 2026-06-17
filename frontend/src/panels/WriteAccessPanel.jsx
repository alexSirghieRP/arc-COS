import React, { useEffect, useState } from 'react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'

const typeLabel = { oneOnOne: '1:1', group: 'group', meeting: 'meeting' }

export default function WriteAccessPanel() {
  const [chats, setChats] = useState([])
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState(null)

  const load = () =>
    fetch('/api/write-access')
      .then((r) => r.json())
      .then(setChats)
      .catch(() => {})

  useEffect(() => {
    load()
  }, [])

  const toggle = async (c) => {
    setBusy(c.chat_id)
    try {
      await post('/api/write-access', { chat_id: c.chat_id, allow: !c.writable, label: c.label })
      await load()
    } finally {
      setBusy(null)
    }
  }

  const shown = chats.filter(
    (c) => !filter || c.label.toLowerCase().includes(filter.toLowerCase()),
  )
  const active = shown.filter((c) => c.writable)
  const inactive = shown.filter((c) => !c.writable)

  return (
    <Panel title="Write access" badge={`${chats.filter((c) => c.writable).length} writable`}>
      <p className="mb-2 text-xs text-zinc-500">
        Reading is always on everywhere. The agent may only <b>write</b> (auto-replies, holding
        messages, PR reviews, transcript requests, approved drafts) into chats enabled here.
        Base rules come from policy.yaml; toggles here override per chat.
      </p>
      <input
        className="mb-2 w-full rounded bg-zinc-800 px-2 py-1 text-sm"
        placeholder="filter chats…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <h3 className="mb-1 mt-2 text-xs font-semibold uppercase text-green-500">
        Writing enabled ({active.length})
      </h3>
      <div className="space-y-1">
        {active.map((c) => (
          <ChatRow key={c.chat_id} c={c} busy={busy} toggle={toggle} />
        ))}
        {active.length === 0 && (
          <div className="text-sm text-zinc-500">none, the agent writes nowhere</div>
        )}
      </div>
      <h3 className="mb-1 mt-4 text-xs font-semibold uppercase text-zinc-500">
        Writing disabled ({inactive.length})
      </h3>
      <div className="space-y-1">
        {inactive.map((c) => (
          <ChatRow key={c.chat_id} c={c} busy={busy} toggle={toggle} />
        ))}
        {inactive.length === 0 && <div className="text-sm text-zinc-500">none</div>}
      </div>
    </Panel>
  )
}

function ChatRow({ c, busy, toggle }) {
  return (
    <div
      className={`flex items-center gap-2 rounded px-2 py-1.5 text-sm ${
        c.writable ? 'bg-green-950/40 border border-green-900' : 'bg-zinc-900'
      }`}
    >
      <button
        disabled={busy === c.chat_id}
        onClick={() => toggle(c)}
        className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${
          c.writable ? 'bg-green-600' : 'bg-zinc-700'
        } disabled:opacity-50`}
        title={c.writable ? 'click to block writing' : 'click to allow writing'}
      >
        <span
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${
            c.writable ? 'left-4.5' : 'left-0.5'
          }`}
        />
      </button>
      <span className="shrink-0 rounded bg-zinc-800 px-1.5 text-[10px] text-zinc-400">
        {typeLabel[c.type] || c.type}
      </span>
      <span className="truncate">{c.label}</span>
      {c.source && <span className="ml-auto shrink-0 text-[10px] text-zinc-500">{c.source}</span>}
    </div>
  )
}
