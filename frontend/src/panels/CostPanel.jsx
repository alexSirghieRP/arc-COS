import React, { useEffect, useState } from 'react'
import { RefreshCw, Cpu, Tags, TrendingUp } from 'lucide-react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'

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
  agent: { name: 'Other', color: '#94a3b8' },
}
const lbl = (k) => LABELS[k] || { name: k, color: '#94a3b8' }

export default function CostPanel() {
  const [d, setD] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = () => fetch('/api/cost/cos').then((r) => r.json()).then(setD).catch(() => {})
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [])
  const refresh = async () => { setBusy(true); try { await load() } finally { setBusy(false) } }

  if (!d) return <Panel title="Cost"><div className="text-sm text-zinc-500">loading…</div></Panel>

  const maxDay = Math.max(0.0001, ...(d.daily || []).map((x) => x.cost))
  const labelTotal = Math.max(0.0001, d.metered_total || 0)

  return (
    <Panel
      title="CoS cost"
      badge={`${d.days || 0}d`}
      actions={
        <button onClick={refresh} disabled={busy} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700 disabled:opacity-40">
          <RefreshCw size={12} className={busy ? 'animate-spin' : ''} /> Refresh
        </button>
      }
    >
      <div className="space-y-4">
        {/* headline numbers */}
        <div className="flex flex-wrap gap-3">
          <Stat label="Today" value={usd(d.today)} tone="text-violet-200" />
          <Stat label={`Month to date (${d.month_label})`} value={usd(d.month)} tone="text-zinc-100" />
          <Stat label="Active days" value={d.days || 0} tone="text-zinc-300" />
          <Stat label="Avg / day" value={usd(d.days ? d.month / d.days : 0)} tone="text-zinc-300" />
        </div>

        {/* daily trend */}
        <Section icon={TrendingUp} title="Daily spend">
          <div className="flex items-end gap-1.5" style={{ height: 96 }}>
            {(d.daily || []).map((x) => (
              <div key={x.day} className="flex flex-1 flex-col items-center justify-end" title={`${x.day}: ${usd4(x.cost)} · ${tok(x.input + x.output)} tok`}>
                <div className="text-[9px] text-zinc-500">{usd(x.cost)}</div>
                <div className="w-full rounded-t bg-violet-500/70" style={{ height: `${Math.max(2, (x.cost / maxDay) * 72)}px` }} />
                <div className="mt-1 text-[9px] text-zinc-600">{x.day.slice(5)}</div>
              </div>
            ))}
            {(d.daily || []).length === 0 && <Empty text="No usage logged yet" />}
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

function Stat({ label, value, tone }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-zinc-600">{label}</div>
      <div className={`text-lg font-semibold tabular-nums ${tone}`}>{value}</div>
    </div>
  )
}

function Section({ icon: Icon, title, note, children }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-zinc-400">
        <Icon size={13} /> {title}
        {note && <span className="font-normal text-zinc-600">· {note}</span>}
      </div>
      {children}
    </div>
  )
}
