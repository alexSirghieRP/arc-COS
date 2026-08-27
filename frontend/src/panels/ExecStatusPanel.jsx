import React, { useEffect, useState } from 'react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'
import { Circle, AlertTriangle, CheckCircle2, Pencil, Save, X, Plus, Trash2 } from 'lucide-react'
import { post } from '../api.js'
import { PanelLoading } from '../ui.jsx'

// Red / Amber / Green for execs.
const RAG = {
  green: { bg: '#1f7a4d', dot: '#36B37E', text: 'text-emerald-200', label: 'On track' },
  amber: { bg: '#9a6a00', dot: '#FFAB00', text: 'text-amber-200', label: 'At risk' },
  red:   { bg: '#a3271b', dot: '#FF5630', text: 'text-red-200', label: 'Blocked' },
}
const ORDER = ['green', 'amber', 'red']
const rag = (k) => RAG[k] || RAG.amber
const cycle = (k) => ORDER[(ORDER.indexOf(k) + 1) % ORDER.length]
const clone = (o) => JSON.parse(JSON.stringify(o || {}))

// See RoadmapPanel.jsx's withKeys: lanes are keyed by array index while the
// delete button splices, shifting a later lane's data into an earlier lane's
// still-focused input. Backfill a stable `_k` when a draft is created.
let _nextKey = 1
const newKey = () => `k${_nextKey++}`
const withKeys = (data) => {
  for (const l of data.lanes || []) l._k ||= newKey()
  return data
}

export default function ExecStatusPanel() {
  const [rm, setRm] = useState(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(null) // working copy of exec_status
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    const load = () => fetch('/api/roadmap').then((r) => r.json()).then((d) => setRm(d.roadmap)).catch(() => {})
    load()
    const t = setInterval(() => { if (!editing) load() }, 30000)
    return () => clearInterval(t)
  }, [editing])

  if (!rm) return <Panel title="Exec status"><PanelLoading /></Panel>

  const startEdit = () => {
    const base = rm.exec_status || {
      overall: 'amber', headline: '',
      weeks: (rm.weeks || []).map((w) => ({ week: w, rag: 'green', note: '' })),
      lanes: (rm.sections || []).map((s) => ({ name: s.name, rag: 'green', note: '' })),
    }
    setDraft(withKeys(clone(base))); setErr(null); setEditing(true)
  }
  const save = async () => {
    setBusy(true); setErr(null)
    try {
      await post('/api/roadmap', { roadmap: { ...rm, exec_status: draft } })
      setRm({ ...rm, exec_status: draft }); setEditing(false)
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  const upd = (fn) => { const d = clone(draft); fn(d); setDraft(d) }

  const es = editing ? draft : rm.exec_status
  if (!es) {
    return (
      <Panel title="Exec status" actions={<EditBtn onClick={startEdit} label="Add status" />}>
        <Empty text="No exec status yet — click Add status, or 'Draft from data' on the Roadmap tab." />
      </Panel>
    )
  }

  const O = rag(es.overall)
  const here = rm.here_week
  const weeks = es.weeks || []
  const lanes = es.lanes || []
  const monthOf = {}
  { let i = 0; for (const m of rm.months || []) { for (let k = 0; k < (m.span || 0); k++) { monthOf[(rm.weeks || [])[i]] = m.label; i++ } } }

  return (
    <Panel
      title="Exec status"
      badge={(es.overall || '').toUpperCase()}
      actions={
        editing ? (
          <div className="flex items-center gap-1.5">
            <button onClick={save} disabled={busy} className="press flex items-center gap-1.5 rounded-lg bg-green-900 px-2.5 py-1 text-[11px] text-green-200 hover:bg-green-800 disabled:opacity-40"><Save size={12} /> Save</button>
            <button onClick={() => setEditing(false)} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700"><X size={12} /> Cancel</button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-zinc-500">{rm.title}</span>
            <EditBtn onClick={startEdit} label="Edit" />
          </div>
        )
      }
    >
      {err && <div className="mb-2 rounded-lg bg-red-950 px-3 py-2 text-[11px] text-red-300">{err}</div>}
      {editing && <div className="mb-2 text-[11px] text-zinc-500">Click any colored cell/dot to cycle green → amber → red. Edit text inline. Then Save.</div>}
      <div className="space-y-5">
        {/* Overall */}
        <div className="flex items-start gap-3 rounded-xl border px-4 py-3" style={{ borderColor: O.dot + '66', background: O.dot + '14' }}>
          <button
            disabled={!editing}
            onClick={() => upd((d) => { d.overall = cycle(d.overall) })}
            className="mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-lg disabled:cursor-default"
            style={{ background: O.dot }}
            title={editing ? 'Click to cycle RAG' : ''}
          >
            {es.overall === 'green' ? <CheckCircle2 size={20} className="text-black/70" />
              : es.overall === 'red' ? <AlertTriangle size={20} className="text-black/70" />
              : <Circle size={18} className="text-black/70" />}
          </button>
          <div className="min-w-0 flex-1">
            <span className={`text-sm font-bold uppercase tracking-wide ${O.text}`}>{es.overall} · {O.label}</span>
            {editing ? (
              <input
                value={es.headline || ''}
                onChange={(e) => upd((d) => { d.headline = e.target.value })}
                placeholder="One-line headline an exec reads in 5 seconds"
                className="mt-1 w-full rounded bg-zinc-950 px-2 py-1 text-sm text-zinc-200 ring-1 ring-zinc-700 focus:outline-none focus:ring-zinc-500"
              />
            ) : (
              <div className="mt-0.5 text-sm text-zinc-200">{es.headline}</div>
            )}
          </div>
        </div>

        {/* Week by week */}
        <div>
          <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Week by week</div>
          <div className="flex gap-1.5 overflow-x-auto">
            {weeks.map((w, idx) => {
              const R = rag(w.rag)
              const isHere = w.week === here
              return (
                <div key={w.week + idx} className="flex min-w-[100px] flex-1 flex-col rounded-lg p-2 text-center"
                     style={{ background: R.bg, outline: isHere ? '2px solid #fff' : 'none' }}>
                  <button disabled={!editing} onClick={() => upd((d) => { d.weeks[idx].rag = cycle(d.weeks[idx].rag) })}
                          className="text-[11px] font-semibold text-white/90 disabled:cursor-default" title={editing ? 'Cycle RAG' : ''}>
                    {isHere ? '▼ ' : ''}{w.week}
                  </button>
                  <div className="text-[9px] text-white/60">{monthOf[w.week] || ''}</div>
                  {editing ? (
                    <input value={w.note || ''} onChange={(e) => upd((d) => { d.weeks[idx].note = e.target.value })}
                           placeholder="why"
                           className="mt-1 w-full rounded bg-black/25 px-1 py-0.5 text-center text-[10px] text-white placeholder-white/40 outline-none ring-1 ring-transparent transition-shadow focus:ring-white/60" />
                  ) : (
                    <div className="mt-1 text-[10px] leading-tight text-white/90">{w.note || ''}</div>
                  )}
                </div>
              )
            })}
            {weeks.length === 0 && <Empty text="No weekly status set" />}
          </div>
        </div>

        {/* By workstream */}
        <div>
          <div className="mb-1.5 flex items-center gap-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
            By workstream
            {editing && (
              <button onClick={() => upd((d) => { (d.lanes ||= []).push({ name: 'New lane', rag: 'amber', note: '', _k: newKey() }) })}
                      className="press flex items-center gap-1 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-300"><Plus size={10} /> lane</button>
            )}
          </div>
          <div className="space-y-1.5">
            {lanes.map((l, idx) => {
              const R = rag(l.rag)
              return (
                <div key={l._k ?? idx} className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2">
                  <button disabled={!editing} onClick={() => upd((d) => { d.lanes[idx].rag = cycle(d.lanes[idx].rag) })}
                          className="h-3 w-3 shrink-0 rounded-full disabled:cursor-default" style={{ background: R.dot }} title={editing ? 'Cycle RAG' : ''} />
                  {editing ? (
                    <>
                      <input value={l.name} onChange={(e) => upd((d) => { d.lanes[idx].name = e.target.value })}
                             className="w-44 shrink-0 rounded bg-zinc-950 px-2 py-0.5 text-sm text-zinc-200 ring-1 ring-zinc-700 outline-none transition-shadow focus:ring-2 focus:ring-[var(--accent)]" />
                      <input value={l.note || ''} onChange={(e) => upd((d) => { d.lanes[idx].note = e.target.value })}
                             placeholder="note"
                             className="flex-1 rounded bg-zinc-950 px-2 py-0.5 text-xs text-zinc-300 ring-1 ring-zinc-700 outline-none transition-shadow focus:ring-2 focus:ring-[var(--accent)]" />
                      <button onClick={() => upd((d) => { d.lanes.splice(idx, 1) })} className="press text-zinc-600 hover:text-red-400"><Trash2 size={13} /></button>
                    </>
                  ) : (
                    <>
                      <span className="w-44 shrink-0 text-sm font-medium text-zinc-200">{l.name}</span>
                      <span className={`shrink-0 text-[10px] font-semibold uppercase ${R.text}`}>{l.rag}</span>
                      <span className="truncate text-xs text-zinc-400">{l.note || ''}</span>
                    </>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {/* What it rides on (roadmap callouts — edit on the Roadmap tab) */}
        {rm.callouts?.rides_on?.length > 0 && (
          <div>
            <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-zinc-500">What it rides on</div>
            <ul className="space-y-1 rounded-lg border border-amber-900/50 bg-amber-950/20 px-4 py-2.5 text-xs text-amber-100/90">
              {rm.callouts.rides_on.map((r, i) => <li key={i} className="ml-3 list-disc">{r}</li>)}
            </ul>
          </div>
        )}

        {/* Legend */}
        <div className="flex items-center gap-3 text-[11px] text-zinc-500">
          {Object.entries(RAG).map(([k, v]) => (
            <span key={k} className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full" style={{ background: v.dot }} /> {k} · {v.label}</span>
          ))}
          <span className="ml-auto italic">Updated by CoS · {rm.here_week}</span>
        </div>
      </div>
    </Panel>
  )
}

function EditBtn({ onClick, label }) {
  return (
    <button onClick={onClick} className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-zinc-700">
      <Pencil size={12} /> {label}
    </button>
  )
}
