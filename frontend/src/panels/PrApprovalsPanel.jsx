import React, { useEffect, useState } from 'react'
import {
  ChevronRight, UserCheck, AlertTriangle, Clock, CheckCircle2, FileEdit,
  ExternalLink, MinusCircle, PlusCircle, MessageSquare,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'
import { post } from '../api.js'

const tone = {
  mine: { cls: 'bg-purple-900 text-purple-200', icon: UserCheck },
  warn: { cls: 'bg-amber-900 text-amber-200', icon: AlertTriangle },
  wait: { cls: 'bg-sky-900 text-sky-200', icon: Clock },
  good: { cls: 'bg-green-900 text-green-200', icon: CheckCircle2 },
  muted: { cls: 'bg-zinc-800 text-zinc-400', icon: FileEdit },
}
const roundLabel = (n) => (n > 0 ? `Review #${n}` : 'Unreviewed')
const ago = (iso) => {
  if (!iso) return ''
  const d = Math.floor((Date.now() - new Date(iso)) / 86400000)
  return d <= 0 ? 'today' : `${d}d ago`
}

export default function PrApprovalsPanel() {
  const [prs, setPrs] = useState([])
  const [open, setOpen] = useState(null)
  const [busy, setBusy] = useState(null)

  const load = () =>
    fetch('/api/pr-approvals')
      .then((r) => r.json())
      .then((d) => setPrs(Array.isArray(d) ? d : []))
      .catch(() => setPrs([]))

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [])

  const toggleQueue = async (pr) => {
    setBusy(pr.url)
    try {
      await post('/api/pr-approvals/queue', { url: pr.url, in_queue: !pr.in_queue })
      await load()
    } finally {
      setBusy(null)
    }
  }

  const inQueue = prs.filter((p) => p.in_queue).length
  return (
    <Panel
      title="PR approvals"
      badge={prs.length}
      actions={<span className="text-[11px] text-zinc-500">{inQueue} in queue</span>}
    >
      <div className="stagger space-y-1.5">
        {prs.length === 0 && <Empty text="No open PRs tracked yet" />}
        {prs.map((pr) => {
          const T = tone[pr.tone] || tone.muted
          const isOpen = open === pr.url
          return (
            <div
              key={pr.url}
              className={`overflow-hidden rounded-xl border transition-colors ${
                isOpen ? 'border-zinc-700 bg-zinc-950' : 'border-zinc-800 bg-zinc-900'
              }`}
            >
              <button
                onClick={() => setOpen(isOpen ? null : pr.url)}
                className="row-hover flex w-full items-center gap-2.5 px-3 py-2.5 text-left text-sm"
              >
                <ChevronRight
                  size={15}
                  className={`shrink-0 text-zinc-500 transition-transform duration-200 ${isOpen ? 'rotate-90' : ''}`}
                />
                <span className="shrink-0 font-medium tabular-nums text-zinc-300">
                  {pr.repo} <span className="text-zinc-500">#{pr.number}</span>
                </span>
                <span className="truncate text-zinc-400">{pr.title}</span>
                <span className={`ml-auto flex shrink-0 items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-medium ${T.cls}`}>
                  <T.icon size={11} />
                  {pr.label}
                </span>
                <span className="shrink-0 rounded-md bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">
                  {roundLabel(pr.chieff_round)}
                </span>
                {!pr.in_queue && (
                  <span className="shrink-0 rounded-md bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-600">paused</span>
                )}
              </button>

              {isOpen && (
                <div className="animate-fade-in border-t border-zinc-800 px-3.5 py-3 text-xs">
                  <div className="mb-3 grid grid-cols-2 gap-x-6 gap-y-1.5 sm:grid-cols-4">
                    <Field label="Author" value={pr.author} />
                    <Field label="Waiting on" value={pr.waiting_on} />
                    <Field label="Decision" value={pr.review_decision || '—'} />
                    <Field label="Updated" value={ago(pr.updated_at)} />
                  </div>
                  {pr.reviewers?.length > 0 && (
                    <div className="mb-3 flex flex-wrap gap-1.5">
                      {pr.reviewers.map((r, i) => (
                        <span key={i} className="rounded-md bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">
                          {r.who} · {r.state}
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="mb-3 flex items-center gap-2">
                    <a
                      href={pr.url}
                      target="_blank"
                      rel="noreferrer"
                      className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-zinc-300 hover:bg-zinc-700"
                    >
                      <ExternalLink size={13} /> GitHub
                    </a>
                    <button
                      onClick={() => toggleQueue(pr)}
                      disabled={busy === pr.url}
                      className={`press flex items-center gap-1.5 rounded-lg px-2.5 py-1 disabled:opacity-40 ${
                        pr.in_queue
                          ? 'bg-zinc-800 text-zinc-300 hover:bg-zinc-700'
                          : 'bg-green-900 text-green-200 hover:bg-green-800'
                      }`}
                    >
                      {pr.in_queue ? <MinusCircle size={13} /> : <PlusCircle size={13} />}
                      {busy === pr.url ? 'working' : pr.in_queue ? 'Pull from queue' : 'Add to queue'}
                    </button>
                  </div>
                  <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-zinc-500">
                    <MessageSquare size={12} />
                    {pr.comments?.length > 0 ? 'Comments Chieff posted' : 'No comments posted yet'}
                  </div>
                  {pr.comments?.map((c, i) => (
                    <div
                      key={i}
                      className="mt-1.5 whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900 p-2.5 text-zinc-300"
                    >
                      {c.body}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </Panel>
  )
}

function Field({ label, value }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-zinc-600">{label}</div>
      <div className="text-zinc-300">{value}</div>
    </div>
  )
}
