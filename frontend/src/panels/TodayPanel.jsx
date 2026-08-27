import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Pencil, Check, Plus, BookOpen, RefreshCw } from 'lucide-react'
import { Panel } from '../App.jsx'
import { toggleCheckbox, post, editLock } from '../api.js'

// Render "[text](url)" markdown links and bare URLs as clickable links.
const LINK_RE = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s)]+)/g

function RichText({ text }) {
  const parts = []
  let last = 0
  let m
  LINK_RE.lastIndex = 0
  while ((m = LINK_RE.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const [, label, url, bare] = m
    parts.push(
      <a
        key={m.index}
        href={url || bare}
        target="_blank"
        rel="noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="text-sky-400 hover:underline"
      >
        {label || (bare || '').replace(/^https?:\/\//, '').slice(0, 60)}
      </a>,
    )
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return <>{parts}</>
}

function Checkbox({ noteDate, cb, refresh }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(cb.text)
  const [busy, setBusy] = useState(false)
  const [savedAt, setSavedAt] = useState(0)
  useEffect(() => { if (!editing) setText(cb.text) }, [cb.text, editing])

  const begin = () => { editLock.acquire(); setText(cb.text); setEditing(true) }
  const stop = () => { setEditing(false); editLock.release() }

  const save = async () => {
    // Disabling the input while busy (below) blurs it per the HTML spec, which
    // re-fires onBlur={save} — guard against the resulting double submit.
    if (busy) return
    const t = text.trim()
    if (!t || t === cb.text) { stop(); setText(cb.text); return }
    setBusy(true)
    try {
      await post('/api/today/edit', { date: noteDate, line: cb.line, text: t })
      stop(); setSavedAt(Date.now()); setTimeout(() => setSavedAt(0), 1500); refresh()
    } finally { setBusy(false) }
  }

  return (
    <div className="group flex items-start gap-2 py-0.5">
      <input
        type="checkbox"
        className="mt-1 cursor-pointer"
        checked={cb.checked}
        onChange={async (e) => { await toggleCheckbox(noteDate, cb.line, e.target.checked); refresh() }}
      />
      {editing ? (
        <input
          autoFocus
          value={text}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') save()
            if (e.key === 'Escape') { stop(); setText(cb.text) }
          }}
          onBlur={save}
          className="flex-1 rounded bg-zinc-950 px-1.5 py-0.5 text-sm text-zinc-100 ring-1 ring-zinc-600 outline-none transition-shadow focus:ring-2 focus:ring-[var(--accent)]"
        />
      ) : (
        <span
          onClick={begin}
          title="Click to edit"
          className={`flex-1 cursor-text ${cb.checked ? 'text-zinc-500 line-through' : ''}`}
        >
          <RichText text={cb.text} />
          {savedAt ? (
            <Check size={12} className="ml-1.5 inline text-emerald-400" />
          ) : (
            <Pencil size={11} className="ml-1.5 inline opacity-0 transition-opacity group-hover:opacity-50" />
          )}
        </span>
      )}
    </div>
  )
}

// Compact single-line time range: "4:30–5:00 PM", "11:30 AM–1:00 PM"
const fmtT = (iso) => {
  const d = new Date(iso)
  return `${d.getHours() % 12 || 12}:${String(d.getMinutes()).padStart(2, '0')}`
}
const mer = (iso) => (new Date(iso).getHours() < 12 ? 'AM' : 'PM')
const fmtRange = (s, e) =>
  mer(s) === mer(e) ? `${fmtT(s)}–${fmtT(e)} ${mer(e)}` : `${fmtT(s)} ${mer(s)}–${fmtT(e)} ${mer(e)}`
const fmtIn = (mins) =>
  mins >= 60 ? `in ${Math.floor(mins / 60)}h${mins % 60 ? ` ${mins % 60}m` : ''}` : `in ${mins}m`

const focusLabel = {
  urgent_ping: { text: 'urgent ping', cls: 'bg-red-900 text-red-200' },
  pr_review: { text: 'PR awaits you', cls: 'bg-purple-900 text-purple-200' },
  my_pr: { text: 'your PR', cls: 'bg-sky-900 text-sky-200' },
  aged_ping: { text: 'aged ping', cls: 'bg-yellow-900 text-yellow-200' },
  ado: { text: 'sprint', cls: 'bg-emerald-900 text-emerald-200' },
}

function QuickStats({ board, setTab }) {
  const { items, pendingDrafts } = useMemo(() => {
    const pings = board.pings || []
    const drafts = board.drafts || []
    const loops = board.open_loops || []
    const activePings = pings.filter(
      (p) => !['dismissed', 'done', 'answered'].includes(p.status),
    ).length
    const pending = drafts.filter((d) => d.status === 'pending').length
    const openLoops = loops.filter((l) => l.status !== 'done').length
    const urgentPings = pings.filter((p) => p.urgent && p.status !== 'dismissed').length
    return {
      pendingDrafts: pending,
      items: [
        urgentPings > 0 && { key: 'urgent', label: `${urgentPings} urgent`, cls: 'text-red-400', tab: 'pings' },
        activePings > 0 && { key: 'pings', label: `${activePings} ping${activePings !== 1 ? 's' : ''}`, cls: 'text-zinc-400', tab: 'pings' },
        pending > 0 && { key: 'drafts', label: `${pending} draft${pending !== 1 ? 's' : ''} awaiting review`, cls: 'text-amber-400 underline-offset-2 hover:underline cursor-pointer', tab: 'approvals' },
        openLoops > 0 && { key: 'loops', label: `${openLoops} loop${openLoops !== 1 ? 's' : ''}`, cls: 'text-zinc-500', tab: 'loops' },
      ].filter(Boolean),
    }
  }, [board.pings, board.drafts, board.open_loops])

  if (items.length === 0) return null
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg bg-zinc-800/40 px-3 py-1.5">
      {items.map((it) => (
        <span
          key={it.key}
          onClick={() => it.tab && setTab?.(it.tab)}
          className={`text-[11px] font-medium ${it.cls}`}
        >
          {it.label}
        </span>
      ))}
    </div>
  )
}

function RelatedVaultNotes({ calendar }) {
  const [notes, setNotes] = useState([])
  const keyword = (calendar || [])
    .map(e => e.subject || '')
    .filter(Boolean)
    .join(' ')
    .split(/\s+/)
    .filter(w => w.length > 4)
    .slice(0, 2)
    .join(' ')

  useEffect(() => {
    if (!keyword) return
    let live = true
    fetch(`/api/obsidian/search?q=${encodeURIComponent(keyword)}&limit=3`)
      .then(r => r.ok ? r.json() : [])
      // A slower request for a stale keyword can resolve after a faster,
      // newer one — ignore it instead of clobbering the current results.
      .then(d => { if (live) setNotes(d) })
      .catch(() => {})
    return () => { live = false }
  }, [keyword])

  if (!notes.length) return null
  return (
    <div className="mt-4">
      <h3 className="mb-1 text-xs font-semibold uppercase text-zinc-500">From your vault</h3>
      <div className="space-y-1">
        {notes.map(n => (
          <a
            key={n.path}
            href={`obsidian://open?vault=OBSIDIAN&file=${encodeURIComponent((n.path || '').replace(/\.md$/, ''))}`}
            className="flex items-start gap-2 rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 hover:border-violet-800/50 hover:bg-violet-950/20"
          >
            <BookOpen size={11} className="mt-0.5 shrink-0 text-violet-500" />
            <div className="min-w-0">
              <div className="truncate text-[12px] font-medium text-zinc-300">{(n.name || '').replace(/\.md$/, '')}</div>
              <div className="truncate text-[10px] text-zinc-600">{(n.snippet || '').slice(0, 80)}</div>
            </div>
          </a>
        ))}
      </div>
    </div>
  )
}

function ObsidianDailyLink() {
  const [exists, setExists] = useState(null)
  const today = new Date().toISOString().slice(0, 10)
  useEffect(() => {
    fetch(`/api/obsidian/daily?date=${today}`)
      .then((r) => r.ok ? r.json() : null)
      .then((d) => setExists(d && d.found !== false && d.content != null))
      .catch(() => setExists(false))
  }, [today])
  if (!exists) return null
  return (
    <a
      href={`obsidian://open?vault=OBSIDIAN&file=${encodeURIComponent(today)}`}
      title="Open today's daily note in Obsidian"
      className="flex items-center gap-1 rounded bg-violet-900/30 px-2 py-0.5 text-[11px] text-violet-400 hover:bg-violet-900/50 hover:text-violet-200"
    >
      <BookOpen size={11} />
      Daily note
    </a>
  )
}

function RebriefButton({ refresh }) {
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try {
      await post('/api/run/morning_brief', {})
      await new Promise((r) => setTimeout(r, 800))
      refresh()
    } finally {
      setBusy(false)
    }
  }
  return (
    <button
      onClick={run}
      disabled={busy}
      title="Re-run morning brief: refresh top 3 AI suggestions, carry forward, calendar"
      className="press flex items-center gap-1 rounded bg-zinc-800 px-2 py-0.5 text-[11px] text-zinc-500 hover:bg-zinc-700 hover:text-zinc-300 disabled:opacity-40"
    >
      <RefreshCw size={10} className={busy ? 'animate-spin' : ''} />
      {busy ? 'Briefing…' : 'Re-brief'}
    </button>
  )
}

export default function TodayPanel({ board, refresh, setTab }) {
  const note = board.note
  const now = new Date()

  // Find the next upcoming meeting (20-min window aligns with pre_meeting brief timing)
  const upcomingMeeting = board.calendar.find((e) => {
    const start = new Date(e.start_local)
    const end = new Date(e.end_local)
    const minsUntil = (start - now) / 60000
    const isOngoing = start <= now && now < end
    return isOngoing || (minsUntil > 0 && minsUntil <= 20)
  })

  return (
    <Panel title={`Today · ${now.toDateString()}`} actions={
      <div className="flex items-center gap-2">
        <RebriefButton refresh={refresh} />
        <ObsidianDailyLink />
      </div>
    }>
      <QuickStats board={board} setTab={setTab} />
      {upcomingMeeting && (() => {
        const start = new Date(upcomingMeeting.start_local)
        const minsUntil = Math.round((start - now) / 60000)
        const isOngoing = start <= now
        return (
          <div className={`mb-3 rounded-lg border px-3 py-2 text-sm ${
            isOngoing
              ? 'border-green-700 bg-green-950/60 text-green-200'
              : 'border-amber-700 bg-amber-950/40 text-amber-200'
          }`}>
            {isOngoing ? '🔴 In progress:' : `⏰ In ${minsUntil}m:`}{' '}
            <strong>{upcomingMeeting.subject}</strong>
            {upcomingMeeting.webLink && (
              <a
                href={upcomingMeeting.webLink}
                target="_blank"
                rel="noreferrer"
                className="ml-2 rounded bg-green-800/50 px-1.5 py-0.5 text-[11px] text-green-300 hover:bg-green-700/60"
              >
                Join
              </a>
            )}
          </div>
        )
      })()}
      {!note && (
        <div className="mb-3 rounded border border-yellow-800 bg-yellow-950/50 p-2 text-sm text-yellow-200">
          No daily note for today yet.
          <button
            className="ml-2 rounded bg-yellow-800 px-2 py-0.5 text-xs"
            onClick={async () => {
              await post('/api/sweep/morning_brief')
              refresh()
            }}
          >
            Run morning brief now
          </button>
          {board.fallback_note && (
            <div className="mt-1 text-xs text-yellow-500">
              showing most recent note: {board.fallback_note.date}
            </div>
          )}
        </div>
      )}
      {/* Two columns on wide screens: tasks/focus left, calendar right — use the width */}
      <div className="grid items-start gap-x-8 gap-y-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,440px)]">
        <div className="min-w-0">
          {(note || board.fallback_note) && (
            <NoteBody note={note || board.fallback_note} refresh={refresh} />
          )}

          <h3 className="mt-4 mb-1 text-xs font-semibold uppercase text-zinc-500">Proposed focus</h3>
          <div className="space-y-1">
            {board.focus.length === 0 && <div className="text-sm text-zinc-500">nothing urgent</div>}
            {board.focus.map((f, i) => {
              const lbl = focusLabel[f.kind] || { text: f.kind, cls: 'bg-zinc-800' }
              return (
                <div key={i} className="flex min-w-0 items-center gap-2 text-sm">
                  <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${lbl.cls}`}>
                    {lbl.text}
                  </span>
                  {f.ref?.startsWith('http') ? (
                    <a href={f.ref} target="_blank" rel="noreferrer" className="min-w-0 flex-1 truncate text-sky-400 hover:underline">
                      {f.title}
                    </a>
                  ) : (
                    <span className="min-w-0 flex-1 truncate">{f.title}</span>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        <div className="min-w-0">
          <h3 className="mt-4 mb-1 flex items-baseline gap-2 text-xs font-semibold uppercase text-zinc-500 xl:mt-0">
            Calendar
            {board.calendar.length > 0 && (
              <span className="font-normal normal-case tracking-normal text-zinc-600">
                {board.calendar.length} meeting{board.calendar.length !== 1 ? 's' : ''}
              </span>
            )}
          </h3>
          <div className="space-y-1">
            {board.calendar.length === 0 && <div className="text-sm text-zinc-500">no meetings today</div>}
            {board.calendar.map((e) => {
              const start = new Date(e.start_local)
              const end = new Date(e.end_local)
              const past = end < now
              const current = start <= now && now <= end
              const minsUntil = Math.round((start - now) / 60000)
              const minsLeft = Math.round((end - now) / 60000)
              return (
                <div
                  key={e.id}
                  className={`group flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm transition-colors ${
                    current
                      ? 'border border-emerald-800 bg-emerald-950/50'
                      : past
                        ? 'opacity-45'
                        : 'bg-zinc-800/40 hover:bg-zinc-800/70'
                  }`}
                >
                  {current ? (
                    <span className="relative flex h-2 w-2 shrink-0">
                      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
                      <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
                    </span>
                  ) : (
                    <span className={`h-2 w-2 shrink-0 rounded-full ${past ? 'bg-zinc-700' : 'bg-zinc-600'}`} />
                  )}
                  <span className="w-[108px] shrink-0 tabular-nums text-[12px] text-zinc-400">
                    {fmtRange(e.start_local, e.end_local)}
                  </span>
                  <span className={`min-w-0 flex-1 truncate ${past ? '' : 'text-zinc-200'}`}>{e.subject}</span>
                  {current && (
                    <span className="shrink-0 rounded-full bg-emerald-900/80 px-1.5 py-px text-[10px] font-medium text-emerald-300">
                      ends in {minsLeft}m
                    </span>
                  )}
                  {!current && !past && (
                    <span className="shrink-0 text-[10px] tabular-nums text-zinc-600">{fmtIn(minsUntil)}</span>
                  )}
                  {e.webLink && !past && (
                    <a
                      href={e.webLink}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(ev) => ev.stopPropagation()}
                      className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-sky-300 opacity-0 transition-opacity hover:bg-zinc-700 group-hover:opacity-100"
                    >
                      Join
                    </a>
                  )}
                </div>
              )
            })}
          </div>
          <RelatedVaultNotes calendar={board.calendar} />
        </div>
      </div>
    </Panel>
  )
}

function QuickAdd({ section, refresh }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const inputRef = useRef(null)

  const submit = async () => {
    const t = text.trim()
    if (!t) return
    setBusy(true)
    try {
      await post('/api/today/add-task', { text: t, section })
      setText('')
      refresh()
    } finally {
      setBusy(false)
      inputRef.current?.focus()
    }
  }

  return (
    <div className="mt-1 flex items-center gap-1">
      <input
        ref={inputRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
        placeholder="Add task…"
        className="flex-1 rounded bg-zinc-800/60 px-2 py-0.5 text-xs text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
      />
      <button
        disabled={busy || !text.trim()}
        onClick={submit}
        title="Add task"
        aria-label="Add task"
        className="press grid h-5 w-5 place-items-center rounded bg-zinc-700 text-zinc-400 hover:bg-[var(--accent-fill)] hover:text-[var(--accent)] disabled:opacity-30"
      >
        <Plus size={12} />
      </button>
    </div>
  )
}

function NoteBody({ note, refresh }) {
  const top3 = note.sections['Top 3 for Today']?.checkboxes || []
  const also = note.sections['Also Do Today']?.checkboxes || []
  const carry = note.sections['Carry Forward']?.checkboxes || []
  const doneFrac = top3.length > 0 ? top3.filter((cb) => cb.checked).length / top3.length : 0
  const doneCount = top3.filter((cb) => cb.checked).length
  return (
    <>
      <div className="mb-1 flex items-center gap-2">
        <h3 className="text-xs font-semibold uppercase text-zinc-500">Top 3 for Today</h3>
        {top3.length > 0 && (
          <div className="flex flex-1 items-center gap-1.5">
            <div className="relative h-1 flex-1 overflow-hidden rounded-full bg-zinc-800">
              <div
                className={`absolute inset-y-0 left-0 rounded-full transition-all duration-500 ${
                  doneFrac === 1 ? 'bg-emerald-500' : 'bg-[var(--accent)]'
                }`}
                style={{ width: `${doneFrac * 100}%` }}
              />
            </div>
            <span className={`text-[10px] tabular-nums ${doneFrac === 1 ? 'text-emerald-400' : 'text-zinc-600'}`}>
              {doneCount}/{top3.length}
            </span>
          </div>
        )}
      </div>
      {top3.length === 0 && <div className="text-sm text-zinc-600 italic">empty</div>}
      {top3.length > 0 && doneFrac === 1 && (
        <div className="mb-1 rounded-lg bg-emerald-950/60 px-3 py-1.5 text-[12px] text-emerald-300 ring-1 ring-emerald-900">
          All done — nice work.
        </div>
      )}
      {top3.map((cb) => (
        <Checkbox key={cb.line} noteDate={note.date} cb={cb} refresh={refresh} />
      ))}
      <QuickAdd section="Top 3 for Today" refresh={refresh} />
      <h3 className="mt-3 mb-1 text-xs font-semibold uppercase text-zinc-500">Also Do Today</h3>
      {also.length === 0 && <div className="text-sm text-zinc-600 italic">empty</div>}
      {also.map((cb) => (
        <Checkbox key={cb.line} noteDate={note.date} cb={cb} refresh={refresh} />
      ))}
      <QuickAdd section="Also Do Today" refresh={refresh} />
      {carry.length > 0 && (
        <>
          <h3 className="mt-3 mb-1 text-xs font-semibold uppercase text-zinc-500">Carry Forward</h3>
          {carry.map((cb) => (
            <Checkbox key={cb.line} noteDate={note.date} cb={cb} refresh={refresh} />
          ))}
        </>
      )}
    </>
  )
}
