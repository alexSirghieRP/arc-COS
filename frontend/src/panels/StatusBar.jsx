import React, { useEffect, useState } from 'react'
import { Sun, Moon, RefreshCw, Power, Activity, ChevronDown, Command, FileText, Bell, HelpCircle, X, BookOpen } from 'lucide-react'
import { setSetting } from '../api.js'
import { useToast, CountUp } from '../ui.jsx'

const SHORTCUTS = [
  { ctx: 'Global', key: '⌘K', desc: 'Command palette (tabs, sweeps, toggles)' },
  { ctx: 'Global', key: '?', desc: 'This keyboard shortcuts reference' },
  { ctx: 'Global', key: '⌘B', desc: 'Toggle sidebar collapse / expand' },
  { ctx: 'Global', key: '⌘↑ / ⌘↓', desc: 'Move to previous / next tab' },
  { ctx: 'Approvals tab', key: 'j / k', desc: 'Navigate between drafts' },
  { ctx: 'Approvals tab', key: 'a', desc: 'Approve & send selected draft' },
  { ctx: 'Approvals tab', key: 'r', desc: 'Focus reject-reason input' },
  { ctx: 'Approvals tab', key: 'd', desc: 'Dismiss (silent / noise)' },
  { ctx: 'Approvals tab', key: 's', desc: 'Skip to next (speedrun mode)' },
  { ctx: 'Approvals tab', key: '⌘↵', desc: 'Approve draft (from textarea)' },
  { ctx: 'Pings tab', key: 'j / k', desc: 'Navigate between pings' },
  { ctx: 'Pings tab', key: '↵ or c', desc: 'Mark selected ping as done' },
  { ctx: 'Pings tab', key: 'd', desc: 'Dismiss selected ping' },
  { ctx: 'Pings tab', key: 's', desc: 'Snooze selected ping 2 hours' },
  { ctx: 'Today tab', key: 'click checkbox', desc: 'Toggle task done/undone' },
  { ctx: 'Today tab', key: 'click text', desc: 'Edit task text in-place (Enter to save)' },
  { ctx: 'Command palette', key: '↑ / ↓ or ⌘K again', desc: 'Navigate results' },
  { ctx: 'Command palette', key: '↵', desc: 'Run selected command' },
  { ctx: 'Command palette', key: '/keyword', desc: 'Search items (pings, audit log)' },
  { ctx: 'Command palette', key: '@note', desc: 'Search Obsidian vault notes by name or content' },
  { ctx: 'Command palette', key: 'Esc', desc: 'Close palette' },
]

function ShortcutsModal({ onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' || e.key === '?') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const groups = SHORTCUTS.reduce((acc, s) => {
    ;(acc[s.ctx] = acc[s.ctx] || []).push(s)
    return acc
  }, {})

  return (
    <div
      className="animate-fade-in fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-[2px] pt-[10vh]"
      onMouseDown={onClose}
    >
      <div
        className="palette-in w-[540px] max-w-[92vw] overflow-hidden rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-zinc-800 px-4 py-3">
          <HelpCircle size={15} className="text-zinc-500" />
          <span className="flex-1 text-[14px] font-semibold text-zinc-200">Keyboard shortcuts</span>
          <button onClick={onClose} title="Close" aria-label="Close" className="press text-zinc-500 hover:text-zinc-200">
            <X size={15} />
          </button>
        </div>
        <div className="max-h-[65vh] overflow-y-auto p-4 space-y-4">
          {Object.entries(groups).map(([ctx, items]) => (
            <div key={ctx}>
              <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-600">{ctx}</div>
              <div className="space-y-1">
                {items.map((s) => (
                  <div key={s.key} className="flex items-center gap-3">
                    <kbd className="shrink-0 rounded bg-zinc-800 px-2 py-0.5 text-[11px] font-mono text-zinc-300 ring-1 ring-zinc-700">
                      {s.key}
                    </kbd>
                    <span className="text-[12px] text-zinc-400">{s.desc}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="border-t border-zinc-800 px-4 py-2 text-[10px] text-zinc-600">
          Press <kbd className="rounded bg-zinc-800 px-1 font-mono text-zinc-500 ring-1 ring-zinc-700">?</kbd> or <kbd className="rounded bg-zinc-800 px-1 font-mono text-zinc-500 ring-1 ring-zinc-700">Esc</kbd> to close
        </div>
      </div>
    </div>
  )
}

function VaultIndicator() {
  const [count, setCount] = useState(null)
  useEffect(() => {
    fetch('/api/obsidian/stats')
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setCount(d.total_notes) })
      .catch(() => {})
  }, [])
  if (count == null) return null
  return (
    <span
      title={`Obsidian vault: ${count} notes`}
      className="flex items-center gap-1 rounded bg-violet-900/30 px-2 py-0.5 text-[11px] text-violet-500"
    >
      <BookOpen size={11} />
      {count}
    </span>
  )
}

function CaptureButton() {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const capture = async () => {
    setBusy(true)
    try {
      const r = await fetch('/api/obsidian/capture-board', { method: 'POST' })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || 'failed')
      toast(`Board captured → ${data.path}`, 'success')
    } catch (e) {
      toast(`Capture failed: ${String(e).slice(0, 100)}`, 'error')
    } finally {
      setBusy(false)
    }
  }
  return (
    <button
      onClick={capture}
      disabled={busy}
      title="Capture current board state to Obsidian"
      className="press flex h-8 items-center gap-1.5 rounded-lg bg-violet-900/40 px-2.5 text-[11px] text-violet-400 hover:bg-violet-900/60 hover:text-violet-200 disabled:opacity-40"
    >
      <BookOpen size={12} className={busy ? 'animate-pulse' : ''} />
      {busy ? 'saving…' : 'Capture'}
    </button>
  )
}

const presenceColor = {
  Available: 'bg-emerald-400',
  Busy: 'bg-red-400',
  DoNotDisturb: 'bg-red-500',
  Away: 'bg-amber-400',
  BeRightBack: 'bg-amber-400',
  Offline: 'bg-zinc-500',
}

const usd = (n) => (n >= 100 ? `$${Math.round(n).toLocaleString()}` : `$${(n ?? 0).toFixed(2)}`)

function Toggle({ on, onClick, label, tone = 'accent' }) {
  const onColor = tone === 'danger' ? 'bg-red-500' : 'bg-[var(--accent-strong)]'
  return (
    <button onClick={onClick} className="press flex items-center gap-2 text-[13px] text-zinc-300">
      <span
        className={`relative h-[18px] w-8 shrink-0 rounded-full transition-colors ${on ? onColor : 'bg-zinc-700'}`}
      >
        <span
          className={`absolute left-0 top-0.5 h-[14px] w-[14px] rounded-full bg-white shadow transition-transform duration-200 ${
            on ? 'translate-x-[16px]' : 'translate-x-[2px]'
          }`}
        />
      </span>
      {label}
    </button>
  )
}

export default function StatusBar({ board, refresh, lastSync, sseConnected, runningJobs, lastSweepMsg, openPalette, setTab }) {
  const toast = useToast()
  const [connectors, setConnectors] = useState({})
  const [cost, setCost] = useState(null)
  const [costBusy, setCostBusy] = useState(false)
  const [showShortcuts, setShowShortcuts] = useState(false)

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== '?') return
      const typing = /input|textarea|select/i.test(document.activeElement?.tagName || '')
      if (typing) return
      e.preventDefault()
      setShowShortcuts((v) => !v)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  // re-render every few seconds so the staleness dot stays honest
  const [, tick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 5000)
    return () => clearInterval(t)
  }, [])
  const stale = lastSync && Date.now() - lastSync > 20000
  const [light, setLight] = useState(
    () => typeof document !== 'undefined' && document.documentElement.classList.contains('light'),
  )

  const refreshCost = async () => {
    setCostBusy(true)
    try {
      const r = await fetch('/api/cost?refresh=1')
      setCost(await r.json())
    } catch (e) {
      /* ignore */
    } finally {
      setCostBusy(false)
    }
  }

  const toggleTheme = () => {
    const next = !light
    setLight(next)
    document.documentElement.classList.toggle('light', next)
    try {
      localStorage.setItem('theme', next ? 'light' : 'dark')
    } catch (e) {}
  }

  useEffect(() => {
    const load = () =>
      fetch('/api/connectors').then((r) => r.json()).then(setConnectors).catch(() => {})
    const loadCost = () =>
      fetch('/api/cost').then((r) => r.json()).then(setCost).catch(() => {})
    load()
    loadCost()
    const t = setInterval(load, 60000)
    const tc = setInterval(loadCost, 300000)
    return () => {
      clearInterval(t)
      clearInterval(tc)
    }
  }, [])

  const flip = async (key, value) => {
    try {
      await setSetting(key, value)
      refresh()
    } catch (e) {
      toast(`Could not set ${key}: ${String(e).slice(0, 120)}`, 'error')
    }
  }

  const presence = board.presence.availability || 'unknown'

  return (
    <header className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-zinc-800 bg-zinc-900 px-4 py-2.5 shadow-[var(--shadow)]">
      {/* Brand + backend liveness */}
      <div className="flex items-center gap-2">
        <span className="grid h-7 w-7 place-items-center rounded-lg bg-[var(--accent-fill)] text-[var(--accent)]">
          <Activity size={16} strokeWidth={2.5} />
        </span>
        <span className="text-[15px] font-semibold tracking-tight text-zinc-100">Chief of Staff</span>
        <span
          title={
            stale
              ? `Board data is stale (last sync ${Math.round((Date.now() - lastSync) / 1000)}s ago)`
              : sseConnected
                ? 'Live via push (SSE) — updates instantly when something changes'
                : 'Live via polling every 5s'
          }
          className={`ml-0.5 flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${
            stale ? 'bg-amber-900 text-amber-200' : 'text-zinc-600'
          }`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${stale ? 'bg-amber-400' : sseConnected ? 'bg-emerald-400' : 'bg-sky-600'}`} />
          {stale ? 'stale' : sseConnected ? 'push' : 'poll'}
        </span>
      </div>

      {/* Connectors */}
      <div className="flex items-center gap-1">
        {Object.entries(connectors).map(([name, s]) => (
          <span
            key={name}
            title={s.detail}
            className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium text-zinc-400"
          >
            <span className={`h-1.5 w-1.5 rounded-full ${s.connected ? 'bg-emerald-400' : 'bg-zinc-600'}`} />
            {name}
          </span>
        ))}
      </div>

      {/* Pending drafts badge */}
      {(board.drafts?.length ?? 0) > 0 && (
        <button
          onClick={() => setTab?.('approvals')}
          title={`${board.drafts.length} draft${board.drafts.length === 1 ? '' : 's'} awaiting your approval`}
          className="press flex items-center gap-1 rounded-full bg-violet-900/70 px-2 py-0.5 text-[10px] font-semibold text-violet-200 ring-1 ring-violet-700/50 hover:bg-violet-800/70"
        >
          <FileText size={10} />
          <CountUp value={board.drafts.length} /> draft{board.drafts.length === 1 ? '' : 's'}
        </button>
      )}

      {/* Active pings badge — items not yet done/dismissed */}
      {(() => {
        const activePings = (board.pings || []).filter(
          (p) => p.status !== 'done' && p.status !== 'dismissed'
        ).length
        return activePings > 0 ? (
          <button
            onClick={() => setTab?.('pings')}
            title={`${activePings} active ping${activePings === 1 ? '' : 's'}`}
            className="press flex items-center gap-1 rounded-full bg-amber-900/60 px-2 py-0.5 text-[10px] font-semibold text-amber-200 hover:bg-amber-800/60"
          >
            <Bell size={10} />
            <CountUp value={activePings} />
          </button>
        ) : null
      })()}

      {/* Running sweeps indicator */}
      {runningJobs && runningJobs.size > 0 && (
        <span className="flex items-center gap-1.5 rounded-full bg-sky-900/60 px-2 py-0.5 text-[10px] text-sky-300">
          <RefreshCw size={10} className="animate-spin" />
          {[...runningJobs].map((j) => j.replace(/_/g, ' ')).join(', ')}
        </span>
      )}
      {/* Sweep result flash: shows what the last sweep found, fades after 8s */}
      {lastSweepMsg && (
        <span className="animate-fade-in flex items-center gap-1 rounded-full bg-emerald-900/40 px-2 py-0.5 text-[10px] text-emerald-300">
          {lastSweepMsg.job?.replace(/_/g, ' ')}: {lastSweepMsg.text}
        </span>
      )}

      {/* Presence */}
      <div className="flex items-center gap-2 text-[13px] text-zinc-300">
        <span className="relative flex h-2.5 w-2.5">
          {presence === 'Available' && (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
          )}
          <span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${presenceColor[presence] || 'bg-zinc-600'}`} />
        </span>
        {presence}
      </div>

      <span
        className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
          board.away ? 'bg-amber-900 text-amber-200' : 'bg-zinc-800 text-zinc-400'
        }`}
      >
        {board.away ? 'Away mode' : 'Observing only'}
        {board.away_override && board.away_override !== 'null' ? ' · manual' : ''}
      </span>

      <div className="relative">
        <select
          className="cursor-pointer appearance-none rounded-lg bg-zinc-800 py-1 pl-2.5 pr-7 text-xs text-zinc-300 hover:bg-zinc-700"
          value={board.away_override ?? 'null'}
          onChange={(e) => flip('away_override', e.target.value)}
        >
          <option value="null">Auto (presence)</option>
          <option value="true">Force away</option>
          <option value="false">Force online</option>
        </select>
        <ChevronDown size={13} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500" />
      </div>

      <div className="grow" />

      {/* Spend */}
      {cost && (
        <div
          className="flex items-center gap-2.5 rounded-xl border border-zinc-800 bg-zinc-950 px-2.5 py-1"
          title={`CLI usage on this Mac. ${cost.codex.rates_missing ? 'Set cost.openai_prices in policy.yaml for Codex cost.' : ''}`}
        >
          <button
            onClick={refreshCost}
            disabled={costBusy}
            title="Recompute usage from logs now"
            className="press text-zinc-500 hover:text-zinc-200"
          >
            <RefreshCw size={13} className={costBusy ? 'animate-spin' : ''} />
          </button>
          <CostStat label="Claude" today={usd(cost.claude.today.cost)} month={usd(cost.claude.month.cost)} m={cost.month_label} />
          <span className="h-4 w-px bg-zinc-800" />
          {cost.codex.rates_missing ? (
            <span className="flex items-baseline gap-1 text-[11px]">
              <span className="font-medium text-zinc-400">Codex</span>
              <span className="tabular-nums text-zinc-500">
                {(cost.codex.month.input + cost.codex.month.output).toLocaleString()} tok
              </span>
            </span>
          ) : (
            <CostStat label="Codex" today={usd(cost.codex.today.cost)} month={usd(cost.codex.month.cost)} m={cost.month_label} />
          )}
          {cost.cos && (
            <>
              <span className="h-4 w-px bg-zinc-800" />
              <span
                className="flex items-baseline gap-1.5 text-[11px]"
                title={
                  `What CoS itself costs to run. Month-to-date from CoS's Claude Code logs` +
                  (cost.cos.days ? ` (${cost.cos.days} active days).` : '.') +
                  (cost.cos.by_label?.length
                    ? '\nBy purpose (since per-call metering began):\n' +
                      cost.cos.by_label.map((b) => `  ${b.label}: $${b.cost.toFixed(4)} (${b.calls})`).join('\n')
                    : '')
                }
              >
                <span className="font-medium text-violet-300">CoS</span>
                <span className="font-semibold tabular-nums text-violet-200">{usd(cost.cos.today.cost)}</span>
                <span className="text-zinc-600">today</span>
                <span className="text-zinc-700">·</span>
                <span className="font-semibold tabular-nums text-zinc-300">{usd(cost.cos.month.cost)}</span>
                <span className="text-zinc-600">/mo ({cost.cos.month_label})</span>
              </span>
            </>
          )}
        </div>
      )}

      <VaultIndicator />

      <Toggle
        on={!board.dry_run}
        onClick={() => flip('dry_run', board.dry_run ? 'false' : 'true')}
        label={board.dry_run ? 'Dry run' : 'Live'}
      />

      <CaptureButton />

      <button
        onClick={openPalette}
        title="Command palette (⌘K)"
        className="press flex h-8 items-center gap-1 rounded-lg bg-zinc-800 px-2 text-[11px] text-zinc-400 hover:bg-zinc-700 hover:text-zinc-100"
      >
        <Command size={13} />K
      </button>

      <button
        onClick={() => setShowShortcuts(true)}
        title="Keyboard shortcuts (?)"
        className="press grid h-8 w-8 place-items-center rounded-lg bg-zinc-800 text-zinc-500 hover:bg-zinc-700 hover:text-zinc-200"
      >
        <HelpCircle size={15} />
      </button>

      {showShortcuts && <ShortcutsModal onClose={() => setShowShortcuts(false)} />}

      <button
        onClick={toggleTheme}
        title={light ? 'Switch to dark' : 'Switch to light'}
        className="press grid h-8 w-8 place-items-center rounded-lg bg-zinc-800 text-zinc-300 hover:bg-zinc-700 hover:text-zinc-100"
      >
        <span key={light ? 'l' : 'd'} className="animate-fade-in">
          {light ? <Moon size={15} /> : <Sun size={15} />}
        </span>
      </button>

      <button
        onClick={() => flip('kill_switch', board.kill_switch ? 'false' : 'true')}
        className={`press flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-semibold ${
          board.kill_switch
            ? 'bg-red-500 text-white shadow-[0_0_0_3px_rgba(239,68,68,0.2)]'
            : 'bg-zinc-800 text-red-400 hover:bg-red-500/15 hover:text-red-300'
        }`}
      >
        <Power size={14} className={board.kill_switch ? 'animate-pulse' : ''} />
        {board.kill_switch ? 'Killed — resume' : 'Kill switch'}
      </button>
    </header>
  )
}

function CostStat({ label, today, month, m }) {
  return (
    <span className="flex items-baseline gap-1.5 text-[11px]">
      <span className="font-medium text-zinc-400">{label}</span>
      <span className="font-semibold tabular-nums text-zinc-100">{today}</span>
      <span className="text-zinc-600">today</span>
      <span className="font-semibold tabular-nums text-zinc-300">{month}</span>
      <span className="text-zinc-600">{m}</span>
    </span>
  )
}
