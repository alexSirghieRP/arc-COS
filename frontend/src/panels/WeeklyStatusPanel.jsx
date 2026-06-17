import React, { useCallback, useEffect, useState } from 'react'
import {
  FileText, ExternalLink, CheckCircle2, RefreshCw, Trash2, Send,
  GitMerge, Hash,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'
import { post } from '../api.js'

const statusTone = {
  draft: 'bg-amber-900 text-amber-200',
  published: 'bg-green-900 text-green-200',
  discarded: 'bg-zinc-800 text-zinc-500',
}

export default function WeeklyStatusPanel() {
  const [reports, setReports] = useState([])
  const [preview, setPreview] = useState({}) // report_id -> {storage_html,...}
  const [open, setOpen] = useState(null)
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)

  const load = useCallback(
    () =>
      fetch('/api/weekly-status')
        .then((r) => r.json())
        .then((d) => setReports(Array.isArray(d) ? d : []))
        .catch(() => setReports([])),
    [],
  )

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  const openReport = async (r) => {
    if (open === r.id) return setOpen(null)
    setOpen(r.id)
    if (!preview[r.id]) {
      const p = await fetch(`/api/weekly-status/${r.id}/preview`).then((x) => x.json())
      setPreview((m) => ({ ...m, [r.id]: p }))
    }
  }

  const run = async (label, fn) => {
    setBusy(label)
    setMsg(null)
    try {
      const res = await fn()
      setMsg(typeof res === 'string' ? res : null)
      await load()
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy(null)
    }
  }

  const generate = () => run('gen', () => post('/api/weekly-status/generate'))
  const approve = (r) =>
    run(`approve-${r.id}`, async () => {
      const res = await post(`/api/weekly-status/${r.id}/approve`)
      return `Published. Teams post: ${res.teams_post}`
    })
  const discard = (r) => run(`discard-${r.id}`, () => post(`/api/weekly-status/${r.id}/discard`))

  const draft = reports.find((r) => r.status === 'draft')

  return (
    <Panel
      title="Weekly status"
      badge={draft ? 1 : 0}
      actions={
        <button
          onClick={generate}
          disabled={busy === 'gen'}
          className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700 disabled:opacity-40"
        >
          <RefreshCw size={12} className={busy === 'gen' ? 'animate-spin' : ''} />
          {busy === 'gen' ? 'Generating' : 'Generate this week'}
        </button>
      }
    >
      {msg && (
        <div className="mb-2 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-[11px] text-zinc-400">
          {msg}
        </div>
      )}
      <div className="stagger space-y-1.5">
        {reports.length === 0 && (
          <Empty text="No weekly reports yet — hit Generate this week" />
        )}
        {reports.map((r) => {
          const isOpen = open === r.id
          const pv = preview[r.id]
          return (
            <div
              key={r.id}
              className={`overflow-hidden rounded-xl border transition-colors ${
                isOpen ? 'border-zinc-700 bg-zinc-950' : 'border-zinc-800 bg-zinc-900'
              }`}
            >
              <button
                onClick={() => openReport(r)}
                className="row-hover flex w-full items-center gap-2.5 px-3 py-2.5 text-left text-sm"
              >
                <FileText size={15} className="shrink-0 text-zinc-500" />
                <span className="shrink-0 font-medium text-zinc-200">{r.week_label}</span>
                <span className="truncate text-zinc-400">{r.date_range}</span>
                <span className="ml-auto flex shrink-0 items-center gap-2 text-[11px] text-zinc-500">
                  <span className="flex items-center gap-1">
                    <GitMerge size={11} /> {r.metrics?.prs ?? 0}
                  </span>
                  <span className="flex items-center gap-1">
                    <Hash size={11} /> {r.metrics?.ado_refs?.length ?? 0}
                  </span>
                </span>
                <span
                  className={`shrink-0 rounded-md px-2 py-0.5 text-[10px] font-medium ${
                    statusTone[r.status] || statusTone.draft
                  }`}
                >
                  {r.status}
                </span>
              </button>

              {isOpen && (
                <div className="animate-fade-in border-t border-zinc-800 px-3.5 py-3 text-xs">
                  <div className="mb-3 flex flex-wrap items-center gap-2">
                    {r.status === 'draft' && (
                      <button
                        onClick={() => approve(r)}
                        disabled={busy === `approve-${r.id}`}
                        className="press flex items-center gap-1.5 rounded-lg bg-green-900 px-2.5 py-1 text-green-200 hover:bg-green-800 disabled:opacity-40"
                      >
                        <CheckCircle2 size={13} />
                        {busy === `approve-${r.id}` ? 'Publishing…' : 'Approve → publish + post'}
                      </button>
                    )}
                    <a
                      href={r.edit_url}
                      target="_blank"
                      rel="noreferrer"
                      className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-zinc-300 hover:bg-zinc-700"
                    >
                      <ExternalLink size={13} /> Edit in Confluence
                    </a>
                    {r.status === 'published' && r.confluence_url && (
                      <a
                        href={r.confluence_url}
                        target="_blank"
                        rel="noreferrer"
                        className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-zinc-300 hover:bg-zinc-700"
                      >
                        <Send size={13} /> View published
                      </a>
                    )}
                    {r.status === 'draft' && (
                      <button
                        onClick={() => discard(r)}
                        disabled={busy === `discard-${r.id}`}
                        className="press ml-auto flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-zinc-400 hover:bg-zinc-700 disabled:opacity-40"
                      >
                        <Trash2 size={13} /> Discard
                      </button>
                    )}
                  </div>
                  <div className="mb-1.5 text-[10px] uppercase tracking-wide text-zinc-600">
                    Teams summary preview
                  </div>
                  <div
                    className="weekly-preview rounded-lg border border-zinc-800 bg-zinc-900 p-3 text-zinc-300"
                    dangerouslySetInnerHTML={{ __html: pv?.teams_html || 'loading…' }}
                  />
                </div>
              )}
            </div>
          )
        })}
      </div>
    </Panel>
  )
}
