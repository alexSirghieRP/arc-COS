import React, { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Copy, Check, RotateCcw, ClipboardList, Pencil, Eye } from 'lucide-react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'
import { post } from '../api.js'

// Small, purpose-built markdown renderer: bold, links, and '- ' bullet lists,
// with paragraph/list spacing. The scrum ticket is short structured markdown
// (a bold lead + bulleted sections), not full markdown, so nothing fancier
// than this is needed.
const LINK_RE = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g
const BOLD_RE = /\*\*(.+?)\*\*/g

function renderInline(text, keyPrefix) {
  // First split on links, then split each non-link chunk on bold, so both
  // can appear in the same line (e.g. "**GitHub**" header, or a bold word
  // next to a PR link).
  const linkParts = []
  let last = 0
  let m
  let key = 0
  LINK_RE.lastIndex = 0
  while ((m = LINK_RE.exec(text)) !== null) {
    if (m.index > last) linkParts.push({ text: text.slice(last, m.index) })
    linkParts.push({ href: m[2], label: m[1] })
    last = m.index + m[0].length
  }
  if (last < text.length) linkParts.push({ text: text.slice(last) })

  return linkParts.flatMap((part, i) => {
    if (part.href) {
      return (
        <a
          key={`${keyPrefix}-l${i}`}
          href={part.href}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-[var(--accent)] underline decoration-dotted underline-offset-2 hover:decoration-solid"
        >
          {part.label}
        </a>
      )
    }
    const bits = []
    let last2 = 0
    let mm
    let bkey = 0
    BOLD_RE.lastIndex = 0
    while ((mm = BOLD_RE.exec(part.text)) !== null) {
      if (mm.index > last2) bits.push(part.text.slice(last2, mm.index))
      bits.push(
        <strong key={`${keyPrefix}-b${i}-${bkey++}`} className="font-semibold text-zinc-100">
          {mm[1]}
        </strong>,
      )
      last2 = mm.index + mm[0].length
    }
    if (last2 < part.text.length) bits.push(part.text.slice(last2))
    return bits
  })
}

function renderMarkdown(text) {
  const lines = (text || '').replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let bullets = []
  let key = 0

  const flushBullets = () => {
    if (bullets.length) {
      blocks.push(
        <ul key={`ul-${key++}`} className="ml-0.5 list-none space-y-1.5">
          {bullets.map((b, i) => (
            <li key={i} className="flex gap-2">
              <span className="mt-[2px] shrink-0 text-zinc-600">•</span>
              <span>{renderInline(b, `b${key}-${i}`)}</span>
            </li>
          ))}
        </ul>,
      )
      bullets = []
    }
  }

  for (const raw of lines) {
    const line = raw.trim()
    if (!line) {
      flushBullets()
      continue
    }
    if (line.startsWith('- ') || line.startsWith('* ')) {
      bullets.push(line.slice(2))
      continue
    }
    flushBullets()
    blocks.push(
      <p key={`p-${key++}`}>{renderInline(line, `p${key}`)}</p>,
    )
  }
  flushBullets()
  return blocks
}

function wordCount(text) {
  return (text || '').trim().split(/\s+/).filter(Boolean).length
}

function Ticket({ r, onSaved }) {
  const [text, setText] = useState(r.summary_text || '')
  const [mode, setMode] = useState('preview') // 'preview' | 'edit'
  const [busy, setBusy] = useState(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    setText(r.summary_text || '')
  }, [r.summary_text])

  const dirty = text !== (r.summary_text || '')
  const words = wordCount(text)
  const wordTone =
    words >= 150 && words <= 220 ? 'text-green-400' : 'text-amber-400'

  const save = async () => {
    setBusy('save')
    try {
      await post(`/api/scrum/${r.id}`, { summary_text: text })
      await onSaved()
    } finally {
      setBusy(null)
    }
  }

  const reset = async () => {
    setBusy('reset')
    try {
      await post(`/api/scrum/${r.id}/reset`)
      await onSaved()
    } finally {
      setBusy(null)
    }
  }

  const copy = async () => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
        <span className="font-medium text-zinc-300">{r.report_date}</span>
        {r.edited === 1 && (
          <span className="rounded-md bg-amber-900 px-1.5 py-0.5 text-[10px] text-amber-200">
            edited
          </span>
        )}
        <span className={`ml-auto font-mono ${wordTone}`}>{words} words</span>
      </div>

      <div className="mb-2 flex items-center gap-1.5">
        <button
          onClick={() => setMode('preview')}
          className={`press flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] ${
            mode === 'preview' ? 'bg-zinc-700 text-zinc-100' : 'bg-zinc-800 text-zinc-400'
          }`}
        >
          <Eye size={11} /> Preview
        </button>
        <button
          onClick={() => setMode('edit')}
          className={`press flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] ${
            mode === 'edit' ? 'bg-zinc-700 text-zinc-100' : 'bg-zinc-800 text-zinc-400'
          }`}
        >
          <Pencil size={11} /> Edit
        </button>
      </div>

      {mode === 'edit' ? (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={10}
          placeholder="Scrum update text… markdown: **bold**, - bullets, [label](url) links"
          className="w-full resize-y rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-[12.5px] leading-relaxed text-zinc-200 outline-none transition-colors focus:border-[var(--accent)]"
        />
      ) : (
        <div className="space-y-2.5 rounded-lg border border-zinc-800 bg-zinc-950 p-3.5 text-[14px] leading-relaxed text-zinc-300">
          {text.trim() ? renderMarkdown(text) : (
            <span className="text-zinc-600">nothing yet</span>
          )}
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <button
          onClick={copy}
          className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"
        >
          {copied ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
        {dirty && (
          <button
            onClick={save}
            disabled={busy === 'save'}
            className="press flex items-center gap-1.5 rounded-lg bg-green-900 px-2.5 py-1 text-[11px] text-green-200 hover:bg-green-800 disabled:opacity-40"
          >
            {busy === 'save' ? 'Saving…' : 'Save edit'}
          </button>
        )}
        {r.edited === 1 && !dirty && (
          <button
            onClick={reset}
            disabled={busy === 'reset'}
            className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-400 hover:bg-zinc-700 disabled:opacity-40"
          >
            <RotateCcw size={12} />
            {busy === 'reset' ? 'Resetting…' : 'Reset to generated'}
          </button>
        )}
      </div>
    </div>
  )
}

export default function ScrumPanel() {
  const [reports, setReports] = useState([])
  const [busy, setBusy] = useState(null)
  const [err, setErr] = useState(null)

  const load = useCallback(
    () =>
      fetch('/api/scrum')
        .then((r) => r.json())
        .then((d) => setReports(Array.isArray(d) ? d : []))
        .catch(() => setReports([])),
    [],
  )

  useEffect(() => {
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [load])

  const generate = async () => {
    setBusy('gen')
    setErr(null)
    try {
      await post('/api/scrum/generate')
      await load()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(null)
    }
  }

  const [latest, ...older] = reports

  return (
    <Panel
      title="Scrum"
      badge={latest ? latest.report_date : 0}
      actions={
        <button
          onClick={generate}
          disabled={busy === 'gen'}
          className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700 disabled:opacity-40"
        >
          <RefreshCw size={12} className={busy === 'gen' ? 'animate-spin' : ''} />
          {busy === 'gen' ? 'Generating' : 'Regenerate today'}
        </button>
      }
    >
      <p className="mb-2 text-xs text-zinc-500">
        Daily "what I did" ticket, generated at 16:00 - rolling window from yesterday 16:30 to
        today 16:30 (Friday 16:30 through the weekend on a Monday), covering
        your tracked repos: GitHub PRs/commits/reviews, ADO items, Confluence edits, and
        Claude Code sessions. Bulleted by source with links and named collaborators, copyable,
        and directly editable below.
      </p>
      {err && (
        <div className="mb-2 rounded-lg border border-red-900/50 bg-zinc-900 px-3 py-2 text-[11px] text-red-300">
          {err}
        </div>
      )}
      <div className="stagger space-y-2">
        {reports.length === 0 && (
          <Empty text="No scrum tickets yet — hit Regenerate today" />
        )}
        {latest && <Ticket key={latest.id} r={latest} onSaved={load} />}
        {older.length > 0 && (
          <>
            <div className="mb-1 mt-3 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-600">
              <ClipboardList size={11} /> Previous days
            </div>
            {older.map((r) => (
              <Ticket key={r.id} r={r} onSaved={load} />
            ))}
          </>
        )}
      </div>
    </Panel>
  )
}
