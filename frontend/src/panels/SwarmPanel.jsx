import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Bot, Play, ExternalLink, CircleDashed, Loader2, CheckCircle2, XCircle,
  AlertTriangle, Zap, Sparkles, History, ChevronDown, ChevronRight,
  GitPullRequest, SquareKanban, Square, Wrench, Bug, SearchCode, Hammer,
  Brain, Flag, Undo2, X, GitBranch, ScrollText, Table2, ListChecks, RotateCcw,
  Eye, MessageSquare, Clock, Pause, Copy, ClipboardList, Repeat,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'
import { useToast, TimeAgo, fmtMs, decodeEntities } from '../ui.jsx'
import { Empty, RefreshButton } from './AdoPanel.jsx'

const STATUS = {
  queued: { icon: CircleDashed, ic: 'text-zinc-500', chip: 'border-zinc-700 bg-zinc-800 text-zinc-400' },
  running: { icon: Loader2, ic: 'text-sky-400 animate-spin', chip: 'border-sky-800 bg-sky-950 text-sky-300' },
  pass: { icon: CheckCircle2, ic: 'text-emerald-400', chip: 'border-emerald-800 bg-emerald-950 text-emerald-300' },
  fail: { icon: XCircle, ic: 'text-red-400', chip: 'border-red-800 bg-red-950 text-red-300' },
  blocked: { icon: AlertTriangle, ic: 'text-amber-400', chip: 'border-amber-800 bg-amber-950 text-amber-300' },
  error: { icon: Zap, ic: 'text-red-400', chip: 'border-red-900 bg-red-950/60 text-red-300' },
}
const meta = (s) => STATUS[s] || STATUS.queued

// Each sidebar tab is one mode of the same panel: its own board, its own
// launchable profiles, its own receipts. Sub-tabs per the owner's sketch.
const MODES = {
  ado: {
    title: 'ADO Swarm',
    runFilter: 'board,ado_scan,ticket,thread',
    // tickets first: plan the tickets, then push them to the board
    subtabs: ['tickets', 'board', 'receipts', 'history', 'log'],
    profiles: [
      { id: 'thread', label: 'Thread pipeline', icon: Zap, desc: '4 threads — each pulls one ticket and walks it end-to-end: scan → code → PR' },
      { id: 'ado_scan', label: 'Scan tickets', icon: SearchCode, desc: 'read-only hygiene: actionability, PR linkage, smoke plans' },
      { id: 'ticket', label: 'Ticket swarm', icon: Hammer, desc: 'investigate + code plan → coder worktrees for codeable tickets' },
    ],
  },
  pr: {
    title: 'PR Swarm',
    runFilter: 'pr_scan,pr_review',
    subtabs: ['prs', 'board', 'receipts', 'history', 'log'],
    profiles: [
      { id: 'pr_scan', label: 'Scan PRs', icon: SearchCode, desc: 'hygiene: description, size, CI, staleness' },
      { id: 'pr_review', label: 'Review PRs', icon: GitPullRequest, desc: 'full diff review: critical issues + tech debt' },
    ],
  },
  debt: {
    title: 'Debt Swarm',
    runFilter: 'tech_debt',
    subtabs: ['backlog', 'board', 'receipts', 'history', 'log'],
    profiles: [
      { id: 'tech_debt', label: 'Fix tech debt', icon: Wrench, desc: 'one coder worktree per repo over unaddressed findings' },
    ],
  },
}

const SUBTAB_META = {
  board: { label: 'Board', icon: SquareKanban },
  tickets: { label: 'Tickets', icon: ListChecks },
  prs: { label: 'PRs', icon: GitPullRequest },
  backlog: { label: 'Backlog', icon: Wrench },
  receipts: { label: 'Receipts', icon: Table2 },
  history: { label: 'History', icon: History },
  log: { label: 'Log', icon: ScrollText },
}

// Scale presets: how many workers run in parallel. Click to select, click
// again to deselect (falls back to policy swarm.max_concurrent).
const SCALES = [
  { id: 'A', n: 4, desc: 'light · 4 parallel workers' },
  { id: 'B', n: 8, desc: 'standard · 8 parallel workers' },
  { id: 'C', n: 12, desc: 'heavy · 12 parallel workers' },
  { id: 'D', n: 16, desc: 'max · 16 parallel workers' },
]

const refLabel = (j) =>
  j.kind === 'pr' ? `${(j.item_project || '').split('/').pop()}#${j.item_id}` : `#${j.item_id}`
const targetKey = (j) => `${j.kind || 'ado'}:${j.item_id}`

const ThinkingDots = ({ className = '' }) => (
  <span className={`tdots ${className}`}><span /><span /><span /></span>
)

// ---------------------------------------------------------------------------
// The board (the owner's sketch): units are TICKETS/PRs marching new -> active ->
// blocked (requesting HITL) -> PR. A ticket aggregates all its swarm workers
// plus its coder worktree; stuck workers drop the unit into blocked and climb
// back out on retry.
// ---------------------------------------------------------------------------
const ZONE_X = [0.07, 0.33, 0.61, 0.9]
const ZONE_SETS = {
  ado: [
    { id: 'new', label: '1 · New', icon: Sparkles, color: '#60a5fa', hint: 'picked-up tickets spawn here' },
    { id: 'active', label: '2 · Active', icon: Brain, color: '#fb7185', hint: 'swarm working the ticket' },
    { id: 'blocked', label: '3 · Blocked · HITL', icon: AlertTriangle, color: '#fbbf24', hint: 'stuck or awaiting you' },
    { id: 'pr', label: '4 · Done', icon: Flag, color: '#a78bfa', hint: 'verdicts land here' },
  ],
  pr: [
    { id: 'new', label: 'New', icon: Sparkles, color: '#60a5fa', hint: 'picked-up PRs spawn here' },
    { id: 'active', label: 'Reviewing', icon: Eye, color: '#fb7185', hint: 'reading the diff' },
    { id: 'blocked', label: 'Writing to ADO', icon: MessageSquare, color: '#38bdf8', hint: 'commenting + tagging the owner' },
    { id: 'pr', label: 'Done', icon: Flag, color: '#a78bfa', hint: 'reviewed · owner notified' },
  ],
}
ZONE_SETS.debt = ZONE_SETS.ado
const zonesFor = (mode) =>
  (ZONE_SETS[mode] || ZONE_SETS.ado).map((z, i) => ({ ...z, x: ZONE_X[i] }))

// Aggregate jobs + worktrees into per-target units.
function buildUnits(jobs, worktrees, phases, runId) {
  const units = new Map()
  for (const j of [...jobs].sort((a, b) => a.id - b.id)) {
    const k = targetKey(j)
    if (!units.has(k)) {
      units.set(k, { key: k, kind: j.kind || 'ado', ref: refLabel(j), title: j.item_title,
                     url: j.item_url, itemId: j.item_id, jobs: [], wt: null, wts: [] })
    }
    units.get(k).jobs.push(j)
  }
  const jobById = new Map(jobs.map((j) => [j.id, j]))
  for (const w of worktrees || []) {
    if (w.status === 'discarded') continue
    let key = null
    if (w.job_id && jobById.has(w.job_id)) key = targetKey(jobById.get(w.job_id))
    else if (/^AB#\d+$/.test(w.target_ref || '')) key = `ado:${w.target_ref.slice(3)}`
    if (key && units.has(key)) {
      const u = units.get(key)
      u.wts.push(w) // one ticket can hold several worktrees/PRs — keep them all
      if (!u.wt || (w.id > u.wt.id)) u.wt = w  // newest drives the unit's zone
    } else if (w.run_id === runId) {
      // debt-swarm worktrees have no jobs — they are their own units
      units.set(`wt:${w.id}`, {
        key: `wt:${w.id}`, kind: 'wt', ref: (w.repo || '').split('/').pop(),
        title: w.title, url: w.pr_url || null, jobs: [], wt: w, wts: [w],
      })
    }
  }
  return [...units.values()]
}

function zoneForUnit(u, phases, mode) {
  // PR board: New -> Reviewing (active) -> Writing to ADO (blocked slot, used
  // for the commenting/tagging stage) -> Done
  if (mode === 'pr') {
    if (u.jobs.some((j) => phases[j.id]?.phase === 'commenting')) return 'blocked'
    if (u.jobs.some((j) => j.status === 'running')) return 'active'
    const queued = u.jobs.filter((j) => j.status === 'queued')
    if (queued.length === u.jobs.length && queued.length) return 'new'
    if (queued.length) return 'active'
    return 'pr'
  }
  const running = u.jobs.filter((j) => j.status === 'running')
  if (running.length)
    return running.some((j) => (phases[j.id]?.phase || j.phase) === 'blocked') ? 'blocked' : 'active'
  if (u.wt) {
    if (u.wt.status === 'active') return 'active'
    if (u.wt.status === 'ready' || u.wt.status === 'error') return 'blocked'
    if (u.wt.status === 'pr_created' || u.wt.status === 'merged') return 'pr'
    if (u.wt.status === 'closed') return 'blocked'
  }
  const queued = u.jobs.filter((j) => j.status === 'queued')
  if (queued.length) return queued.length === u.jobs.length ? 'new' : 'active'
  if (!u.jobs.length) return 'new'
  return u.jobs.some((j) => j.status === 'blocked') ? 'blocked' : 'pr'
}

function noteForUnit(u, zone, phases) {
  if (u.jobs.some((j) => ['blocked', 'fail'].includes(j.status) && j.verdict?.clarifying_questions?.length)
      && !u.jobs.some((j) => j.status === 'running')) {
    if (u.clarifyResult === 'sent') return 'questions posted on the ticket, waiting for answers'
    if ((u.clarifyResult || '').includes('dry_run')) return 'questions ready — dry run held the comment'
    if (u.clarifyResult) return `clarify: ${u.clarifyResult}`
    return 'needs clarification on the ticket'
  }
  if (u.wt?.status === 'ready') return 'committed — dry run is holding the PR push'
  if (u.wt?.status === 'active')
    return (u.wt.job_id && phases[u.wt.job_id]?.note) || 'coder working in an isolated worktree'
  if (u.wt?.status === 'pr_created') return 'draft PR opened, posted for review'
  if (u.wt?.status === 'merged') return 'PR merged on GitHub — ticket updated'
  if (u.wt?.status === 'closed') return 'PR closed without merging — needs a look'
  if (u.wt?.status === 'error') return u.wt.error || 'coder failed'
  const running = u.jobs.filter((j) => j.status === 'running')
  const live = running.map((j) => phases[j.id]?.note || j.phase_note).filter(Boolean)
  if (live.length) return live[live.length - 1]
  if (zone === 'new') return 'waiting for a worker'
  const done = u.jobs.filter((j) => !['queued', 'running'].includes(j.status))
  if (done.length) {
    const h = done.find((j) => j.status !== 'pass' && j.verdict?.headline)?.verdict?.headline
    if (h) return h
    const f = done.filter((j) => j.status === 'fail').length
    const b = done.filter((j) => j.status === 'blocked').length
    return `${done.length} check${done.length !== 1 ? 's' : ''} done${f ? ` · ${f} fail` : ''}${b ? ` · ${b} blocked` : ''}`
  }
  return ''
}

const unitWorkable = (u) =>
  u.jobs.some((j) => j.verdict?.workable === true) &&
  !u.jobs.some((j) => ['queued', 'running'].includes(j.status)) && !u.wt

// the two workers can disagree: investigate says workable, but the planner
// concludes there is nothing to code (e.g. a UAT validation task)
const planNotCodeable = (u) =>
  !u.wt && u.jobs.some((j) => j.runbook === 'ticket-code-plan' && j.verdict?.codeable === false)

// The verb pill on top of every agent: what it is doing RIGHT NOW.
function unitAction(u, phases) {
  const entries = u.jobs.map((j) => phases[j.id]).filter(Boolean)
  if (entries.some((p) => p.phase === 'commenting'))
    return { verb: 'commenting', icon: MessageSquare, cls: 'border-sky-800 bg-sky-950/90 text-sky-300' }
  const running = u.jobs.filter((j) => j.status === 'running')
  if (running.length) {
    if (running.some((j) => (phases[j.id]?.attempt || 0) > 0))
      return { verb: 'retrying', icon: Undo2, cls: 'border-amber-800 bg-amber-950/90 text-amber-300' }
    if (running.some((j) => (phases[j.id]?.phase || j.phase) === 'reasoning'))
      return { verb: 'thinking', icon: Brain, cls: 'border-violet-800 bg-violet-950/90 text-violet-300', dots: true }
    return { verb: u.kind === 'pr' ? 'reading PR' : 'reading ticket', icon: Eye,
             cls: 'border-sky-800 bg-sky-950/90 text-sky-300', dots: true }
  }
  if (u.wt?.status === 'active')
    return { verb: 'coding', icon: Hammer, cls: 'border-violet-800 bg-violet-950/90 text-violet-300', dots: true }
  if (u.wt?.status === 'ready')
    return { verb: 'committed', icon: GitBranch, cls: 'border-emerald-800 bg-emerald-950/90 text-emerald-300' }
  if (u.wt?.status === 'merged') {
    const us = u.wt.uat_status
    if (us === 'smoke_fail')
      return { verb: 'UAT smoke failed', icon: XCircle, cls: 'border-red-900 bg-red-950/90 text-red-300' }
    if (us === 'smoke_pass')
      return { verb: 'UAT verified ✓', icon: CheckCircle2, cls: 'border-emerald-800 bg-emerald-950/90 text-emerald-300' }
    if (us === 'deployed')
      return { verb: 'smoke testing UAT', icon: Eye, cls: 'border-sky-800 bg-sky-950/90 text-sky-300', dots: true }
    if (us === 'skipped')
      return { verb: 'merged ✓', icon: CheckCircle2, cls: 'border-emerald-800 bg-emerald-950/90 text-emerald-300' }
    return { verb: 'merged · awaiting UAT', icon: Clock, cls: 'border-sky-800 bg-sky-950/90 text-sky-300' }
  }
  if (u.wt?.status === 'closed')
    return { verb: 'PR closed', icon: XCircle, cls: 'border-red-900 bg-red-950/90 text-red-300' }
  if (u.wt?.status === 'pr_created') {
    const w = u.wt
    if (w.review_status === 'ready_to_merge')
      return { verb: 'ready to merge', icon: CheckCircle2, cls: 'border-emerald-800 bg-emerald-950/90 text-emerald-300' }
    if (w.review_status === 'needs_human')
      return { verb: 'needs you', icon: AlertTriangle, cls: 'border-amber-800 bg-amber-950/90 text-amber-300' }
    if (w.review_status === 'fixing')
      return { verb: 'fixing review comments', icon: Wrench, cls: 'border-rose-800 bg-rose-950/90 text-rose-300', dots: true }
    if (w.ci_status === 'fixing')
      return { verb: 'fixing CI', icon: Hammer, cls: 'border-rose-800 bg-rose-950/90 text-rose-300', dots: true }
    if (w.ci_status === 'failed')
      return { verb: 'checks failed', icon: XCircle, cls: 'border-red-900 bg-red-950/90 text-red-300' }
    if (w.ci_status === 'green')
      return { verb: 'in review · green', icon: Eye, cls: 'border-violet-800 bg-violet-950/90 text-violet-300' }
    return { verb: 'in review', icon: Eye, cls: 'border-violet-800 bg-violet-950/90 text-violet-300' }
  }
  if (!u.jobs.length || u.jobs.some((j) => j.status === 'queued'))
    return { verb: 'waiting', icon: Clock, cls: 'border-zinc-700 bg-zinc-900/90 text-zinc-500' }
  if (u.jobs.some((j) => ['blocked', 'fail'].includes(j.status) && j.verdict?.clarifying_questions?.length))
    return { verb: 'awaiting clarification', icon: MessageSquare, cls: 'border-amber-800 bg-amber-950/90 text-amber-300' }
  if (planNotCodeable(u))
    return { verb: 'not codeable', icon: AlertTriangle, cls: 'border-amber-800 bg-amber-950/90 text-amber-300' }
  if (unitWorkable(u))
    return { verb: 'workable · ready to code', icon: Hammer, cls: 'border-emerald-800 bg-emerald-950/90 text-emerald-300' }
  const fails = u.jobs.filter((j) => ['fail', 'error'].includes(j.status)).length
  const blocked = u.jobs.filter((j) => j.status === 'blocked').length
  if (blocked && !fails)
    return { verb: 'blocked', icon: AlertTriangle, cls: 'border-amber-800 bg-amber-950/90 text-amber-300' }
  return fails
    ? { verb: 'done · issues', icon: XCircle, cls: 'border-red-900 bg-red-950/90 text-red-300' }
    : { verb: 'done', icon: CheckCircle2, cls: 'border-emerald-900 bg-emerald-950/90 text-emerald-300' }
}

function unitElapsed(u) {
  const running = u.jobs.filter((j) => j.status === 'running' && j.started_at)
  if (running.length) return fmtMs(Date.now() - new Date(running[0].started_at))
  if (u.wt?.status === 'active') return fmtMs(Date.now() - new Date(u.wt.created_at || u.wt.updated_at))
  const ms = u.jobs.reduce((n, j) => n + (j.duration_ms || 0), 0)
  return ms ? fmtMs(ms) : ''
}

const ROWS = 4

function UnitBadge({ u, zone, live }) {
  // what shows inside the unit's circle
  if (u.wt?.status === 'pr_created') return <GitPullRequest size={16} className="text-violet-300" />
  if (u.wt?.status === 'ready') return <AlertTriangle size={16} className="text-amber-300" />
  if (live) return <Bot size={16} className="text-sky-300" />
  if (zone === 'pr') {
    const fails = u.jobs.filter((j) => ['fail', 'error'].includes(j.status)).length
    return fails ? <XCircle size={16} className="text-red-400" /> : <CheckCircle2 size={16} className="text-emerald-400" />
  }
  if (zone === 'blocked') return <AlertTriangle size={16} className="text-amber-400" />
  return <Bot size={16} className="text-zinc-500" />
}

function AgentUnit({ u, zone, zones, phases, onClick, active,
                     paused, stopped, onControl, onRetry }) {
  const live = u.jobs.some((j) => j.status === 'running') || u.wt?.status === 'active'
  const note = noteForUnit(u, zone, phases)
  let action = unitAction(u, phases)
  if (stopped) action = { verb: 'stopped', icon: Square, cls: 'border-zinc-700 bg-zinc-900/90 text-zinc-500' }
  else if (paused) action = { verb: 'paused', icon: Pause, cls: 'border-amber-800 bg-amber-950/90 text-amber-300' }
  const cost = u.jobs.reduce((n, j) => n + (j.cost_usd || 0), 0)
  const ring = live ? 'unit-active border-sky-500/70'
    : zone === 'blocked' ? 'border-amber-500/60'
    : zone === 'pr' ? 'flag-plant border-violet-500/50'
    : 'border-zinc-700'
  return (
    <button
      onClick={onClick}
      className="card-pop group relative w-full min-w-0 text-left hover:z-40"
    >
      {/* everything on ONE square — PRs render purple, tickets neutral/sky */}
      <div className={`card-pop relative rounded-xl border-2 px-2 py-1.5 ${ring} ${
        u.kind === 'pr' ? 'bg-purple-950/30' : 'bg-zinc-900/95'
      } ${active ? 'ring-2 ring-[var(--accent)]' : ''}`}>
        <div className="flex items-center gap-1">
          <span className={`flex min-w-0 items-center gap-1 truncate rounded-full border px-1.5 py-px text-[8px] font-bold ${action.cls}`}>
            <action.icon size={8} className="shrink-0" />
            <span className="truncate">{action.verb}</span>
            {action.dots && <ThinkingDots className="ml-0.5 shrink-0" />}
          </span>
          <span className="ml-auto shrink-0"><UnitBadge u={u} zone={zone} live={live} /></span>
        </div>
        {(onControl || onRetry) && (
          <span className="absolute -top-2 right-1 z-20 hidden gap-0.5 group-hover:flex">
            {onControl && !stopped && (
              <span
                role="button"
                title={paused ? 'Resume this ticket' : 'Pause this ticket'}
                onClick={(e) => { e.stopPropagation(); onControl(paused ? 'resume' : 'pause', u.key) }}
                className="press grid h-5 w-5 place-items-center rounded-md border border-zinc-700 bg-zinc-900 text-zinc-300 hover:text-amber-300"
              >
                {paused ? <Play size={10} /> : <Pause size={10} />}
              </span>
            )}
            {onControl && !stopped && (
              <span
                role="button"
                title="Stop this ticket (remaining workers are cancelled)"
                onClick={(e) => { e.stopPropagation(); onControl('stop', u.key) }}
                className="press grid h-5 w-5 place-items-center rounded-md border border-zinc-700 bg-zinc-900 text-zinc-300 hover:text-red-300"
              >
                <Square size={9} />
              </span>
            )}
            {onRetry && (
              <span
                role="button"
                title="Retry: spawn fresh workers on this ticket"
                onClick={(e) => { e.stopPropagation(); onRetry(u) }}
                className="press grid h-5 w-5 place-items-center rounded-md border border-zinc-700 bg-zinc-900 text-zinc-300 hover:text-sky-300"
              >
                <RotateCcw size={10} />
              </span>
            )}
          </span>
        )}
        <div className="mt-1 flex items-baseline gap-1">
          {u.kind === 'pr'
            ? <GitPullRequest size={9} className="shrink-0 self-center text-purple-400" />
            : <SquareKanban size={9} className="shrink-0 self-center text-sky-500" />}
          <span className={`shrink-0 truncate text-[10px] font-semibold tabular-nums ${
            u.kind === 'pr' ? 'text-purple-200' : 'text-zinc-200'
          }`}>{u.ref}</span>
          {u.jobs.length > 1 && <span className="shrink-0 text-[8px] text-zinc-500">⚙{u.jobs.length}</span>}
          <span className="ml-auto shrink-0 text-[8px] tabular-nums text-zinc-600">{unitElapsed(u)}</span>
        </div>
        <div className="truncate text-[10px] text-zinc-300">{u.title}</div>
        {(u.wts?.length ? u.wts : u.wt ? [u.wt] : []).slice(-3).map((w) => (
          <div key={w.id} className="mt-0.5 flex items-center gap-1">
            <GitBranch size={8} className="shrink-0 text-violet-400" />
            <span className="min-w-0 truncate font-mono text-[8px] text-violet-300/90">{w.branch}</span>
            {w.status === 'merged' && <span className="shrink-0 rounded bg-emerald-950 px-1 text-[7px] font-bold text-emerald-300">merged</span>}
            {w.status === 'closed' && <span className="shrink-0 rounded bg-red-950/70 px-1 text-[7px] font-bold text-red-300">closed</span>}
            {w.status === 'pr_created' && <span className="shrink-0 rounded bg-violet-950 px-1 text-[7px] font-bold text-violet-300">PR</span>}
            {w.pr_url && (
              <a
                href={w.pr_url}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="shrink-0 text-sky-300 hover:text-sky-200"
                title={w.pr_url}
              >
                <ExternalLink size={8} />
              </a>
            )}
          </div>
        ))}
        {(u.wts?.length || 0) > 3 && (
          <div className="mt-0.5 text-[8px] text-zinc-600">+{u.wts.length - 3} more worktrees — click for all</div>
        )}
        {note && <div className="mt-0.5 truncate text-[9px] italic text-zinc-500">{note}</div>}
      </div>

      {/* hover: live detail popover — ticket, process, per-worker status */}
      <div className="pointer-events-none absolute left-1/2 top-full z-30 mt-1.5 hidden w-72 -translate-x-1/2 group-hover:block">
        <div className="animate-fade-in rounded-xl border border-zinc-600 bg-zinc-950 p-2.5 shadow-2xl">
          <div className="flex items-start gap-1.5">
            {u.kind === 'pr'
              ? <GitPullRequest size={11} className="mt-0.5 shrink-0 text-purple-400" />
              : <SquareKanban size={11} className="mt-0.5 shrink-0 text-sky-500" />}
            <span className="text-[11px] font-medium leading-tight text-zinc-100">{u.ref} {u.title}</span>
          </div>
          <div className="mt-1 text-[9px] text-zinc-500">
            {u.kind === 'pr' ? 'pull request' : 'work item'}
            {u.jobs[0]?.item_state ? ` · ${u.jobs[0].item_state}` : ''} · {action.verb}
            {cost > 0 && ` · $${cost.toFixed(3)}`}
          </div>
          <div className="mt-1.5 space-y-1 border-t border-zinc-800 pt-1.5">
            {u.jobs.map((j) => {
              const jm = meta(j.status)
              const liveNote = j.status === 'running' ? phases[j.id]?.note || j.phase_note : null
              return (
                <div key={j.id} className="flex items-start gap-1.5 text-[9px]">
                  <jm.icon size={10} className={`mt-px shrink-0 ${jm.ic}`} />
                  <span className="shrink-0 font-semibold text-zinc-400">{j.runbook}</span>
                  <span className="min-w-0 flex-1 truncate text-zinc-500">
                    {liveNote || j.verdict?.headline || j.error || j.status}
                  </span>
                </div>
              )
            })}
            {(u.wts?.length ? u.wts : u.wt ? [u.wt] : []).map((w) => (
              <div key={w.id} className="flex items-start gap-1.5 text-[9px]">
                <GitBranch size={10} className="mt-px shrink-0 text-violet-400" />
                <span className="shrink-0 font-semibold text-zinc-400">{w.branch}</span>
                <span className="min-w-0 flex-1 truncate text-zinc-500">
                  {w.status}{w.pr_url ? ` · ${w.pr_url.split('/').slice(-1)[0] ? `PR #${w.pr_url.split('/').pop()}` : ''}` : ''}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-1.5 text-[8px] text-zinc-600">click for the full inspector</div>
        </div>
      </div>
    </button>
  )
}

// side inspector inside the arena: this run's workers live, PLUS every swarm
// that ever touched this target and all worktrees spun up for it.
function InspectorJob({ j, phases }) {
  const running = j.status === 'running'
  const note = running ? phases?.[j.id]?.note || j.phase_note : null
  return (
    <div className="mb-1.5 rounded-lg border border-zinc-800 p-2">
      <div className="flex items-center gap-1.5 text-[11px]">
        <StatusChip status={j.status} />
        <span className="font-medium text-zinc-300">{j.runbook}</span>
        <span className="ml-auto text-[9px] tabular-nums text-zinc-600">
          {fmtMs(j.duration_ms)}{j.cost_usd != null ? ` · $${j.cost_usd.toFixed(3)}` : ''}
        </span>
      </div>
      {running && note && (
        <div className="mt-1 flex items-center gap-1.5 text-[10px] italic text-sky-300/90">
          <ThinkingDots className="text-sky-400" /> {note}
        </div>
      )}
      {j.verdict?.headline && <p className="mt-1 text-[12px] text-zinc-200">{j.verdict.headline}</p>}
      {(j.verdict?.clarifying_questions || []).length > 0 && (
        <ol className="mt-1 list-inside list-decimal space-y-0.5 rounded bg-amber-950/20 px-1.5 py-1 text-[10px] text-amber-200/90">
          {j.verdict.clarifying_questions.map((q, i) => <li key={i}>{q}</li>)}
        </ol>
      )}
      {(j.verdict?.evidence || []).slice(0, 3).map((e, i) => (
        <p key={i} className="mt-0.5 text-[10px] text-zinc-500">• {e}</p>
      ))}
      {j.verdict?.suggested_next_action && (
        <p className="mt-1 text-[10px] text-sky-300/80">next: {j.verdict.suggested_next_action}</p>
      )}
      {j.error && <p className="mt-1 text-[10px] text-red-300">{j.error}</p>}
    </div>
  )
}

function InspectorWorktree({ w }) {
  return (
    <div className={`mb-1.5 rounded-lg border px-2.5 py-2 text-[11px] ${
      w.status === 'ready' ? 'border-amber-800/60 bg-amber-950/20 text-amber-200' : 'border-zinc-800 text-zinc-300'
    }`}>
      <div className="flex items-center gap-1.5 font-medium">
        <GitBranch size={11} /> {w.branch}
        <span className="ml-auto text-[10px] opacity-80">{w.status}</span>
      </div>
      <div className="mt-0.5 text-[9px] text-zinc-500">
        run #{w.run_id} · created <TimeAgo iso={w.created_at} />
        {w.status === 'ready' && ' · awaiting approval on the Worktrees tab'}
        {w.status === 'discarded' && w.error ? ` · ${w.error}` : ''}
      </div>
      {w.pr_url && (
        <a href={w.pr_url} target="_blank" rel="noreferrer" className="mt-0.5 block truncate text-[10px] text-sky-300 hover:underline">
          {w.pr_url}
        </a>
      )}
      {w.diff_stat && <pre className="mt-1 overflow-x-auto font-mono text-[9px] text-zinc-500">{w.diff_stat.split('\n').slice(-3).join('\n')}</pre>}
    </div>
  )
}

function jobMd(j, phases) {
  const v = j.verdict || {}
  const lines = [
    `### ${j.runbook} — ${j.status} (${fmtMs(j.duration_ms)}${j.cost_usd != null ? ` · $${j.cost_usd.toFixed(3)}` : ''} · ${j.model || ''})`,
  ]
  const live = j.status === 'running' ? phases?.[j.id]?.note || j.phase_note : null
  if (live) lines.push(`_live: ${live}_`)
  if (v.headline) lines.push(v.headline)
  if (v.codeable != null) lines.push(`codeable: ${v.codeable}${v.change_plan ? ` — plan: ${v.change_plan}` : ''}`)
  if (v.clarifying_questions?.length)
    lines.push('Clarifying questions:', ...v.clarifying_questions.map((q, i) => `${i + 1}. ${q}`))
  if (v.critical_issues?.length)
    lines.push('Critical issues:', ...v.critical_issues.map((f) => `- ${f.title} (${f.where}): ${f.why}`))
  if (v.tech_debt?.length)
    lines.push('Tech debt:', ...v.tech_debt.map((f) => `- ${f.title} (${f.where}): ${f.why}`))
  if (v.evidence?.length) lines.push('Evidence:', ...v.evidence.map((e) => `- ${e}`))
  if (v.suggested_next_action) lines.push(`next: ${v.suggested_next_action}`)
  if (j.error) lines.push(`error: ${j.error}`)
  return lines.join('\n')
}

function unitReportMd(u, hist, runs, phases) {
  const out = [`# ${u.ref} ${u.title}`, u.url || '', '', '## This run']
  u.jobs.forEach((j) => out.push(jobMd(j, phases), ''))
  const wts = hist?.worktrees?.length ? hist.worktrees : u.wt ? [u.wt] : []
  if (wts.length) {
    out.push('## Worktrees')
    for (const w of wts) {
      out.push(`- ${w.branch} — ${w.status} — run #${w.run_id}${w.pr_url ? ` — ${w.pr_url}` : ''}${w.error ? ` — ${w.error}` : ''}`)
      if (w.summary) out.push(`  summary: ${w.summary.split('\n')[0]}`)
      if (w.diff_stat) out.push('  ' + w.diff_stat.trim().split('\n').slice(-1)[0].trim())
    }
    out.push('')
  }
  if (runs.length) {
    out.push('## Earlier swarms on this target')
    for (const g of runs) {
      out.push(`### run #${g.run_id} · ${g.profile || 'board'} · ${g.started || ''}`)
      g.jobs.forEach((j) => out.push(jobMd(j), ''))
    }
  }
  return out.join('\n')
}

function UnitInspector({ u, phases, onClose }) {
  const toast = useToast()
  const [hist, setHist] = useState(null)
  useEffect(() => {
    if (u.kind !== 'ado' && u.kind !== 'pr') { setHist({ jobs: [], worktrees: u.wt ? [u.wt] : [] }); return }
    fetch(`/api/swarm/target?kind=${u.kind}&id=${u.itemId}`)
      .then((r) => r.json())
      .then(setHist)
      .catch(() => setHist({ jobs: [], worktrees: [] }))
  }, [u.kind, u.itemId])

  const currentIds = new Set(u.jobs.map((j) => j.id))
  // group historical jobs by run (each run = one swarm that touched this target)
  const runs = []
  for (const j of hist?.jobs || []) {
    if (currentIds.has(j.id)) continue
    let g = runs.find((r) => r.run_id === j.run_id)
    if (!g) { g = { run_id: j.run_id, profile: j.run_profile, started: j.run_started, jobs: [] }; runs.push(g) }
    g.jobs.push(j)
  }
  const wts = hist?.worktrees?.length ? hist.worktrees : u.wt ? [u.wt] : []

  return (
    <div className="absolute bottom-2 right-2 top-12 z-20 w-[380px] overflow-y-auto rounded-xl border border-zinc-700 bg-zinc-950/95 p-3 shadow-2xl backdrop-blur-sm">
      <div className="mb-2 flex items-center gap-2">
        {u.kind === 'pr' ? <GitPullRequest size={13} className="text-purple-400" /> : <SquareKanban size={13} className="text-sky-500" />}
        {u.url ? (
          <a href={u.url} target="_blank" rel="noreferrer" className="min-w-0 flex-1 truncate text-[13px] font-medium text-zinc-100 hover:underline">
            {u.ref} {u.title}
          </a>
        ) : (
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-zinc-100">{u.ref} {u.title}</span>
        )}
        <button
          onClick={() => {
            navigator.clipboard.writeText(unitReportMd(u, hist, runs, phases))
              .then(() => toast('Report copied — paste it anywhere for debugging', 'success'))
              .catch(() => toast('Copy failed', 'error'))
          }}
          title="Copy the full report (verdicts, questions, evidence, worktrees, history) as markdown"
          className="press shrink-0 text-zinc-500 hover:text-zinc-200"
        >
          <Copy size={13} />
        </button>
        <button onClick={onClose} className="press shrink-0 text-zinc-500 hover:text-zinc-200"><X size={14} /></button>
      </div>

      {u.jobs.length > 0 && (
        <>
          <div className="mb-1 text-[9px] font-semibold uppercase tracking-wide text-zinc-500">This run</div>
          {u.jobs.map((j) => <InspectorJob key={j.id} j={j} phases={phases} />)}
        </>
      )}

      {wts.length > 0 && (
        <>
          <div className="mb-1 mt-2 text-[9px] font-semibold uppercase tracking-wide text-violet-400">
            Worktrees · {wts.length}
          </div>
          {wts.map((w) => <InspectorWorktree key={w.id} w={w} />)}
        </>
      )}

      {!hist && <div className="skeleton mt-2 h-12 w-full rounded-lg" />}
      {runs.length > 0 && (
        <>
          <div className="mb-1 mt-2 text-[9px] font-semibold uppercase tracking-wide text-zinc-500">
            Earlier swarms on this target · {runs.length}
          </div>
          {runs.map((g) => (
            <div key={g.run_id} className="mb-2">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] text-zinc-500">
                <Bot size={10} />
                run #{g.run_id} · {g.profile || 'board'} · <TimeAgo iso={g.started} />
              </div>
              {g.jobs.map((j) => <InspectorJob key={j.id} j={j} />)}
            </div>
          ))}
        </>
      )}
    </div>
  )
}

// One arena strip: zones, route, banners, units. Rendered once for scan-only
// modes, twice (scan + coding pipeline) for the ADO swarm.
function Arena({ zones, placed, phases, selectedKey, onSelect, log, emptyHint, control, onControl, onRetry, compact, gates, gateValues, onSetGate }) {
  const counts = Object.fromEntries(zones.map((z) => [z.id, 0]))
  placed.forEach((p) => counts[p.zone]++)
  return (
    <div
      className={`relative overflow-hidden rounded-xl border border-zinc-800/80 ${
        compact ? 'min-h-[146px] flex-1' : 'min-h-[210px] flex-1'
      }`}
      style={{
        background:
          `radial-gradient(ellipse 55% 85% at 7% 50%, ${zones[0].color}0d, transparent),` +
          `radial-gradient(ellipse 50% 85% at 33% 50%, ${zones[1].color}0d, transparent),` +
          `radial-gradient(ellipse 50% 85% at 61% 50%, ${zones[2].color}0b, transparent),` +
          `radial-gradient(ellipse 55% 85% at 90% 50%, ${zones[3].color}0f, transparent),` +
          'repeating-linear-gradient(0deg, rgba(113,113,122,0.05) 0 1px, transparent 1px 34px),' +
          'repeating-linear-gradient(90deg, rgba(113,113,122,0.05) 0 1px, transparent 1px 34px)',
      }}
    >
      <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 100 40" preserveAspectRatio="none">
        <path
          d="M 7 20 C 18 16, 25 24, 33 20 S 51 16, 61 20 S 82 24, 90 20"
          fill="none" stroke="rgba(148,163,184,0.2)" strokeWidth="0.35"
          strokeDasharray="1.6 1.4" vectorEffect="non-scaling-stroke"
        />
      </svg>
      {zones.map((z, i) => (
        <div
          key={z.id}
          className="absolute top-2 z-10 flex -translate-x-1/2 items-center gap-1.5 whitespace-nowrap rounded-full border border-zinc-800 bg-zinc-950/90 px-2.5 py-1"
          style={{ left: `${i * 25 + 12.5}%` }}
        >
          <z.icon size={11} style={{ color: z.color }} />
          <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-zinc-400">{z.label}</span>
          <span className={`text-[9px] font-bold tabular-nums ${counts[z.id] ? 'text-zinc-200' : 'text-zinc-700'}`}>
            {counts[z.id]}
          </span>
        </div>
      ))}
      {(gates || []).map((g) => (
        <GateChip
          key={g.at}
          gate={g}
          value={g.key ? gateValues?.[g.key] : null}
          onSet={onSetGate}
        />
      ))}
      {zones.map((z, i) => {
        const zoneUnits = placed.filter((p) => p.zone === z.id).map((p) => p.u)
        return (
          <div
            key={z.id}
            className="absolute z-10 overflow-y-auto px-1.5 pt-1"
            style={{ left: `${i * 25}%`, width: '25%', top: 38, maxHeight: 158 }}
          >
            <div
              className="grid content-start gap-1.5"
              style={{ gridTemplateColumns: `repeat(auto-fill, minmax(min(${zoneUnits.length > 8 ? 118 : 136}px, 100%), 1fr))` }}
            >
              {zoneUnits.map((u) => (
                <AgentUnit
                  key={u.key}
                  u={u}
                  zone={z.id}
                  zones={zones}
                  phases={phases}
                  active={selectedKey === u.key}
                  paused={control?.paused || control?.paused_targets?.includes(u.key)}
                  stopped={control?.stopped_targets?.includes(u.key)}
                  onControl={onControl}
                  onRetry={onRetry}
                  onClick={() => onSelect(selectedKey === u.key ? null : u.key)}
                />
              ))}
            </div>
          </div>
        )
      })}
      {placed.length === 0 && emptyHint && (
        <div className="absolute inset-0 grid place-items-center">
          <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-950/60 px-4 py-2 text-[11px] italic text-zinc-600">
            {emptyHint}
          </div>
        </div>
      )}
      {(log || []).length > 0 && (
        <div className="absolute bottom-2 left-2 z-10 max-w-[62%] space-y-0.5">
          {log.slice(-3).map((l, i, arr) => (
            <div
              key={l.ts}
              className="truncate rounded-md bg-zinc-950/85 px-2 py-0.5 font-mono text-[9px] text-zinc-400"
              style={{ opacity: 0.45 + (0.55 * (i + 1)) / arr.length }}
            >
              <span className="text-zinc-600">{l.who}</span> {l.note}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// The coding pipeline (bottom board of the ADO swarm): fixing -> committing
// (work committed, awaiting approval) -> blocked -> PR created.
const CODE_ZONES = [
  { id: 'fixing', label: '5 · Fixing', icon: Hammer, color: '#38bdf8', hint: 'planning + coding in worktrees' },
  { id: 'committing', label: '6 · Committing', icon: GitBranch, color: '#34d399', hint: 'work committed · PR opening' },
  { id: 'blocked', label: '7 · Blocked', icon: AlertTriangle, color: '#fb7185', hint: 'stuck or not codeable' },
  { id: 'pr', label: '8 · PR created', icon: GitPullRequest, color: '#a78bfa', hint: '→ moves to the review row' },
].map((z, i) => ({ ...z, x: ZONE_X[i] }))

// Row 3: the review pipeline — PR in review, fixes (comments + CI errors,
// non-blocking feedback banked as tech debt), then ready to merge.
const REVIEW_ZONES = [
  { id: 'inreview', label: '9 · PR in review', icon: Eye, color: '#a78bfa', hint: 'checks + reviewers looking' },
  { id: 'prfixes', label: '10 · PR fixes', icon: Wrench, color: '#fb7185', hint: 'fixing comments + CI · banking tech debt' },
  { id: 'needs', label: '11 · Needs you', icon: AlertTriangle, color: '#fbbf24', hint: 'fix rounds exhausted' },
  { id: 'ready', label: '12 · Ready to merge', icon: CheckCircle2, color: '#34d399', hint: 'comments addressed · all green' },
].map((z, i) => ({ ...z, x: ZONE_X[i] }))

// Gates between lanes: `pass` is a free transition; `loop` bounds how many
// times the swarm re-tries that stage before the unit drops through. Loop
// gates map to the swarm's real retry knobs and are tuned in place (0..5).
const SCAN_GATES = [
  { at: 1, type: 'pass' },
  { at: 2, type: 'loop', key: 'retries', label: 'worker retries before a unit blocks' },
  { at: 3, type: 'pass' },
]
const CODE_GATES = [
  { at: 1, type: 'loop', key: 'repair_attempts', label: 'coder repair loops before blocked' },
  { at: 2, type: 'pass' },
  { at: 3, type: 'pass' },
]
const REVIEW_GATES = [
  { at: 1, type: 'loop', key: 'ci_fix_attempts', label: 'CI-failure fix loops' },
  { at: 2, type: 'loop', key: 'review_fix_attempts', label: 'review-comment fix loops before Needs you' },
  { at: 3, type: 'pass' },
]

function GateChip({ gate, value, onSet }) {
  const [open, setOpen] = useState(false)
  if (gate.type === 'pass') {
    return (
      <div
        className="absolute top-2.5 z-20 grid h-5 w-5 -translate-x-1/2 place-items-center rounded-full border border-zinc-800 bg-zinc-950/90"
        style={{ left: `${gate.at * 25}%` }}
        title="pass-through gate"
      >
        <ChevronRight size={10} className="text-zinc-600" />
      </div>
    )
  }
  return (
    <div className="absolute top-2 z-20 -translate-x-1/2" style={{ left: `${gate.at * 25}%` }}>
      <button
        onClick={() => setOpen((v) => !v)}
        title={`${gate.label} — ×${value ?? '…'} (click to change)`}
        className={`press flex items-center gap-1 rounded-full border px-1.5 py-1 text-[9px] font-bold tabular-nums ${
          open
            ? 'border-[var(--accent-strong)] bg-zinc-900 text-[var(--accent)]'
            : 'border-zinc-800 bg-zinc-950/90 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200'
        }`}
      >
        <Repeat size={10} className="text-[var(--accent)]" />
        ×{value ?? '…'}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute left-1/2 top-7 z-20 -translate-x-1/2 rounded-xl border border-zinc-700 bg-zinc-900 p-2 shadow-xl">
            <div className="mb-1.5 whitespace-nowrap text-[9px] font-medium uppercase tracking-wide text-zinc-500">
              {gate.label}
            </div>
            <div className="flex gap-1">
              {[0, 1, 2, 3, 4, 5].map((n) => (
                <button
                  key={n}
                  onClick={() => { onSet?.(gate.key, n); setOpen(false) }}
                  className={`press h-6 w-6 rounded-md text-[11px] font-bold tabular-nums ${
                    value === n
                      ? 'bg-[var(--accent-fill)] text-[var(--accent)] ring-1 ring-[var(--accent-strong)]'
                      : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200'
                  }`}
                >
                  {n}
                </button>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

const reviewZoneFor = (u) => {
  const w = u.wt
  if (w?.status === 'merged') return 'ready' // landed — flag stays planted
  if (w?.status === 'closed') return 'needs' // closed unmerged: human call
  if (w?.review_status === 'ready_to_merge') return 'ready'
  if (w?.review_status === 'needs_human') return 'needs'
  if (w?.review_status === 'fixing' || w?.ci_status === 'fixing' || w?.ci_status === 'failed') return 'prfixes'
  return 'inreview'
}

const codeZoneFor = (u) => {
  if (u.wt) {
    if (u.wt.status === 'active') return 'fixing'
    if (u.wt.status === 'ready') return 'committing'
    if (u.wt.status === 'pr_created' || u.wt.status === 'merged') return 'pr'
    return 'blocked' // error / closed unmerged
  }
  // ticket-swarm units without a worktree yet: planning IS part of fixing
  if (u.jobs.some((j) => ['queued', 'running'].includes(j.status))) return 'fixing'
  if (u.jobs.some((j) => j.verdict?.codeable === true) || unitWorkable(u)) return 'fixing'
  return 'blocked' // not codeable / failed → needs a human
}

// ---- Thread pipeline board (the owner's architecture diagram, 2026-07-09) --
// Incoming queue -> dispatcher -> N worker threads, one ticket at a time,
// walked end-to-end. Stages inside one thread mirror the diagram.
const THREAD_STAGE_LABELS = ['Scan', 'Ready', 'Trees', 'Code', 'PRs', 'Review', 'Re-review', 'Merge', 'UAT', 'Smoke']

// Where a ticket is on the thread's path, and whether it is stuck there.
// fail: the current stage is a stop, not progress. tone: amber = waiting on
// humans/answers (re-enters on its own), red = something actually broke.
function threadStage(u, phases) {
  const w = u.wt
  if (w) {
    if (w.status === 'merged') {
      if (w.uat_status === 'smoke_fail')
        return { idx: 9, fail: true, tone: 'red', why: (w.uat_note || 'UAT smoke test failed').slice(0, 90) }
      if (w.uat_status === 'smoke_pass' || w.uat_status === 'skipped') return { idx: 10 } // fully done
      if (w.uat_status === 'deployed') return { idx: 9 } // smoke running
      return { idx: 8 } // waiting for the change to arrive in UAT
    }
    if (w.status === 'closed')
      return { idx: 5, fail: true, tone: 'red', why: 'PR closed without merging — needs a human look' }
    if (w.review_status === 'ready_to_merge') return { idx: 7 }
    if (w.review_status === 'needs_human')
      return { idx: 6, fail: true, tone: 'amber', why: 'fix rounds exhausted — waiting for you' }
    if (w.review_status === 'fixing' || w.ci_status === 'fixing' || w.ci_status === 'failed') return { idx: 6 }
    if (w.status === 'pr_created') return { idx: 5 }
    if (w.status === 'ready') return { idx: 4 }
    if (w.status === 'active') return { idx: 3 }
    if (w.status === 'error')
      return { idx: 3, fail: true, tone: 'red', why: (w.error || 'coder failed').slice(0, 90) }
  }
  const planJ = [...u.jobs].reverse().find((j) => j.runbook === 'ticket-code-plan' && j.verdict)
  if (planJ && planJ.verdict.codeable === false) {
    // the planner declined at the READY gate — Trees was never entered
    return { idx: 1, fail: true, tone: 'amber',
             why: `not codeable — ${planJ.verdict.headline || 'planner declined'}`.slice(0, 90) }
  }
  if (u.jobs.some((j) => j.runbook === 'ticket-code-plan')) return { idx: 2 }
  if (u.jobs.some((j) => j.status === 'running')) return { idx: 0 }
  const done = u.jobs.filter((j) => !['queued', 'running'].includes(j.status))
  if (done.length) {
    if (u.jobs.some((j) => j.verdict?.workable)) return { idx: 1 }
    if (u.jobs.some((j) => j.status === 'error'))
      return { idx: 0, fail: true, tone: 'red',
               why: (u.jobs.find((j) => j.error)?.error || 'worker failed').slice(0, 90) }
    const clarify = u.jobs.some((j) => j.verdict?.clarifying_questions?.length)
    return { idx: 1, fail: true, tone: 'amber',
             why: clarify
               ? 'questions asked on the ticket — re-enters when answered'
               : 'not workable yet — re-enters on the next run' }
  }
  return { idx: -1 }
}

// One square per stage, showing WHAT is happening inside it: worktree rows
// under Trees, PR number+title rows under PR, CI/review state under Review.
function StageBoxes({ u, phases }) {
  const st = threadStage(u, phases)
  const wts = u.wts || []
  const prs = wts.filter((w) => w.pr_url)
  const prNo = (w) => (w.pr_url || '').split('/').pop()
  const inv = u.jobs.find((j) => j.runbook === 'ticket-investigate')
  const plan = u.jobs.find((j) => j.runbook === 'ticket-code-plan')
  const liveNote = (j) => (j?.status === 'running' && (phases[j.id]?.note || j.phase_note)) || null
  const activeWt = wts.find((w) => w.status === 'active')
  const boxes = [
    { label: 'Scan', rows: [
        liveNote(inv) ? { text: liveNote(inv) }
          : inv?.verdict?.headline ? { text: inv.verdict.headline }
          : inv ? { text: inv.status } : null,
      ].filter(Boolean) },
    { label: 'Ready', rows: [
        u.jobs.some((j) => j.verdict?.workable) ? { text: 'workable — ready for dev', tone: 'ok' }
          : st.idx === 1 && st.why ? { text: st.why, tone: st.tone === 'red' ? 'bad' : 'warn' } : null,
        inv?.verdict?.clarifying_questions?.length
          ? { text: `${inv.verdict.clarifying_questions.length} question${inv.verdict.clarifying_questions.length > 1 ? 's' : ''} on the ticket`, tone: 'warn' } : null,
      ].filter(Boolean) },
    { label: 'Trees', rows: wts.map((w) => ({
        text: w.branch,
        sub: [w.status, w.diff_stat].filter(Boolean).join(' · '),
        tone: w.status === 'error' ? 'bad' : undefined })) },
    { label: 'Code', rows: [
        activeWt ? { text: (activeWt.job_id && phases[activeWt.job_id]?.note) || 'coder working…' } : null,
        !activeWt && plan?.verdict?.change_plan
          ? { text: 'plan ready', sub: String(plan.verdict.change_plan).slice(0, 70) } : null,
        ...wts.filter((w) => w.status === 'error')
          .map((w) => ({ text: (w.error || 'coder failed').slice(0, 70), tone: 'bad' })),
        ...wts.filter((w) => w.summary && w.status !== 'error')
          .map((w) => ({ text: String(w.summary).slice(0, 70) })),
      ].filter(Boolean) },
    { label: 'PRs', rows: prs.map((w) => ({
        text: `PR #${prNo(w)}${w.status === 'merged' ? ' · merged' : w.status === 'closed' ? ' · closed' : ''}`,
        sub: w.title || w.branch, href: w.pr_url,
        tone: w.status === 'merged' ? 'ok' : w.status === 'closed' ? 'bad' : undefined })) },
    { label: 'Review', rows: prs.map((w) => ({
        text: `#${prNo(w)} · CI ${w.ci_status || 'pending'}`,
        sub: (w.review_status || 'no review yet').replace(/_/g, ' '),
        tone: w.ci_status === 'green' ? 'ok' : w.ci_status === 'failed' ? 'bad' : undefined })) },
    { label: 'Re-review', rows: prs
        .filter((w) => w.ci_status === 'fixing' || ['fixing', 'needs_human'].includes(w.review_status))
        .map((w) => ({
          text: `#${prNo(w)} ${w.review_status === 'needs_human' ? 'needs you' : 'fixing feedback'}`,
          tone: w.review_status === 'needs_human' ? 'warn' : undefined })) },
    { label: 'Merge', rows: prs
        .filter((w) => w.status === 'merged' || w.review_status === 'ready_to_merge')
        .map((w) => ({
          text: w.status === 'merged' ? `#${prNo(w)} merged ✓` : `#${prNo(w)} ready — your call`,
          tone: 'ok' })) },
    { label: 'UAT', rows: prs.filter((w) => w.status === 'merged').map((w) => ({
        text: w.uat_status ? 'change arrived in UAT ✓' : 'waiting for UAT deploy…',
        sub: w.uat_note || '',
        tone: w.uat_status ? 'ok' : undefined })) },
    { label: 'Smoke', rows: prs.filter((w) => w.status === 'merged' && w.uat_status).map((w) => ({
        text: w.uat_status === 'smoke_pass' ? 'smoke test passed ✓'
          : w.uat_status === 'smoke_fail' ? 'smoke test FAILED'
          : w.uat_status === 'skipped' ? 'smoke skipped'
          : 'smoke test queued…',
        sub: w.uat_note || '',
        tone: w.uat_status === 'smoke_pass' ? 'ok' : w.uat_status === 'smoke_fail' ? 'bad' : undefined })) },
  ]
  const rowCls = (t) =>
    t === 'ok' ? 'text-emerald-300' : t === 'bad' ? 'text-red-300' : t === 'warn' ? 'text-amber-300' : 'text-zinc-300'
  return (
    <div className="flex min-w-0 flex-1 gap-1.5 overflow-x-auto pb-0.5">
      {boxes.map((b, i) => {
        const isStop = st.fail && i === st.idx
        const state = isStop ? (st.tone === 'red' ? 'bad' : 'warn')
          : i < st.idx ? 'done' : i === st.idx ? 'active' : 'future'
        const border = state === 'bad' ? 'border-red-900/70'
          : state === 'warn' ? 'border-amber-800/70'
          : state === 'done' ? 'border-emerald-900/60'
          : state === 'active' ? 'border-sky-700/70'
          : 'border-zinc-800/60'
        return (
          <div
            key={b.label}
            className={`flex h-[104px] w-[138px] shrink-0 flex-col rounded-lg border bg-zinc-950/50 p-1.5 ${border} ${
              state === 'future' ? 'opacity-55' : ''
            }`}
          >
            <div className="flex shrink-0 items-center gap-1">
              {isStop ? (
                st.tone === 'red'
                  ? <XCircle size={9} className="shrink-0 text-red-400" />
                  : <AlertTriangle size={9} className="shrink-0 text-amber-400" />
              ) : (
                <div className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                  state === 'done' ? 'bg-emerald-500/80' : state === 'active' ? 'bg-sky-400' : 'bg-zinc-700'
                }`} />
              )}
              <span className={`text-[8px] font-bold uppercase tracking-wider ${
                state === 'bad' ? 'text-red-300' : state === 'warn' ? 'text-amber-300'
                  : state === 'done' ? 'text-emerald-400/80' : state === 'active' ? 'text-sky-300' : 'text-zinc-600'
              }`}>{b.label}</span>
              {b.rows.length > 1 && (
                <span className="ml-auto text-[8px] font-semibold tabular-nums text-zinc-500">{b.rows.length}</span>
              )}
            </div>
            <div className="mt-1 min-h-0 flex-1 space-y-1 overflow-y-auto pr-0.5">
              {b.rows.length === 0 && <div className="text-[8px] italic text-zinc-700">nothing yet</div>}
              {b.rows.map((r, k) => {
                const inner = (
                  <>
                    <div className={`truncate text-[9px] leading-tight ${rowCls(r.tone)}`}>{r.text}</div>
                    {r.sub && <div className="truncate text-[8px] leading-tight text-zinc-500">{r.sub}</div>}
                  </>
                )
                return r.href ? (
                  <a
                    key={k}
                    href={r.href}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="block rounded border border-zinc-800/70 bg-zinc-900/70 px-1 py-0.5 hover:border-zinc-600"
                  >
                    {inner}
                  </a>
                ) : (
                  <div key={k} className="rounded border border-zinc-800/50 bg-zinc-900/50 px-1 py-0.5">{inner}</div>
                )
              })}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// The retry circle: pick WHICH stage to re-enter the pipeline from.
const RETRY_STAGES = ['Scan', 'Ready', 'Trees', 'Code', 'PRs', 'Review', 'Re-review', 'Merge', 'UAT', 'Smoke']

// Where a resumed ticket should re-enter: exactly where it was left.
function autoStage(u) {
  const w = u.wt
  if (w) {
    if (w.status === 'merged') return w.uat_status ? 'Smoke' : 'UAT'
    if (w.review_status === 'ready_to_merge') return 'Merge'
    if (w.review_status === 'needs_human') return 'Re-review'
    if (w.status === 'pr_created') return 'Review'
    if (w.status === 'ready') return 'PRs'
    if (['active', 'error', 'closed'].includes(w.status)) return 'Code'
  }
  if (unitWorkable(u)) return 'Code'
  return 'Scan'
}

async function fireRetry(u, stage, toast) {
  try {
    const r = await post('/api/swarm/retry', { id: u.itemId, stage: stage.toLowerCase() })
    toast(`${u.ref}: ${r.note || (r.ok ? 'retry started' : 'could not retry')}`, r.ok ? 'success' : 'error')
  } catch (e) {
    toast(`Retry failed: ${String(e).slice(0, 120)}`, 'error')
  }
}

function RetryMenu({ u }) {
  const [open, setOpen] = useState(false)
  const toast = useToast()
  const fire = (stage) => {
    setOpen(false)
    fireRetry(u, stage, toast)
  }
  return (
    <div className="relative shrink-0">
      <button
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
        title="Retry this ticket from a chosen stage"
        className={`press grid h-5 w-5 place-items-center rounded-md border bg-zinc-900 ${
          open ? 'border-[var(--accent-strong)] text-[var(--accent)]' : 'border-zinc-700 text-zinc-400 hover:text-sky-300'
        }`}
      >
        <RotateCcw size={10} />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={(e) => { e.stopPropagation(); setOpen(false) }} />
          <div className="retry-open absolute right-0 top-6 z-50 w-40 overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 py-1 shadow-xl">
            <button
              onClick={(e) => { e.stopPropagation(); fire(autoStage(u)) }}
              className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-[11px] font-semibold text-[var(--accent)] hover:bg-zinc-800"
            >
              <Play size={9} className="shrink-0" />
              Resume — {autoStage(u)}
            </button>
            <div className="mx-2 my-0.5 h-px bg-zinc-800" />
            <div className="px-2.5 py-1 text-[8px] font-semibold uppercase tracking-wider text-zinc-500">
              or retry from stage
            </div>
            {RETRY_STAGES.map((s) => (
              <button
                key={s}
                onClick={(e) => { e.stopPropagation(); fire(s) }}
                className="flex w-full items-center gap-1.5 px-2.5 py-1 text-left text-[11px] text-zinc-300 hover:bg-zinc-800"
              >
                <RotateCcw size={9} className="shrink-0 text-[var(--accent)]" />
                {s}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

// One entry in the results stack: outcome, which thread did it, its PRs.
// Draggable: pick it up and drop it onto the thread sketch to re-process it.
function ResultRow({ u, tid, phases, active, onClick, onDragStart, onDragEnd }) {
  const a = unitAction(u, phases)
  const prs = (u.wts || []).filter((w) => w.pr_url)
  const draggable = u.kind === 'ado' && !!u.itemId
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      draggable={draggable}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = 'move'
        e.dataTransfer.setData('text/plain', String(u.itemId))
        onDragStart?.(u)
      }}
      onDragEnd={() => onDragEnd?.()}
      title={`${u.ref} ${u.title} — ${a.verb}${draggable ? ' · drag onto a thread to re-process' : ''}`}
      className={`card-pop relative w-full cursor-grab rounded-lg border bg-zinc-900/70 p-1.5 text-left hover:border-zinc-700 active:cursor-grabbing has-[.retry-open]:z-40 ${
        active ? 'border-zinc-700 ring-1 ring-[var(--accent)]' : 'border-zinc-800'
      }`}
    >
      <div className="flex items-center gap-1">
        <span className={`flex min-w-0 items-center gap-1 truncate rounded-full border px-1.5 py-px text-[8px] font-bold ${a.cls}`}>
          <a.icon size={8} className="shrink-0" />
          <span className="truncate">{a.verb}</span>
        </span>
        <span className="ml-auto shrink-0 rounded bg-zinc-800 px-1 py-px text-[8px] font-semibold tabular-nums text-zinc-500">
          T{tid}
        </span>
        {u.kind === 'ado' && u.itemId && <RetryMenu u={u} />}
      </div>
      <div className="mt-1 flex items-baseline gap-1">
        <SquareKanban size={8} className="shrink-0 self-center text-sky-500" />
        <span className="shrink-0 text-[10px] font-semibold tabular-nums text-zinc-200">{u.ref}</span>
        <span className="min-w-0 truncate text-[9px] text-zinc-400">{u.title}</span>
      </div>
      {prs.map((w) => (
        <a
          key={w.id}
          href={w.pr_url}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => e.stopPropagation()}
          className="mt-0.5 flex items-center gap-1 text-[8px] text-sky-300 hover:underline"
        >
          <GitPullRequest size={8} className="shrink-0" />
          <span className="truncate font-mono">{w.branch}</span>
          <span className="shrink-0 opacity-70">PR #{(w.pr_url || '').split('/').pop()} · {w.status === 'merged' ? 'merged' : w.status === 'closed' ? 'closed' : 'open'}</span>
        </a>
      ))}
      {(u.wts || []).filter((w) => (w.uat_status || '').startsWith('smoke')).map((w) => (
        <div
          key={`smoke-${w.id}`}
          title={w.uat_note || ''}
          className={`mt-0.5 flex items-center gap-1 text-[8px] ${
            w.uat_status === 'smoke_pass' ? 'text-emerald-300' : 'text-red-300'
          }`}
        >
          {w.uat_status === 'smoke_pass'
            ? <CheckCircle2 size={8} className="shrink-0" />
            : <XCircle size={8} className="shrink-0" />}
          <span className="shrink-0 font-semibold">
            UAT smoke {w.uat_status === 'smoke_pass' ? 'passed' : 'FAILED'}
          </span>
          <span className="min-w-0 truncate opacity-80">— {w.uat_note}</span>
        </div>
      ))}
    </div>
  )
}

// Compact chip for tickets a thread already finished (or parked).
function ThreadChip({ u, phases, active, onClick }) {
  const a = unitAction(u, phases)
  const prs = (u.wts || []).filter((w) => w.pr_url)
  return (
    <button
      onClick={onClick}
      title={`${u.ref} ${u.title} — ${a.verb}${prs.length ? ` · ${prs.length} PR${prs.length > 1 ? 's' : ''}` : ''}`}
      className={`press flex max-w-[230px] items-center gap-1 rounded-lg border px-1.5 py-1 text-[9px] font-semibold ${a.cls} ${
        active ? 'ring-1 ring-[var(--accent)]' : ''
      }`}
    >
      <a.icon size={9} className="shrink-0" />
      <span className="shrink-0 tabular-nums">{u.ref}</span>
      <span className="min-w-0 truncate font-normal opacity-80">{a.verb}</span>
      {prs.slice(0, 3).map((w) => (
        <a
          key={w.id}
          href={w.pr_url}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => e.stopPropagation()}
          title={`${w.branch} — ${w.pr_url}`}
          className="shrink-0 text-sky-300 hover:text-sky-200"
        >
          <GitPullRequest size={9} />
        </a>
      ))}
    </button>
  )
}

// The drop sketch: a ghost thread lane that appears while dragging a result,
// one drop cell per stage, the state-based suggestion glowing.
function DropSketch({ dragU, overStage, setOverStage, onDrop }) {
  const suggested = autoStage(dragU)
  return (
    <div className="animate-fade-in rounded-xl border-2 border-dashed border-sky-700/60 bg-sky-950/10 p-2">
      <div className="mb-1.5 flex items-center gap-2">
        <Bot size={11} className="text-sky-400" />
        <span className="text-[9px] font-semibold uppercase tracking-[0.14em] text-sky-300">
          drop {dragU.ref} into a thread
        </span>
        <span className="text-[9px] text-zinc-500">
          suggested stage based on its state: <b className="text-sky-300">{suggested}</b>
        </span>
      </div>
      <div className="flex gap-1.5 overflow-x-auto">
        {RETRY_STAGES.map((s) => {
          const isSuggested = s === suggested
          const isOver = overStage === s
          return (
            <div
              key={s}
              onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setOverStage(s) }}
              onDragLeave={() => setOverStage((cur) => (cur === s ? null : cur))}
              onDrop={(e) => { e.preventDefault(); onDrop(s) }}
              className={`flex h-[56px] w-[118px] shrink-0 flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed transition-colors ${
                isOver ? 'border-emerald-500 bg-emerald-950/40'
                  : isSuggested ? 'unit-ring border-sky-500/80 bg-sky-950/40'
                  : 'border-zinc-800 bg-zinc-950/40'
              }`}
            >
              <span className={`text-[9px] font-bold uppercase tracking-wider ${
                isOver ? 'text-emerald-300' : isSuggested ? 'text-sky-300' : 'text-zinc-600'
              }`}>{s}</span>
              {isSuggested && !isOver && (
                <span className="rounded-full bg-sky-900/80 px-1.5 text-[7px] font-semibold uppercase text-sky-300">
                  suggested
                </span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ThreadBoard({ run, jobs, worktrees, phases, log, selectedKey, onSelect, control, onControl, onRetry }) {
  const toast = useToast()
  const [dragU, setDragU] = useState(null)
  const [overStage, setOverStage] = useState(null)
  // the live board merges the last runs (newest state per target) — show it
  // all: lanes for thread-owned work, queue for waiting, results for outcomes
  const units = buildUnits(jobs || [], worktrees, phases, run?.id)
  const running = run?.status === 'running'
  // The owner's rule: only DONE work drops to the results stack. Anything blocked
  // or waiting (HITL, clarification answers, failed coder/PR, smoke failure)
  // STAYS on its thread until a human or an answer unblocks it — the clarify
  // loop tags people and auto-resumes when they reply.
  const isLive = (u) =>
    u.jobs.some((j) => ['queued', 'running'].includes(j.status)) ||
    ['active', 'ready', 'pr_created', 'error', 'closed'].includes(u.wt?.status || '') ||
    (u.wt?.status === 'merged' &&
      !['smoke_pass', 'skipped'].includes(u.wt?.uat_status || '')) ||
    u.wt?.review_status === 'needs_human' ||
    (!u.wt && u.jobs.some((j) => j.status === 'blocked')) ||
    (unitWorkable(u) && !planNotCodeable(u))
  const queue = []
  const noThread = []
  const lanes = new Map()
  for (const u of units) {
    const tids = u.jobs.map((j) => j.thread).filter(Boolean)
    if (!tids.length) {
      // wave-run tickets have no thread: waiting ones queue, done ones stack
      ;(isLive(u) ? queue : noThread).push(u)
      continue
    }
    const tid = Math.max(...tids)
    lanes.set(tid, [...(lanes.get(tid) || []), u])
  }
  const allLaneIds = [...lanes.keys()].sort((a, b) => a - b)
  // auto-scaling UI: a lane exists only while its thread is busy — threads
  // appear as the dispatcher hands out tickets and vanish once they drain
  const laneIds = allLaneIds.filter((tid) => lanes.get(tid).some(isLive))
  // the results stack: every finished ticket across all threads, newest first
  const results = [
    ...allLaneIds.flatMap((tid) =>
      lanes.get(tid).filter((u) => !isLive(u)).map((u) => ({ u, tid }))),
    ...noThread.map((u) => ({ u, tid: '—' })),
  ].sort((a, b) => Math.max(...b.u.jobs.map((j) => j.id)) - Math.max(...a.u.jobs.map((j) => j.id)))
  const selectedUnit = units.find((u) => u.key === selectedKey)
  return (
    <div className="relative flex h-full min-h-0 flex-col gap-1.5">
      {/* 1 · incoming ticket queue -> the dispatcher hands these to free threads */}
      <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-dashed border-zinc-800 bg-zinc-950/40 px-2.5 py-1.5">
        <span className="text-[9px] font-semibold uppercase tracking-[0.14em] text-zinc-500">
          Queue <span className="tabular-nums text-zinc-300">{queue.length}</span>
        </span>
        {queue.length === 0 && (
          <span className="text-[10px] italic text-zinc-600">
            empty — the dispatcher handed everything to the threads
          </span>
        )}
        {queue.map((u) => (
          <ThreadChip
            key={u.key}
            u={u}
            phases={phases}
            active={selectedKey === u.key}
            onClick={() => onSelect(selectedKey === u.key ? null : u.key)}
          />
        ))}
      </div>
      {/* while dragging a result: the ghost thread appears to catch the drop */}
      {dragU && (
        <DropSketch
          dragU={dragU}
          overStage={overStage}
          setOverStage={setOverStage}
          onDrop={(stage) => {
            const u = dragU
            setDragU(null)
            setOverStage(null)
            fireRetry(u, stage, toast)
          }}
        />
      )}
      {/* 2/3 · worker threads (left) + the results stack (right) */}
      <div className="flex min-h-0 flex-1 gap-1.5">
        <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1">
          {laneIds.length === 0 && (
            <div className="grid h-full place-items-center">
              <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-950/60 px-4 py-2 text-[11px] italic text-zinc-600">
                {running
                  ? 'threads spin up as the dispatcher hands out tickets…'
                  : 'no active threads — finished work lives in the results stack; run the pipeline to spin them up'}
              </div>
            </div>
          )}
          {laneIds.map((tid) => {
            const us = lanes.get(tid)
            const live = us.filter(isLive)
            const doneUs = us.filter((u) => !isLive(u))
            // a lane only holds what the thread still owns; the busiest ticket
            // gets the big card + stage boxes, other in-flight ones show as chips
            const working = live.filter((u) =>
              u.jobs.some((j) => ['queued', 'running'].includes(j.status)) || u.wt?.status === 'active')
            const current = working[0] || live[0] || null
            const alsoLive = live.filter((u) => u !== current)
            return (
              <div
                key={tid}
                className="flex min-h-[120px] items-center gap-2.5 rounded-xl border border-zinc-800/80 p-2"
                style={{ background: 'radial-gradient(ellipse 45% 90% at 5% 50%, rgba(56,189,248,0.06), transparent)' }}
              >
                <div className="flex w-[74px] shrink-0 flex-col items-start gap-1">
                  <span className="rounded-full border border-zinc-800 bg-zinc-950/90 px-2 py-1 text-[9px] font-semibold uppercase tracking-[0.12em] text-zinc-300">
                    Thread {tid}
                  </span>
                  <span className="px-1 text-[9px] tabular-nums text-zinc-600">
                    {doneUs.length}/{us.length} done
                  </span>
                  {working.length > 0 && (
                    <span className="flex items-center gap-1 px-1 text-[9px] text-sky-400">
                      <Bot size={9} /> busy<ThinkingDots />
                    </span>
                  )}
                </div>
                {current ? (
                  <div className="w-[196px] shrink-0">
                    <AgentUnit
                      u={current}
                      zone={zoneForUnit(current, phases, 'ado')}
                      zones={zonesFor('ado')}
                      phases={phases}
                      active={selectedKey === current.key}
                      paused={control?.paused || control?.paused_targets?.includes(current.key)}
                      stopped={control?.stopped_targets?.includes(current.key)}
                      onControl={running ? onControl : null}
                      onRetry={onRetry}
                      onClick={() => onSelect(selectedKey === current.key ? null : current.key)}
                    />
                  </div>
                ) : (
                  <span className="w-[196px] shrink-0 text-[10px] italic text-zinc-600">
                    {running ? 'waiting for the next ticket…' : 'idle — everything handed to the results stack'}
                  </span>
                )}
                {current
                  ? <StageBoxes u={current} phases={phases} />
                  : <div className="min-w-0 flex-1" />}
                {alsoLive.length > 0 && (
                  <div className="flex max-w-[220px] shrink-0 flex-col items-end gap-1">
                    {alsoLive.map((u) => (
                      <ThreadChip
                        key={u.key}
                        u={u}
                        phases={phases}
                        active={selectedKey === u.key}
                        onClick={() => onSelect(selectedKey === u.key ? null : u.key)}
                      />
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
        {/* 4 · results stack: every finished ticket from every thread, newest first */}
        <div className="flex w-[248px] shrink-0 flex-col rounded-xl border border-zinc-800/80 bg-zinc-950/40">
          <div className="flex items-center gap-1.5 border-b border-zinc-800 px-2.5 py-1.5">
            <Flag size={10} className="text-emerald-400" />
            <span className="text-[9px] font-semibold uppercase tracking-[0.14em] text-zinc-500">
              Results <span className="tabular-nums text-zinc-300">{results.length}</span>
            </span>
          </div>
          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-1.5">
            {results.length === 0 && (
              <div className="p-2 text-[10px] italic text-zinc-600">
                finished tickets stack here — outcome, thread, and every PR
              </div>
            )}
            {results.map(({ u, tid }) => (
              <ResultRow
                key={u.key}
                u={u}
                tid={tid}
                phases={phases}
                active={selectedKey === u.key}
                onClick={() => onSelect(selectedKey === u.key ? null : u.key)}
                onDragStart={setDragU}
                onDragEnd={() => { setDragU(null); setOverStage(null) }}
              />
            ))}
          </div>
        </div>
      </div>
      {(log || []).length > 0 && (
        <div className="space-y-0.5">
          {log.slice(-2).map((l, i, arr) => (
            <div
              key={l.ts}
              className="truncate rounded-md bg-zinc-950/85 px-2 py-0.5 font-mono text-[9px] text-zinc-400"
              style={{ opacity: 0.5 + (0.5 * (i + 1)) / arr.length }}
            >
              <span className="text-zinc-600">{l.who}</span> {l.note}
            </div>
          ))}
        </div>
      )}
      {selectedUnit && <UnitInspector u={selectedUnit} phases={phases} onClose={() => onSelect(null)} />}
      {run && <Totals run={run} jobs={(jobs || []).filter((j) => (j.run_id ?? run.id) === run.id)} />}
    </div>
  )
}

function SwarmBoard({ mode, run, jobs, worktrees, phases, log, selectedKey, onSelect, control, onControl, onRetry, gateValues, onSetGate }) {
  // the ADO swarm ALWAYS shows the thread architecture view (queue -> threads
  // -> results); non-thread runs still render — their tickets appear in the
  // queue while live and in the results stack when finished
  if (mode === 'ado' || run?.profile === 'thread') {
    return (
      <ThreadBoard
        run={run}
        jobs={jobs}
        worktrees={worktrees}
        phases={phases}
        log={log}
        selectedKey={selectedKey}
        onSelect={onSelect}
        control={control}
        onControl={onControl}
        onRetry={onRetry}
      />
    )
  }
  const ZONES = zonesFor(mode)
  const units = buildUnits(jobs, worktrees, phases, run?.id)
  const clarifyMap = new Map((run?.totals?.receipts?.clarifications || [])
    .map((c) => [`ado:${c.item}`, c.result]))
  units.forEach((u) => { u.clarifyResult = clarifyMap.get(u.key) })
  const running = run?.status === 'running'
  const isAdo = mode === 'ado'
  // the pipeline: everything in a ticket-swarm run, tickets with worktrees,
  // and scanned-workable tickets (which auto-chain into the ticket swarm)
  const codeUnits = isAdo
    ? units.filter((u) => u.wt || run?.profile === 'ticket'
        || (!running && zoneForUnit(u, phases, mode) === 'pr' && unitWorkable(u)))
    : []
  const codeKeys = new Set(codeUnits.map((u) => u.key))
  const topSlots = { new: 0, active: 0, blocked: 0, pr: 0 }
  const topPlaced = units.filter((u) => !codeKeys.has(u.key)).map((u) => {
    const zone = zoneForUnit(u, phases, mode)
    return { u, zone, slot: topSlots[zone]++ }
  })
  const reviewUnits = codeUnits.filter((u) =>
    ['pr_created', 'merged', 'closed'].includes(u.wt?.status))
  const reviewKeys = new Set(reviewUnits.map((u) => u.key))
  const codeSlots = { fixing: 0, committing: 0, blocked: 0, pr: 0 }
  const codePlaced = codeUnits.filter((u) => !reviewKeys.has(u.key)).map((u) => {
    const zone = codeZoneFor(u)
    return { u, zone, slot: codeSlots[zone]++ }
  })
  const reviewSlots = { inreview: 0, prfixes: 0, needs: 0, ready: 0 }
  const reviewPlaced = reviewUnits.map((u) => {
    const zone = reviewZoneFor(u)
    return { u, zone, slot: reviewSlots[zone]++ }
  })
  const selectedUnit = units.find((u) => u.key === selectedKey)
  return (
    <div className="relative flex h-full flex-col gap-1.5">
      <Arena
        zones={ZONES}
        placed={topPlaced}
        phases={phases}
        selectedKey={selectedKey}
        onSelect={onSelect}
        log={log}
        control={running ? control : null}
        onControl={running ? onControl : null}
        onRetry={onRetry}
        compact={isAdo}
        gates={SCAN_GATES}
        gateValues={gateValues}
        onSetGate={onSetGate}
        emptyHint="board is empty — plan tickets in the Tickets tab and push them here"
      />
      {isAdo && (
        <>
          <div className="flex items-center gap-2 px-1">
            <span className="text-[9px] font-semibold uppercase tracking-[0.14em] text-zinc-600">
              ↓ coding pipeline · ticket → PR · flows on its own
            </span>
          </div>
          <Arena
            zones={CODE_ZONES}
            placed={codePlaced}
            phases={phases}
            selectedKey={selectedKey}
            onSelect={onSelect}
            control={running ? control : null}
            onControl={running ? onControl : null}
            onRetry={onRetry}
            compact
            gates={CODE_GATES}
            gateValues={gateValues}
            onSetGate={onSetGate}
            emptyHint="workable tickets drop here after a scan; coders march them to PR"
          />
          <div className="flex items-center gap-2 px-1">
            <span className="text-[9px] font-semibold uppercase tracking-[0.14em] text-zinc-600">
              ↓ review pipeline · pr → merge · fixes itself
            </span>
          </div>
          <Arena
            zones={REVIEW_ZONES}
            placed={reviewPlaced}
            phases={phases}
            selectedKey={selectedKey}
            onSelect={onSelect}
            onRetry={onRetry}
            compact
            gates={REVIEW_GATES}
            gateValues={gateValues}
            onSetGate={onSetGate}
            emptyHint="shipped PRs land here; the swarm watches checks + comments to merge-ready"
          />
        </>
      )}
      {selectedUnit && <UnitInspector u={selectedUnit} phases={phases} onClose={() => onSelect(null)} />}
      {run && <Totals run={run} jobs={jobs} />}
    </div>
  )
}

function StatusChip({ status, onClick, active }) {
  const M = meta(status)
  return (
    <button
      onClick={onClick}
      disabled={!onClick}
      className={`press inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${M.chip} ${
        active ? 'ring-1 ring-[var(--accent)]' : ''
      }`}
    >
      <M.icon size={11} className={M.ic} />
      {status}
    </button>
  )
}

function ReceiptsMatrix({ jobs, selected, onSelect }) {
  const items = []
  const seen = new Set()
  for (const j of jobs) {
    if (!seen.has(targetKey(j))) {
      seen.add(targetKey(j))
      items.push(j)
    }
  }
  const runbooks = [...new Set(jobs.map((j) => j.runbook))]
  const cell = {}
  for (const j of jobs) cell[`${targetKey(j)}:${j.runbook}`] = j
  return (
    <div className="overflow-x-auto rounded-xl border border-zinc-800">
      <table className="w-full text-[12px]">
        <thead>
          <tr className="border-b border-zinc-800 bg-zinc-950 text-left text-[10px] uppercase tracking-wide text-zinc-500">
            <th className="px-2.5 py-1.5 font-semibold">Target</th>
            {runbooks.map((r) => (
              <th key={r} className="px-2.5 py-1.5 font-semibold">{r}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((it) => (
            <tr key={targetKey(it)} className="border-b border-zinc-800/60 last:border-0 hover:bg-zinc-800/30">
              <td className="max-w-[340px] px-2.5 py-1.5">
                <a href={it.item_url} target="_blank" rel="noreferrer" className="group flex items-center gap-1.5">
                  {it.kind === 'pr'
                    ? <GitPullRequest size={11} className="shrink-0 text-purple-400" />
                    : <SquareKanban size={11} className="shrink-0 text-sky-500" />}
                  <span className="shrink-0 tabular-nums text-zinc-500">{refLabel(it)}</span>
                  <span className="truncate text-zinc-200 group-hover:text-zinc-50">{it.item_title}</span>
                  <ExternalLink size={11} className="shrink-0 text-zinc-700 opacity-0 group-hover:opacity-100" />
                </a>
              </td>
              {runbooks.map((r) => {
                const j = cell[`${targetKey(it)}:${r}`]
                return (
                  <td key={r} className="px-2.5 py-1.5">
                    {j ? (
                      <StatusChip status={j.status} active={selected === j.id} onClick={() => onSelect(j)} />
                    ) : (
                      <span className="text-zinc-700">—</span>
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FindingList({ title, items, tone }) {
  if (!items?.length) return null
  return (
    <div className="mt-2">
      <div className={`mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide ${tone === 'red' ? 'text-red-400' : 'text-amber-400'}`}>
        {tone === 'red' ? <Bug size={11} /> : <Wrench size={11} />}
        {title} · {items.length}
      </div>
      <ul className="space-y-1">
        {items.map((f, i) => (
          <li key={i} className={`rounded-lg border px-2.5 py-1.5 text-[12px] ${
            tone === 'red' ? 'border-red-900/50 bg-red-950/20' : 'border-amber-900/40 bg-amber-950/20'
          }`}>
            <div className="font-medium text-zinc-200">{f.title}</div>
            <div className="text-[11px] text-zinc-500">{f.where} — {f.why}</div>
          </li>
        ))}
      </ul>
    </div>
  )
}

function JobDetail({ job }) {
  const M = meta(job.status)
  const v = job.verdict || {}
  return (
    <div className="animate-fade-in rounded-xl border border-zinc-800 bg-zinc-950 p-3">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <M.icon size={14} className={M.ic} />
        <a href={job.item_url} target="_blank" rel="noreferrer" className="font-medium text-zinc-200 hover:text-zinc-50">
          {refLabel(job)} {job.item_title}
        </a>
        <span className="rounded-md bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">{job.runbook}</span>
        <span className="ml-auto flex items-center gap-2 text-[10px] tabular-nums text-zinc-500">
          <span>{job.model}</span>
          <span>{fmtMs(job.duration_ms)}</span>
          <span>{job.cost_usd != null ? `$${job.cost_usd.toFixed(4)}` : '·'}</span>
        </span>
      </div>
      {v.headline && <p className="mt-2 text-[13px] font-medium text-zinc-100">{v.headline}</p>}
      {job.error && <p className="mt-2 text-[12px] text-red-300">{job.error}</p>}
      {(v.clarifying_questions || []).length > 0 && (
        <div className="mt-2 rounded-lg border border-amber-900/50 bg-amber-950/20 px-2.5 py-1.5">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-amber-400">
            <MessageSquare size={11} /> Clarifying questions asked on the ticket
          </div>
          <ol className="list-inside list-decimal space-y-0.5 text-[12px] text-zinc-300">
            {v.clarifying_questions.map((q, i) => <li key={i}>{q}</li>)}
          </ol>
        </div>
      )}
      <FindingList title="Critical issues" items={v.critical_issues} tone="red" />
      <FindingList title="Tech debt" items={v.tech_debt} tone="amber" />
      {v.codeable != null && (
        <p className="mt-2 text-[12px]">
          <span className={`mr-1.5 rounded px-1.5 py-0.5 text-[10px] font-semibold ${v.codeable ? 'bg-emerald-950 text-emerald-300' : 'bg-zinc-800 text-zinc-500'}`}>
            {v.codeable ? 'codeable' : 'not codeable'}
          </span>
          {v.codeable && <span className="text-zinc-400">{(v.change_plan || '').slice(0, 300)}</span>}
        </p>
      )}
      {(v.evidence || []).length > 0 && (
        <ul className="mt-2 space-y-1">
          {v.evidence.map((e, i) => (
            <li key={i} className="flex gap-1.5 text-[12px] text-zinc-400">
              <span className="text-zinc-600">•</span>
              <span>{e}</span>
            </li>
          ))}
        </ul>
      )}
      {v.suggested_next_action && (
        <p className="mt-2 rounded-lg bg-zinc-900 px-2.5 py-1.5 text-[12px] text-sky-200/90">
          <span className="mr-1.5 text-[10px] font-semibold uppercase tracking-wide text-sky-500">next</span>
          {v.suggested_next_action}
        </p>
      )}
    </div>
  )
}

function Totals({ run, jobs }) {
  const t = run.totals || {
    pass: jobs.filter((j) => j.status === 'pass').length,
    fail: jobs.filter((j) => j.status === 'fail').length,
    blocked: jobs.filter((j) => j.status === 'blocked').length,
    error: jobs.filter((j) => j.status === 'error').length,
    cost_usd: jobs.reduce((n, j) => n + (j.cost_usd || 0), 0),
  }
  const done = jobs.filter((j) => !['queued', 'running'].includes(j.status)).length
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
      {run.status === 'running' && (
        <span className="flex items-center gap-1 rounded-md border border-sky-800 bg-sky-950 px-2 py-0.5 text-sky-300">
          <Loader2 size={11} className="animate-spin" /> {done}/{jobs.length} workers
        </span>
      )}
      {run.status === 'stopped' && (
        <span className="rounded-md border border-amber-800 bg-amber-950 px-2 py-0.5 text-amber-300">stopped</span>
      )}
      {['pass', 'fail', 'blocked', 'error'].map((s) =>
        t[s] > 0 ? (
          <span key={s} className={`rounded-md border px-2 py-0.5 ${meta(s).chip}`}>
            {t[s]} {s}
          </span>
        ) : null,
      )}
      <span className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-0.5 tabular-nums text-zinc-400">
        ${(t.cost_usd || 0).toFixed(2)}
      </span>
      <span className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-0.5 tabular-nums text-zinc-400">
        {run.status === 'running' && run.started_at ? fmtMs(Date.now() - new Date(run.started_at)) : fmtMs(run.duration_ms)}
      </span>
      {(run.totals?.worktrees || []).length > 0 && (
        <span className="rounded-md border border-violet-900 bg-violet-950 px-2 py-0.5 text-violet-300">
          {run.totals.worktrees.length} worktree{run.totals.worktrees.length !== 1 ? 's' : ''} → Worktrees tab
        </span>
      )}
      {run.totals?.receipts?.teams_summary && (
        <span className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-zinc-500">
          teams: {String(run.totals.receipts.teams_summary).slice(0, 40)}
        </span>
      )}
      {(run.totals?.receipts?.ado_comments || []).length > 0 && (
        <span className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-zinc-500">
          ado: {run.totals.receipts.ado_comments[0].result}
        </span>
      )}
      {(run.totals?.receipts?.pr_comments || []).length > 0 && (
        <span className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-zinc-500">
          pr: {run.totals.receipts.pr_comments[0].result}
        </span>
      )}
    </div>
  )
}

function RunBlock({ run, jobs, selected, onSelect }) {
  return (
    <div className="space-y-2.5">
      <Totals run={run} jobs={jobs} />
      {jobs.length > 0 && <ReceiptsMatrix jobs={jobs} selected={selected?.id} onSelect={onSelect} />}
      {selected && jobs.some((j) => j.id === selected.id) && (
        <JobDetail job={jobs.find((j) => j.id === selected.id)} />
      )}
      {run.summary && (
        <div className="rounded-xl border border-zinc-800 bg-zinc-950 p-3">
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
            <Sparkles size={11} className="text-[var(--accent)]" /> Reviewer summary
          </div>
          <pre className="whitespace-pre-wrap font-sans text-[12px] leading-relaxed text-zinc-300">{run.summary}</pre>
        </div>
      )}
      {run.error && run.status === 'error' && <p className="text-[12px] text-red-300">{run.error}</p>}
    </div>
  )
}

// Copy the swarm-ready ticket template (what to write in which ADO field so
// the swarm can code it) — canonical source: runbooks/_ticket-template.md
function TemplateButton() {
  const toast = useToast()
  return (
    <button
      onClick={() =>
        fetch('/api/swarm/ticket-template')
          .then((r) => r.json())
          .then((d) => navigator.clipboard.writeText(d.template || ''))
          .then(() => toast('Ticket template copied — paste into the ADO fields', 'success'))
          .catch(() => toast('Copy failed', 'error'))
      }
      title="Copy the swarm-ready ticket template: what to put in Repro Steps / Description / Acceptance Criteria so the swarm can code it"
      className="press flex items-center gap-1 rounded-lg bg-zinc-800 px-2 py-1 text-[10px] text-zinc-400 hover:text-zinc-200"
    >
      <ClipboardList size={10} /> template
    </button>
  )
}

// Tickets sub-tab: owner/type/state dropdowns filter the list; multi-select
// tickets and push them to the board. Filters + selection persist across tab
// switches and reloads; only the Reset button clears the filters.
function TargetPicker({ running, onRun, mode }) {
  const KEY = `swarmpicker:${mode}`
  const saved = useMemo(() => {
    try { return JSON.parse(localStorage.getItem(KEY) || '{}') } catch { return {} }
  }, [KEY])
  const [items, setItems] = useState(null)
  const [owner, setOwner] = useState(saved.owner || '')
  const [type, setType] = useState(saved.type || '')
  const [state, setState] = useState(saved.state || '')
  const [tag, setTag] = useState(saved.tag || '')
  const [sel, setSel] = useState(() => new Set(saved.sel || []))
  const [menuOpen, setMenuOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify({ owner, type, state, tag, sel: [...sel] })) } catch {}
  }, [KEY, owner, type, state, tag, sel])

  const load = useCallback((refresh) => {
    if (refresh) setBusy(true)
    fetch(`/api/swarm/candidates${refresh ? '?refresh=1' : ''}`)
      .then((r) => r.json())
      .then((d) => setItems(d.items || []))
      .catch(() => setItems([]))
      .finally(() => setBusy(false))
  }, [])
  useEffect(() => { load() }, [load])

  const opts = useMemo(() => {
    const uniq = (xs) => [...new Set(xs.filter(Boolean))].sort()
    return {
      owners: uniq((items || []).map((i) => i.assigned_to)),
      types: uniq((items || []).map((i) => i.type)),
      states: uniq((items || []).map((i) => i.state)),
      tags: uniq((items || []).flatMap((i) => i.tags || [])),
    }
  }, [items])

  const filtered = (items || []).filter(
    (i) => (!owner || i.assigned_to === owner) && (!type || i.type === type) &&
           (!state || i.state === state) && (!tag || (i.tags || []).includes(tag)),
  )
  const toggle = (id) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const allVisible = filtered.length > 0 && filtered.every((i) => sel.has(i.id))

  const Select = ({ value, onChange, label, options }) => (
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="cursor-pointer appearance-none rounded-lg border border-zinc-800 bg-zinc-950 py-1 pl-2.5 pr-7 text-[11px] text-zinc-300 hover:border-zinc-700"
      >
        <option value="">{label}: all</option>
        {options.map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
      <ChevronDown size={11} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-zinc-600" />
    </div>
  )

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <Select value={owner} onChange={setOwner} label="Owner" options={opts.owners} />
        <Select value={type} onChange={setType} label="Type" options={opts.types} />
        <Select value={state} onChange={setState} label="State" options={opts.states} />
        <Select value={tag} onChange={setTag} label="Tag" options={opts.tags} />
        <button
          onClick={() => setSel(allVisible ? new Set() : new Set(filtered.map((i) => i.id)))}
          className="press rounded-lg bg-zinc-800 px-2 py-1 text-[10px] text-zinc-400 hover:text-zinc-200"
        >
          {allVisible ? 'clear' : 'select all'} ({filtered.length})
        </button>
        {(owner || type || state || tag) && (
          <button
            onClick={() => { setOwner(''); setType(''); setState(''); setTag('') }}
            title="Clear all filters"
            className="press flex items-center gap-1 rounded-lg bg-zinc-800 px-2 py-1 text-[10px] text-zinc-400 hover:text-zinc-200"
          >
            <RotateCcw size={10} /> reset
          </button>
        )}
        <RefreshButton busy={busy} onClick={() => load(true)} title="Refresh from Azure DevOps" />
        <TemplateButton />
        {sel.size > 0 && (
          <div className="relative ml-auto">
            <button
              onClick={() => setMenuOpen((v) => !v)}
              disabled={running}
              className="press flex items-center gap-1.5 rounded-lg bg-[var(--accent-fill)] px-2.5 py-1 text-[11px] font-medium text-zinc-100 hover:brightness-110 disabled:opacity-40"
            >
              <Play size={11} /> Push {sel.size} to board <ChevronDown size={11} className="opacity-70" />
            </button>
            {menuOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
                <div className="absolute right-0 top-8 z-20 w-64 overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 shadow-xl">
                  {MODES.ado.profiles.map((p) => (
                    <button
                      key={p.id}
                      onClick={() => { setMenuOpen(false); onRun(p.id, [...sel]); setSel(new Set()) }}
                      className="flex w-full items-start gap-2.5 px-3 py-2 text-left hover:bg-zinc-800"
                    >
                      <p.icon size={13} className="mt-0.5 shrink-0 text-[var(--accent)]" />
                      <span className="min-w-0">
                        <span className="block text-[12px] font-medium text-zinc-200">{p.label}</span>
                        <span className="block text-[10px] text-zinc-500">{p.desc}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </div>
      {!items && <div className="skeleton mt-2 h-24 w-full rounded-lg" />}
      {items && (
        <div className="mt-2 max-h-[calc(100vh-330px)] space-y-0.5 overflow-y-auto pr-1">
          {filtered.length === 0 && <div className="py-3 text-center text-[12px] text-zinc-600">no matching items</div>}
          {filtered.map((i) => (
            <label
              key={i.id}
              className={`flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1 text-[12px] hover:bg-zinc-800/50 ${
                sel.has(i.id) ? 'bg-[var(--accent-fill)]/40' : ''
              }`}
            >
              <input
                type="checkbox"
                checked={sel.has(i.id)}
                onChange={() => toggle(i.id)}
                className="shrink-0 accent-[var(--accent)]"
              />
              <span className="shrink-0 text-[10px] font-semibold uppercase text-zinc-500">{i.type}</span>
              <span className="shrink-0 tabular-nums text-zinc-600">#{i.id}</span>
              <span className="min-w-0 flex-1 truncate text-zinc-200">{decodeEntities(i.title)}</span>
              <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-px text-[10px] text-zinc-400">{i.state}</span>
              <span className="w-28 shrink-0 truncate text-right text-[10px] text-zinc-500">{i.assigned_to || '—'}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

// PRs sub-tab: open PRs involving the owner; filter by author, repo, or search by
// PR number / linked AB# ticket; multi-select and push to the board.
function PrPicker({ running, onRun }) {
  const KEY = 'swarmprpicker'
  const saved = useMemo(() => {
    try { return JSON.parse(localStorage.getItem(KEY) || '{}') } catch { return {} }
  }, [])
  const [prs, setPrs] = useState(null)
  const [repos, setRepos] = useState([])
  const [author, setAuthor] = useState(saved.author || '')
  const [repo, setRepo] = useState(saved.repo || '')
  const [q, setQ] = useState(saved.q || '')
  const [sel, setSel] = useState(() => new Set(saved.sel || []))
  const [menuOpen, setMenuOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify({ author, repo, q, sel: [...sel] })) } catch {}
  }, [author, repo, q, sel])

  // picking a repo fetches ALL open PRs of that repo (anyone's), not just
  // the ones involving you — whole repos can be swarmed
  const load = useCallback((refresh) => {
    setBusy(true)
    fetch(`/api/swarm/pr-candidates?repo=${encodeURIComponent(repo)}${refresh ? '&refresh=1' : ''}`)
      .then((r) => r.json())
      .then((d) => { setPrs(d.prs || []); if (d.repos?.length) setRepos(d.repos) })
      .catch(() => setPrs([]))
      .finally(() => setBusy(false))
  }, [repo])
  useEffect(() => { load() }, [load])

  const opts = useMemo(() => {
    const uniq = (xs) => [...new Set(xs.filter(Boolean))].sort()
    return {
      authors: uniq((prs || []).map((p) => p.author)),
      repos: repos.length ? repos : uniq((prs || []).map((p) => (p.repo || '').split('/').pop())),
    }
  }, [prs, repos])

  const needle = q.trim().toLowerCase()
  const filtered = (prs || []).filter((p) =>
    (!author || p.author === author) &&
    (!needle || String(p.number).includes(needle) ||
      (p.ab || '').includes(needle.replace(/^ab#?/, '')) ||
      (p.title || '').toLowerCase().includes(needle)))
  const key = (p) => `${p.repo}#${p.number}`
  const toggle = (p) => setSel((s) => { const n = new Set(s); n.has(key(p)) ? n.delete(key(p)) : n.add(key(p)); return n })
  const allVisible = filtered.length > 0 && filtered.every((p) => sel.has(key(p)))
  const selRefs = () => (prs || []).filter((p) => sel.has(key(p)))
    .map((p) => ({ repo: p.repo, number: p.number }))

  const Select = ({ value, onChange, label, options }) => (
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="cursor-pointer appearance-none rounded-lg border border-zinc-800 bg-zinc-950 py-1 pl-2.5 pr-7 text-[11px] text-zinc-300 hover:border-zinc-700"
      >
        <option value="">{label}: all</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
      <ChevronDown size={11} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-zinc-600" />
    </div>
  )

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <Select value={author} onChange={setAuthor} label="Owner" options={opts.authors} />
        <Select value={repo} onChange={setRepo} label="Repo" options={opts.repos} />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="PR # · AB# ticket · title"
          className="w-44 rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1 text-[11px] text-zinc-200 placeholder:text-zinc-600 focus:border-[var(--accent)] focus:outline-none"
        />
        <button
          onClick={() => setSel(allVisible ? new Set() : new Set(filtered.map(key)))}
          className="press rounded-lg bg-zinc-800 px-2 py-1 text-[10px] text-zinc-400 hover:text-zinc-200"
        >
          {allVisible ? 'clear' : 'select all'} ({filtered.length})
        </button>
        {(author || repo || q) && (
          <button
            onClick={() => { setAuthor(''); setRepo(''); setQ('') }}
            title="Clear all filters"
            className="press flex items-center gap-1 rounded-lg bg-zinc-800 px-2 py-1 text-[10px] text-zinc-400 hover:text-zinc-200"
          >
            <RotateCcw size={10} /> reset
          </button>
        )}
        <RefreshButton busy={busy} onClick={() => load(true)} title="Refresh from GitHub" />
        {sel.size > 0 && (
          <div className="relative ml-auto">
            <button
              onClick={() => setMenuOpen((v) => !v)}
              disabled={running}
              className="press flex items-center gap-1.5 rounded-lg bg-[var(--accent-fill)] px-2.5 py-1 text-[11px] font-medium text-zinc-100 hover:brightness-110 disabled:opacity-40"
            >
              <Play size={11} /> Push {sel.size} to board <ChevronDown size={11} className="opacity-70" />
            </button>
            {menuOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
                <div className="absolute right-0 top-8 z-20 w-64 overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 shadow-xl">
                  {MODES.pr.profiles.map((p) => (
                    <button
                      key={p.id}
                      onClick={() => { setMenuOpen(false); onRun(p.id, null, selRefs()); setSel(new Set()) }}
                      className="flex w-full items-start gap-2.5 px-3 py-2 text-left hover:bg-zinc-800"
                    >
                      <p.icon size={13} className="mt-0.5 shrink-0 text-[var(--accent)]" />
                      <span className="min-w-0">
                        <span className="block text-[12px] font-medium text-zinc-200">{p.label}</span>
                        <span className="block text-[10px] text-zinc-500">{p.desc}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </div>
      {!prs && <div className="skeleton mt-2 h-24 w-full rounded-lg" />}
      {prs && (
        <div className="mt-2 max-h-[calc(100vh-330px)] space-y-0.5 overflow-y-auto pr-1">
          {filtered.length === 0 && <div className="py-3 text-center text-[12px] text-zinc-600">no matching PRs</div>}
          {filtered.map((p) => (
            <label
              key={key(p)}
              className={`flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1 text-[12px] hover:bg-zinc-800/50 ${
                sel.has(key(p)) ? 'bg-[var(--accent-fill)]/40' : ''
              }`}
            >
              <input
                type="checkbox"
                checked={sel.has(key(p))}
                onChange={() => toggle(p)}
                className="shrink-0 accent-[var(--accent)]"
              />
              <GitPullRequest size={11} className="shrink-0 text-purple-400" />
              <span className="shrink-0 tabular-nums text-zinc-600">{(p.repo || '').split('/').pop()}#{p.number}</span>
              <span className="min-w-0 flex-1 truncate text-zinc-200">{p.title}</span>
              {p.draft && <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-px text-[10px] text-zinc-500">draft</span>}
              {p.ab && <span className="shrink-0 rounded bg-sky-950 px-1.5 py-px text-[10px] text-sky-300">AB#{p.ab}</span>}
              <span className="w-24 shrink-0 truncate text-right text-[10px] text-zinc-500">{p.author}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

// Debt Swarm backlog: findings collected by pr_review runs, not yet picked up.
function DebtBacklog() {
  const [debt, setDebt] = useState(null)
  useEffect(() => {
    fetch('/api/swarm/debt').then((r) => r.json()).then((d) => setDebt(d.debt || {})).catch(() => setDebt({}))
  }, [])
  if (!debt) return <div className="skeleton h-20 w-full rounded-xl" />
  const repos = Object.entries(debt)
  if (repos.length === 0)
    return <Empty text="No unaddressed tech debt collected yet — run the PR Swarm's Review first; its tech-debt findings land here." />
  return (
    <div className="space-y-2">
      {repos.map(([repo, items]) => (
        <div key={repo} className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-2.5">
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold text-zinc-300">
            <Wrench size={12} className="text-amber-400" />
            {repo.split('/').pop()}
            <span className="text-zinc-600">{items.length} finding{items.length !== 1 ? 's' : ''}</span>
          </div>
          <ul className="space-y-1">
            {items.map((f, i) => (
              <li key={i} className="rounded-lg border border-amber-900/40 bg-amber-950/20 px-2.5 py-1.5 text-[12px]">
                <div className="font-medium text-zinc-200">{f.title}</div>
                <div className="text-[11px] text-zinc-500">{f.where} — {f.why}</div>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

const ACT_TONE = {
  swarm_verdict: 'text-zinc-400',
  swarm_run: 'text-emerald-300',
  swarm_worktree_created: 'text-violet-300',
  swarm_worker_done: 'text-violet-300',
  swarm_worktree_discarded: 'text-zinc-500',
  swarm_pr_open: 'text-emerald-300',
  swarm_ado_comment: 'text-sky-300',
  swarm_summary_post: 'text-sky-300',
  swarm_stopped: 'text-amber-300',
  github_comment: 'text-sky-300',
}

// Log sub-tab: every action this swarm took, newest first, from the audit trail.
function ReceiptsLog({ runFilter, refreshKey }) {
  const [rows, setRows] = useState(null)
  useEffect(() => {
    fetch(`/api/swarm/activity?limit=120&profiles=${runFilter}`)
      .then((r) => r.json())
      .then((d) => setRows(d.activity || []))
      .catch(() => setRows([]))
  }, [runFilter, refreshKey])
  if (!rows) return <div className="skeleton h-16 w-full rounded-xl" />
  if (rows.length === 0) return <Empty text="No receipts yet" />
  return (
    <div className="max-h-[calc(100vh-280px)] space-y-0.5 overflow-y-auto pr-1">
      {rows.map((a) => (
        <div key={a.id} className="flex items-baseline gap-2 rounded-lg px-2 py-1 text-[11px] hover:bg-zinc-800/40">
          <TimeAgo iso={a.ts} className="w-14 shrink-0 tabular-nums text-zinc-600" />
          <span className={`shrink-0 font-mono text-[10px] ${ACT_TONE[a.action] || 'text-zinc-500'}`}>
            {a.action.replace('swarm_', '')}
          </span>
          <span className="min-w-0 flex-1 truncate text-zinc-400" title={a.line}>{a.line}</span>
          {a.run_id && <span className="shrink-0 tabular-nums text-zinc-600">#{a.run_id}</span>}
        </div>
      ))}
    </div>
  )
}

// Everything on the board, serialized for debugging. Always current: run,
// control state, every unit with zone + verb + note + verdicts, worktrees.
function boardReportMd(mode, latest, worktrees, phases, control, runs) {
  const M = MODES[mode] || MODES.ado
  const r = latest?.run
  const L = [`# ${M.title} — board report`, `generated: ${new Date().toISOString()}`, '']
  if (!r) return L.concat('no runs yet').join('\n')
  L.push(`## Run #${r.id} · ${r.profile || 'board'} · ${r.status} · trigger: ${r.trigger}`,
         `started: ${r.started_at} · duration: ${r.duration_ms != null ? fmtMs(r.duration_ms) : 'live'}`,
         `totals: ${JSON.stringify(r.totals || {})}`, '')
  if (control && (control.paused || control.paused_targets?.length || control.stopped_targets?.length))
    L.push(`control: ${JSON.stringify(control)}`, '')
  const units = buildUnits(latest.jobs || [], worktrees, phases, r.id)
  const clarifyMap = new Map((r.totals?.receipts?.clarifications || []).map((c) => [`ado:${c.item}`, c.result]))
  units.forEach((u) => { u.clarifyResult = clarifyMap.get(u.key) })
  L.push(`## Units · ${units.length}`)
  for (const u of units) {
    const inPipe = mode === 'ado' && (u.wt || r.profile === 'ticket' || unitWorkable(u))
    const zone = inPipe ? `pipeline/${codeZoneFor(u)}` : zoneForUnit(u, phases, mode)
    L.push(`### ${u.ref} ${u.title}`, `zone: ${zone} · doing: ${unitAction(u, phases).verb}`)
    const note = noteForUnit(u, zone, phases)
    if (note) L.push(`note: ${note}`)
    u.jobs.forEach((j) => L.push(jobMd(j, phases)))
    if (u.wt)
      L.push(`worktree: ${u.wt.branch} — ${u.wt.status}${u.wt.pr_url ? ` — ${u.wt.pr_url}` : ''}${u.wt.error ? ` — ${u.wt.error}` : ''}`)
    L.push('')
  }
  if (r.summary) L.push('## Reviewer summary', r.summary, '')
  L.push('## Recent runs',
    ...(runs || []).slice(0, 8).map((x) => {
      const t = x.totals || {}
      return `- #${x.id} ${x.profile || 'board'} ${x.status} · pass ${t.pass ?? '-'} fail ${t.fail ?? '-'} blocked ${t.blocked ?? '-'} · $${t.cost_usd ?? '-'} · ${fmtMs(x.duration_ms)}`
    }))
  return L.join('\n')
}

export default function SwarmPanel({ mode = 'ado' }) {
  const M = MODES[mode] || MODES.ado
  const [sub, setSub] = useState(() => {
    try {
      const v = localStorage.getItem(`swarmtab2:${mode}`)
      return v && M.subtabs.includes(v) ? v : M.subtabs[0]
    } catch { return M.subtabs[0] }
  })
  const pickSub = (id) => { setSub(id); try { localStorage.setItem(`swarmtab2:${mode}`, id) } catch {} }

  const [latest, setLatest] = useState(null) // {run, jobs}
  const [runs, setRuns] = useState(null)
  const [worktrees, setWorktrees] = useState([])
  const [phases, setPhases] = useState({})
  const [log, setLog] = useState([])
  const [selectedKey, setSelectedKey] = useState(null) // board unit
  const [selected, setSelected] = useState(null) // receipts-matrix job
  const [openRun, setOpenRun] = useState(null)
  const [openSelected, setOpenSelected] = useState(null)
  const [busy, setBusy] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [control, setControl] = useState(null)
  const [boardRun, setBoardRun] = useState(() => {
    try { return localStorage.getItem(`swarmboard:${mode}`) || 'live' } catch { return 'live' }
  })
  const pickBoardRun = (v) => { setBoardRun(v); try { localStorage.setItem(`swarmboard:${mode}`, v) } catch {} }
  const [boardJobs, setBoardJobs] = useState(null)
  const [boardRunObj, setBoardRunObj] = useState(null)
  const [scale, setScale] = useState(() => {
    try { return new Set((localStorage.getItem('swarmscale') || '').split(',').filter(Boolean)) } catch { return new Set() }
  })
  const pickScale = (id) => {
    setScale((cur) => {
      const next = new Set(cur)
      next.has(id) ? next.delete(id) : next.add(id)
      try { localStorage.setItem('swarmscale', [...next].join(',')) } catch {}
      return next
    })
  }
  const scaleTotal = SCALES.filter((x) => scale.has(x.id)).reduce((n, x) => n + x.n, 0)
  const toast = useToast()

  // lane gates: loop counts shared by all swarm tabs (they tune the same knobs)
  const [gateValues, setGateValues] = useState(null)
  useEffect(() => {
    fetch('/api/swarm/gates').then((x) => x.json())
      .then((g) => setGateValues(g.gates)).catch(() => {})
  }, [])
  const setGate = async (key, loops) => {
    setGateValues((cur) => ({ ...cur, [key]: loops })) // optimistic
    try {
      const r = await fetch('/api/swarm/gates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gate: key, loops }),
      }).then((x) => x.json())
      setGateValues(r.gates)
      toast(`Gate set: ${key.replace(/_/g, ' ')} ×${loops}`, 'success')
    } catch (e) {
      toast(`Could not set gate: ${String(e).slice(0, 120)}`, 'error')
    }
  }

  const load = useCallback(async () => {
    try {
      const [l, r, w] = await Promise.all([
        fetch(`/api/swarm/latest?profiles=${M.runFilter}`).then((x) => x.json()),
        fetch(`/api/swarm/runs?limit=20&profiles=${M.runFilter}`).then((x) => x.json()),
        fetch('/api/worktrees?limit=30').then((x) => x.json()).catch(() => ({ worktrees: [] })),
      ])
      setLatest(l)
      setControl(l.control || null)
      setRuns(r.runs || [])
      setWorktrees(w.worktrees || [])
      if (boardRun === 'live') {
        // persistent board: last 10 runs merged, tickets never vanish
        const b = await fetch(`/api/swarm/board?profiles=${M.runFilter}&span=10`)
          .then((x) => x.json()).catch(() => null)
        setBoardJobs(b?.jobs || l.jobs || [])
        setBoardRunObj(l.run)
      } else {
        const b = await fetch(`/api/swarm/runs/${boardRun}`).then((x) => x.json()).catch(() => null)
        setBoardJobs(b?.jobs || [])
        setBoardRunObj(b?.run || null)
      }
    } catch {}
  }, [M.runFilter, boardRun])

  useEffect(() => { load() }, [load])

  const running = latest?.run?.status === 'running'
  const runId = latest?.run?.id

  useEffect(() => { setPhases({}); setLog([]); setSelectedKey(null) }, [runId])

  useEffect(() => {
    let es
    try {
      es = new EventSource('/api/stream')
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.type === 'swarm_job_update') {
            setLatest((cur) => {
              if (!cur?.run || d.run_id !== cur.run.id) return cur
              return { ...cur, jobs: cur.jobs.map((j) => (j.id === d.job.id ? d.job : j)) }
            })
            setBoardJobs((cur) => cur ? cur.map((j) => (j.id === d.job.id ? d.job : j)) : cur)
          } else if (d.type === 'swarm_job_phase') {
            setPhases((p) => ({ ...p, [d.job_id]: d }))
            if (d.note) {
              setLog((l) => [...l.slice(-6),
                { ts: Date.now() + Math.random(), who: d.ref ? `${d.ref}·${d.runbook || ''}` : `job ${d.job_id}`, note: d.note }])
            }
          } else if (d.type === 'swarm_control') {
            setControl({ paused: d.paused, paused_targets: d.paused_targets, stopped_targets: d.stopped_targets })
          } else if (d.type === 'swarm_run_update' || d.type === 'worktree_update') {
            load()
          }
        } catch {}
      }
    } catch {}
    const t = setInterval(load, running ? 5000 : 30000)
    return () => { es?.close(); clearInterval(t) }
  }, [load, running])

  const [, setTick] = useState(0)
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [running])

  const runSwarm = async (profile, itemIds, prRefs) => {
    setBusy(true)
    setMenuOpen(false)
    try {
      const body = { profile }
      if (itemIds?.length) body.item_ids = itemIds
      if (prRefs?.length) body.prs = prRefs
      if (scaleTotal) body.concurrency = scaleTotal
      const r = await post('/api/swarm/run', body)
      toast(`${M.profiles.find((p) => p.id === profile)?.label || profile} — run #${r.run_id} started`, 'success')
      pickSub('board') // the game starts: show it
      pickBoardRun('live')
      await load()
    } catch (e) {
      toast(/409/.test(String(e)) ? 'A swarm run is already active' : `Swarm failed to start: ${e}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const retryUnit = (u) => {
    const prof = latest?.run?.profile || M.profiles[0].id
    if (u.kind === 'pr' && u.jobs[0]?.item_project) {
      runSwarm(prof, null, [{ repo: u.jobs[0].item_project, number: u.itemId }])
    } else if (u.itemId) {
      runSwarm(prof, [u.itemId])
    }
  }

  const sendControl = async (action, target) => {
    if (!latest?.run) return
    try {
      const st = await post(`/api/swarm/runs/${latest.run.id}/control`, { action, target })
      setControl(st)
    } catch (e) {
      toast(`${action} failed: ${String(e).slice(0, 100)}`, 'error')
    }
  }

  const stopSwarm = async () => {
    if (!latest?.run) return
    try {
      await post(`/api/swarm/runs/${latest.run.id}/stop`)
      toast(`Stopping run #${latest.run.id}…`, 'info')
    } catch (e) {
      toast(`Could not stop: ${String(e).slice(0, 120)}`, 'error')
    }
  }

  const history = (runs || []).filter((r) => r.id !== latest?.run?.id)
  const liveJobs = latest?.jobs || []

  const toggleHistory = async (r) => {
    if (openRun?.run?.id === r.id) { setOpenRun(null); return }
    if (latest?.run?.id === r.id) { setOpenRun({ ...latest }); return }
    try {
      setOpenRun(await fetch(`/api/swarm/runs/${r.id}`).then((x) => x.json()))
      setOpenSelected(null)
    } catch {}
  }

  return (
    <Panel
      title={M.title}
      badge={latest?.run ? `run #${latest.run.id}` : null}
      fill={sub === 'board'}
      actions={
        <div className="flex flex-1 items-center gap-2 pl-3">
          <div className="flex w-fit gap-0.5 rounded-lg bg-zinc-950 p-0.5">
            {M.subtabs.map((id) => {
              const T = SUBTAB_META[id]
              const active = sub === id
              return (
                <button
                  key={id}
                  onClick={() => pickSub(id)}
                  className={`press flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors ${
                    active ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300'
                  }`}
                >
                  <T.icon size={12} className={active ? 'text-[var(--accent)]' : ''} />
                  {T.label}
                </button>
              )
            })}
          </div>
          {sub === 'board' && (
            <div className="relative">
              <select
                value={boardRun}
                onChange={(e) => pickBoardRun(e.target.value)}
                title="Which board to show: the persistent live board or one specific run"
                className="max-w-[210px] cursor-pointer appearance-none rounded-lg border border-zinc-800 bg-zinc-950 py-1 pl-2.5 pr-7 text-[11px] text-zinc-300 hover:border-zinc-700"
              >
                <option value="live">Live — persistent (last 10 runs)</option>
                {(runs || []).map((r) => (
                  <option key={r.id} value={String(r.id)}>
                    run #{r.id} · {r.profile || 'board'} · {r.status}
                  </option>
                ))}
              </select>
              <ChevronDown size={11} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-zinc-600" />
            </div>
          )}
          <div className="ml-auto" />
          {latest?.run && (
            <span className="hidden items-center gap-1.5 text-[11px] text-zinc-600 md:flex">
              <Bot size={12} />
              {M.profiles.find((p) => p.id === latest.run.profile)?.label || latest.run.profile}
              {' · '}
              <TimeAgo iso={latest.run.started_at} />
            </span>
          )}
          <div className="flex items-center gap-0.5 rounded-lg bg-zinc-950 p-0.5"
               title="Swarm scale: selections stack (A+B+C+D = 40 parallel workers)">
            {SCALES.map((x) => (
              <button
                key={x.id}
                onClick={() => pickScale(x.id)}
                title={`${x.desc} — combine freely`}
                className={`press h-6 w-6 rounded-md text-[11px] font-bold transition-colors ${
                  scale.has(x.id)
                    ? 'bg-[var(--accent-fill)] text-[var(--accent)] ring-1 ring-[var(--accent-strong)]'
                    : 'text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300'
                }`}
              >
                {x.id}
              </button>
            ))}
            {scaleTotal > 0 && (
              <span className="px-1.5 text-[10px] font-semibold tabular-nums text-[var(--accent)]">{scaleTotal}×</span>
            )}
          </div>
          <button
            onClick={() => {
              navigator.clipboard.writeText(boardReportMd(mode, latest, worktrees, phases, control, runs))
                .then(() => toast('Board report copied — paste it anywhere for debugging', 'success'))
                .catch(() => toast('Copy failed', 'error'))
            }}
            title="Copy the current board state as a debugging report"
            className="press grid h-7 w-7 place-items-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
          >
            <ClipboardList size={14} />
          </button>
          <RefreshButton busy={false} onClick={load} title="Refresh" />
          {running && (
            <>
              <button
                onClick={() => sendControl(control?.paused ? 'resume' : 'pause')}
                title={control?.paused ? 'Resume the run' : 'Pause the run (workers finish their current step)'}
                className={`press flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] font-medium ${
                  control?.paused
                    ? 'bg-emerald-800/70 text-emerald-200 hover:bg-emerald-700/70'
                    : 'bg-amber-900/60 text-amber-200 hover:bg-amber-800/70'
                }`}
              >
                {control?.paused ? <Play size={11} /> : <Pause size={11} />}
                {control?.paused ? 'Resume' : 'Pause'}
              </button>
              <button
                onClick={stopSwarm}
                title="Stop this run — queued and running workers are cancelled"
                className="press flex items-center gap-1.5 rounded-lg bg-red-900/60 px-3 py-1.5 text-[12px] font-medium text-red-200 hover:bg-red-800/70"
              >
                <Square size={11} /> Stop
              </button>
            </>
          )}
          <div className="relative">
            <button
              onClick={() => setMenuOpen((v) => !v)}
              disabled={busy || running}
              className="press flex items-center gap-1.5 rounded-lg bg-[var(--accent-fill)] px-3 py-1.5 text-[12px] font-medium text-zinc-100 hover:brightness-110 disabled:opacity-40"
            >
              {running ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
              {running ? 'Running' : 'Run'}
              {!running && <ChevronDown size={12} className="opacity-70" />}
            </button>
            {menuOpen && !running && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
                <div className="absolute right-0 top-9 z-20 w-72 overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 shadow-xl">
                  {M.profiles.map((p) => (
                    <button
                      key={p.id}
                      onClick={() => runSwarm(p.id)}
                      className="flex w-full items-start gap-2.5 px-3 py-2 text-left hover:bg-zinc-800"
                    >
                      <p.icon size={14} className="mt-0.5 shrink-0 text-[var(--accent)]" />
                      <span className="min-w-0">
                        <span className="block text-[12px] font-medium text-zinc-200">{p.label}</span>
                        <span className="block text-[10px] text-zinc-500">{p.desc}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>
      }
    >
      {sub === 'board' && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1">
            {!latest ? (
              <div className="skeleton h-full w-full rounded-xl" />
            ) : (
              <SwarmBoard
                mode={mode}
                run={boardRunObj || latest.run}
                jobs={boardJobs ?? liveJobs}
                worktrees={worktrees}
                phases={phases}
                log={log}
                selectedKey={selectedKey}
                onSelect={setSelectedKey}
                control={control}
                onControl={sendControl}
                onRetry={retryUnit}
                gateValues={gateValues}
                onSetGate={setGate}
              />
            )}
          </div>
        </div>
      )}

      {sub === 'tickets' && <TargetPicker running={running} onRun={runSwarm} mode={mode} />}
      {sub === 'prs' && <PrPicker running={running} onRun={runSwarm} />}
      {sub === 'backlog' && <DebtBacklog />}

      {sub === 'receipts' && (
        latest?.run
          ? <RunBlock run={latest.run} jobs={liveJobs} selected={selected} onSelect={setSelected} />
          : <Empty text="No run yet" />
      )}

      {sub === 'history' && (
        <div className="space-y-1">
          {runs && history.length === 0 && <Empty text="No previous runs" />}
          {history.map((r) => {
            const open = openRun?.run?.id === r.id
            const t = r.totals || {}
            return (
              <div key={r.id} className="rounded-xl border border-transparent hover:border-zinc-800">
                <button
                  onClick={() => toggleHistory(r)}
                  className="row-hover flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left text-[12px]"
                >
                  {open ? <ChevronDown size={13} className="text-zinc-500" /> : <ChevronRight size={13} className="text-zinc-600" />}
                  <History size={13} className="text-zinc-600" />
                  <span className="font-medium text-zinc-300">run #{r.id}</span>
                  <span className="text-zinc-600">{r.profile || 'board'}</span>
                  <TimeAgo iso={r.started_at} className="text-zinc-500" />
                  <span className="ml-auto flex items-center gap-1.5">
                    {r.status === 'error' && <StatusChip status="error" />}
                    {r.status === 'stopped' && (
                      <span className="rounded-md border border-amber-800 bg-amber-950 px-1.5 py-0.5 text-[10px] text-amber-300">stopped</span>
                    )}
                    {['pass', 'fail', 'blocked'].map((s) =>
                      t[s] > 0 ? (
                        <span key={s} className={`rounded-md border px-1.5 py-0.5 text-[10px] ${meta(s).chip}`}>
                          {t[s]}
                        </span>
                      ) : null,
                    )}
                    <span className="tabular-nums text-[10px] text-zinc-500">
                      {t.cost_usd != null ? `$${t.cost_usd.toFixed(2)}` : ''}
                    </span>
                    <span className="tabular-nums text-[10px] text-zinc-600">{fmtMs(r.duration_ms)}</span>
                  </span>
                </button>
                {open && openRun && (
                  <div className="border-t border-zinc-800/60 p-2.5">
                    <RunBlock run={openRun.run} jobs={openRun.jobs} selected={openSelected} onSelect={setOpenSelected} />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {sub === 'log' && (
        <ReceiptsLog runFilter={M.runFilter} refreshKey={latest?.run?.status + ':' + (latest?.run?.id || 0)} />
      )}
    </Panel>
  )
}
