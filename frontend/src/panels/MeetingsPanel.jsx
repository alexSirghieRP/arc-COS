import React, { useEffect, useState } from 'react'
import {
  ChevronRight, FileText, Copy, Check, ExternalLink, NotebookPen, MessagesSquare, CircleSlash, BookOpen,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'
import { post } from '../api.js'
import { useToast } from '../ui.jsx'

export default function MeetingsPanel() {
  const [meetings, setMeetings] = useState([])
  const [busyId, setBusyId] = useState(null)
  const [capturingId, setCapturingId] = useState(null)
  const [open, setOpen] = useState(null)
  const [transcripts, setTranscripts] = useState({}) // chat_id -> {loading, text, count}
  const [copied, setCopied] = useState(null)
  const toast = useToast()

  const load = () =>
    fetch('/api/meetings').then((r) => r.json()).then(setMeetings).catch(() => {})
  useEffect(() => {
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [])

  const summarize = async (m) => {
    setBusyId(m.id)
    try {
      await post('/api/meetings/summarize', { event_id: m.id, day: m.day, subject: m.subject, chat_id: m.chat_id })
      load()
    } finally {
      setBusyId(null)
    }
  }

  const toggle = async (m) => {
    const key = `${m.day}-${m.id}`
    if (open === key) {
      setOpen(null)
      return
    }
    setOpen(key)
    if (m.chat_id && !transcripts[m.chat_id]) {
      setTranscripts((t) => ({ ...t, [m.chat_id]: { loading: true } }))
      try {
        const r = await fetch(`/api/meetings/transcript?chat_id=${encodeURIComponent(m.chat_id)}`)
        const d = await r.json()
        setTranscripts((t) => ({ ...t, [m.chat_id]: { text: d.text, count: d.count } }))
      } catch {
        setTranscripts((t) => ({ ...t, [m.chat_id]: { text: '', count: 0, error: true } }))
      }
    }
  }

  const copy = async (text, key) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500)
    } catch {
      /* ignore */
    }
  }

  const createMeetingNote = async (m) => {
    setCapturingId(m.id)
    try {
      const date = m.day
      const time = fmt(m.start_local)
      const attendees = (m.attendees || [])
        .filter(a => a?.emailAddress?.address)
        .map(a => a.emailAddress.name || a.emailAddress.address)
        .join(', ')
      const content = [
        `# ${m.subject}`,
        '',
        `**Date:** ${date} ${time}`,
        attendees ? `**Attendees:** ${attendees}` : '',
        '',
        '## Notes',
        '',
        '## Action Items',
        '',
        '## Decisions',
        '',
        '---',
        `*Created from Chief of Staff*`,
      ].filter(s => s !== null).join('\n')
      const r = await fetch('/api/obsidian/note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: `${date} ${m.subject}`.slice(0, 80), content, folder: 'Chieff' }),
      })
      if (!r.ok) throw new Error(await r.text())
      const data = await r.json()
      toast(`Meeting note created: ${data.path}`, 'success')
    } catch (e) {
      toast(`Could not create note: ${String(e).slice(0, 100)}`, 'error')
    } finally {
      setCapturingId(null)
    }
  }

  const fmt = (iso) =>
    iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''

  const sources = (m) =>
    [
      m.chat_id && { label: 'transcript', icon: MessagesSquare },
      m.notes_in_vault && { label: 'notes in vault', icon: NotebookPen },
    ].filter(Boolean)

  return (
    <Panel title="Meetings" badge={meetings.length}>
      <div className="stagger space-y-1.5">
        {meetings.length === 0 && <Empty text="No meetings" />}
        {meetings.map((m) => {
          const key = `${m.day}-${m.id}`
          const isOpen = open === key
          const src = sources(m)
          const tr = m.chat_id ? transcripts[m.chat_id] : null
          return (
            <div
              key={key}
              className={`overflow-hidden rounded-xl border transition-colors ${
                isOpen ? 'border-zinc-700 bg-zinc-950' : 'border-zinc-800 bg-zinc-900'
              }`}
            >
              <div className="flex items-center gap-2.5 px-3 py-2.5">
                <button onClick={() => toggle(m)} className="row-hover flex min-w-0 flex-1 items-center gap-2.5 text-left">
                  <ChevronRight
                    size={15}
                    className={`shrink-0 text-zinc-500 transition-transform duration-200 ${isOpen ? 'rotate-90' : ''}`}
                  />
                  <span className="shrink-0 text-[11px] tabular-nums text-zinc-500">
                    {m.day.slice(5)} · {fmt(m.start_local)}
                  </span>
                  <span className="truncate text-sm font-medium text-zinc-200">{m.subject}</span>
                  {(() => {
                    const att = (m.attendees || []).filter(a => a?.emailAddress?.address)
                    return att.length > 0 && (
                      <span className="hidden truncate text-[11px] text-zinc-500 md:block">
                        {att.slice(0, 3).map(a => a.emailAddress.name || a.emailAddress.address.split('@')[0]).join(', ')}
                        {att.length > 3 && ` +${att.length - 3}`}
                      </span>
                    )
                  })()}
                  <span className="ml-2 flex shrink-0 items-center gap-1.5">
                    {src.length === 0 ? (
                      <span className="flex items-center gap-1 text-[11px] text-zinc-600">
                        <CircleSlash size={11} /> nothing captured
                      </span>
                    ) : (
                      src.map((s) => (
                        <span key={s.label} className="flex items-center gap-1 rounded-md bg-green-900 px-1.5 py-0.5 text-[10px] text-green-200">
                          <s.icon size={10} /> {s.label}
                        </span>
                      ))
                    )}
                  </span>
                </button>
                {m.webLink && (
                  <a
                    href={m.webLink}
                    target="_blank"
                    rel="noreferrer"
                    title="Open the meeting"
                    className="press grid h-7 w-7 shrink-0 place-items-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
                  >
                    <ExternalLink size={14} />
                  </a>
                )}
                <button
                  disabled={capturingId === m.id}
                  onClick={(e) => { e.stopPropagation(); createMeetingNote(m) }}
                  title="Create meeting notes template in Obsidian"
                  className="press grid h-7 w-7 shrink-0 place-items-center rounded-lg text-zinc-500 hover:bg-violet-900/40 hover:text-violet-300 disabled:opacity-40"
                >
                  <BookOpen size={14} className={capturingId === m.id ? 'animate-pulse' : ''} />
                </button>
                <button
                  disabled={busyId === m.id}
                  onClick={() => summarize(m)}
                  className="press shrink-0 rounded-lg bg-zinc-800 px-2.5 py-1 text-[12px] text-zinc-300 hover:bg-zinc-700 disabled:opacity-50"
                >
                  {busyId === m.id ? 'Summarizing' : 'Summarize'}
                </button>
              </div>

              {isOpen && (
                <div className="animate-fade-in border-t border-zinc-800 px-3.5 py-3">
                  {!m.chat_id ? (
                    <p className="text-xs text-zinc-500">
                      No transcript available for this meeting{m.notes_in_vault ? ' — notes are in the vault.' : '.'}
                    </p>
                  ) : tr?.loading ? (
                    <p className="text-xs text-zinc-500">Loading transcript…</p>
                  ) : !tr?.text ? (
                    <p className="text-xs text-zinc-500">Transcript is empty.</p>
                  ) : (
                    <>
                      <div className="mb-2 flex items-center gap-2">
                        <FileText size={13} className="text-zinc-500" />
                        <span className="text-[11px] font-medium text-zinc-400">Transcript</span>
                        <span className="text-[11px] text-zinc-600">{tr.count} lines</span>
                        <button
                          onClick={() => copy(tr.text, key)}
                          className="press ml-auto flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"
                        >
                          {copied === key ? <Check size={13} className="text-green-400" /> : <Copy size={13} />}
                          {copied === key ? 'Copied' : 'Copy'}
                        </button>
                      </div>
                      <pre className="max-h-80 overflow-y-auto whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900 p-3 text-[12px] leading-relaxed text-zinc-300">
                        {tr.text}
                      </pre>
                    </>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </Panel>
  )
}
