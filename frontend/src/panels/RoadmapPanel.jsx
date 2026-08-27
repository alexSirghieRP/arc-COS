import React, { useEffect, useState } from 'react'
import { Pencil, Save, X, FileDown, RefreshCw, Diamond, Wand2, Plus, Trash2, Braces } from 'lucide-react'
import { Panel } from '../App.jsx'
import { post, editLock } from '../api.js'
import { PanelLoading } from '../ui.jsx'

const STATUS = {
  done: { bg: '#36B37E', fg: '#06281a', label: 'Done / landed' },
  build: { bg: '#4C9AFF', fg: '#08233f', label: 'Build' },
  spike: { bg: '#FFAB00', fg: '#3d2a00', label: 'Spike / prep / hardening' },
  tbd: { bg: '#C1C7D0', fg: '#2b2f36', label: 'Unscoped / TBD' },
}
const ORDER = ['done', 'build', 'spike', 'tbd']
const cycle = (k) => ORDER[(ORDER.indexOf(k) + 1) % ORDER.length]
const MILESTONE = '#a78bfa'
const GATE = '#FF5630'
const SECTION_TINT = ['#1e293b', '#1f2937', '#27272a', '#292524']
const clone = (o) => JSON.parse(JSON.stringify(o || {}))

// Editable rows are keyed by array index below because the data has no id
// field of its own; splice() then shifts the DOM/focus of a later row into an
// earlier one's slot. Backfill a stable `_k` once (survives the JSON clone()
// `upd` uses) so React tracks rows by identity instead of position.
let _nextKey = 1
const newKey = () => `k${_nextKey++}`
const withKeys = (data) => {
  for (const sec of data.sections || []) {
    sec._k ||= newKey()
    for (const lane of sec.lanes || []) {
      lane._k ||= newKey()
      for (const bar of lane.bars || []) bar._k ||= newKey()
    }
  }
  for (const m of data.milestones || []) m._k ||= newKey()
  return data
}

// module-level (stable identity so inputs keep focus)
function Txt({ value, onChange, ph, cls = '' }) {
  return <input value={value || ''} onChange={(e) => onChange(e.target.value)} placeholder={ph}
    className={`rounded bg-zinc-950 px-1.5 py-0.5 text-zinc-100 ring-1 ring-zinc-700 outline-none transition-shadow focus:ring-2 focus:ring-[var(--accent)] ${cls}`} />
}
function WeekSel({ weeks, value, onChange }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}
      className="rounded bg-zinc-950 px-1 py-0.5 text-[10px] text-zinc-200 ring-1 ring-zinc-700 outline-none transition-shadow focus:ring-2 focus:ring-[var(--accent)]">
      {weeks.map((w) => <option key={w} value={w}>{w}</option>)}
    </select>
  )
}

export default function RoadmapPanel() {
  const [rm, setRm] = useState(null)
  const [saved, setSaved] = useState(false)
  const [mode, setMode] = useState('view')   // view | edit | json
  const [d, setD] = useState(null)           // working object (inline edit)
  const [jsonText, setJsonText] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = () => fetch('/api/roadmap').then((r) => r.json())
    .then((x) => { setRm(x.roadmap); setSaved(x.saved) }).catch(() => {})
  useEffect(() => {
    load(); const t = setInterval(() => { if (!editLock.active()) load() }, 30000); return () => clearInterval(t)
  }, [])

  if (!rm) return <Panel title="Roadmap"><PanelLoading /></Panel>

  const view = mode === 'view' ? rm : d
  const weeks = view.weeks || []
  const wIdx = Object.fromEntries(weeks.map((w, i) => [w, i]))
  const hereIdx = weeks.indexOf(view.here_week)
  const gridCols = { display: 'grid', gridTemplateColumns: `220px repeat(${weeks.length}, minmax(34px, 1fr))` }
  const editing = mode === 'edit'

  const generate = async () => {
    setBusy(true); setErr(null)
    try { const x = await post('/api/roadmap/generate'); setRm(x.roadmap); setSaved(true) }
    catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const startEdit = () => { editLock.acquire(); setD(withKeys(clone(rm))); setErr(null); setMode('edit') }
  const startJson = () => { editLock.acquire(); setJsonText(JSON.stringify(rm, null, 2)); setErr(null); setMode('json') }
  const cancel = () => { editLock.release(); setMode('view'); setErr(null) }
  const upd = (fn) => { const x = clone(d); fn(x); setD(x) }
  const saveInline = async () => {
    setBusy(true); setErr(null)
    try { await post('/api/roadmap', { roadmap: d }); setRm(d); setSaved(true); cancel() }
    catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const saveJson = async () => {
    setErr(null); let parsed
    try { parsed = JSON.parse(jsonText) } catch (e) { return setErr('Invalid JSON: ' + e.message) }
    setBusy(true)
    try { await post('/api/roadmap', { roadmap: parsed }); setRm(parsed); setSaved(true); cancel() }
    catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  // view-mode: click a bar to cycle its status + auto-save
  const quickCycleBar = async (si, li, bi) => {
    const prev = rm; const next = clone(rm)
    const bar = next.sections[si].lanes[li].bars[bi]; bar.status = cycle(bar.status || 'tbd')
    setRm(next); editLock.acquire()
    try { await post('/api/roadmap', { roadmap: next }) }
    catch (e) { setRm(prev); setErr(String(e)) } finally { editLock.release() }
  }

  const Bar = ({ b, onClick }) => {
    const s = wIdx[b.start], e = wIdx[b.end]
    if (s == null || e == null) return null
    const st = STATUS[b.status] || STATUS.tbd
    return (
      <div onClick={onClick} title={`${b.label} — click to change status`}
        style={{ gridColumn: `${s + 1} / ${e + 2}`, background: st.bg, color: st.fg }}
        className="my-0.5 cursor-pointer truncate rounded px-1.5 py-1 text-center text-[10px] font-medium hover:ring-2 hover:ring-white/40">
        {b.label}
      </div>
    )
  }

  const actions = mode !== 'view' ? (
    <div className="flex items-center gap-1.5">
      <button onClick={mode === 'json' ? saveJson : saveInline} disabled={busy} className="press flex items-center gap-1.5 rounded-lg bg-green-900 px-2.5 py-1 text-[11px] text-green-200 hover:bg-green-800 disabled:opacity-40"><Save size={12} /> Save</button>
      <button onClick={cancel} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"><X size={12} /> Cancel</button>
    </div>
  ) : (
    <div className="flex items-center gap-1.5">
      <button onClick={load} className="press rounded-lg bg-zinc-800 p-1.5 text-zinc-400 hover:bg-zinc-700" title="Reload"><RefreshCw size={13} /></button>
      <button onClick={generate} disabled={busy} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700 disabled:opacity-40"><Wand2 size={12} className={busy ? 'animate-pulse' : ''} /> {busy ? 'Drafting…' : 'Draft from data'}</button>
      <button onClick={startEdit} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"><Pencil size={12} /> Edit</button>
      <button onClick={startJson} className="press rounded-lg bg-zinc-800 p-1.5 text-zinc-400 hover:bg-zinc-700" title="Edit raw JSON"><Braces size={13} /></button>
      <a href="/api/roadmap/pdf" target="_blank" rel="noreferrer" className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"><FileDown size={12} /> Export PDF</a>
    </div>
  )

  return (
    <Panel title="Roadmap" badge={mode === 'view' ? (saved ? 'saved' : 'draft') : mode} actions={actions}>
      {err && <div className="mb-2 rounded-lg bg-red-950 px-3 py-2 text-[11px] text-red-300">{err}</div>}

      {mode === 'json' ? (
        <textarea value={jsonText} onChange={(e) => setJsonText(e.target.value)} spellCheck={false}
          className="h-[65vh] w-full rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-[11px] text-zinc-300 focus:border-zinc-600 focus:outline-none" />
      ) : mode === 'edit' ? (
        <EditView d={d} weeks={weeks} upd={upd} />
      ) : (
        <div className="overflow-x-auto">
          <h2 className="text-base font-semibold text-zinc-100">{rm.title}</h2>
          {rm.subtitle && <p className="mb-2 text-xs text-zinc-400">{rm.subtitle}</p>}
          <div className="mb-3 grid gap-2 sm:grid-cols-2">
            {rm.callouts?.what_changed?.length > 0 && <Callout title="What changed" tone="border-violet-800 bg-violet-950/40 text-violet-200" items={rm.callouts.what_changed} />}
            {rm.callouts?.rides_on?.length > 0 && <Callout title="What it rides on" tone="border-amber-800 bg-amber-950/40 text-amber-200" items={rm.callouts.rides_on} />}
          </div>
          <div style={gridCols} className="min-w-[760px] text-[10px] text-zinc-500">
            <div />
            {(rm.months || []).map((m, i) => (
              <div key={i} style={{ gridColumn: `span ${m.span}` }} className="border-b border-zinc-800 pb-0.5 text-center font-medium uppercase tracking-wide text-zinc-400">{m.label}</div>
            ))}
          </div>
          <div style={gridCols} className="min-w-[760px] text-[10px]">
            <div />
            {weeks.map((w, i) => (
              <div key={w} className={`text-center ${i === hereIdx ? 'rounded-t bg-rose-950 font-semibold text-rose-300' : 'text-zinc-600'}`}>{i === hereIdx ? '▼' : ''}{w}</div>
            ))}
          </div>
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
                    <div style={{ gridColumn: `2 / ${weeks.length + 2}`, display: 'grid', gridTemplateColumns: `repeat(${weeks.length}, minmax(34px, 1fr))` }}>
                      {(lane.bars || []).map((b, bi) => <Bar key={bi} b={b} onClick={() => quickCycleBar(si, li, bi)} />)}
                    </div>
                  </div>
                ))}
              </div>
            ))}
            {rm.milestones?.length > 0 && (
              <div style={gridCols} className="mt-1 items-center border-t border-zinc-800 pt-1">
                <div className="py-1 pr-2 text-[11px] italic text-zinc-500">Milestones</div>
                {weeks.map((w) => {
                  const ms = (rm.milestones || []).filter((m) => m.week === w)
                  return <div key={w} className="flex justify-center" title={ms.map((m) => m.label).join('; ')}>{ms.length > 0 && <Diamond size={12} style={{ color: ms.some((m) => m.gate) ? GATE : MILESTONE }} fill="currentColor" />}</div>
                })}
              </div>
            )}
            {hereIdx >= 0 && <div className="pointer-events-none absolute top-0 bottom-0 border-l-2 border-dashed border-rose-500/70" style={{ left: `calc(220px + (100% - 220px) * ${(hereIdx + 0.5) / weeks.length})` }} />}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 text-[10px]">
            {Object.values(STATUS).map((s) => <span key={s.label} style={{ background: s.bg, color: s.fg }} className="rounded px-2 py-0.5">{s.label}</span>)}
            <span className="flex items-center gap-1" style={{ color: MILESTONE }}><Diamond size={11} fill="currentColor" /> Milestone</span>
            <span className="flex items-center gap-1" style={{ color: GATE }}><Diamond size={11} fill="currentColor" /> Hard gate</span>
            <span className="ml-auto text-zinc-600">tip: click a bar to change its status</span>
          </div>
          {rm.footer && <p className="mt-2 text-[10px] italic text-zinc-600">{rm.footer}</p>}
        </div>
      )}
    </Panel>
  )
}

// ---- inline structured editor ----------------------------------------------
function EditView({ d, weeks, upd }) {
  const StatusDot = ({ status, onClick }) => {
    const st = STATUS[status] || STATUS.tbd
    return <button onClick={onClick} title={`${st.label} — click to cycle`} className="h-4 w-4 shrink-0 cursor-pointer rounded hover:ring-2 hover:ring-white/40" style={{ background: st.bg }} />
  }
  const List = ({ label, arr, path }) => (
    <div>
      <div className="mb-1 text-[11px] font-semibold text-zinc-400">{label}</div>
      <div className="space-y-1">
        {(arr || []).map((v, i) => (
          <div key={i} className="flex items-center gap-1">
            <Txt value={v} onChange={(nv) => upd((x) => { path(x)[i] = nv })} cls="flex-1 text-[11px]" />
            <button onClick={() => upd((x) => path(x).splice(i, 1))} className="press text-zinc-600 hover:text-red-400"><Trash2 size={12} /></button>
          </div>
        ))}
        <button onClick={() => upd((x) => { const a = path(x); a.push('') })} className="press flex items-center gap-1 text-[10px] text-zinc-400"><Plus size={10} /> add</button>
      </div>
    </div>
  )
  d.callouts = d.callouts || {}
  return (
    <div className="space-y-4 text-zinc-300">
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="text-[11px] text-zinc-500">Title<Txt value={d.title} onChange={(v) => upd((x) => { x.title = v })} cls="mt-0.5 w-full text-sm" /></label>
        <label className="text-[11px] text-zinc-500">Subtitle<Txt value={d.subtitle} onChange={(v) => upd((x) => { x.subtitle = v })} cls="mt-0.5 w-full text-xs" /></label>
        <label className="text-[11px] text-zinc-500">Current week (here)
          <select value={d.here_week} onChange={(e) => upd((x) => { x.here_week = e.target.value })} className="mt-0.5 w-full rounded bg-zinc-950 px-1.5 py-0.5 text-xs text-zinc-200 ring-1 ring-zinc-700">
            {weeks.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <List label="What changed" arr={d.callouts.what_changed} path={(x) => (x.callouts.what_changed ||= [])} />
        <List label="What it rides on" arr={d.callouts.rides_on} path={(x) => (x.callouts.rides_on ||= [])} />
      </div>

      {/* sections / lanes / bars */}
      {(d.sections || []).map((sec, si) => (
        <div key={sec._k} className="rounded-lg border border-zinc-800 p-2">
          <div className="mb-2 flex items-center gap-1.5">
            <Txt value={sec.name} onChange={(v) => upd((x) => { x.sections[si].name = v })} ph="Section" cls="flex-1 text-xs font-semibold" />
            <Txt value={sec.owner} onChange={(v) => upd((x) => { x.sections[si].owner = v })} ph="owner" cls="w-40 text-[11px]" />
            <button onClick={() => upd((x) => x.sections.splice(si, 1))} className="press text-zinc-600 hover:text-red-400" title="Remove section"><Trash2 size={13} /></button>
          </div>
          {(sec.lanes || []).map((lane, li) => (
            <div key={lane._k} className="mb-2 ml-2 border-l border-zinc-800 pl-2">
              <div className="mb-1 flex items-center gap-1.5">
                <Txt value={lane.name} onChange={(v) => upd((x) => { x.sections[si].lanes[li].name = v })} ph="Lane" cls="flex-1 text-[11px]" />
                <button onClick={() => upd((x) => x.sections[si].lanes.splice(li, 1))} className="press text-zinc-600 hover:text-red-400" title="Remove lane"><Trash2 size={12} /></button>
              </div>
              {(lane.bars || []).map((b, bi) => (
                <div key={b._k} className="mb-1 flex items-center gap-1">
                  <StatusDot status={b.status} onClick={() => upd((x) => { const bb = x.sections[si].lanes[li].bars[bi]; bb.status = cycle(bb.status || 'tbd') })} />
                  <Txt value={b.label} onChange={(v) => upd((x) => { x.sections[si].lanes[li].bars[bi].label = v })} ph="bar label" cls="flex-1 text-[10px]" />
                  <WeekSel weeks={weeks} value={b.start} onChange={(v) => upd((x) => { x.sections[si].lanes[li].bars[bi].start = v })} />
                  <span className="text-[10px] text-zinc-600">→</span>
                  <WeekSel weeks={weeks} value={b.end} onChange={(v) => upd((x) => { x.sections[si].lanes[li].bars[bi].end = v })} />
                  <button onClick={() => upd((x) => x.sections[si].lanes[li].bars.splice(bi, 1))} className="press text-zinc-600 hover:text-red-400"><Trash2 size={11} /></button>
                </div>
              ))}
              <button onClick={() => upd((x) => { x.sections[si].lanes[li].bars.push({ start: weeks[0], end: weeks[0], status: 'build', label: 'new', _k: newKey() }) })} className="press flex items-center gap-1 text-[10px] text-zinc-500"><Plus size={10} /> bar</button>
            </div>
          ))}
          <button onClick={() => upd((x) => { x.sections[si].lanes.push({ name: 'New lane', bars: [], _k: newKey() }) })} className="press ml-2 flex items-center gap-1 text-[10px] text-zinc-500"><Plus size={10} /> lane</button>
        </div>
      ))}
      <button onClick={() => upd((x) => { (x.sections ||= []).push({ name: 'New section', owner: '', lanes: [], _k: newKey() }) })} className="press flex items-center gap-1 text-[11px] text-zinc-400"><Plus size={11} /> section</button>

      {/* milestones */}
      <div>
        <div className="mb-1 text-[11px] font-semibold text-zinc-400">Milestones</div>
        <div className="space-y-1">
          {(d.milestones || []).map((m, i) => (
            <div key={m._k} className="flex items-center gap-1.5">
              <WeekSel weeks={weeks} value={m.week} onChange={(v) => upd((x) => { x.milestones[i].week = v })} />
              <Txt value={m.label} onChange={(v) => upd((x) => { x.milestones[i].label = v })} ph="milestone" cls="flex-1 text-[11px]" />
              <label className="flex items-center gap-1 text-[10px] text-zinc-500"><input type="checkbox" checked={!!m.gate} onChange={(e) => upd((x) => { x.milestones[i].gate = e.target.checked })} /> hard gate</label>
              <button onClick={() => upd((x) => x.milestones.splice(i, 1))} className="press text-zinc-600 hover:text-red-400"><Trash2 size={12} /></button>
            </div>
          ))}
          <button onClick={() => upd((x) => { (x.milestones ||= []).push({ week: weeks[0], label: '', gate: false, _k: newKey() }) })} className="press flex items-center gap-1 text-[10px] text-zinc-400"><Plus size={10} /> milestone</button>
        </div>
      </div>
    </div>
  )
}

function Callout({ title, tone, items }) {
  return (
    <div className={`rounded-lg border px-3 py-2 ${tone}`}>
      <div className="mb-1 text-[11px] font-semibold">{title}</div>
      <ul className="list-disc space-y-0.5 pl-4 text-[11px] opacity-90">{items.map((it, i) => <li key={i}>{it}</li>)}</ul>
    </div>
  )
}
