import React, { useCallback, useEffect, useRef, useState } from 'react'
import { SendHorizonal, RotateCcw, Zap, ChevronDown, ChevronUp, FastForward, X } from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'
import { useToast, decodeEntities } from '../ui.jsx'

const tierCls = { A: 'bg-zinc-700 text-zinc-300', B: 'bg-sky-900 text-sky-200', C: 'bg-red-900 text-red-200' }

const CONTEXT_PREVIEW = 280

function renderMd(text) {
  if (!text) return ''
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code class="bg-zinc-800 px-1 rounded text-[11px]">$1</code>')
    .replace(/^#{1,3} (.+)$/gm, '<div class="font-semibold text-zinc-200 mt-2">$1</div>')
    .replace(/^- (.+)$/gm, '<div class="flex gap-2"><span class="text-zinc-600">•</span><span>$1</span></div>')
    .replace(/\n/g, '<br/>')
}

function ago(iso) {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  return `${d}d ago`
}

const STALE_DAYS = 3  // matches backend stale_draft_days

function draftUrgency(iso) {
  if (!iso) return { level: 'ok', label: null }
  const ageMs = Date.now() - new Date(iso).getTime()
  const ageDays = ageMs / 86400000
  const remainingHrs = Math.max(0, (STALE_DAYS * 24) - (ageMs / 3600000))
  if (ageDays >= STALE_DAYS - 0.5) return { level: 'critical', label: `expires ~${Math.round(remainingHrs)}h` }
  if (ageDays >= STALE_DAYS - 1) return { level: 'warn', label: `expires ~${Math.round(remainingHrs)}h` }
  if (ageDays >= 1) return { level: 'old', label: ago(iso) }
  return { level: 'ok', label: null }
}

function isOld(iso) {
  if (!iso) return false
  return Date.now() - new Date(iso).getTime() > 24 * 60 * 60 * 1000
}

function ExpandableContext({ content: raw, sender }) {
  const [expanded, setExpanded] = useState(false)
  const content = decodeEntities(raw)
  const long = content.length > CONTEXT_PREVIEW
  const display = expanded ? content : content.slice(0, CONTEXT_PREVIEW)
  return (
    <div className="mb-1.5 rounded-md bg-zinc-950 px-2 py-1.5 text-xs text-zinc-400">
      <span className="mr-1 font-medium text-zinc-500">{sender ? `${sender}:` : 'them:'}</span>
      <span className="whitespace-pre-wrap">{display}{!expanded && long ? '…' : ''}</span>
      {long && (
        <button
          onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v) }}
          className="ml-1.5 inline-flex items-center gap-0.5 text-[10px] text-zinc-600 hover:text-zinc-300"
        >
          {expanded ? <><ChevronUp size={10} /> less</> : <><ChevronDown size={10} /> {content.length - CONTEXT_PREVIEW} more chars</>}
        </button>
      )}
    </div>
  )
}

function Draft({ d, refresh, dryRun, selected, onSelect, approveRef, rejectRef, dismissRef, onActed, speedrun }) {
  const toast = useToast()
  const [body, setBody] = useState(d.body)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [previewMode, setPreviewMode] = useState(false)
  const edited = body !== d.body
  const urgency = draftUrgency(d.created_at)

  // Reset preview mode whenever the selected draft changes
  useEffect(() => {
    setPreviewMode(false)
  }, [d.id])

  const act = useCallback(async (action) => {
    setBusy(true)
    try {
      let r
      if (action === 'dismiss') {
        r = await post(`/api/drafts/${d.id}/dismiss`, {})
        toast('Draft dismissed', 'info')
      } else if (action === 'approve') {
        r = await post(`/api/drafts/${d.id}/approve`, { body })
        toast(
          r.result === 'dry_run'
            ? 'Approved (dry run — nothing actually sent)'
            : `Sent to ${d.recipient || d.sender}`,
          'success',
        )
      } else {
        r = await post(`/api/drafts/${d.id}/reject`, { reason })
        toast('Draft rejected (logged for calibration)', 'info')
      }
      onActed?.()
      refresh()
    } catch (e) {
      toast(`${action} failed: ${String(e).slice(0, 160)}`, 'error')
    } finally {
      setBusy(false)
    }
  }, [body, d.id, d.recipient, d.sender, reason, refresh, toast, onActed])

  // Expose approve/reject/dismiss handlers upward for keyboard shortcuts
  useEffect(() => {
    if (!selected) return
    if (approveRef) approveRef.current = () => act('approve')
    if (rejectRef) rejectRef.current = () => act('reject')
    if (dismissRef) dismissRef.current = () => act('dismiss')
  }, [selected, act, approveRef, rejectRef, dismissRef])

  const borderCls = selected
    ? 'border-[var(--accent)] bg-zinc-800/60'
    : urgency.level === 'critical' ? 'border-red-700 bg-red-950/20 hover:border-red-600'
    : urgency.level === 'warn'     ? 'border-amber-700 bg-amber-950/10 hover:border-amber-600'
    : 'border-zinc-700 bg-zinc-900 hover:border-zinc-600'

  return (
    <div
      onClick={onSelect}
      className={`rounded-xl border p-3 cursor-pointer transition-colors ${borderCls}`}
    >
      <div className="mb-1.5 flex items-center gap-2 text-xs text-zinc-400">
        <span className={`rounded px-1.5 font-bold ${d.channel === 'email' ? 'bg-indigo-900 text-indigo-200' : 'bg-purple-900 text-purple-200'}`}>
          {d.channel}
        </span>
        {d.tier && (
          <span className={`rounded px-1.5 text-[10px] font-bold ${tierCls[d.tier] || 'bg-zinc-700 text-zinc-300'}`}>
            {d.tier}
          </span>
        )}
        {d.urgent ? (
          <span className="flex items-center gap-0.5 rounded bg-red-900 px-1.5 text-[10px] font-bold text-red-200">
            <Zap size={9} /> urgent
          </span>
        ) : null}
        <span>to {d.recipient || d.sender}</span>
        {d.subject && <span className="truncate">· {d.subject}</span>}
        {edited && (
          <span className="rounded bg-amber-900 px-1.5 text-[10px] font-medium text-amber-200">
            edited
          </span>
        )}
        {selected && (
          <span className="ml-1 rounded bg-zinc-700 px-1.5 text-[9px] font-semibold uppercase tracking-wide text-zinc-400">
            selected · a approve · r reject · d dismiss{speedrun ? ' · s skip' : ''}
          </span>
        )}
        {/* Expiry countdown — red when critical, amber when warn */}
        {urgency.label ? (
          <span className={`ml-auto shrink-0 rounded px-1.5 text-[10px] font-semibold ${
            urgency.level === 'critical' ? 'bg-red-900 text-red-300' : 'bg-amber-900 text-amber-300'
          }`}>
            {urgency.label}
          </span>
        ) : (
          <span className="ml-auto shrink-0 rounded px-1.5 text-[10px] font-medium bg-zinc-800 text-zinc-500">
            {ago(d.created_at)}
          </span>
        )}
      </div>
      {d.item_content && <ExpandableContext content={d.item_content} sender={d.sender} />}
      <div className="mb-1 flex justify-end">
        <button
          onClick={() => setPreviewMode((v) => !v)}
          className="press rounded bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200"
        >
          {previewMode ? 'Edit' : 'Preview'}
        </button>
      </div>
      {previewMode ? (
        <div
          className="min-h-[44px] rounded bg-zinc-950 p-2.5 text-[12px] text-zinc-300 leading-relaxed"
          dangerouslySetInnerHTML={{ __html: renderMd(body) }}
        />
      ) : (
        <textarea
          className="w-full resize-y rounded-md bg-zinc-800 p-2 text-sm text-zinc-100 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
          rows={Math.min(8, Math.max(3, body.split('\n').length))}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onFocus={onSelect}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') act('approve')
          }}
        />
      )}
      <div className="mt-0.5 text-[10px] text-zinc-600">
        {body.length} chars · {body.trim() ? body.trim().split(/\s+/).length : 0} words
      </div>
      <div className="mt-1.5 flex items-center gap-2">
        <button
          disabled={busy}
          onClick={() => act('approve')}
          title="⌘Enter · or press 'a' when selected"
          className="press flex items-center gap-1.5 rounded-lg bg-green-700 px-3 py-1 text-sm font-medium text-white hover:bg-green-600 disabled:opacity-50"
        >
          <SendHorizonal size={13} />
          {dryRun ? 'Approve (dry run)' : 'Approve & send'}
        </button>
        {edited && (
          <button
            disabled={busy}
            onClick={() => setBody(d.body)}
            title="Restore Chief's draft"
            className="press flex items-center gap-1 rounded-lg bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200"
          >
            <RotateCcw size={11} /> reset
          </button>
        )}
        <input
          id={`reject-reason-${d.id}`}
          className="grow rounded-lg bg-zinc-800 px-2 py-1 text-xs text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
          placeholder="reject reason → Chief auto-retries with feedback"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') act('reject') }}
        />
        <button
          disabled={busy}
          onClick={() => act('reject')}
          title="press 'r' when selected to focus reason input"
          className="press rounded-lg bg-zinc-700 px-3 py-1 text-sm hover:bg-red-900 disabled:opacity-50"
        >
          Reject
        </button>
        <button
          disabled={busy}
          onClick={() => act('dismiss')}
          title="d — dismiss silently (noise, already handled)"
          className="press ml-auto flex items-center gap-1 rounded-lg bg-zinc-800 px-2 py-1 text-xs text-zinc-500 hover:bg-zinc-700 hover:text-zinc-300 disabled:opacity-50"
        >
          <X size={11} /> dismiss
        </button>
      </div>
    </div>
  )
}

export default function ApprovalsPanel({ board, refresh }) {
  const drafts = board.drafts || []
  const [selectedIdx, setSelectedIdx] = useState(0)
  const approveRef = useRef(null)
  const rejectRef = useRef(null)
  const dismissRef = useRef(null)
  const [speedrun, setSpeedrun] = useState(false)
  const [sessionTotal, setSessionTotal] = useState(0)
  const [sessionApproved, setSessionApproved] = useState(0)
  const [bulkBusy, setBulkBusy] = useState(false)
  const toast = useToast()

  const bulkDismissStale = useCallback(async () => {
    setBulkBusy(true)
    try {
      const r = await post('/api/drafts/bulk-dismiss-stale', { tier: 'B', older_than_hours: 12 })
      if (r.dismissed > 0) {
        toast(`Dismissed ${r.dismissed} stale B-tier drafts (>12h)`, 'success')
        refresh()
      } else {
        toast('No stale B-tier drafts to dismiss', 'info')
      }
    } catch (e) {
      toast(`Bulk dismiss failed: ${String(e).slice(0, 120)}`, 'error')
    } finally {
      setBulkBusy(false)
    }
  }, [refresh, toast])

  // Auto-disable speedrun when the queue drains to zero
  useEffect(() => {
    if (speedrun && drafts.length === 0) setSpeedrun(false)
  }, [speedrun, drafts.length])

  // Clamp selectedIdx when drafts change
  useEffect(() => {
    setSelectedIdx((i) => Math.min(i, Math.max(0, drafts.length - 1)))
  }, [drafts.length])

  const handleActed = useCallback(() => {
    setSessionApproved((n) => n + 1)
  }, [])

  const toggleSpeedrun = useCallback(() => {
    setSpeedrun((v) => {
      if (!v) {
        setSessionTotal(drafts.length)
        setSessionApproved(0)
      }
      return !v
    })
  }, [drafts.length])

  useEffect(() => {
    if (!drafts.length) return
    const onKey = (e) => {
      const typing = /input|textarea|select/i.test(document.activeElement?.tagName || '')
      if (typing) return
      if (e.key === 'j') {
        e.preventDefault()
        setSelectedIdx((i) => Math.min(i + 1, drafts.length - 1))
      } else if (e.key === 'k') {
        e.preventDefault()
        setSelectedIdx((i) => Math.max(i - 1, 0))
      } else if (e.key === 'a') {
        e.preventDefault()
        approveRef.current?.()
      } else if (e.key === 'r') {
        e.preventDefault()
        const id = drafts[selectedIdx]?.id
        if (id) document.getElementById(`reject-reason-${id}`)?.focus()
      } else if (e.key === 'd') {
        e.preventDefault()
        dismissRef.current?.()
      } else if (e.key === 's' && speedrun) {
        e.preventDefault()
        setSelectedIdx((i) => Math.min(i + 1, drafts.length - 1))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drafts, selectedIdx, speedrun])

  const approvedPct = sessionTotal > 0 ? Math.round((sessionApproved / sessionTotal) * 100) : 0

  const staleBTier = drafts.filter((d) => d.tier === 'B').length

  return (
    <Panel
      title="Approvals"
      badge={drafts.length}
      actions={
        <div className="flex items-center gap-2">
          {staleBTier > 0 && (
            <button
              onClick={bulkDismissStale}
              disabled={bulkBusy}
              title="Dismiss B-tier drafts older than 12h (conversation has moved on)"
              className="press flex items-center gap-1 rounded-full bg-zinc-800 px-2 py-0.5 text-[11px] text-zinc-500 hover:bg-zinc-700 hover:text-zinc-300 disabled:opacity-40"
            >
              <X size={10} /> stale B ({staleBTier})
            </button>
          )}
          {speedrun && sessionTotal > 0 && (
            <span className="tabular-nums text-[11px] text-zinc-400">
              {sessionApproved}/{sessionTotal} done
            </span>
          )}
          {drafts.length > 1 && (
            <span className="text-[11px] text-zinc-500">
              {selectedIdx + 1}/{drafts.length} · j/k navigate{speedrun ? ' · s skip' : ''}
            </span>
          )}
          {drafts.length > 0 && (
            <button
              onClick={toggleSpeedrun}
              title="Toggle speedrun mode: auto-advance after each action, s to skip"
              className={`press flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors ${
                speedrun
                  ? 'bg-amber-600 text-white hover:bg-amber-500'
                  : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200'
              }`}
            >
              <FastForward size={11} />
              Speedrun
            </button>
          )}
          {board.dry_run && (
            <span className="rounded-full bg-amber-900 px-2 py-0.5 text-[11px] font-medium text-amber-200">
              dry run — approvals won't actually send
            </span>
          )}
        </div>
      }
    >
      {speedrun && sessionTotal > 0 && (
        <div className="mb-3 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2">
          <div className="mb-1.5 flex items-center justify-between text-[11px]">
            <span className="flex items-center gap-1 font-medium text-amber-400">
              <FastForward size={11} />
              Speedrun
            </span>
            <span className="tabular-nums text-zinc-400">
              {sessionApproved} / {sessionTotal} approved
            </span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-zinc-800">
            <div
              className="h-full rounded-full bg-emerald-500 transition-all duration-300"
              style={{ width: `${approvedPct}%` }}
            />
          </div>
        </div>
      )}
      <div className="stagger space-y-2">
        {drafts.length === 0 && <div className="text-sm text-zinc-500">nothing waiting on you</div>}
        {drafts.map((d, i) => (
          <Draft
            key={d.id}
            d={d}
            refresh={refresh}
            dryRun={board.dry_run}
            selected={i === selectedIdx}
            onSelect={() => setSelectedIdx(i)}
            approveRef={i === selectedIdx ? approveRef : null}
            rejectRef={i === selectedIdx ? rejectRef : null}
            dismissRef={i === selectedIdx ? dismissRef : null}
            onActed={speedrun ? handleActed : null}
            speedrun={speedrun}
          />
        ))}
      </div>
    </Panel>
  )
}
