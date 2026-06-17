import React, { useEffect, useState } from 'react'
import { Sun, Moon, RefreshCw, Power, Activity, ChevronDown } from 'lucide-react'
import { setSetting } from '../api.js'

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
        className={`relative h-[18px] w-8 rounded-full transition-colors ${on ? onColor : 'bg-zinc-700'}`}
      >
        <span
          className={`absolute top-0.5 h-[14px] w-[14px] rounded-full bg-white shadow transition-transform duration-200 ${
            on ? 'translate-x-[15px]' : 'translate-x-0.5'
          }`}
        />
      </span>
      {label}
    </button>
  )
}

export default function StatusBar({ board, refresh }) {
  const [connectors, setConnectors] = useState({})
  const [cost, setCost] = useState(null)
  const [costBusy, setCostBusy] = useState(false)
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
    await setSetting(key, value)
    refresh()
  }

  const presence = board.presence.availability || 'unknown'

  return (
    <header className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-zinc-800 bg-zinc-900 px-4 py-2.5 shadow-[var(--shadow)]">
      {/* Brand */}
      <div className="flex items-center gap-2">
        <span className="grid h-7 w-7 place-items-center rounded-lg bg-[var(--accent-fill)] text-[var(--accent)]">
          <Activity size={16} strokeWidth={2.5} />
        </span>
        <span className="text-[15px] font-semibold tracking-tight text-zinc-100">Chief of Staff</span>
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

      <Toggle on={board.dry_run} onClick={() => flip('dry_run', board.dry_run ? 'false' : 'true')} label={board.dry_run ? 'Dry run' : 'Live'} />

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
