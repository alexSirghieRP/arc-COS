import React, { useEffect, useRef, useState } from 'react'
import { Check, X, Undo2, Clock, CheckSquare, BellRing, Search, AlertTriangle } from 'lucide-react'
import { Panel } from '../App.jsx'
import { post, snoozeItem, bulkStatus, wakeAllSnoozed } from '../api.js'
import { useToast, decodeEntities } from '../ui.jsx'

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
  done: 'done',
}

const ago = (iso) => {
  const m = Math.round((Date.now() - new Date(iso)) / 60000)
  if (m < 60) return `${m}m`
  if (m < 1440) return `${Math.round(m / 60)}h`
  return `${Math.round(m / 1440)}d`
}

const SNOOZE_OPTIONS = [
  { label: '2h', hours: 2 },
  { label: '4h', hours: 4 },
  { label: 'EOD', hours: 6 },
  { label: 'Tomorrow', hours: 20 },
]

function Ping({ p, refresh, selectable, selected, onToggle, keySelected }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [snoozeOpen, setSnoozeOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const closed = p.status === 'dismissed' || p.status === 'done'
  const long = (p.content || '').length > 160

  const setStatus = async (status) => {
    setBusy(true)
    try {
      await post(`/api/items/${p.id}/status`, { status })
      refresh()
    } catch (e) {
      toast(`Could not update: ${String(e).slice(0, 120)}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const snooze = async (hours) => {
    setSnoozeOpen(false)
    setBusy(true)
    try {
      await snoozeItem(p.id, hours)
      toast(`Snoozed for ${hours < 10 ? hours + 'h' : 'tomorrow'}`, 'info')
      refresh()
    } catch (e) {
      toast(`Snooze failed: ${String(e).slice(0, 100)}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={`group relative rounded-lg border px-2.5 py-1.5 text-sm ${
        keySelected ? 'border-[var(--accent)] bg-zinc-800/40 ring-1 ring-[var(--accent)]/30' :
        selectable && selected ? 'border-sky-700 bg-sky-950/20' :
        p.urgent && !closed
          ? 'border-red-900/30 border-l-[3px] border-l-red-500 bg-red-950/15'
          : 'border-zinc-800 bg-zinc-900'
      } ${closed ? 'opacity-50' : ''}`}
    >
      <div className="flex items-center gap-2">
        {selectable && !closed && (
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            className="shrink-0 accent-sky-500"
            onClick={(e) => e.stopPropagation()}
          />
        )}
        <span className="font-medium text-zinc-100">{p.sender}</span>
        {p.type === 'mention' && (
          <span className="rounded bg-orange-900 px-1 text-[10px] text-orange-200">@you</span>
        )}
        {p.tier && (
          <span className={`rounded px-1.5 text-[10px] font-bold ${tierCls[p.tier]}`}>
            {p.tier}
          </span>
        )}
        <span className="truncate text-[11px] text-zinc-500">{p.subject}</span>
        <span className="ml-auto flex shrink-0 items-center gap-1">
          <span className="mr-1 text-[11px] text-zinc-500">{ago(p.received_at)}</span>
          <span className="text-[10px] text-zinc-600">{statusText[p.status] || p.status}</span>
          {!closed && (
            <>
              {/* Snooze menu */}
              <div className="relative">
                <button
                  disabled={busy}
                  onClick={() => setSnoozeOpen((v) => !v)}
                  title="Snooze"
                  className="press grid h-5 w-5 place-items-center rounded text-zinc-500 opacity-0 hover:bg-zinc-800 hover:text-amber-300 group-hover:opacity-100 disabled:opacity-30"
                >
                  <Clock size={12} />
                </button>
                {snoozeOpen && (
                  <div className="absolute right-0 top-6 z-20 flex flex-col overflow-hidden rounded-lg border border-zinc-700 bg-zinc-900 shadow-lg">
                    {SNOOZE_OPTIONS.map((o) => (
                      <button
                        key={o.label}
                        onClick={() => snooze(o.hours)}
                        className="px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800"
                      >
                        {o.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <button
                disabled={busy}
                onClick={() => setStatus('done')}
                title="Mark handled"
                className="press grid h-5 w-5 place-items-center rounded text-zinc-500 opacity-0 hover:bg-emerald-900/50 hover:text-emerald-300 group-hover:opacity-100 disabled:opacity-30"
              >
                <Check size={13} />
              </button>
              <button
                disabled={busy}
                onClick={() => setStatus('dismissed')}
                title="Dismiss (noise)"
                className="press grid h-5 w-5 place-items-center rounded text-zinc-500 opacity-0 hover:bg-zinc-800 hover:text-zinc-200 group-hover:opacity-100 disabled:opacity-30"
              >
                <X size={13} />
              </button>
            </>
          )}
          {closed && (
            <button
              disabled={busy}
              onClick={() => setStatus('new')}
              title="Reopen"
              className="press grid h-5 w-5 place-items-center rounded text-zinc-500 opacity-0 hover:bg-zinc-800 hover:text-zinc-200 group-hover:opacity-100 disabled:opacity-30"
            >
              <Undo2 size={12} />
            </button>
          )}
        </span>
      </div>
      <div
        onClick={() => long && setExpanded((v) => !v)}
        className={`text-zinc-400 ${expanded ? 'whitespace-pre-wrap break-words' : 'truncate'} ${long ? 'cursor-pointer hover:text-zinc-300' : ''}`}
        title={!expanded && long ? 'Click to expand' : undefined}
      >
        {decodeEntities(p.content)}
        {!expanded && long && <span className="ml-1 text-[10px] text-zinc-600">···</span>}
      </div>
      {p.tier_reasoning && (
        <div className="mt-0.5 truncate text-[11px] italic text-zinc-600" title={p.tier_reasoning}>
          {p.tier_reasoning}
        </div>
      )}
      {snoozeOpen && (
        <div className="fixed inset-0 z-10" onClick={() => setSnoozeOpen(false)} />
      )}
    </div>
  )
}

export default function PingsPanel({ board, refresh }) {
  const toast = useToast()
  const [showClosed, setShowClosed] = useState(false)
  const [tierFilter, setTierFilter] = useState('all')
  const [searchQ, setSearchQ] = useState('')
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState(new Set())
  const [bulkBusy, setBulkBusy] = useState(false)
  const [keyIdx, setKeyIdx] = useState(0)
  const pingRefs = useRef([])

  const all = board.pings || []
  const snoozedCount = board.snoozed_count || 0

  const sorted = [...all].sort((a, b) => {
    if (a.urgent !== b.urgent) return (b.urgent || 0) - (a.urgent || 0)
    return new Date(b.received_at) - new Date(a.received_at)
  })

  const active = sorted.filter((p) => p.status !== 'dismissed' && p.status !== 'done')
  const closed = sorted.filter((p) => p.status === 'dismissed' || p.status === 'done')

  const q = searchQ.trim().toLowerCase()
  const filtered = (showClosed ? sorted : active)
    .filter((p) => tierFilter === 'all' || p.tier === tierFilter)
    .filter((p) => !q || (p.sender || '').toLowerCase().includes(q) ||
                         (p.content || '').toLowerCase().includes(q) ||
                         (p.subject || '').toLowerCase().includes(q))

  const tiers = ['all', ...['A', 'B', 'C'].filter((t) => active.some((p) => p.tier === t))]

  // Urgent items sort first; show section headers only in the default view
  // (any filter changes the ordering guarantees the split relies on).
  const sectionAt =
    !showClosed && !q && tierFilter === 'all' ? filtered.filter((p) => p.urgent).length : 0

  // Clamp keyboard cursor when the filtered list changes.
  useEffect(() => {
    setKeyIdx((i) => Math.min(i, Math.max(0, filtered.length - 1)))
  }, [filtered.length])

  // Scroll keyboard-selected ping into view.
  useEffect(() => {
    pingRefs.current[keyIdx]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [keyIdx])

  // j/k to navigate, Enter/c to mark done, d to dismiss.
  useEffect(() => {
    if (!filtered.length || selectMode) return
    const onKey = async (e) => {
      const typing = /input|textarea|select/i.test(document.activeElement?.tagName || '')
      if (typing) return
      if (e.key === 'j') {
        e.preventDefault()
        setKeyIdx((i) => Math.min(i + 1, filtered.length - 1))
      } else if (e.key === 'k') {
        e.preventDefault()
        setKeyIdx((i) => Math.max(i - 1, 0))
      } else if (e.key === 'Enter' || e.key === 'c') {
        const p = filtered[keyIdx]
        if (!p || p.status === 'dismissed' || p.status === 'done') return
        e.preventDefault()
        await post(`/api/items/${p.id}/status`, { status: 'done' })
        refresh()
      } else if (e.key === 'd') {
        const p = filtered[keyIdx]
        if (!p || p.status === 'dismissed' || p.status === 'done') return
        e.preventDefault()
        await post(`/api/items/${p.id}/status`, { status: 'dismissed' })
        refresh()
      } else if (e.key === 's') {
        const p = filtered[keyIdx]
        if (!p || p.status === 'dismissed' || p.status === 'done') return
        e.preventDefault()
        try {
          await snoozeItem(p.id, 2)
          toast('Snoozed 2h', 'info')
          refresh()
        } catch {}
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [filtered, keyIdx, selectMode, refresh, toast])

  const toggleSelect = (id) =>
    setSelected((s) => {
      const n = new Set(s)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })

  const toggleAll = () => {
    const activeIds = filtered.filter((p) => p.status !== 'dismissed' && p.status !== 'done').map((p) => p.id)
    if (selected.size === activeIds.length && activeIds.every((id) => selected.has(id))) {
      setSelected(new Set())
    } else {
      setSelected(new Set(activeIds))
    }
  }

  const doBulk = async (status) => {
    const ids = [...selected]
    if (!ids.length) return
    setBulkBusy(true)
    try {
      const r = await bulkStatus(ids, status)
      toast(`${status === 'done' ? 'Marked done' : 'Dismissed'}: ${r.updated} pings`, 'success')
      setSelected(new Set())
      setSelectMode(false)
      refresh()
    } catch (e) {
      toast(`Bulk action failed: ${String(e).slice(0, 120)}`, 'error')
    } finally {
      setBulkBusy(false)
    }
  }

  const exitSelect = () => { setSelectMode(false); setSelected(new Set()) }

  return (
    <Panel
      title="Pings"
      badge={active.length}
      actions={
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search size={11} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-zinc-500" />
            <input
              value={searchQ}
              onChange={(e) => setSearchQ(e.target.value)}
              placeholder="search…"
              className="w-32 rounded-lg bg-zinc-800 py-0.5 pl-6 pr-2 text-[11px] text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
            />
          </div>
          {tiers.length > 2 && !selectMode && (
            <div className="flex gap-1">
              {tiers.map((t) => (
                <button
                  key={t}
                  onClick={() => setTierFilter(t)}
                  className={`rounded px-2 py-0.5 text-[10px] font-bold transition-colors ${
                    tierFilter === t
                      ? 'bg-zinc-600 text-zinc-100'
                      : 'bg-zinc-800 text-zinc-500 hover:text-zinc-300'
                  }`}
                >
                  {t === 'all' ? 'All' : `T${t}`}
                </button>
              ))}
            </div>
          )}
          {snoozedCount > 0 && !selectMode && (
            <button
              onClick={async () => {
                try {
                  const r = await wakeAllSnoozed()
                  toast(`Woke ${r.woken} snoozed ping${r.woken === 1 ? '' : 's'}`, 'info')
                  refresh()
                } catch (e) {
                  toast(`Wake failed: ${String(e).slice(0, 100)}`, 'error')
                }
              }}
              title="Un-snooze all — bring back all deferred pings now"
              className="press flex items-center gap-1 rounded-full bg-amber-900/40 px-2 py-0.5 text-[10px] text-amber-300 hover:bg-amber-800/60 hover:text-amber-100"
            >
              <BellRing size={10} />
              {snoozedCount} snoozed · wake all
            </button>
          )}
          {active.length > 0 && !selectMode && (
            <span className="text-[10px] text-zinc-600" title="j/k navigate · Enter/c mark done · d dismiss · s snooze 2h">
              j/k · ↵ done · d · s snooze
            </span>
          )}
          {active.length > 1 && (
            <button
              onClick={() => (selectMode ? exitSelect() : setSelectMode(true))}
              className={`press flex items-center gap-1 rounded-lg px-2.5 py-1 text-[11px] ${
                selectMode
                  ? 'bg-zinc-700 text-zinc-200'
                  : 'bg-zinc-800 text-zinc-400 hover:text-zinc-200'
              }`}
            >
              <CheckSquare size={12} />
              {selectMode ? 'cancel' : 'select'}
            </button>
          )}
          {!selectMode && closed.length > 0 && (
            <button
              onClick={() => setShowClosed((v) => !v)}
              className="press rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-400 hover:text-zinc-200"
            >
              {showClosed ? 'hide' : 'show'} {closed.length} handled
            </button>
          )}
        </div>
      }
    >
      {selectMode && (
        <div className="mb-2 flex items-center gap-2 rounded-lg border border-sky-800 bg-sky-950/30 px-3 py-2">
          <button
            onClick={toggleAll}
            className="press text-[11px] text-sky-300 hover:text-sky-100"
          >
            {selected.size > 0 ? 'deselect all' : 'select all'}
          </button>
          <span className="text-[11px] text-zinc-500">{selected.size} selected</span>
          <div className="ml-auto flex items-center gap-2">
            <button
              disabled={selected.size === 0 || bulkBusy}
              onClick={() => doBulk('done')}
              className="press flex items-center gap-1 rounded bg-emerald-800 px-2.5 py-1 text-[11px] text-emerald-100 hover:bg-emerald-700 disabled:opacity-40"
            >
              <Check size={11} /> Done ({selected.size})
            </button>
            <button
              disabled={selected.size === 0 || bulkBusy}
              onClick={() => doBulk('dismissed')}
              className="press flex items-center gap-1 rounded bg-zinc-700 px-2.5 py-1 text-[11px] text-zinc-200 hover:bg-zinc-600 disabled:opacity-40"
            >
              <X size={11} /> Dismiss ({selected.size})
            </button>
          </div>
        </div>
      )}
      <div className="stagger space-y-1.5">
        {filtered.length === 0 && (
          <div className="text-sm text-zinc-500">
            {tierFilter !== 'all' ? `no tier ${tierFilter} pings` : 'quiet so far'}
          </div>
        )}
        {filtered.map((p, i) => (
          <React.Fragment key={p.id}>
            {sectionAt > 0 && i === 0 && (
              <div className="flex items-center gap-1.5 px-1 pb-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-red-400/90">
                <AlertTriangle size={11} />
                Needs attention · {sectionAt}
              </div>
            )}
            {sectionAt > 0 && i === sectionAt && (
              <div className="flex items-center gap-1.5 px-1 pb-0.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-600">
                Everything else · {filtered.length - sectionAt}
              </div>
            )}
            <div ref={(el) => { pingRefs.current[i] = el }}>
              <Ping
                p={p}
                refresh={refresh}
                selectable={selectMode}
                selected={selected.has(p.id)}
                onToggle={() => toggleSelect(p.id)}
                keySelected={!selectMode && i === keyIdx}
              />
            </div>
          </React.Fragment>
        ))}
      </div>
    </Panel>
  )
}
