import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import {
  Activity, AlertTriangle, AlertOctagon, CheckCircle2, RefreshCw, Bell,
  ChevronRight, Zap, Users, GitBranch, Clock, ShieldAlert, ShieldCheck,
  TrendingUp, Ban, PauseCircle, X, Check, HeartPulse, Cpu, FileWarning,
  Mail, Bot, Radio, Info, Flag, Database, Play, UserCheck, Mailbox,
  FileUp, MessageSquare, ClipboardCheck,
} from 'lucide-react'
import { useToast, SkeletonRows, timeAgo } from '../ui.jsx'

// ── Constants ─────────────────────────────────────────────────────────────────

const STATUS_META = {
  green:   { label: 'HEALTHY',  sub: 'All systems operational', color: '#10b981', tw: 'emerald' },
  amber:   { label: 'DEGRADED', sub: 'Attention needed',        color: '#f59e0b', tw: 'amber' },
  red:     { label: 'FAILING',  sub: 'Failures detected',       color: '#ef4444', tw: 'red' },
  unknown: { label: 'UNKNOWN',  sub: 'Telemetry unavailable',   color: '#64748b', tw: 'zinc' },
}

const KIND_META = {
  escalation:    { label: 'Escalation',   Icon: ShieldAlert },
  blocked:       { label: 'Blocked',      Icon: Ban },
  paused:        { label: 'Paused',       Icon: PauseCircle },
  dead_letter:   { label: 'Dead-letter',  Icon: AlertOctagon },
  orphaned:      { label: 'Orphaned CSR', Icon: GitBranch },
  stalled:       { label: 'Stalled run',  Icon: Clock },
  probe_failure: { label: 'Health probe', Icon: HeartPulse },
  scheduling:    { label: 'Capacity',     Icon: Cpu },
  pod_error:     { label: 'Log errors',   Icon: FileWarning },
  log_warning:   { label: 'Log warnings', Icon: FileWarning },
}

const ACT_META = {
  run:     { Icon: Radio, color: '#38bdf8' },
  email:   { Icon: Mail,  color: '#10b981' },
  agent:   { Icon: Bot,   color: '#8b5cf6' },
  cadence: { Icon: Bell,  color: '#f59e0b' },
}

// The 12-step pipeline (mirrors the arch-map flow view)
const PIPE_STEPS = [
  { n: 1,  name: 'Workflow Start',    perf: 'auto',  Icon: Play },
  { n: 2,  name: 'Client Intake',     perf: 'hitl',  Icon: UserCheck },
  { n: 3,  name: 'Data Setup',        perf: 'agent', Icon: Database },
  { n: 4,  name: 'Email Review',      perf: 'hitl',  Icon: ClipboardCheck },
  { n: 5,  name: 'Welcome Email',     perf: 'auto',  Icon: Mailbox },
  { n: 6,  name: 'Reminder Emails',   perf: 'auto',  Icon: Bell },
  { n: 7,  name: 'Parse Inbound',     perf: 'agent', Icon: MessageSquare },
  { n: 8,  name: 'Parse Docs',        perf: 'agent', Icon: FileUp },
  { n: 9,  name: 'Data Completeness', perf: 'agent', Icon: CheckCircle2 },
  { n: 10, name: 'Digitization',      perf: 'agent', Icon: Cpu },
  { n: 11, name: 'Delivery',          perf: 'agent', Icon: TrendingUp },
  { n: 12, name: 'Complete',          perf: 'auto',  Icon: Flag },
]
const PERF_COLOR = { auto: '#10b981', hitl: '#f59e0b', agent: '#6C5CE7' }
const PERF_LABEL = { auto: 'Automated', hitl: 'Human Review', agent: 'AI Agent' }

const CARD = 'rounded-xl border border-[#16233b] bg-[#0a1220]'

// ── Small pieces ──────────────────────────────────────────────────────────────

function Spark({ points, color = '#38bdf8', w = 76, h = 20 }) {
  if (!points || points.length < 2) return <div style={{ height: h }} />
  const max = Math.max(1, ...points)
  const min = Math.min(...points)
  const span = Math.max(1, max - min)
  const pts = points.map((v, i) =>
    `${(i / (points.length - 1)) * w},${h - 2 - ((v - min) / span) * (h - 4)}`).join(' ')
  return (
    <svg width={w} height={h} className="block">
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.3} opacity={0.9} />
    </svg>
  )
}

function Kpi({ label, value, spark, color = '#e2e8f0', sparkColor, alert }) {
  return (
    <div className={`${CARD} flex flex-col justify-between px-3 py-2 ${alert ? 'border-red-900/70' : ''}`}>
      <div className="text-2xl font-bold tabular-nums leading-7" style={{ color }}>{value}</div>
      <div className="mt-0.5 text-[9px] font-semibold uppercase tracking-[0.08em] text-slate-500">{label}</div>
      <div className="mt-1"><Spark points={spark} color={sparkColor || color} /></div>
    </div>
  )
}

function UtcClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  const pad = (n) => String(n).padStart(2, '0')
  return (
    <div className="text-right leading-tight">
      <div className="text-[13px] font-bold tabular-nums text-slate-200">
        {pad(now.getUTCHours())}:{pad(now.getUTCMinutes())}:{pad(now.getUTCSeconds())} <span className="text-slate-500">UTC</span>
      </div>
      <div className="text-[9px] text-slate-500">
        {now.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })}
      </div>
    </div>
  )
}

// ── Live pipeline row ─────────────────────────────────────────────────────────

function LivePipeline({ health }) {
  const flow = health?.flow || {}
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-2 flex items-center justify-between">
        <div>
          <span className="text-[11px] font-bold uppercase tracking-wide text-slate-300">Live Pipeline</span>
          <span className="ml-2 text-[9px] text-slate-500">real-time case flow · uat+dev</span>
        </div>
        <div className="flex gap-2 text-[9px] text-slate-500">
          {Object.entries(PERF_LABEL).map(([k, v]) => (
            <span key={k} className="flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: PERF_COLOR[k] }} />{v}
            </span>
          ))}
        </div>
      </div>
      <div className="flex items-stretch gap-1 overflow-x-auto pb-1">
        {PIPE_STEPS.map((s, i) => {
          const ls = flow[String(s.n)] || {}
          // escalated/blocked overlap on the same clients — max, not sum
          const problems = Math.max(ls.escalations || 0, ls.blocked || 0)
          const ingress = ls.ingress_issues || 0
          const count = ls.count || 0
          const state = problems > 0 ? 'red' : (ls.paused || 0) > 0 || ingress > 0 ? 'amber' : count > 0 ? 'live' : 'idle'
          const border = state === 'red' ? '#ef4444' : state === 'amber' ? '#f59e0b' : state === 'live' ? '#38bdf8' : '#16233b'
          const { Icon } = s
          return (
            <React.Fragment key={s.n}>
              <div title={`${s.name} — ${count} active${problems ? ` · ${problems} stuck` : ''}${ingress ? ` · ${ingress} ingress issues` : ''}`}
                className="flex min-w-[86px] flex-1 flex-col items-center rounded-lg border px-1.5 py-2"
                style={{ borderColor: border, background: state === 'red' ? '#180a0e' : '#0d1526',
                         boxShadow: state === 'red' ? '0 0 12px #ef444433' : undefined }}>
                <div className="flex w-full items-center justify-between">
                  <span className="flex h-4 w-4 items-center justify-center rounded-full text-[8px] font-black text-black"
                    style={{ background: border === '#16233b' ? '#334155' : border }}>{s.n}</span>
                  <Icon size={11} style={{ color: border === '#16233b' ? '#475569' : border }} />
                </div>
                <div className="mt-1.5 text-xl font-bold tabular-nums"
                  style={{ color: state === 'red' ? '#ef4444' : state === 'idle' ? '#334155' : '#e2e8f0' }}>
                  {count || (ingress ? `${ingress > 99 ? '99+' : ingress}` : 0)}
                </div>
                <div className="mt-0.5 h-6 text-center text-[8px] leading-tight text-slate-400">{s.name}</div>
                <span className="mt-0.5 rounded px-1 py-px text-[7px] font-bold"
                  style={{ color: PERF_COLOR[s.perf], background: PERF_COLOR[s.perf] + '1a' }}>
                  {PERF_LABEL[s.perf]}
                </span>
                {problems > 0 && (
                  <span className="mt-0.5 animate-pulse text-[8px] font-bold text-red-400">⚠ {problems} stuck</span>
                )}
                {ingress > 0 && problems === 0 && (
                  <span className="mt-0.5 text-[8px] font-bold text-amber-500">⚠ ingress</span>
                )}
              </div>
              {i < PIPE_STEPS.length - 1 && (
                <div className="flex items-center text-slate-700"><ChevronRight size={10} /></div>
              )}
            </React.Fragment>
          )
        })}
      </div>
    </div>
  )
}

// ── Bottlenecks / trend / run mix ────────────────────────────────────────────

function Bottlenecks({ problems }) {
  const rows = useMemo(() => {
    const active = (problems || []).filter((p) => p.status !== 'resolved')
    return active.slice().sort((a, b) => (b.count || 0) - (a.count || 0)).slice(0, 6)
  }, [problems])
  const max = Math.max(1, ...rows.map((r) => r.count || 0))
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-2 text-[10px] font-bold uppercase tracking-wide text-slate-300">Bottlenecks <span className="ml-1 font-normal normal-case text-slate-500">by affected count</span></div>
      <div className="space-y-1.5">
        {rows.length === 0 && <div className="py-4 text-center text-[11px] text-slate-600">no active problems</div>}
        {rows.map((r) => (
          <div key={r.key} className="flex items-center gap-2" title={r.title}>
            <span className="w-32 shrink-0 truncate text-[10px] text-slate-400">{r.title?.replace(/\[(uat|dev)\]$/, '')}</span>
            <div className="h-2 flex-1 overflow-hidden rounded-sm bg-[#111a2e]">
              <div className="h-2 rounded-sm" style={{
                width: `${((r.count || 0) / max) * 100}%`,
                background: r.severity === 'high' ? '#ef4444' : '#3b82f6' }} />
            </div>
            <span className="w-8 text-right text-[10px] font-bold tabular-nums text-slate-300">{r.count}</span>
            {/\[(uat|dev)\]/.test(r.title || '') && (
              <span className="w-7 text-[8px] font-bold text-slate-500">{r.title.match(/\[(uat|dev)\]/)[1]}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function TrendChart({ trend }) {
  const snaps = trend || []
  const W = 300, H = 110, P = 8
  const lines = [
    { key: 'active_clients', color: '#10b981', label: 'active' },
    { key: 'escalations', color: '#ef4444', label: 'escalated' },
    { key: 'stalled', color: '#f59e0b', label: null },
  ]
  const path = (key) => {
    const vals = snaps.map((s) => s[key] ?? 0)
    if (vals.length < 2) return ''
    const max = Math.max(1, ...vals)
    return vals.map((v, i) =>
      `${i === 0 ? 'M' : 'L'}${P + (i / (vals.length - 1)) * (W - P * 2)},${H - P - (v / max) * (H - P * 2)}`).join(' ')
  }
  const last = snaps[snaps.length - 1] || {}
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[10px] font-bold uppercase tracking-wide text-slate-300">Trend <span className="ml-1 font-normal normal-case text-slate-500">per 5-min snapshot</span></span>
        <div className="flex gap-2 text-[9px]">
          <span className="text-emerald-500">— active</span>
          <span className="text-red-500">— escalated</span>
        </div>
      </div>
      {snaps.length < 2 ? (
        <div className="flex h-[110px] items-center justify-center text-[11px] text-slate-600">collecting history…</div>
      ) : (
        <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
          {lines.filter((l) => l.label !== undefined).map((l) => (
            <path key={l.key} d={path(l.key)} fill="none" stroke={l.color} strokeWidth={1.5} opacity={0.85} />
          ))}
        </svg>
      )}
      <div className="mt-1 flex justify-between text-[9px] text-slate-500">
        <span>{snaps.length} snapshots</span>
        <span className="tabular-nums">now: {last.active_clients ?? '—'} active · {last.escalations ?? '—'} esc</span>
      </div>
    </div>
  )
}

function RunMix({ health }) {
  const rs = health?.throughput?.run_status || {}
  const total = Object.values(rs).reduce((a, b) => a + b, 0) || 1
  const colorFor = (s) => s === 'orphaned' ? '#ef4444' : s === 'hitl' ? '#f59e0b'
    : /complete|deliver|done|closed/.test(s) ? '#10b981' : '#38bdf8'
  // donut segments
  const R = 34, CX = 45, CY = 45, C = 2 * Math.PI * R
  let acc = 0
  const segs = Object.entries(rs).sort(([, a], [, b]) => b - a).map(([s, n]) => {
    const frac = n / total
    const seg = { s, n, dash: frac * C, off: -acc * C }
    acc += frac
    return seg
  })
  const hitlPct = Math.round(((rs.hitl || 0) / total) * 100)
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-1 text-[10px] font-bold uppercase tracking-wide text-slate-300">
        Run Mix <span className="ml-1 font-normal normal-case text-slate-500">{health?.throughput?.sampled_runs ?? 0} sampled</span>
      </div>
      <div className="flex items-center gap-3">
        <svg width={90} height={90} className="shrink-0">
          {segs.map((g) => (
            <circle key={g.s} cx={CX} cy={CY} r={R} fill="none" stroke={colorFor(g.s)} strokeWidth={11}
              strokeDasharray={`${g.dash} ${C - g.dash}`} strokeDashoffset={C * 0.25 + g.off} />
          ))}
          <text x={CX} y={CY - 2} textAnchor="middle" className="fill-slate-200" style={{ fontSize: 15, fontWeight: 800 }}>{hitlPct}%</text>
          <text x={CX} y={CY + 11} textAnchor="middle" className="fill-slate-500" style={{ fontSize: 7 }}>in HITL</text>
        </svg>
        <div className="min-w-0 flex-1 space-y-1">
          {segs.slice(0, 5).map((g) => (
            <div key={g.s} className="flex items-center gap-1.5 text-[10px]">
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: colorFor(g.s) }} />
              <span className="truncate text-slate-400">{g.s}</span>
              <span className="ml-auto tabular-nums text-slate-300">{g.n}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ── Right column: alerts, review queue, activity ─────────────────────────────

const LOG_KINDS = new Set(['probe_failure', 'scheduling', 'pod_error', 'log_warning'])
const SEV_TEXT = { ERROR: '#ef4444', CRITICAL: '#ef4444', WARNING: '#f59e0b', INFO: '#64748b', DEFAULT: '#475569', NOTICE: '#64748b' }

function LogDrawer({ workload, onClose }) {
  const [data, setData] = useState(null)
  useEffect(() => {
    let alive = true
    setData(null)
    fetch(`/api/osca/logs?workload=${encodeURIComponent(workload)}&hours=12&limit=40`)
      .then((r) => r.json())
      .then((d) => { if (alive) setData(d) })
      .catch(() => { if (alive) setData({ entries: [], error: 'fetch failed' }) })
    return () => { alive = false }
  }, [workload])
  return (
    <div className="mt-1.5 rounded-lg border border-[#1e3a5f] bg-[#050a12] p-2">
      <div className="mb-1 flex items-center justify-between">
        <span className="font-mono text-[9px] font-bold text-sky-400">{workload} · logs (12h)</span>
        <button onClick={onClose} title="Close" aria-label="Close" className="text-slate-500 hover:text-slate-200"><X size={10} /></button>
      </div>
      {data === null ? (
        <div className="py-2 text-center text-[9px] text-slate-600">loading logs…</div>
      ) : data.error ? (
        <div className="py-1 text-[9px] text-amber-500">{data.error}</div>
      ) : data.entries.length === 0 ? (
        <div className="py-1 text-[9px] text-slate-600">no log lines in window</div>
      ) : (
        <div className="max-h-40 space-y-0.5 overflow-y-auto font-mono text-[8.5px] leading-3">
          {data.entries.map((e, i) => (
            <div key={i} className="flex gap-1.5">
              <span className="shrink-0 text-slate-600">{(e.ts || '').slice(5, 16).replace('T', ' ')}</span>
              <span className="shrink-0 font-bold" style={{ color: SEV_TEXT[e.severity] || '#475569' }}>{e.severity.slice(0, 4)}</span>
              <span className="min-w-0 break-all text-slate-400">{e.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Literal per-severity classes — a template literal like `bg-${color}-950/20`
// only generates CSS if that exact string also appears elsewhere verbatim in
// the source (Tailwind's scanner does static text search, not JS eval).
const SEVERITY_CLS = {
  red: { box: 'border-red-900/50 bg-red-950/20', label: 'text-red-400', dot: 'bg-red-500' },
  amber: { box: 'border-amber-900/50 bg-amber-950/20', label: 'text-amber-400', dot: 'bg-amber-500' },
  sky: { box: 'border-sky-900/50 bg-sky-950/20', label: 'text-sky-400', dot: 'bg-sky-500' },
}

function AlertsPanel({ problems, onAck, envFilter, onClearEnvFilter }) {
  const [logFor, setLogFor] = useState(null)  // workload name with open log drawer
  const buckets = useMemo(() => {
    let active = (problems || []).filter((p) => p.status !== 'resolved')
    // Every problem's title ends with "[envname]" (see osca.py _emit callers).
    if (envFilter) active = active.filter((p) => p.title?.endsWith(`[${envFilter}]`))
    return {
      critical: active.filter((p) => p.severity === 'high'),
      warning: active.filter((p) => p.severity === 'medium'),
      info: active.filter((p) => p.severity === 'low'),
    }
  }, [problems, envFilter])
  const Section = ({ label, items, color, Icon }) => {
    const cls = SEVERITY_CLS[color] || SEVERITY_CLS.sky
    return (
    <div className={`rounded-lg border px-2.5 py-2 ${cls.box}`}>
      <div className={`flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide ${cls.label}`}>
        <Icon size={11} /> {label} ({items.length})
      </div>
      {items.length === 0 ? (
        <div className="mt-1 text-[10px] text-slate-600">none</div>
      ) : (
        <div className="mt-1 space-y-1">
          {items.slice(0, 5).map((p) => {
            const hasLogs = LOG_KINDS.has(p.kind)
            const wl = p.key?.split(':').pop()
            return (
              <div key={p.key}>
                <div className="group flex items-start gap-1.5">
                  <span className={`mt-1 h-1 w-1 shrink-0 rounded-full ${cls.dot}`} />
                  <span
                    onClick={hasLogs ? () => setLogFor(logFor === wl ? null : wl) : undefined}
                    className={`min-w-0 flex-1 truncate text-[10px] leading-4 text-slate-400 ${hasLogs ? 'cursor-pointer hover:text-sky-300 hover:underline decoration-dotted' : ''}`}
                    title={`${p.title}\n${p.detail || ''}${hasLogs ? '\n(click to view raw logs)' : ''}`}>
                    {p.count > 1 && <b className={cls.label}>{p.count}× </b>}
                    {p.title}
                  </span>
                  <span className="hidden shrink-0 gap-0.5 group-hover:flex">
                    {p.status !== 'acknowledged' && (
                      <button onClick={() => onAck(p.key, 'acknowledged')} title="Acknowledge"
                        className="rounded border border-slate-700 p-0.5 text-slate-500 hover:text-slate-200"><Check size={9} /></button>
                    )}
                    <button onClick={() => onAck(p.key, 'resolved')} title="Resolve"
                      className="rounded border border-slate-700 p-0.5 text-slate-500 hover:text-emerald-400"><X size={9} /></button>
                  </span>
                  {p.status === 'acknowledged' && <span className="shrink-0 text-[8px] text-slate-600">ack</span>}
                </div>
                {hasLogs && logFor === wl && <LogDrawer workload={wl} onClose={() => setLogFor(null)} />}
              </div>
            )
          })}
          {items.length > 5 && <div className="text-[9px] text-slate-600">+{items.length - 5} more</div>}
        </div>
      )}
    </div>
    )
  }
  const total = buckets.critical.length + buckets.warning.length + buckets.info.length
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-2 flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-slate-300">
          <Bell size={12} /> Alerts
          {envFilter && (
            <button onClick={onClearEnvFilter}
              className="flex items-center gap-1 rounded-md bg-sky-950 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-sky-300 hover:bg-sky-900">
              {envFilter} <X size={9} />
            </button>
          )}
        </span>
        <span className="text-[9px] tabular-nums text-slate-500">{total} total</span>
      </div>
      <div className="space-y-2">
        <Section label="Critical" items={buckets.critical} color="red" Icon={AlertOctagon} />
        <Section label="Warnings" items={buckets.warning} color="amber" Icon={AlertTriangle} />
        <Section label="Info" items={buckets.info} color="sky" Icon={Info} />
      </div>
    </div>
  )
}

function ReviewQueue({ problems }) {
  const hitl = useMemo(() => {
    const g = (problems || []).find((p) => p.kind === 'stalled' && /hitl/.test(p.title || '') && p.status !== 'resolved')
    return g ? { count: g.count, examples: Array.isArray(g.examples) ? g.examples : [] } : null
  }, [problems])
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-2 flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-slate-300">
          <Users size={12} /> Human Review Queue
        </span>
        <span className="text-[9px] tabular-nums text-slate-500">{hitl?.count ?? 0} waiting</span>
      </div>
      {!hitl ? (
        <div className="py-2 text-center text-[10px] text-slate-600">queue clear</div>
      ) : (
        <div className="space-y-1">
          {hitl.examples.slice(0, 5).map((ex, i) => (
            <div key={i} className="flex items-center gap-2 text-[10px]">
              <span className="font-mono text-amber-500">{ex.entity}</span>
              <span className="min-w-0 flex-1 truncate text-slate-500">{ex.detail}</span>
            </div>
          ))}
          {hitl.count > 5 && <div className="text-[9px] text-slate-600">+{hitl.count - 5} more in HITL</div>}
        </div>
      )}
    </div>
  )
}

function ActivityFeed({ health }) {
  const activity = health?.activity || []
  return (
    <div className={`${CARD} flex min-h-0 flex-col p-3`}>
      <div className="mb-2 flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-slate-300">
          <Zap size={12} className="text-emerald-500" /> Recent Activity
        </span>
        <span className="flex items-center gap-1 text-[9px] text-slate-500">
          <span className="h-1 w-1 animate-pulse rounded-full bg-emerald-500" /> live feed
        </span>
      </div>
      <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto pr-1" style={{ maxHeight: 300 }}>
        {activity.length === 0 && <div className="py-3 text-center text-[10px] text-slate-600">no recent events</div>}
        {activity.map((a, i) => {
          const meta = ACT_META[a.kind] || ACT_META.run
          const { Icon } = meta
          return (
            <div key={`${a.ts}-${i}`} className={`flex items-center gap-2 rounded px-1.5 py-[3px] text-[10px] ${i === 0 ? 'bg-[#0d1a2e]' : ''}`}>
              <Icon size={10} style={{ color: meta.color }} className="shrink-0" />
              <span className="w-12 shrink-0 tabular-nums text-slate-600">{timeAgo(a.ts)}</span>
              {a.env && (
                <span className={`w-6 shrink-0 rounded text-center text-[8px] font-bold ${
                  a.env === 'uat' ? 'bg-sky-950 text-sky-500' : 'bg-violet-950 text-violet-500'}`}>{a.env}</span>
              )}
              <span className="w-20 shrink-0 truncate font-mono text-[9px]" style={{ color: meta.color }}>{a.entity || '—'}</span>
              <span className="min-w-0 flex-1 truncate text-slate-400">{a.message}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Follow-up timeline + customer actions + integrations ────────────────────

function CustomerActions({ health }) {
  const activity = health?.activity || []
  const emails = activity.filter((a) => a.kind === 'email')
  const withAtt = emails.filter((a) => /attachment/.test(a.message)).length
  const agents = activity.filter((a) => a.kind === 'agent').length
  const runs = activity.filter((a) => a.kind === 'run').length
  const cards = [
    { v: emails.length, l: 'Emails received', s: 'in recent feed', Icon: Mail, c: '#10b981' },
    { v: withAtt, l: 'With documents', s: 'attachments uploaded', Icon: FileUp, c: '#38bdf8' },
    { v: agents, l: 'Agent actions', s: 'processing events', Icon: Bot, c: '#8b5cf6' },
    { v: runs, l: 'Run events', s: 'pipeline movements', Icon: Radio, c: '#f59e0b' },
  ]
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-2 text-[10px] font-bold uppercase tracking-wide text-slate-300">
        Customer & Service Actions <span className="ml-1 font-normal normal-case text-slate-500">what's being triggered</span>
      </div>
      <div className="grid grid-cols-4 gap-2">
        {cards.map(({ v, l, s, Icon, c }) => (
          <div key={l} className="rounded-lg border border-[#16233b] bg-[#0d1526] px-2.5 py-2">
            <div className="flex items-center justify-between">
              <span className="text-lg font-bold tabular-nums" style={{ color: c }}>{v}</span>
              <Icon size={13} style={{ color: c, opacity: 0.7 }} />
            </div>
            <div className="text-[9px] font-semibold text-slate-400">{l}</div>
            <div className="text-[8px] text-slate-600">{s}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function UpcomingTriggers({ health }) {
  const up = health?.upcoming || []
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-2 flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-300">
          <Bell size={11} className="text-amber-500" /> Scheduled Triggers
        </span>
        <span className="text-[9px] text-slate-500">next cadence fires</span>
      </div>
      {up.length === 0 ? (
        <div className="py-3 text-center text-[10px] text-slate-600">nothing scheduled in the next 4 days</div>
      ) : (
        <div className="max-h-[104px] space-y-1 overflow-y-auto pr-1">
          {up.map((u, i) => {
            const ms = Date.parse(u.ts) - Date.now()
            const overdue = ms < 0
            const hrs = Math.abs(ms) / 3600000
            const when = hrs < 1 ? `${Math.round(hrs * 60)}m` : hrs < 48 ? `${Math.round(hrs)}h` : `${Math.round(hrs / 24)}d`
            return (
              <div key={`${u.ts}-${i}`} className="flex items-center gap-2 text-[10px]">
                <span className={`w-14 shrink-0 rounded px-1 text-center font-bold tabular-nums ${
                  overdue ? 'bg-red-950 text-red-400' : 'bg-amber-950/60 text-amber-400'}`}>
                  {overdue ? `-${when}` : `in ${when}`}
                </span>
                <span className={`w-6 shrink-0 rounded text-center text-[8px] font-bold ${
                  u.env === 'uat' ? 'bg-sky-950 text-sky-500' : 'bg-violet-950 text-violet-500'}`}>{u.env}</span>
                <span className="shrink-0 font-semibold text-slate-300">{u.label}</span>
                <span className="shrink-0 font-mono text-[9px] text-slate-500">{u.entity}</span>
                <span className="min-w-0 flex-1 truncate text-slate-600">{u.target}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function FollowUpTimeline() {
  const gates = [
    { t: 'T+0', title: 'Initial Email', sub: 'Day 0', c: '#10b981' },
    { t: 'T+3', title: '1st Reminder', sub: 'no response → reminder', c: '#f59e0b' },
    { t: 'T+7', title: '1-Week Follow-Up', sub: 'reminder + SF touchpoint', c: '#f59e0b' },
    { t: 'T+14', title: '2-Week Follow-Up', sub: 'reminder + SF touchpoint', c: '#f97316' },
    { t: 'T+21', title: 'Deadline', sub: 'final reminder day before', c: '#ef4444' },
  ]
  return (
    <div className={`${CARD} p-3`}>
      <div className="mb-2 text-[10px] font-bold uppercase tracking-wide text-slate-300">
        Follow-Up Timeline <span className="ml-1 font-normal normal-case text-slate-500">automated cadence</span>
      </div>
      <div className="flex items-center gap-1">
        {gates.map((g, i) => (
          <React.Fragment key={g.t}>
            <div className="flex-1 rounded-lg border px-2 py-1.5" style={{ borderColor: g.c + '55', background: g.c + '0d' }}>
              <span className="rounded px-1 py-px text-[8px] font-black text-black" style={{ background: g.c }}>{g.t}</span>
              <div className="mt-1 text-[9px] font-bold" style={{ color: g.c }}>{g.title}</div>
              <div className="text-[8px] text-slate-600">{g.sub}</div>
            </div>
            {i < gates.length - 1 && <span className="text-slate-700">→</span>}
          </React.Fragment>
        ))}
      </div>
    </div>
  )
}

function IntegrationsBar({ health, trend }) {
  const totals = health?.totals || {}
  const logs = health?.log_summary || {}
  const checks = health?.checks || []
  const byName = (n) => checks.find((c) => c.name === n)
  // Watchdog on the monitor itself: the sweep persists a snapshot every 5 min;
  // a stale last-snapshot means telemetry collection died (e.g. gcloud token expired).
  const lastSnap = trend?.length ? trend[trend.length - 1]?.ts : null
  const sweepFresh = lastSnap ? (Date.now() - Date.parse(lastSnap)) < 15 * 60 * 1000 : false
  const items = [
    { l: 'Firestore', ok: byName('Firestore')?.status === 'ok', msg: byName('Firestore')?.message },
    { l: 'Cloud Logging', ok: byName('Cloud Logging')?.status === 'ok', msg: `${logs.warnings ?? 0} warn · ${logs.errors ?? 0} err / ${logs.window_hours ?? 24}h` },
    { l: 'CDC ingest', ok: (totals.dead_letter_recent || 0) === 0, msg: `${totals.dead_letter_recent ?? 0} recent dead-letters` },
    { l: 'CSR resolution', ok: (totals.orphaned_recent || 0) === 0, msg: `${totals.orphaned_recent ?? 0} recent orphans` },
    { l: 'Data flow', ok: byName('Data flow')?.status === 'ok', msg: byName('Data flow')?.message },
    { l: 'Monitor sweep', ok: sweepFresh, msg: lastSnap ? `last snapshot ${timeAgo(lastSnap)}` : 'no snapshots yet' },
  ]
  const failing = checks.filter((c) => c.status === 'fail')
  return (
    <div className={`${CARD} flex flex-wrap items-center gap-x-4 gap-y-1.5 px-3 py-2`}>
      <span className="text-[9px] font-bold uppercase tracking-wide text-slate-500">System Integrations</span>
      {items.map((it) => (
        <span key={it.l} className="flex items-center gap-1.5 text-[10px]" title={it.msg}>
          <span className={`h-1.5 w-1.5 rounded-full ${it.ok ? 'bg-emerald-500' : 'animate-pulse bg-red-500'}`} />
          <span className={it.ok ? 'text-slate-400' : 'font-semibold text-red-400'}>{it.l}</span>
        </span>
      ))}
      {failing.map((c) => (
        <span key={c.name} className="flex items-center gap-1 rounded bg-red-950/60 px-1.5 py-0.5 text-[9px] font-semibold text-red-400" title={c.message}>
          <HeartPulse size={9} /> {c.name}
        </span>
      ))}
      <span className="ml-auto flex items-center gap-3 text-[9px] text-slate-500">
        <span className="flex items-center gap-1">
          <Activity size={9} className={health?.status === 'green' ? 'text-emerald-500' : 'text-red-500'} />
          {health?.status === 'green' ? 'all systems nominal' : `${(health?.totals?.escalations || 0)} escalated · ${(health?.totals?.checks_failing || 0)} checks failing`}
        </span>
        <span>last poll {health?.polled_at ? timeAgo(health.polled_at) : '—'}{health?.stale ? ' · syncing…' : ''}</span>
      </span>
    </div>
  )
}

// ── Main ─────────────────────────────────────────────────────────────────────

export default function OscaMonitorPanel({ actions = null }) {
  const [health, setHealth] = useState(null)
  const [problems, setProblems] = useState([])
  const [trend, setTrend] = useState([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [envFilter, setEnvFilter] = useState(null)  // env name, or null = all envs
  const staleRetryRef = useRef(null)
  const toast = useToast()

  const load = useCallback(async (force = false) => {
    try {
      const q = force ? '?force=true' : ''
      const [h, p, t] = await Promise.all([
        fetch(`/api/osca/health${q}`).then((r) => r.json()),
        fetch(`/api/osca/problems?status=open${force ? '&force=true' : ''}`).then((r) => r.json()),
        fetch('/api/osca/trend?limit=96').then((r) => r.json()),
      ])
      setHealth(h)
      setProblems(p.problems || [])
      setTrend(t.snapshots || [])
      if (h?.stale && !staleRetryRef.current) {
        staleRetryRef.current = setTimeout(() => { staleRetryRef.current = null; load() }, 9000)
      }
    } catch {
      toast('Failed to load mission control', 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  const pollNow = useCallback(async () => {
    setBusy(true)
    try {
      await fetch('/api/osca/refresh', { method: 'POST' })
      await load(true)
      toast('Pipeline polled', 'success')
    } catch {
      toast('Poll failed', 'error')
    } finally {
      setBusy(false)
    }
  }, [load])

  const ack = useCallback(async (key, status) => {
    setProblems((ps) => ps.map((p) => (p.key === key ? { ...p, status } : p))
      .filter((p) => p.status !== 'resolved'))
    try {
      await fetch(`/api/osca/problems/${encodeURIComponent(key)}/ack`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      })
    } catch {
      toast('Update failed', 'error')
      load()
    }
  }, [load])

  useEffect(() => {
    load()
    const t = setInterval(() => load(), 30000)
    return () => { clearInterval(t); if (staleRetryRef.current) clearTimeout(staleRetryRef.current) }
  }, [load])

  const totals = health?.totals || {}
  const meta = STATUS_META[health?.status] || STATUS_META.unknown
  const sparkOf = (key) => trend.map((s) => s[key] ?? 0)
  const hitlCount = health?.throughput?.run_status?.hitl || 0

  return (
    <section className="overflow-hidden rounded-2xl border border-[#16233b] bg-[#05090f] shadow-[var(--shadow)]">
      {/* Mission-control header */}
      <header className="flex items-center justify-between border-b border-[#16233b] px-4 py-2.5">
        <div className="flex items-baseline gap-3">
          <h2 className="text-[14px] font-black tracking-wide text-slate-100">PIPELINE AGENT</h2>
          <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-slate-500">Mission Control</span>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right leading-tight">
            <div className="text-[8px] uppercase tracking-wide text-slate-600">Environment</div>
            <div className="text-[11px] font-bold text-slate-300">{health?.environment || 'uat+dev'}</div>
          </div>
          <span className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[10px] font-black tracking-wide`}
            style={{ background: meta.color + '1a', color: meta.color, border: `1px solid ${meta.color}55` }}>
            <span className="h-1.5 w-1.5 animate-pulse rounded-full" style={{ background: meta.color }} />
            LIVE
          </span>
          <UtcClock />
          <button onClick={pollNow} disabled={busy}
            className="flex items-center gap-1.5 rounded-lg border border-[#1e3a5f] bg-[#0a1220] px-2.5 py-1.5 text-[10px] font-semibold text-slate-300 hover:border-[#2d4a75] disabled:opacity-50">
            <RefreshCw size={11} className={busy ? 'animate-spin' : ''} />
            {busy ? 'Polling…' : 'Poll now'}
          </button>
          {actions}
        </div>
      </header>

      {loading ? (
        <div className="p-4"><SkeletonRows n={8} /></div>
      ) : (
        <div className="space-y-2.5 p-2.5">
          {health?.ok === false && (
            <div className="flex items-start gap-2 rounded-lg border border-amber-800 bg-amber-950/40 px-3 py-2 text-[12px] text-amber-200">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <div>
                <div className="font-medium">Can't reach the pipeline telemetry</div>
                <div className="text-amber-300/80">{health.error}</div>
              </div>
            </div>
          )}

          {/* Per-environment health — dev/uat/prod side by side, whichever are
              configured (osca.environments in policy.yaml). */}
          {Object.keys(totals.by_env || {}).length > 0 && (
            <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${Object.keys(totals.by_env).length}, minmax(0, 1fr))` }}>
              {Object.entries(totals.by_env).map(([name, e]) => {
                const em = STATUS_META[e.status] || STATUS_META.unknown
                const selected = envFilter === name
                return (
                  <button key={name}
                    onClick={() => setEnvFilter(selected ? null : name)}
                    title={selected ? `Showing ${name} alerts only — click to clear` : `Filter Alerts to ${name}`}
                    className={`${CARD} flex items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-[#0d1830]`}
                    style={{ borderColor: em.color + (selected ? 'ff' : '55'),
                             boxShadow: selected ? `0 0 0 1px ${em.color}` : 'none' }}>
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full"
                      style={{ background: em.color + '22', border: `1.5px solid ${em.color}` }}>
                      <span className="h-2 w-2 rounded-full" style={{ background: em.color }} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="text-[12px] font-bold uppercase tracking-wide text-slate-200">{name}</span>
                        <span className="text-[9px] font-semibold uppercase tracking-wide" style={{ color: em.color }}>{em.label}</span>
                      </div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[10px] text-slate-500">
                        <span>{e.active_clients} active</span>
                        {e.escalations > 0 && <span className="font-semibold text-red-400">{e.escalations} escalated</span>}
                        {e.checks_failing > 0 && <span className="font-semibold text-red-400">{e.checks_failing} checks failing</span>}
                        {e.stalled > 0 && <span className="font-semibold text-amber-400">{e.stalled} stalled</span>}
                        {e.orphaned > 0 && <span className="text-amber-400">{e.orphaned} orphaned</span>}
                        {e.dead_letter > 0 && <span className="text-amber-400">{e.dead_letter} dead-letter</span>}
                        <span className="text-slate-600">last activity {e.last_activity_at ? timeAgo(e.last_activity_at) : '—'}</span>
                      </div>
                    </div>
                  </button>
                )
              })}
            </div>
          )}

          {/* KPI strip */}
          <div className="grid grid-cols-4 gap-2 xl:grid-cols-10">
            {/* System health hero */}
            <div className={`${CARD} col-span-2 flex items-center gap-3 px-3 py-2`}
              style={{ borderColor: meta.color + '66', background: `linear-gradient(135deg, ${meta.color}14, #0a1220)` }}>
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full"
                style={{ background: meta.color + '22', border: `1.5px solid ${meta.color}` }}>
                {health?.status === 'green'
                  ? <ShieldCheck size={22} style={{ color: meta.color }} />
                  : <ShieldAlert size={22} style={{ color: meta.color }} className="animate-pulse" />}
              </div>
              <div>
                <div className="text-[8px] font-bold uppercase tracking-[0.15em]" style={{ color: meta.color }}>System Health</div>
                <div className="text-xl font-black leading-6" style={{ color: meta.color }}>{meta.label}</div>
                <div className="text-[9px] text-slate-500">{meta.sub}</div>
              </div>
            </div>
            <Kpi label="Active cases" value={totals.active_clients ?? '—'} spark={sparkOf('active_clients')} sparkColor="#38bdf8" />
            <Kpi label="Escalations" value={totals.escalations ?? 0} color={totals.escalations > 0 ? '#ef4444' : '#e2e8f0'} spark={sparkOf('escalations')} sparkColor="#ef4444" alert={totals.escalations > 0} />
            <Kpi label="Human review" value={hitlCount} color="#f59e0b" spark={sparkOf('paused')} sparkColor="#f59e0b" />
            <Kpi label="Blocked" value={totals.blocked ?? 0} color={totals.blocked > 0 ? '#ef4444' : '#e2e8f0'} spark={sparkOf('blocked')} sparkColor="#ef4444" alert={totals.blocked > 0} />
            <Kpi label="Dead-letters" value={totals.dead_letter_recent ?? 0} color={totals.dead_letter_recent > 0 ? '#ef4444' : '#e2e8f0'} spark={sparkOf('dead_letter')} sparkColor="#ef4444" alert={totals.dead_letter_recent > 0} />
            <Kpi label="Orphaned" value={totals.orphaned_recent ?? 0} color="#f59e0b" spark={sparkOf('orphaned')} sparkColor="#f59e0b" />
            <Kpi label="SLA risks" value={totals.sla_risks ?? 0} color={totals.sla_risks > 0 ? '#f97316' : '#e2e8f0'} alert={totals.sla_risks > 0} />
            <Kpi label="Log warn / err" value={`${totals.log_warnings ?? 0}/${totals.log_errors ?? 0}`} color="#94a3b8" />
          </div>

          {/* Main grid */}
          <div className="grid grid-cols-1 gap-2.5 xl:grid-cols-[3fr_1.15fr]">
            <div className="min-w-0 space-y-2.5">
              <LivePipeline health={health} />
              <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-3">
                <Bottlenecks problems={problems} />
                <TrendChart trend={trend} />
                <RunMix health={health} />
              </div>
              <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-[1.3fr_1fr]">
                <CustomerActions health={health} />
                <UpcomingTriggers health={health} />
              </div>
              <FollowUpTimeline />
            </div>
            <div className="min-w-0 space-y-2.5">
              <AlertsPanel problems={problems} onAck={ack} envFilter={envFilter}
                onClearEnvFilter={() => setEnvFilter(null)} />
              <ReviewQueue problems={problems} />
              <ActivityFeed health={health} />
            </div>
          </div>

          <IntegrationsBar health={health} trend={trend} />
        </div>
      )}
    </section>
  )
}
