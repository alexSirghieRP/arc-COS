import React, { useEffect, useState } from 'react'
import { Panel } from '../App.jsx'
import { Pencil, Save, X, FileDown, RefreshCw, Diamond, Wand2 } from 'lucide-react'
import { post } from '../api.js'

const STATUS = {
  done: { bg: '#36B37E', fg: '#06281a', label: 'Done / landed' },
  build: { bg: '#4C9AFF', fg: '#08233f', label: 'Build' },
  spike: { bg: '#FFAB00', fg: '#3d2a00', label: 'Spike / prep / hardening' },
  tbd: { bg: '#C1C7D0', fg: '#2b2f36', label: 'Unscoped / TBD' },
}
const MILESTONE = '#a78bfa'
const GATE = '#FF5630'

const SECTION_TINT = ['#1e293b', '#1f2937', '#27272a', '#292524']

export default function RoadmapPanel() {
  const [rm, setRm] = useState(null)
  const [saved, setSaved] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = () =>
    fetch('/api/roadmap').then((r) => r.json()).then((d) => { setRm(d.roadmap); setSaved(d.saved) }).catch(() => {})
  useEffect(() => { load() }, [])

  if (!rm) return <Panel title="Roadmap"><div className="text-sm text-zinc-500">loading…</div></Panel>

  const weeks = rm.weeks || []
  const hereIdx = weeks.indexOf(rm.here_week)
  const wIdx = Object.fromEntries(weeks.map((w, i) => [w, i]))
  // grid: col 1 = lane label (220px), then one col per week
  const gridCols = { display: 'grid', gridTemplateColumns: `220px repeat(${weeks.length}, minmax(34px, 1fr))` }

  const generate = async () => {
    setBusy(true); setErr(null)
    try { const d = await post('/api/roadmap/generate'); setRm(d.roadmap); setSaved(true) }
    catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const startEdit = () => { setDraft(JSON.stringify(rm, null, 2)); setErr(null); setEditing(true) }
  const saveEdit = async () => {
    setErr(null)
    let parsed
    try { parsed = JSON.parse(draft) } catch (e) { return setErr('Invalid JSON: ' + e.message) }
    setBusy(true)
    try { await post('/api/roadmap', { roadmap: parsed }); setRm(parsed); setSaved(true); setEditing(false) }
    catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }

  const Bar = ({ b }) => {
    const s = wIdx[b.start], e = wIdx[b.end]
    if (s == null || e == null) return null
    const st = STATUS[b.status] || STATUS.tbd
    return (
      // bars live in the week-only sub-grid (col 1 = first week), so week index
      // s maps to grid-column s+1 .. e+2 (1-based). No label-column offset here.
      <div
        style={{ gridColumn: `${s + 1} / ${e + 2}`, background: st.bg, color: st.fg }}
        className="my-0.5 truncate rounded px-1.5 py-1 text-center text-[10px] font-medium"
        title={b.label}
      >
        {b.label}
      </div>
    )
  }

  return (
    <Panel
      title="Roadmap"
      badge={saved ? 'saved' : 'draft'}
      actions={
        <div className="flex items-center gap-1.5">
          <button onClick={load} className="press rounded-lg bg-zinc-800 p-1.5 text-zinc-400 hover:bg-zinc-700" title="Reload">
            <RefreshCw size={13} />
          </button>
          {editing ? (
            <>
              <button onClick={saveEdit} disabled={busy} className="press flex items-center gap-1.5 rounded-lg bg-green-900 px-2.5 py-1 text-[11px] text-green-200 hover:bg-green-800 disabled:opacity-40">
                <Save size={12} /> Save
              </button>
              <button onClick={() => setEditing(false)} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300">
                <X size={12} /> Cancel
              </button>
            </>
          ) : (
            <>
              <button onClick={generate} disabled={busy} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700 disabled:opacity-40">
                <Wand2 size={12} className={busy ? 'animate-pulse' : ''} /> {busy ? 'Drafting…' : 'Draft from data'}
              </button>
              <button onClick={startEdit} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700">
                <Pencil size={12} /> Edit
              </button>
              <a href="/api/roadmap/pdf" target="_blank" rel="noreferrer" className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700">
                <FileDown size={12} /> Export PDF
              </a>
            </>
          )}
        </div>
      }
    >
      {editing ? (
        <div>
          {err && <div className="mb-2 rounded-lg bg-red-950 px-3 py-2 text-[11px] text-red-300">{err}</div>}
          <p className="mb-1.5 text-[11px] text-zinc-500">Edit the roadmap (HITL). Add/adjust lanes, bars, milestones, callouts, then Save.</p>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className="h-[60vh] w-full rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-[11px] text-zinc-300 focus:border-zinc-600 focus:outline-none"
          />
        </div>
      ) : (
        <div className="overflow-x-auto">
          {/* header */}
          <h2 className="text-base font-semibold text-zinc-100">{rm.title}</h2>
          {rm.subtitle && <p className="mb-2 text-xs text-zinc-400">{rm.subtitle}</p>}

          {/* callouts */}
          <div className="mb-3 grid gap-2 sm:grid-cols-2">
            {rm.callouts?.what_changed?.length > 0 && (
              <Callout title="What changed" tone="border-violet-800 bg-violet-950/40 text-violet-200" items={rm.callouts.what_changed} />
            )}
            {rm.callouts?.rides_on?.length > 0 && (
              <Callout title="What it rides on" tone="border-amber-800 bg-amber-950/40 text-amber-200" items={rm.callouts.rides_on} />
            )}
          </div>

          {/* month + week header */}
          <div style={gridCols} className="min-w-[760px] text-[10px] text-zinc-500">
            <div />
            {(rm.months || []).map((m, i) => (
              <div key={i} style={{ gridColumn: `span ${m.span}` }} className="border-b border-zinc-800 pb-0.5 text-center font-medium uppercase tracking-wide text-zinc-400">
                {m.label}
              </div>
            ))}
          </div>
          <div style={gridCols} className="min-w-[760px] text-[10px]">
            <div />
            {weeks.map((w, i) => (
              <div key={w} className={`text-center ${i === hereIdx ? 'rounded-t bg-rose-950 font-semibold text-rose-300' : 'text-zinc-600'}`}>
                {i === hereIdx ? '▼' : ''}{w}
              </div>
            ))}
          </div>

          {/* swimlanes */}
          <div className="relative min-w-[760px]">
            {(rm.sections || []).map((sec, si) => (
              <div key={si} className="mt-1">
                <div style={{ background: SECTION_TINT[si % SECTION_TINT.length] }} className="flex items-center gap-2 rounded px-2 py-1">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-zinc-200">{sec.name}</span>
                  {sec.owner && <span className="text-[10px] text-zinc-500">· {sec.owner}</span>}
                </div>
                {(sec.lanes || []).map((lane, li) => (
                  <div key={li} style={gridCols} className="items-center">
                    <div className="truncate py-1 pr-2 text-[11px] text-zinc-400" title={lane.name}>{lane.name}</div>
                    {/* bar track: render bars into the same grid row */}
                    <div style={{ gridColumn: `2 / ${weeks.length + 2}`, ...{ display: 'grid', gridTemplateColumns: `repeat(${weeks.length}, minmax(34px, 1fr))` } }}>
                      {(lane.bars || []).map((b, bi) => <Bar key={bi} b={b} />)}
                    </div>
                  </div>
                ))}
              </div>
            ))}

            {/* milestones */}
            {rm.milestones?.length > 0 && (
              <div style={gridCols} className="mt-1 items-center border-t border-zinc-800 pt-1">
                <div className="py-1 pr-2 text-[11px] italic text-zinc-500">Milestones</div>
                {weeks.map((w, i) => {
                  const ms = (rm.milestones || []).filter((m) => m.week === w)
                  return (
                    <div key={w} className="flex justify-center" title={ms.map((m) => m.label).join('; ')}>
                      {ms.length > 0 && (
                        <Diamond size={12} style={{ color: ms.some((m) => m.gate) ? GATE : MILESTONE }} fill="currentColor" />
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            {/* today vertical line */}
            {hereIdx >= 0 && (
              <div
                className="pointer-events-none absolute top-0 bottom-0 border-l-2 border-dashed border-rose-500/70"
                style={{ left: `calc(220px + (100% - 220px) * ${(hereIdx + 0.5) / weeks.length})` }}
              />
            )}
          </div>

          {/* legend */}
          <div className="mt-3 flex flex-wrap items-center gap-2 text-[10px]">
            {Object.values(STATUS).map((s) => (
              <span key={s.label} style={{ background: s.bg, color: s.fg }} className="rounded px-2 py-0.5">{s.label}</span>
            ))}
            <span className="flex items-center gap-1" style={{ color: MILESTONE }}><Diamond size={11} fill="currentColor" /> Milestone</span>
            <span className="flex items-center gap-1" style={{ color: GATE }}><Diamond size={11} fill="currentColor" /> Hard gate</span>
          </div>

          {/* dependency / sections table */}
          <table className="mt-3 w-full border-collapse text-[11px]">
            <thead>
              <tr className="border-b border-zinc-800 text-left text-zinc-500">
                <th className="py-1 pr-3 font-medium uppercase tracking-wide">Lane / workstream</th>
                <th className="py-1 pr-3 font-medium uppercase tracking-wide">Owner</th>
                <th className="py-1 font-medium uppercase tracking-wide">Notes</th>
              </tr>
            </thead>
            <tbody>
              {(rm.sections || []).map((sec, i) => (
                <tr key={i} className="border-b border-zinc-900">
                  <td className="py-1 pr-3 text-zinc-300">{sec.name}</td>
                  <td className="py-1 pr-3 text-zinc-500">{sec.owner || '—'}</td>
                  <td className="py-1 text-zinc-500">{(sec.lanes || []).map((l) => l.name).join(' · ')}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {rm.footer && <p className="mt-2 text-[10px] italic text-zinc-600">{rm.footer}</p>}
        </div>
      )}
    </Panel>
  )
}

function Callout({ title, tone, items }) {
  return (
    <div className={`rounded-lg border px-3 py-2 ${tone}`}>
      <div className="mb-1 text-[11px] font-semibold">{title}</div>
      <ul className="list-disc space-y-0.5 pl-4 text-[11px] opacity-90">
        {items.map((it, i) => <li key={i}>{it}</li>)}
      </ul>
    </div>
  )
}
