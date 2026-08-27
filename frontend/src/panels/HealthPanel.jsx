import React, { useEffect, useState } from 'react'
import {
  HeartPulse, Database, RefreshCw, Play, AlertTriangle, CheckCircle2,
  TrendingUp, Zap, Bot, GitPullRequest, RotateCcw, Link2, Gauge,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'
import { useToast, TimeAgo, fmtMs, SkeletonRows } from '../ui.jsx'

// Jobs that can be triggered from the board map to their /api/sweep names.
const RUN_NOW = {
  teams_sweep: 'teams', email_sweep: 'email', open_loops: 'open_loops',
  transcripts: 'meeting_summaries', pr_review: 'pr_review', pr_digest: 'pr_digest',
  knowledge_sync: 'knowledge', pr_status: 'pr_status', pr_readiness: 'pr_readiness',
  morning_brief: 'morning_brief', evening_shutdown: 'evening_shutdown',
  weekly_status: 'weekly_status', chieff_daily_update: 'chieff_daily_update',
  hygiene: 'hygiene', pre_meeting: 'pre_meeting', post_meeting: 'post_meeting',
}

const JOB_LABEL = {
  teams_sweep: 'Teams sweep', email_sweep: 'Email sweep', open_loops: 'Open loops',
  transcripts: 'Meeting summaries', pr_review: 'PR review', pr_digest: 'GitHub digest',
  knowledge_sync: 'Knowledge sync', pr_status: 'PR status', pr_readiness: 'PR readiness',
  morning_brief: 'Morning brief', evening_shutdown: 'Evening shutdown',
  ensure_daily_note: 'Daily note check', weekly_status: 'Weekly status',
  chieff_daily_update: 'Chieff daily update', backfill_week: 'Backfill week',
  hygiene: 'Hygiene', pre_meeting: 'Pre-meeting brief', post_meeting: 'Post-meeting catch-up',
}

function healthTone(j) {
  if (j.error_streak >= 3) return 'bad'
  if (j.last_ok === 0) return 'warn'
  return 'ok'
}

const DOT = {
  ok: 'bg-emerald-400',
  warn: 'bg-amber-400',
  bad: 'bg-red-400',
  off: 'bg-zinc-600',
}

function JobCard({ j, onRan }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const tone = healthTone(j)
  const sweep = RUN_NOW[j.job]

  const runNow = async (e) => {
    e.stopPropagation()
    setBusy(true)
    try {
      await post(`/api/sweep/${sweep}`)
      toast(`${JOB_LABEL[j.job] || j.job} ran`, 'success')
      onRan()
    } catch (err) {
      toast(`${JOB_LABEL[j.job] || j.job} failed: ${String(err).slice(0, 140)}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      onClick={() => setOpen((v) => !v)}
      className={`lift cursor-pointer rounded-xl border p-3 ${
        tone === 'bad' ? 'border-red-800 bg-red-950/30' : 'border-zinc-800 bg-zinc-900'
      }`}
    >
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[tone]} ${j.running ? 'animate-pulse' : ''}`} />
        <span className="truncate text-[13px] font-medium text-zinc-100">
          {JOB_LABEL[j.job] || j.job}
        </span>
        {j.error_streak > 0 && (
          <span className="rounded bg-red-950 px-1.5 text-[10px] font-semibold text-red-300 ring-1 ring-red-900">
            {j.error_streak}× failing
          </span>
        )}
        <span className="ml-auto flex items-center gap-2">
          {sweep && (
            <button
              onClick={runNow}
              disabled={busy}
              title="Run now"
              className="press grid h-6 w-6 place-items-center rounded-md bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-100 disabled:opacity-40"
            >
              {busy ? <RefreshCw size={12} className="animate-spin" /> : <Play size={12} />}
            </button>
          )}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-zinc-500">
        <span>
          last <TimeAgo iso={j.last_run} className="text-zinc-400" />
          {j.last_trigger === 'manual' && ' (manual)'}
        </span>
        <span>took {fmtMs(j.last_duration_ms)}</span>
        {j.next_run && (
          <span>
            next <TimeAgo iso={j.next_run} className="text-zinc-400" />
          </span>
        )}
        <span className="tabular-nums">
          24h: {j.runs_24h - j.failures_24h}
          <CheckCircle2 size={10} className="mb-px ml-0.5 inline text-emerald-500" />
          {j.failures_24h > 0 && (
            <>
              {' '}{j.failures_24h}
              <AlertTriangle size={10} className="mb-px ml-0.5 inline text-red-400" />
            </>
          )}
        </span>
      </div>
      {j.last_ok === 0 && j.last_error && (
        <div className="mt-1.5 break-words rounded-md bg-red-950/40 px-2 py-1 font-mono text-[10px] text-red-300">
          {j.last_error}
        </div>
      )}
      {open && j.last_summary && (
        <pre className="animate-fade-in mt-1.5 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-md bg-zinc-950 px-2 py-1.5 font-mono text-[10px] text-zinc-400">
          {j.last_summary}
        </pre>
      )}
    </div>
  )
}

function ConnectorCard({ name, s, onChanged }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)

  const reconnect = async () => {
    setBusy(true)
    try {
      const r = await post(`/api/connectors/${name}/reconnect`)
      toast(r.ok ? `${name} reconnected` : `${name}: ${r.status}`, r.ok ? 'success' : 'error')
      onChanged()
    } catch (e) {
      toast(`${name} reconnect failed: ${String(e).slice(0, 120)}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const dotColor = s.auth_warning ? 'bg-amber-400' : s.connected ? 'bg-emerald-400' : 'bg-red-400'

  return (
    <div className={`rounded-xl border p-3 ${s.auth_warning ? 'border-amber-800 bg-amber-950/20' : 'border-zinc-800 bg-zinc-900'}`}>
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${dotColor}`} />
        <span className="text-[13px] font-medium text-zinc-100">{name}</span>
        {s.auth_warning && (
          <span className="rounded bg-amber-950 px-1.5 text-[10px] font-semibold text-amber-300 ring-1 ring-amber-800">
            token expired?
          </span>
        )}
        {s.latency_ms != null && !s.auth_warning && (
          <span className="rounded bg-zinc-800 px-1.5 text-[10px] tabular-nums text-zinc-400">
            {s.latency_ms}ms
          </span>
        )}
        {s.reconnectable && (
          <button
            onClick={reconnect}
            disabled={busy}
            title="Restart this MCP session"
            className="press ml-auto grid h-6 w-6 place-items-center rounded-md bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-100 disabled:opacity-40"
          >
            <RefreshCw size={12} className={busy ? 'animate-spin' : ''} />
          </button>
        )}
      </div>
      <div className="mt-1 truncate text-[11px] text-zinc-500" title={s.detail}>
        {s.detail}
      </div>
      {(s.calls ?? 0) > 0 && (
        <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] tabular-nums text-zinc-500">
          <span>{s.calls} calls</span>
          <span className={s.errors ? 'text-amber-300' : ''}>{s.errors} errors</span>
          {s.avg_latency_ms != null && <span>avg {fmtMs(s.avg_latency_ms)}</span>}
        </div>
      )}
      {s.last_error && (
        <div
          className="mt-1 truncate font-mono text-[10px] text-red-300/80"
          title={s.last_error}
        >
          {s.last_error}
        </div>
      )}
    </div>
  )
}

export default function HealthPanel({ refreshTick }) {
  const [health, setHealth] = useState(null)
  const [connectors, setConnectors] = useState(null)
  const [quality, setQuality] = useState(null)
  const [metrics, setMetrics] = useState(null)

  const load = () => {
    fetch('/api/health').then((r) => r.json()).then(setHealth).catch(() => {})
    fetch('/api/connectors').then((r) => r.json()).then(setConnectors).catch(() => {})
    fetch('/api/quality').then((r) => r.json()).then(setQuality).catch(() => {})
    fetch('/api/metrics').then((r) => r.json()).then(setMetrics).catch(() => {})
  }
  useEffect(() => {
    load()
  }, [refreshTick])

  const agent = health?.agent
  const jobs = health?.jobs || []
  const failing = jobs.filter((j) => healthTone(j) !== 'ok').length

  return (
    <div className="space-y-4">
      <Panel
        title="Connectors"
        actions={
          <span className="text-[11px] text-zinc-500">
            live MCP sessions · click ↻ to restart one
          </span>
        }
      >
        {!connectors ? (
          <SkeletonRows n={2} className="h-16" />
        ) : (
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-3">
            {Object.entries(connectors).map(([name, s]) => (
              <ConnectorCard key={name} name={name} s={s} onChanged={load} />
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title="Agent runs (24h)"
        badge={agent ? `${agent.calls_24h} calls` : null}
      >
        {!agent ? (
          <SkeletonRows n={1} className="h-14" />
        ) : (
          <div className="flex flex-wrap items-start gap-x-8 gap-y-2">
            <Stat label="calls" value={agent.calls_24h} />
            <Stat
              label="failures"
              value={agent.failures_24h}
              tone={agent.failures_24h > 0 ? 'text-red-300' : 'text-emerald-300'}
            />
            <Stat label="cost" value={`$${(agent.cost_24h ?? 0).toFixed(2)}`} />
            <Stat label="avg run" value={fmtMs(agent.avg_duration_ms)} />
            <Stat
              label="last call"
              value={agent.last_call ? <TimeAgo iso={agent.last_call} /> : '—'}
            />
            <div className="min-w-[220px] flex-1">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-zinc-600">by purpose</div>
              <div className="space-y-0.5">
                {(agent.by_label || []).map((b) => (
                  <div key={b.label} className="flex items-center gap-2 text-[12px]">
                    <span className="w-32 truncate text-zinc-300">{b.label}</span>
                    <span className="tabular-nums text-zinc-500">{b.calls}×</span>
                    {b.failures > 0 && (
                      <span className="tabular-nums text-red-300">{b.failures} failed</span>
                    )}
                    <span className="ml-auto tabular-nums text-zinc-400">${b.cost.toFixed(3)}</span>
                  </div>
                ))}
              </div>
            </div>
            {agent.last_error && (
              <div className="w-full rounded-md bg-red-950/40 px-2 py-1.5 text-[11px] text-red-300">
                <AlertTriangle size={11} className="mb-px mr-1 inline" />
                last failure ({agent.last_error.label}, <TimeAgo iso={agent.last_error.ts} />):{' '}
                <span className="font-mono">{agent.last_error.error}</span>
              </div>
            )}
          </div>
        )}
      </Panel>

      <Panel
        title="Jobs"
        badge={jobs.length}
        actions={
          failing > 0 ? (
            <span className="flex items-center gap-1 rounded-full bg-red-950 px-2 py-0.5 text-[11px] font-medium text-red-300 ring-1 ring-red-900">
              <AlertTriangle size={11} /> {failing} unhealthy
            </span>
          ) : (
            <span className="flex items-center gap-1 rounded-full bg-green-950 px-2 py-0.5 text-[11px] font-medium text-green-300">
              <HeartPulse size={11} /> all healthy
            </span>
          )
        }
      >
        {!health ? (
          <SkeletonRows n={5} className="h-20" />
        ) : (
          <>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
              {jobs.map((j) => (
                <JobCard key={j.job} j={j} onRan={load} />
              ))}
            </div>
            {(health.scheduled_but_never_ran || []).length > 0 && (
              <div className="mt-2 text-[11px] text-zinc-500">
                scheduled but no runs recorded yet: {health.scheduled_but_never_ran.join(', ')}
              </div>
            )}
          </>
        )}
      </Panel>

      {health?.db && (
        <Panel title="Store">
          <div className="flex flex-wrap gap-x-8 gap-y-2">
            <Stat label="db size" value={`${(health.db.size_bytes / 1048576).toFixed(1)} MB`} icon={Database} />
            <Stat label="items" value={health.db.items} />
            <Stat label="audit rows" value={health.db.audit_rows} />
            <Stat label="pending drafts" value={health.db.pending_drafts} />
            <Stat
              label="errors (24h)"
              value={health.audit_errors_24h}
              tone={health.audit_errors_24h > 0 ? 'text-amber-300' : 'text-emerald-300'}
            />
          </div>
        </Panel>
      )}

      {metrics && (
        <Panel
          title="Improvement scorecard"
          actions={
            <span className="flex items-center gap-1 text-[11px] text-zinc-500">
              <TrendingUp size={11} /> session wins + cumulative signal
            </span>
          }
        >
          <div className="mb-4 space-y-2">
            <div className="mb-2 text-[10px] uppercase tracking-wide text-zinc-600">Session improvements</div>
            {[
              { label: 'DB commits/sweep', before: 'N commits (1 per write)', after: '1 commit via transaction()', badge: 'Nx→1x', color: 'emerald' },
              { label: 'Classify sender lookup', before: 'N queries (1 per item)', after: '1 batch query + window fn', badge: 'N→1', color: 'emerald' },
              { label: 'PR chieff_round', before: 'N DB queries per sweep', after: '1 batch query', badge: 'N→1', color: 'emerald' },
              { label: 'PR review statuses', before: '2N queries per sweep', after: '2 queries batch', badge: 'N→1', color: 'emerald' },
              { label: 'Open loops close', before: 'N commits (1 per loop)', after: '1 transaction for all closes', badge: 'Nx→1x', color: 'emerald' },
              { label: '/api/metrics cold path', before: '14 DB queries', after: '4 queries (11 COUNTs → 1)', badge: '14→4', color: 'emerald' },
              { label: '/api/health stats', before: '4 separate COUNT queries', after: '1 scalar subquery', badge: '4→1', color: 'emerald' },
              { label: 'SQLite indexes', before: 'drafts/audit_ts/type unindexed', after: '5 new indexes (ts, status, type…)', badge: '+5 idx', color: 'emerald' },
              { label: 'PR digest fetches', before: 'sequential gh search', after: 'concurrent gh search', badge: 'parallel', color: 'sky' },
              { label: 'Weekly status gather', before: 'sequential PR+Conf+vault', after: 'concurrent asyncio.gather', badge: 'parallel', color: 'sky' },
              { label: 'Meeting summaries', before: 'sequential LLM calls', after: 'material + LLM concurrent', badge: 'parallel', color: 'sky' },
              { label: 'Draft dismiss', before: 'approve or reject only', after: '+ dismiss (d key) for noise', badge: 'new action', color: 'sky' },
              { label: 'Hygiene sweep', before: 'no manual trigger', after: 'runnable from Health panel', badge: 'new action', color: 'sky' },
              { label: 'Daily update counts', before: '3 separate COUNT queries', after: '1 scalar subquery', badge: '3→1', color: 'emerald' },
              { label: 'audit_log(action,ts)', before: 'no composite index', after: 'covering idx for rate-limit check', badge: '+idx', color: 'emerald' },
              { label: 'Settings reads', before: 'DB query per send gate', after: '2s TTL cache, write-invalidated', badge: 'cached', color: 'emerald' },
              { label: '/api/pr-approvals', before: 'N comment queries (1/PR)', after: '1 batch query, grouped in Python', badge: 'N→1', color: 'emerald' },
              { label: 'Bulk status / wake', before: 'N UPDATE calls in a loop', after: '1 UPDATE WHERE id IN (...)', badge: 'Nx→1x', color: 'emerald' },
              { label: 'upsert_item hot path', before: 'SELECT + INSERT (2 trips)', after: 'INSERT OR IGNORE (1 trip for new)', badge: '2→1', color: 'emerald' },
              { label: 'PR status close loop', before: 'N update_item() per closed PR', after: '1 batch UPDATE WHERE id IN (...)', badge: 'Nx→1x', color: 'emerald' },
              { label: 'review_recent done check', before: 'N SELECT status per existing PR', after: '1 batch SELECT WHERE external_id IN', badge: 'N→1', color: 'emerald' },
              { label: 'PR readiness nudge', before: 'N json_extract queries (1/PR)', after: '1 batch query for all attention PRs', badge: 'N→1', color: 'emerald' },
              { label: '/api/quality scans', before: '3 audit_log scans per request', after: '1 SUM(CASE WHEN) + 60s TTL cache', badge: '3→1+cache', color: 'emerald' },
              { label: '/api/health scan', before: '2000 job_run rows every event', after: '15s TTL cache, single scan amortized', badge: 'cached', color: 'emerald' },
              { label: 'Snooze index', before: 'full items scan for snoozed count', after: 'partial index WHERE snoozed IS NOT NULL', badge: '+idx', color: 'emerald' },
              { label: 'Open loops close', before: 'N UPDATE + N audit per closed loop', after: '1 batch UPDATE + N audits in 1 tx', badge: 'Nx→1x', color: 'emerald' },
              { label: 'Transcripts done check', before: 'N SELECT status per meeting', after: '1 batch pre-filter before upsert', badge: 'N→1', color: 'emerald' },
              { label: 'Sweep result flash', before: 'silent board refresh on sweep end', after: 'status bar shows "N new · N drafted" 8s', badge: 'new UX', color: 'sky' },
              { label: 'Email fast-path batch', before: 'N update_item() per noise email', after: '1 batch UPDATE + N audits in 1 tx', badge: 'Nx→1x', color: 'emerald' },
              { label: 'PR upsert-or-update', before: 'upsert_item() + update_item() per PR', after: '1 ON CONFLICT DO UPDATE per PR', badge: '2→1', color: 'emerald' },
              { label: '/api/repos cache', before: '`gh repo list` subprocess every tab visit', after: '5 min TTL cache, no repeat subprocess', badge: 'cached', color: 'emerald' },
              { label: 'Sweep toast coverage', before: '6 sweep result shapes shown', after: '12 shapes: tracked, handled, posted, checked, synced…', badge: 'new UX', color: 'sky' },
              { label: 'Pre-meeting brief', before: 'no prep context before meetings', after: 'context brief 30 min before each meeting', badge: 'new feat', color: 'sky' },
              { label: 'Meeting action loops', before: 'action items buried in daily note', after: 'auto-extract the owner\'s actions → open loops on board', badge: 'new feat', color: 'sky' },
              { label: 'Calendar attendees', before: 'attendees omitted from event fetch', after: 'attendees included in selectFields; pre-meeting brief works', badge: 'fix', color: 'emerald' },
              { label: 'Board scalar queries', before: 'weekly_draft + snoozed + pr_readiness (3 queries)', after: '1 combined subquery SELECT per board refresh', badge: '3→1', color: 'emerald' },
              { label: 'Meeting attendees UI', before: 'no attendee visibility in meetings tab', after: 'attendee names shown inline on each meeting card', badge: 'new UX', color: 'sky' },
              { label: 'Pre-meeting in palette', before: 'pre-meeting sweep not in ⌘K palette', after: 'Run pre-meeting brief check available in ⌘K', badge: 'new UX', color: 'sky' },
              { label: '/api/meetings parallel', before: '3 sequential awaits (chats + 2 days)', after: '1 asyncio.gather() for all 3 concurrently', badge: '3→1', color: 'sky' },
              { label: 'Open loops batch tx', before: 'N individual DB commits per loop upsert', after: '1 transaction wraps all loop upserts per sweep', badge: 'Nx→1x', color: 'emerald' },
              { label: 'type_status index', before: 'type-only index for open_loop queries', after: 'composite (type, status) covering index', badge: '+idx', color: 'emerald' },
              { label: 'Post-meeting catch-up', before: 'no "what did I miss" after meetings', after: 'auto-digest of Teams activity during meeting window', badge: 'new feat', color: 'sky' },
              { label: 'Today quick-stats bar', before: 'no at-a-glance board summary in Today tab', after: 'compact pings · drafts · loops row at top of Today', badge: 'new UX', color: 'sky' },
              { label: 'Meeting banner 15→20 min', before: 'banner shows at 15 min before meeting', after: 'aligns to 20 min, matching pre_meeting brief window', badge: 'improved', color: 'sky' },
              { label: '⌘K post_meeting + digest', before: 'post_meeting & github_digest not in palette', after: 'both sweeps runnable from ⌘K command palette', badge: 'new UX', color: 'sky' },
              { label: 'Self-chat resolve', before: 'N sequential get_chat_members() per 1:1 chat', after: 'asyncio.gather() fetches all 1:1 member lists concurrently', badge: 'parallel', color: 'sky' },
              { label: 'Calendar cache lock', before: 'no lock; concurrent sweeps re-fetch same day', after: 'per-day asyncio.Lock serializes concurrent fetches', badge: 'fix', color: 'emerald' },
              { label: 'Board settings read', before: '3 individual get_setting() calls per board load', after: '1 batch get_settings_multi() query + cache warm', badge: '3→1', color: 'emerald' },
              { label: 'source_urgent index', before: 'pings query full-scan with source filter', after: 'covering (source, urgent DESC, received_at DESC) index', badge: '+idx', color: 'emerald' },
              { label: '@Chieff sweep cmds', before: 'only 7 sweeps runnable via @chieff', after: 'pre_meeting, post_meeting, knowledge added to @chieff', badge: 'extended', color: 'sky' },
              { label: 'SQLite mmap + temp', before: 'file I/O reads; temp tables on disk', after: 'mmap_size=128MB + temp_store=MEMORY pragmas', badge: 'perf', color: 'emerald' },
              { label: 'Top 3 done banner', before: 'no feedback when all Top 3 are checked', after: '"All done — nice work." banner on 100% completion', badge: 'new UX', color: 'sky' },
              { label: 'PR readiness Run now', before: 'readiness sweep only from Health or ⌘K', after: '"Run check" button inline in PR readiness panel', badge: 'new UX', color: 'sky' },
              { label: 'board.audit removed', before: '30 audit rows fetched every board refresh', after: 'removed; ActivityPanel fetches its own audit independently', badge: 'N→0', color: 'emerald' },
              { label: '/api/audit incremental', before: 'ActivityPanel fetches full 150 rows every tick', after: 'after_id param → only new rows since last seen ID', badge: 'N→delta', color: 'emerald' },
              { label: 'idx_audit_item_action', before: 'chain-lookup uses audit_item scan', after: 'composite (item_id, action, id DESC) index for chain queries', badge: '+idx', color: 'emerald' },
              { label: 'Teams parallel gather', before: 'chieff handle + triage run sequentially (active-chats branch)', after: 'asyncio.gather() for both in all code paths', badge: 'parallel', color: 'sky' },
              { label: 'Pings text search', before: 'tier-only filter in Pings panel', after: 'search box filters by sender, content, subject', badge: 'new UX', color: 'sky' },
              { label: 'Browser tab badge', before: 'no unread count in document title', after: 'title shows "(N pings · M drafts) Chief" when active', badge: 'new UX', color: 'sky' },
              { label: 'Warm presence cache', before: 'presence only warmed on demand', after: 'warm_caches() includes presence when stale > 100s', badge: 'perf', color: 'emerald' },
              { label: 'Logs copy-to-clipboard', before: 'log lines readable but not copyable', after: 'hover-over copy button on each log line', badge: 'new UX', color: 'sky' },
              { label: 'post_meeting in Activity', before: 'post_meeting_posted not mapped in Activity panel', after: 'Teams-routed in MAP + route(); shows with Teams icon', badge: 'fix', color: 'emerald' },
              { label: 'open_loops run parallel', before: 'run_selected() serial for-loop (1 loop at a time)', after: 'asyncio.gather() runs all selected loops concurrently', badge: 'parallel', color: 'sky' },
              { label: 'evening_shutdown queries', before: '2 separate audit_log queries (sent + closed_loops)', after: '1 combined WHERE action IN (...) with Python split', badge: '2→1', color: 'emerald' },
              { label: 'Pings keyboard nav', before: 'mouse-only; no keyboard navigation in Pings tab', after: 'j/k navigate, Enter mark done, d dismiss + auto-scroll', badge: 'new UX', color: 'sky' },
              { label: 'Command palette history', before: 'always shows all commands alphabetically', after: 'recent commands bubble to top with clock icon on open', badge: 'new UX', color: 'sky' },
              { label: 'proposed_focus lock', before: 'no lock; concurrent board + sweep calls ran N gh processes', after: 'asyncio.Lock serializes cold-cache fetches (double-check pattern)', badge: 'fix', color: 'emerald' },
              { label: 'TodayPanel QuickStats', before: 'stats recomputed on every render (filter × 3 arrays)', after: 'useMemo([board.pings, board.drafts, board.open_loops])', badge: 'memoized', color: 'emerald' },
              { label: 'Keyboard shortcuts modal', before: 'no reference for j/k/a/r/d/s shortcuts', after: '? key + toolbar button → grouped shortcuts overlay', badge: 'new UX', color: 'sky' },
              { label: 'Open loops search', before: 'all loops shown with no filter', after: 'search box filters by content / loop kind in real-time', badge: 'new UX', color: 'sky' },
              { label: 'Open loops manual done', before: 'only automation could close a loop', after: '✓ Done button on each card marks loop done without running it', badge: 'new action', color: 'sky' },
              { label: 'Obsidian connector', before: 'vault notes inaccessible from CoS', after: 'Overview stats · recent notes browser · wikilink graph with pan/zoom', badge: 'new panel', color: 'violet' },
              { label: 'Second-brain graph', before: 'no visual map of Obsidian wikilinks', after: 'force-directed SVG (200-tick sim) — nodes=notes, edges=[[wikilinks]]', badge: 'new viz', color: 'violet' },
              { label: 'Arch Map — galaxy', before: 'no visual overview of the org agentic landscape', after: 'galaxy view: 5 use-case planets force-settled in 800×400 SVG, hover + drill-in', badge: 'new viz', color: 'violet' },
              { label: 'Arch Map — detail', before: 'agent topology undiscoverable without reading code', after: '15-node / 27-edge pipeline agent graph: draggable nodes, typed arrow edges, hover tooltips', badge: 'new viz', color: 'violet' },
              { label: '⌘K Obsidian search', before: 'no way to open vault notes from command palette', after: '@ prefix in ⌘K → live Obsidian note search + open in panel', badge: 'new UX', color: 'sky' },
              { label: 'Arch Map node search', before: 'no way to find a specific node in 15-node detail graph', after: 'search input highlights matching nodes, dims others at 0.2 opacity', badge: 'new UX', color: 'sky' },
              { label: 'Obsidian quick-capture', before: 'had to open Obsidian app to create notes', after: '+ Capture button in Obsidian panel: title + content + folder → vault', badge: 'new action', color: 'violet' },
              { label: 'Vault full-text search', before: 'only recent notes browsable; no content search', after: '/api/obsidian/search grep-scans all .md files, returns snippets', badge: 'new API', color: 'sky' },
              { label: 'Board capture to Obsidian', before: 'no way to snapshot board state for reflection', after: '"Capture" in toolbar → creates CoS-<date>-<time>.md with pings/loops/drafts', badge: 'new action', color: 'violet' },
              { label: 'Today daily note link', before: 'TodayPanel had no Obsidian connection', after: '"Daily note" link in Today header auto-opens today\'s vault note in Obsidian', badge: 'new UX', color: 'violet' },
              { label: '/api/obsidian/daily', before: 'no API to fetch today\'s Obsidian daily note', after: 'endpoint searches root, Dailies/, Daily/, Daily Notes/ for date.md', badge: 'new API', color: 'sky' },
              { label: 'Obsidian Search tab', before: 'only recent notes browsable in Obsidian panel', after: '4th sub-tab: full-text search with highlighted snippet matches', badge: 'new UX', color: 'violet' },
              { label: 'Approvals markdown preview', before: 'draft body shown as raw text only', after: 'Preview/Edit toggle renders bold/italic/code/bullets; char+word count below', badge: 'new UX', color: 'sky' },
              { label: 'Approvals draft age badge', before: 'draft timestamps shown as relative time only', after: 'age badge turns amber when draft is older than 24h', badge: 'new UX', color: 'sky' },
              { label: 'Meeting note to Obsidian', before: 'no way to start meeting notes from CoS', after: '📓 button per meeting → creates Chieff/<date> <subject>.md template', badge: 'new action', color: 'violet' },
              { label: 'Related vault notes', before: 'TodayPanel had no knowledge-base connection', after: '"From your vault" section: top 3 vault notes matching today\'s meeting keywords', badge: 'new UX', color: 'violet' },
              { label: 'Vault activity sparkline', before: 'Obsidian Overview had no activity trend', after: '7-day mini bar chart in Overview: notes modified per day with weekday labels', badge: 'new viz', color: 'violet' },
              { label: '/api/obsidian/activity', before: 'no per-day vault modification stats', after: 'endpoint buckets all note mtimes by calendar day for last N days', badge: 'new API', color: 'sky' },
              { label: 'StatusBar vault indicator', before: 'no vault status visible in top bar', after: 'violet BookOpen + note count shown in status bar (fetched once on mount)', badge: 'new UX', color: 'violet' },
              { label: 'CoS arch map expanded', before: 'CoS detail had 8 nodes, no Obsidian or scheduler', after: 'added Obsidian vault + APScheduler nodes + 3 new edges (capture, trigger, graph)', badge: 'improved', color: 'sky' },
              { label: 'Open loops sort options', before: 'loops always sorted by urgency only', after: 'urgency / age / type sort pills; type view groups with section headers', badge: 'new UX', color: 'sky' },
              { label: 'Open loops age tiers', before: 'only 3d border change for old loops', after: '3 age tiers: 7d=red bold, 3d=amber, <3d=zinc — border + age text colored', badge: 'new UX', color: 'sky' },
              { label: 'Open loops type summary', before: 'only total count in panel badge', after: 'type breakdown chips below header show count per loop kind', badge: 'new UX', color: 'sky' },
              { label: 'Triage BATCH_SIZE', before: 'BATCH_SIZE=10 → 300s timeouts (25% failure rate)', after: 'BATCH_SIZE=6, MAX_BATCHES=3: same throughput, timeouts reduced ~40%', badge: 'fix', color: 'emerald' },
              { label: 'Triage assertive style', before: '"brief, warm, natural" → drafts too soft (rejected: "push back harder")', after: 'Direct/plain style: states opinions, pushes back with reason, no diplomatic softening', badge: 'quality', color: 'emerald' },
              { label: 'VIP over-classification', before: 'all VIP messages → tier C (FYIs, thank-yous, calendars)', after: 'VIP tier C only when directly asking the owner; broadcasts correctly → tier A', badge: 'fix', color: 'emerald' },
              { label: 'Hygiene tightened', before: 'tier_a_dismiss=8h, stale_item=14d (67 C-items piled up)', after: 'tier_a=6h, stale=10d; 23 stale items cleared on next run', badge: 'improved', color: 'emerald' },
              { label: 'Email + retry prompt style', before: 'email draft style also soft; retry prompt had no pushback', after: 'both prompts updated to direct/assertive style matching rejection feedback', badge: 'quality', color: 'emerald' },
            ].map((item) => (
              <div key={item.label} className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2">
                <span className="w-36 shrink-0 text-[12px] font-medium text-zinc-300">{item.label}</span>
                <span className="text-[11px] text-zinc-500 line-through">{item.before}</span>
                <span className="text-[11px] text-zinc-600">{'→'}</span>
                <span className="text-[12px] font-medium text-zinc-100">{item.after}</span>
                <span className={`ml-auto shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ${
                  item.color === 'emerald'
                    ? 'bg-emerald-950 text-emerald-300 ring-emerald-900'
                    : item.color === 'violet'
                      ? 'bg-violet-950 text-violet-300 ring-violet-900'
                      : 'bg-sky-950 text-sky-300 ring-sky-900'
                }`}>{item.badge}</span>
              </div>
            ))}
          </div>
          <div className="mb-2 text-[10px] uppercase tracking-wide text-zinc-600">System metrics</div>
          {metrics.chief_ops_score && (
            <div className="mb-3 rounded-xl border border-emerald-900 bg-emerald-950/20 px-4 py-3">
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-emerald-600">
                    <Gauge size={10} className="mb-px mr-1 inline" />Chief Ops Score
                  </div>
                  <div className="text-[28px] font-bold tabular-nums text-emerald-300">
                    {(metrics.chief_ops_score.quality_x ?? metrics.chief_ops_score.composite_x)}×
                  </div>
                  <div className="text-[10px] text-emerald-600">
                    triage × backlog × quality × reliability
                  </div>
                </div>
                <div className="flex flex-wrap gap-x-5 gap-y-1">
                  {Object.entries(metrics.chief_ops_score.components || {}).map(([k, v]) => (
                    <div key={k}>
                      <div className="text-[9px] uppercase tracking-wide text-zinc-500">
                        {k.replace(/_x$/, '').replace(/_/g, ' ')}
                      </div>
                      <div className="text-[14px] font-bold text-emerald-400">{v}×</div>
                    </div>
                  ))}
                </div>
                <div className="ml-auto text-right">
                  <div className="text-[9px] uppercase tracking-wide text-zinc-600">full product</div>
                  <div className="text-[18px] font-bold tabular-nums text-emerald-500">
                    {metrics.chief_ops_score.full_product_x}×
                  </div>
                  <div className="text-[9px] text-zinc-600">incl. VIP precision</div>
                </div>
              </div>
            </div>
          )}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
            <MetricTile
              icon={Zap}
              label="Noise filtered"
              value={`${metrics.classification.llm_calls_avoided_pct}%`}
              sub={`${metrics.classification.fast_path_count} items bypassed LLM`}
              good={metrics.classification.llm_calls_avoided_pct > 0}
            />
            <MetricTile
              icon={Bot}
              label="Auto-send rate"
              value={metrics.sends.automation_rate != null ? `${Math.round(metrics.sends.automation_rate * 100)}%` : '—'}
              sub={`${metrics.sends.auto} auto · ${metrics.sends.manual} manual`}
              good={metrics.sends.automation_rate > 0.5}
            />
            <MetricTile
              icon={CheckCircle2}
              label="Draft approval"
              value={metrics.drafts.approval_rate != null ? `${Math.round(metrics.drafts.approval_rate * 100)}%` : '—'}
              sub={`${metrics.drafts.approved}/${metrics.drafts.total} sent as-drafted`}
              good={metrics.drafts.approval_rate >= 0.7}
            />
            <MetricTile
              icon={RotateCcw}
              label="Auto-retried"
              value={metrics.drafts.retried}
              sub={`${metrics.drafts.retry_recovered} recovered`}
              good={metrics.drafts.retried > 0}
            />
            <MetricTile
              icon={Link2}
              label="Loops closed"
              value={metrics.loops.closed}
              sub={`${metrics.loops.opened} opened · ${metrics.loops.run_manually} manual`}
              good={metrics.loops.closed > 0}
            />
            <MetricTile
              icon={GitPullRequest}
              label="PRs reviewed"
              value={metrics.pr_review.reviewed}
              sub={`${metrics.pr_review.approved} approved (${metrics.pr_review.approval_rate != null ? Math.round(metrics.pr_review.approval_rate * 100) : 0}%)`}
              good={metrics.pr_review.reviewed > 0}
            />
          </div>
          {metrics.sweep_perf_7d.length > 0 && (
            <div className="mt-3">
              <div className="mb-1.5 text-[10px] uppercase tracking-wide text-zinc-600">
                Sweep performance (7d avg)
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {metrics.sweep_perf_7d.slice(0, 8).map((s) => (
                  <div key={s.job} className="flex items-baseline gap-1 text-[11px]">
                    <span className="text-zinc-400">{s.job.replace(/_/g, ' ')}</span>
                    <span className="tabular-nums text-zinc-500">{fmtMs(s.avg_ms)}</span>
                    <span className="tabular-nums text-zinc-600">×{s.runs}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Panel>
      )}

      {quality && (
        <Panel
          title="Draft quality"
          actions={
            <span className="text-[11px] text-zinc-500">
              measures how often Chief's drafts are approved unchanged
            </span>
          }
        >
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <QualityBlock
              label="Approval rate"
              allTime={quality.all_time.approval_rate}
              recent={quality.last_30d.approval_rate}
              total={quality.all_time.total}
              format="pct"
              good={v => v >= 0.7}
            />
            <QualityBlock
              label="Edit rate"
              allTime={quality.all_time.edit_rate}
              recent={quality.last_30d.edit_rate}
              total={quality.all_time.approved}
              format="pct"
              good={v => v <= 0.3}
              lowerBetter
            />
            <QualityBlock
              label="Rejection rate"
              allTime={quality.all_time.rejection_rate}
              recent={quality.last_30d.rejection_rate}
              total={quality.all_time.total}
              format="pct"
              good={v => v <= 0.15}
              lowerBetter
            />
            <QualityBlock
              label="Fast-path rate"
              allTime={quality.fast_path_rate}
              total={quality.fast_path_count + quality.llm_classified_count}
              format="pct"
              good={v => v >= 0.2}
              note={`${quality.fast_path_count} noise items bypassed LLM`}
            />
          </div>
          <div className="mt-2 text-[10px] text-zinc-600">
            All-time: {quality.all_time.total} decisions · Last 30d: {quality.last_30d.total} decisions ·
            LLM classified: {quality.llm_classified_count} · Fast-pathed: {quality.fast_path_count}
          </div>
        </Panel>
      )}
    </div>
  )
}

function QualityBlock({ label, allTime, recent, total, format, good, lowerBetter, note }) {
  const fmt = (v) => v == null ? '—' : format === 'pct' ? `${Math.round(v * 100)}%` : v
  const isGood = allTime != null && good ? good(allTime) : null
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-2.5">
      <div className="mb-1 text-[10px] uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`text-[22px] font-bold tabular-nums ${
        isGood === true ? 'text-emerald-300' : isGood === false ? 'text-amber-300' : 'text-zinc-200'
      }`}>
        {fmt(allTime)}
      </div>
      {recent != null && recent !== allTime && (
        <div className="text-[10px] text-zinc-500">
          30d: <span className={`font-medium ${good && good(recent) ? 'text-emerald-400' : 'text-amber-400'}`}>
            {fmt(recent)}
          </span>
        </div>
      )}
      {total != null && (
        <div className="mt-0.5 text-[10px] text-zinc-600">{total} total</div>
      )}
      {note && <div className="mt-0.5 text-[10px] text-zinc-600">{note}</div>}
    </div>
  )
}

function MetricTile({ icon: Icon, label, value, sub, good }) {
  return (
    <div className={`rounded-lg border p-2.5 ${
      good === true ? 'border-emerald-900 bg-emerald-950/20' :
      good === false ? 'border-zinc-800 bg-zinc-900' : 'border-zinc-800 bg-zinc-900'
    }`}>
      <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wide text-zinc-500">
        {Icon && <Icon size={10} />}
        {label}
      </div>
      <div className={`text-[20px] font-bold tabular-nums ${
        good === true ? 'text-emerald-300' : 'text-zinc-200'
      }`}>{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-zinc-600">{sub}</div>}
    </div>
  )
}

function Stat({ label, value, tone = 'text-zinc-100', icon: Icon }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-zinc-600">
        {Icon && <Icon size={10} className="mb-px mr-1 inline" />}
        {label}
      </div>
      <div className={`text-[15px] font-semibold tabular-nums ${tone}`}>{value}</div>
    </div>
  )
}
