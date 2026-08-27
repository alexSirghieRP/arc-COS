import React, { useEffect, useState } from 'react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'
import { Pencil, Save, X, Plus, Trash2, FileDown } from 'lucide-react'
import { post, editLock } from '../api.js'
import { PanelLoading } from '../ui.jsx'

const RAG = {
  green: { dot: '#2f8f5b', text: '#bbf7d0', label: 'GREEN', desc: 'built & on track', glyph: 'G' },
  amber: { dot: '#c98a1e', text: '#fde68a', label: 'AMBER', desc: 'at risk, mitigating', glyph: 'A' },
  red:   { dot: '#b23b2e', text: '#fecaca', label: 'RED', desc: 'off track', glyph: 'R' },
  grey:  { dot: '#6b7280', text: '#d4d4d8', label: 'GREY', desc: 'not in this phase', glyph: '–' },
}
const ORDER = ['green', 'amber', 'red', 'grey']
const rag = (k) => RAG[k] || RAG.amber
const cycle = (k) => ORDER[(ORDER.indexOf(k) + 1) % ORDER.length]
const clone = (o) => JSON.parse(JSON.stringify(o || {}))

// See RoadmapPanel.jsx's withKeys: categories/items are keyed by array index
// while delete buttons splice(), which shifts a later row's data into an
// earlier row's still-focused DOM node. Backfill a stable `_k` on edit start.
let _nextKey = 1
const newKey = () => `k${_nextKey++}`
const withKeys = (data) => {
  for (const cat of data.categories || []) {
    cat._k ||= newKey()
    for (const it of cat.items || []) it._k ||= newKey()
  }
  return data
}

// Module-level so its identity is stable across renders (an inline component
// would remount the <input> every keystroke and drop focus).
function Txt({ value, onChange, ph, cls = '' }) {
  return (
    <input value={value || ''} onChange={(e) => onChange(e.target.value)} placeholder={ph}
      className={`rounded bg-zinc-950 px-1.5 py-0.5 text-zinc-100 ring-1 ring-zinc-700 outline-none transition-shadow focus:ring-2 focus:ring-[var(--accent)] ${cls}`} />
  )
}

function Dot({ rag: r, onClick }) {
  const R = rag(r)
  return (
    <button onClick={onClick} title={`${R.label} — click to change`}
      className="grid h-5 w-5 shrink-0 cursor-pointer place-items-center rounded-full text-[10px] font-bold text-black/80 hover:ring-2 hover:ring-white/40"
      style={{ background: R.dot }}>{R.glyph}</button>
  )
}

export default function ScorecardPanel() {
  const [rm, setRm] = useState(null)
  const [editing, setEditing] = useState(false)
  const [d, setD] = useState(null)   // working copy of scorecard
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    const load = () => fetch('/api/roadmap').then((r) => r.json()).then((x) => setRm(x.roadmap)).catch(() => {})
    load(); const t = setInterval(() => { if (!editLock.active()) load() }, 30000); return () => clearInterval(t)
  }, [])

  if (!rm) return <Panel title="Scorecard"><PanelLoading /></Panel>
  const sc = editing ? d : rm.scorecard
  if (!sc) {
    return <Panel title="Scorecard"><Empty text="No scorecard yet — 'Draft from data' on the Roadmap tab, or add one via Edit on the Roadmap JSON." /></Panel>
  }

  const start = () => { editLock.acquire(); setD(withKeys(clone(rm.scorecard))); setErr(null); setEditing(true) }
  const stop = () => { editLock.release(); setEditing(false) }
  const upd = (fn) => { const x = clone(d); fn(x); setD(x) }
  // Quick toggle in VIEW mode: cycle a status and auto-save immediately.
  const quickSave = async (mutator) => {
    const prev = rm
    const next = clone(rm); mutator(next.scorecard); setRm(next)
    editLock.acquire()
    try { await post('/api/roadmap', { roadmap: next }) }
    catch (e) { setRm(prev); setErr(String(e)) }
    finally { editLock.release() }
  }
  const cycleItem = (ci, ii) =>
    editing ? upd((x) => { x.categories[ci].items[ii].rag = cycle(x.categories[ci].items[ii].rag) })
            : quickSave((s) => { s.categories[ci].items[ii].rag = cycle(s.categories[ci].items[ii].rag) })
  const cycleOverall = () =>
    editing ? upd((x) => { x.overall = cycle(x.overall) })
            : quickSave((s) => { s.overall = cycle(s.overall) })
  const cycleCapacity = () =>
    editing ? upd((x) => { x.capacity = x.capacity || {}; x.capacity.rag = cycle(x.capacity.rag || 'amber') })
            : quickSave((s) => { s.capacity = s.capacity || {}; s.capacity.rag = cycle(s.capacity.rag || 'amber') })
  const cycleRisks = () =>
    editing ? upd((x) => { x.risks_rag = cycle(x.risks_rag || 'amber') })
            : quickSave((s) => { s.risks_rag = cycle(s.risks_rag || 'amber') })
  const save = async () => {
    setBusy(true); setErr(null)
    try { await post('/api/roadmap', { roadmap: { ...rm, scorecard: d } }); setRm({ ...rm, scorecard: d }); stop() }
    catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const O = rag(sc.overall)

  return (
    <Panel
      title="Scorecard"
      badge={(sc.overall || '').toUpperCase()}
      actions={editing ? (
        <div className="flex items-center gap-1.5">
          <button onClick={save} disabled={busy} className="press flex items-center gap-1.5 rounded-lg bg-green-900 px-2.5 py-1 text-[11px] text-green-200 hover:bg-green-800 disabled:opacity-40"><Save size={12} /> Save</button>
          <button onClick={stop} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"><X size={12} /> Cancel</button>
        </div>
      ) : (
        <div className="flex items-center gap-1.5">
          <a href="/api/scorecard/pdf" target="_blank" rel="noreferrer" className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"><FileDown size={12} /> Export PDF</a>
          <button onClick={start} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"><Pencil size={12} /> Edit</button>
        </div>
      )}
    >
      {err && <div className="mb-2 rounded-lg bg-red-950 px-3 py-2 text-[11px] text-red-300">{err}</div>}
      <div className="overflow-hidden rounded-xl border border-zinc-800 bg-[#0d1117]">
        {/* Header band */}
        <div className="flex items-center justify-between gap-3 px-4 py-3" style={{ background: '#1e2a4a' }}>
          <div className="min-w-0">
            {editing
              ? <Txt value={sc.title} onChange={(v) => upd((x) => { x.title = v })} ph="Scorecard title" cls="w-full text-base font-bold" />
              : <div className="truncate text-base font-bold text-zinc-100">{sc.title}</div>}
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <div className="text-right text-[10px] text-zinc-400">
              {editing ? (
                <>
                  <Txt value={sc.week_of} onChange={(v) => upd((x) => { x.week_of = v })} ph="Week of" cls="mb-0.5 w-40 text-right text-[10px]" /><br />
                  <Txt value={sc.target} onChange={(v) => upd((x) => { x.target = v })} ph="Target" cls="w-40 text-right text-[10px]" />
                </>
              ) : (<><div>Week of {sc.week_of}</div><div>Target: {sc.target}</div></>)}
            </div>
            <button onClick={cycleOverall} title="Click to change overall status"
              className="cursor-pointer rounded-md px-2.5 py-1 text-[11px] font-bold text-black/80 hover:ring-2 hover:ring-white/40" style={{ background: O.dot }}>
              OVERALL {O.label}
            </button>
          </div>
        </div>

        <div className="space-y-3 p-4">
          {/* summary */}
          {editing
            ? <Txt value={sc.summary} onChange={(v) => upd((x) => { x.summary = v })} ph="One-line summary" cls="w-full text-sm italic" />
            : sc.summary && <p className="text-sm italic text-zinc-300">{sc.summary}</p>}

          {/* capacity */}
          {sc.capacity && (
            <div className="rounded-lg border p-3" style={{ borderColor: rag(sc.capacity.rag).dot + '80', background: rag(sc.capacity.rag).dot + '12' }}>
              <div className="flex items-center gap-2">
                <Dot rag={sc.capacity.rag || 'amber'} onClick={cycleCapacity} />
                <span className="text-[11px] font-bold uppercase tracking-wide" style={{ color: rag(sc.capacity.rag).text }}>Critical-path dependencies & capacity</span>
              </div>
              {editing
                ? <Txt value={sc.capacity.note} onChange={(v) => upd((x) => { x.capacity.note = v })} ph="note" cls="mt-1 w-full text-[11px] italic" />
                : <p className="mt-0.5 text-[11px] italic text-amber-100/70">{sc.capacity.note}</p>}
              <div className="mt-2 grid gap-3 sm:grid-cols-2">
                {(sc.capacity.columns || []).map((c, i) => (
                  <div key={i}>
                    {editing ? (
                      <>
                        <Txt value={c.title} onChange={(v) => upd((x) => { x.capacity.columns[i].title = v })} ph="title" cls="w-full text-xs font-semibold" />
                        <Txt value={c.desc} onChange={(v) => upd((x) => { x.capacity.columns[i].desc = v })} ph="desc" cls="mt-0.5 w-full text-[11px]" />
                      </>
                    ) : (<><div className="text-xs font-semibold text-zinc-200">{c.title}</div><div className="text-[11px] text-zinc-400">{c.desc}</div></>)}
                  </div>
                ))}
              </div>
              {editing
                ? <div className="mt-2 flex items-center gap-1"><span className="text-[11px] font-bold text-amber-300">ASK:</span><Txt value={sc.capacity.ask} onChange={(v) => upd((x) => { x.capacity.ask = v })} ph="the ask" cls="flex-1 text-[11px]" /></div>
                : sc.capacity.ask && <p className="mt-2 text-[11px] font-semibold text-amber-200"><span className="font-bold">ASK:</span> {sc.capacity.ask}</p>}
            </div>
          )}

          {/* risks */}
          {sc.risks?.length > 0 && (
            <div className="rounded-lg border-l-2 px-3 py-2" style={{ borderColor: rag(sc.risks_rag).dot, background: rag(sc.risks_rag).dot + '12' }}>
              <div className="flex items-center gap-2">
                <Dot rag={sc.risks_rag || 'amber'} onClick={cycleRisks} />
                <span className="text-[11px] font-bold uppercase tracking-wide" style={{ color: rag(sc.risks_rag).text }}>Risks — we own, managing</span>
              </div>
              <ul className="mt-1 space-y-0.5">
                {sc.risks.map((r, i) => (
                  <li key={i} className="flex items-start gap-1 text-[11px] text-amber-100/90">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-amber-400" />
                    {editing
                      ? <><Txt value={r} onChange={(v) => upd((x) => { x.risks[i] = v })} cls="flex-1" /><button onClick={() => upd((x) => x.risks.splice(i, 1))} className="press text-zinc-600 hover:text-red-400"><Trash2 size={12} /></button></>
                      : <span>{r}</span>}
                  </li>
                ))}
              </ul>
              {editing && <button onClick={() => upd((x) => { (x.risks ||= []).push('') })} className="press mt-1 flex items-center gap-1 text-[10px] text-zinc-400"><Plus size={10} /> risk</button>}
            </div>
          )}

          {/* categories */}
          <div className="grid gap-x-6 gap-y-3 md:grid-cols-3">
            {(sc.categories || []).map((cat, ci) => (
              <div key={cat._k ?? ci}>
                <div className="mb-1.5 flex items-center gap-2 border-b border-zinc-700 pb-1">
                  {editing
                    ? <Txt value={cat.name} onChange={(v) => upd((x) => { x.categories[ci].name = v })} cls="flex-1 text-xs font-bold" />
                    : <span className="text-xs font-bold uppercase tracking-wide text-sky-300">{cat.name}</span>}
                </div>
                <div className="space-y-1.5">
                  {(cat.items || []).map((it, ii) => (
                    <div key={it._k ?? ii} className="flex items-start gap-2">
                      <Dot rag={it.rag} onClick={() => cycleItem(ci, ii)} />
                      <div className="min-w-0 flex-1">
                        {editing ? (
                          <div className="flex items-center gap-1">
                            <Txt value={it.name} onChange={(v) => upd((x) => { x.categories[ci].items[ii].name = v })} ph="item" cls="flex-1 text-xs font-semibold" />
                            <button onClick={() => upd((x) => x.categories[ci].items.splice(ii, 1))} className="press text-zinc-600 hover:text-red-400"><Trash2 size={12} /></button>
                          </div>
                        ) : <div className="text-xs font-semibold text-zinc-200">{it.name}</div>}
                        {editing
                          ? <Txt value={it.note} onChange={(v) => upd((x) => { x.categories[ci].items[ii].note = v })} ph="note (optional)" cls="mt-0.5 w-full text-[10px]" />
                          : it.note && <div className="text-[10px] text-zinc-500">{it.note}</div>}
                      </div>
                    </div>
                  ))}
                  {editing && <button onClick={() => upd((x) => { x.categories[ci].items.push({ rag: 'amber', name: 'New item', note: '' }) })} className="press flex items-center gap-1 text-[10px] text-zinc-400"><Plus size={10} /> item</button>}
                </div>
              </div>
            ))}
          </div>

          {/* legend */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-zinc-800 pt-2 text-[10px] text-zinc-500">
            {Object.values(RAG).map((v) => (
              <span key={v.label} className="flex items-center gap-1.5">
                <span className="grid h-3.5 w-3.5 place-items-center rounded-full text-[8px] font-bold text-black/80" style={{ background: v.dot }}>{v.glyph}</span>
                <b className="text-zinc-400">{v.label}</b> = {v.desc}
              </span>
            ))}
            <span className="ml-auto italic">Updated by CoS · {rm.here_week}</span>
          </div>
        </div>
      </div>
    </Panel>
  )
}
