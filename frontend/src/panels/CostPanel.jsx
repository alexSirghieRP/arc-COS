import React, { useEffect, useState } from 'react'
import { RefreshCw, Cpu, Tags, TrendingUp, ChevronLeft, ChevronRight, FileDown, Receipt } from 'lucide-react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'
import { CountUp, PanelLoading } from '../ui.jsx'

const usd = (n) => `$${(n ?? 0).toFixed(2)}`
const usd4 = (n) => `$${(n ?? 0).toFixed(4)}`
const tok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}k` : `${n || 0}`)

// friendly labels + colors per purpose
const LABELS = {
  triage: { name: 'Triage', color: '#a78bfa' },
  pr_review: { name: 'PR review', color: '#818cf8' },
  weekly_status: { name: 'Weekly status', color: '#fb923c' },
  roadmap: { name: 'Roadmap', color: '#22d3ee' },
  morning_brief: { name: 'Morning brief', color: '#fbbf24' },
  chieff: { name: '@CoS replies', color: '#34d399' },
  pre_meeting_brief: { name: 'Pre-meeting brief', color: '#f472b6' },
  draft_retry: { name: 'Draft retry', color: '#c084fc' },
  agent: { name: 'Other', color: '#94a3b8' },
}
const lbl = (k) => LABELS[k] || { name: k, color: '#94a3b8' }

export default function CostPanel() {
  const [d, setD] = useState(null)
  const [busy, setBusy] = useState(false)
  const [month, setMonth] = useState(null)  // null = current month
  const [granularity, setGranularity] = useState('day')  // 'day' | 'week'

  const load = () => fetch(`/api/cost/cos${month ? `?month=${month}` : ''}`)
    .then((r) => r.json()).then(setD).catch(() => {})
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [month])
  const refresh = async () => { setBusy(true); try { await load() } finally { setBusy(false) } }

  if (!d) return <Panel title="Cost"><PanelLoading /></Panel>

  const trend = granularity === 'week'
    ? (d.weekly || []).map((w) => ({ key: w.week, cost: w.cost, tokens: w.input + w.output,
        tick: w.label.replace('Week of ', ''), tip: `${w.label}: ${usd4(w.cost)} · ${tok(w.input + w.output)} tok (${w.days}d)` }))
    : (d.daily || []).map((x) => ({ key: x.day, cost: x.cost, tokens: x.input + x.output,
        tick: x.day.slice(5), tip: `${x.day}: ${usd4(x.cost)} · ${tok(x.input + x.output)} tok` }))
  const maxTrend = Math.max(0.0001, ...trend.map((x) => x.cost))
  const labelTotal = Math.max(0.0001, d.metered_total || 0)

  const months = d.months || []
  const idx = months.indexOf(d.month_key)
  const canOlder = idx >= 0 && idx < months.length - 1
  const canNewer = idx > 0

  return (
    <Panel
      title="CoS cost"
      badge={`${d.days || 0}d`}
      actions={
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 rounded-lg bg-zinc-800 px-1 py-0.5 text-[11px] text-zinc-300">
            <button onClick={() => canOlder && setMonth(months[idx + 1])} disabled={!canOlder}
              title="Previous month"
              className="press rounded p-0.5 hover:bg-zinc-700 disabled:opacity-30">
              <ChevronLeft size={13} />
            </button>
            <span className="min-w-[64px] text-center tabular-nums">{d.month_label}</span>
            <button onClick={() => canNewer && setMonth(months[idx - 1])} disabled={!canNewer}
              title="Next month"
              className="press rounded p-0.5 hover:bg-zinc-700 disabled:opacity-30">
              <ChevronRight size={13} />
            </button>
          </div>
          <button onClick={refresh} disabled={busy} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700 disabled:opacity-40">
            <RefreshCw size={12} className={busy ? 'animate-spin' : ''} /> Refresh
          </button>
          <a
            href={`/api/cost/cos/report.pdf${month ? `?month=${month}` : ''}`}
            target="_blank" rel="noreferrer"
            title="Open this month's cost report as a PDF"
            className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"
          >
            <FileDown size={12} /> Report
          </a>
          <a
            href={`/api/cost/cos/report.csv${month ? `?month=${month}` : ''}`}
            title="Download this month's cost breakdown as CSV"
            className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"
          >
            <Receipt size={12} /> CSV
          </a>
        </div>
      }
    >
      <div className="space-y-4">
        {/* headline numbers */}
        {(() => {
          const now = new Date()
          const dayOfMonth = now.getDate()
          const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()
          const daysLeft = daysInMonth - dayOfMonth
          const projected = d.is_current && dayOfMonth > 0 ? (d.month / dayOfMonth) * daysInMonth : null
          const projDelta = projected != null ? projected - d.month : null
          return (
            <div className="flex flex-wrap gap-3">
              {d.is_current && <Stat label="Today" num={d.today} fmt={usd} tone="text-violet-200" />}
              <Stat label={`${d.is_current ? 'Month to date' : 'Month total'} (${d.month_label})`} num={d.month} fmt={usd} tone="text-zinc-100" />
              {projected != null && (
                <Stat
                  label={`Projected month-end (${daysLeft}d left)`}
                  num={projected}
                  fmt={usd}
                  sub={projDelta ? `+${usd(projDelta)} remaining` : undefined}
                  tone={projected > 10 ? 'text-amber-300' : 'text-emerald-300'}
                />
              )}
              <Stat label="Active days" num={d.days || 0} tone="text-zinc-300" />
              <Stat label="Avg / day" num={d.days ? d.month / d.days : 0} fmt={usd} tone="text-zinc-300" />
            </div>
          )
        })()}


        {/* daily/weekly trend */}
        <Section
          icon={TrendingUp}
          title={granularity === 'week' ? 'Weekly spend' : 'Daily spend'}
          actions={
            <div className="flex items-center gap-0.5 rounded-lg bg-zinc-800 p-0.5 text-[10px]">
              {['day', 'week'].map((g) => (
                <button
                  key={g}
                  onClick={() => setGranularity(g)}
                  className={`press rounded px-2 py-0.5 capitalize ${
                    granularity === g ? 'bg-zinc-700 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300'
                  }`}
                >
                  {g}
                </button>
              ))}
            </div>
          }
        >
          <div className="flex items-end gap-1.5" style={{ height: 96 }}>
            {trend.map((x) => (
              <div key={x.key} className="flex flex-1 flex-col items-center justify-end" title={x.tip}>
                <div className="text-[9px] text-zinc-500">{usd(x.cost)}</div>
                <div className="w-full rounded-t bg-violet-500/70" style={{ height: `${Math.max(2, (x.cost / maxTrend) * 72)}px` }} />
                <div className="mt-1 text-[9px] text-zinc-600">{x.tick}</div>
              </div>
            ))}
            {trend.length === 0 && <Empty text="No usage logged yet" />}
          </div>
        </Section>

        {/* by purpose */}
        <Section icon={Tags} title="Where it goes — by purpose"
          note={`covers ${usd(d.metered_total)} metered since per-call tracking began`}>
          {(d.by_label || []).length === 0 ? (
            <Empty text="No metered calls yet — runs as sweeps fire" />
          ) : (
            <div className="space-y-1.5">
              {d.by_label.map((b) => {
                const L = lbl(b.label)
                return (
                  <div key={b.label} className="flex items-center gap-2 text-[11px]">
                    <span className="w-28 shrink-0 text-zinc-300">{L.name}</span>
                    <div className="h-3.5 flex-1 overflow-hidden rounded bg-zinc-800">
                      <div className="h-full rounded" style={{ width: `${(b.cost / labelTotal) * 100}%`, background: L.color }} />
                    </div>
                    <span className="w-16 shrink-0 text-right tabular-nums text-zinc-200">{usd4(b.cost)}</span>
                    <span className="w-20 shrink-0 text-right text-zinc-600">{b.calls} calls · {tok(b.input + b.output)}</span>
                  </div>
                )
              })}
            </div>
          )}
        </Section>

        {/* by model */}
        <Section icon={Cpu} title="By model">
          {(d.by_model || []).length === 0 ? (
            <Empty text="No metered calls yet" />
          ) : (
            <div className="space-y-1">
              {d.by_model.map((m) => (
                <div key={m.model} className="flex items-center justify-between text-[11px]">
                  <span className="text-zinc-300">{m.model}</span>
                  <span className="text-zinc-500">{m.calls} calls · <span className="tabular-nums text-zinc-200">{usd4(m.cost)}</span></span>
                </div>
              ))}
            </div>
          )}
        </Section>

        {/* levers */}
        <div className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-[11px] text-zinc-400">
          <span className="font-medium text-zinc-300">Levers to reduce cost</span> — biggest first:
          triage runs every <code className="text-zinc-300">teams_sweep_minutes</code> (3m) on Haiku;
          PR review &amp; weekly/roadmap use Sonnet (<code className="text-zinc-300">pr_review.model</code> /
          <code className="text-zinc-300"> weekly_status.model</code>). Widen sweep intervals or downgrade
          models in <code className="text-zinc-300">policy.yaml</code> to cut spend.
        </div>
      </div>
    </Panel>
  )
}

function Stat({ label, num, fmt, tone, sub }) {
  return (
    <div className="lift rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-zinc-600">{label}</div>
      <div className={`text-lg font-semibold tabular-nums ${tone}`}>
        <CountUp value={num} format={fmt} />
      </div>
      {sub && <div className="text-[10px] text-zinc-600 mt-0.5">{sub}</div>}
    </div>
  )
}

function Section({ icon: Icon, title, note, actions, children }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-zinc-400">
        <Icon size={13} /> {title}
        {note && <span className="font-normal text-zinc-600">· {note}</span>}
        {actions && <span className="ml-auto">{actions}</span>}
      </div>
      {children}
    </div>
  )
}
