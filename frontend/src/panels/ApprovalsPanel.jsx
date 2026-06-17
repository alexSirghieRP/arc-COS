import React, { useState } from 'react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'

function Draft({ d, refresh }) {
  const [body, setBody] = useState(d.body)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  const act = async (action) => {
    setBusy(true)
    try {
      await post(`/api/drafts/${d.id}/${action}`, action === 'reject' ? { reason } : { body })
      refresh()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-900 p-2.5">
      <div className="mb-1 flex items-center gap-2 text-xs text-zinc-400">
        <span className={`rounded px-1.5 font-bold ${d.channel === 'email' ? 'bg-indigo-900 text-indigo-200' : 'bg-violet-900 text-violet-200'}`}>
          {d.channel}
        </span>
        <span>to {d.recipient || d.sender}</span>
        {d.subject && <span className="truncate">· {d.subject}</span>}
      </div>
      {d.item_content && (
        <div className="mb-1.5 rounded bg-zinc-950 px-2 py-1 text-xs text-zinc-500">
          them: {d.item_content.slice(0, 200)}
        </div>
      )}
      <textarea
        className="w-full rounded bg-zinc-800 p-2 text-sm"
        rows={3}
        value={body}
        onChange={(e) => setBody(e.target.value)}
      />
      <div className="mt-1.5 flex items-center gap-2">
        <button
          disabled={busy}
          onClick={() => act('approve')}
          className="rounded bg-green-700 px-3 py-1 text-sm font-medium hover:bg-green-600 disabled:opacity-50"
        >
          Approve & send
        </button>
        <input
          className="grow rounded bg-zinc-800 px-2 py-1 text-xs"
          placeholder="reject reason (logged for calibration)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <button
          disabled={busy}
          onClick={() => act('reject')}
          className="rounded bg-zinc-700 px-3 py-1 text-sm hover:bg-red-900 disabled:opacity-50"
        >
          Reject
        </button>
      </div>
    </div>
  )
}

export default function ApprovalsPanel({ board, refresh }) {
  const drafts = board.drafts || []
  return (
    <Panel title="Approvals" badge={drafts.length}>
      <div className="space-y-2">
        {drafts.length === 0 && <div className="text-sm text-zinc-500">nothing waiting on you</div>}
        {drafts.map((d) => (
          <Draft key={d.id} d={d} refresh={refresh} />
        ))}
      </div>
    </Panel>
  )
}
