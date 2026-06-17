import React from 'react'
import { Panel } from '../App.jsx'
import { toggleCheckbox, post } from '../api.js'

function Checkbox({ noteDate, cb, refresh }) {
  return (
    <label className="flex cursor-pointer items-start gap-2 py-0.5">
      <input
        type="checkbox"
        className="mt-1"
        checked={cb.checked}
        onChange={async (e) => {
          await toggleCheckbox(noteDate, cb.line, e.target.checked)
          refresh()
        }}
      />
      <span className={cb.checked ? 'text-zinc-500 line-through' : ''}>{cb.text}</span>
    </label>
  )
}

const fmtTime = (iso) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''

const focusLabel = {
  pr_review: { text: 'PR awaits you', cls: 'bg-purple-900 text-purple-200' },
  my_pr: { text: 'your PR', cls: 'bg-sky-900 text-sky-200' },
  aged_ping: { text: 'aged ping', cls: 'bg-yellow-900 text-yellow-200' },
  ado: { text: 'sprint', cls: 'bg-emerald-900 text-emerald-200' },
}

export default function TodayPanel({ board, refresh }) {
  const note = board.note
  const now = new Date()

  return (
    <Panel title={`Today · ${now.toDateString()}`}>
      {!note && (
        <div className="mb-3 rounded border border-yellow-800 bg-yellow-950/50 p-2 text-sm text-yellow-200">
          No daily note for today yet.
          <button
            className="ml-2 rounded bg-yellow-800 px-2 py-0.5 text-xs"
            onClick={async () => {
              await post('/api/sweep/morning_brief')
              refresh()
            }}
          >
            Run morning brief now
          </button>
          {board.fallback_note && (
            <div className="mt-1 text-xs text-yellow-500">
              showing most recent note: {board.fallback_note.date}
            </div>
          )}
        </div>
      )}
      {(note || board.fallback_note) && (
        <NoteBody note={note || board.fallback_note} refresh={refresh} />
      )}

      <h3 className="mt-4 mb-1 text-xs font-semibold uppercase text-zinc-500">Calendar</h3>
      <div className="space-y-1">
        {board.calendar.length === 0 && <div className="text-sm text-zinc-500">no meetings today</div>}
        {board.calendar.map((e) => {
          const past = new Date(e.end_local) < now
          const current = new Date(e.start_local) <= now && now <= new Date(e.end_local)
          return (
            <div
              key={e.id}
              className={`flex gap-2 rounded px-2 py-1 text-sm ${
                current
                  ? 'bg-green-950 border border-green-800'
                  : past
                    ? 'text-zinc-600'
                    : 'bg-zinc-800/50'
              }`}
            >
              <span className="w-24 shrink-0 tabular-nums">
                {fmtTime(e.start_local)}–{fmtTime(e.end_local)}
              </span>
              <span className="truncate">{e.subject}</span>
            </div>
          )
        })}
      </div>

      <h3 className="mt-4 mb-1 text-xs font-semibold uppercase text-zinc-500">Proposed focus</h3>
      <div className="space-y-1">
        {board.focus.length === 0 && <div className="text-sm text-zinc-500">nothing urgent</div>}
        {board.focus.map((f, i) => {
          const lbl = focusLabel[f.kind] || { text: f.kind, cls: 'bg-zinc-800' }
          return (
            <div key={i} className="flex items-center gap-2 text-sm">
              <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${lbl.cls}`}>
                {lbl.text}
              </span>
              {f.ref?.startsWith('http') ? (
                <a href={f.ref} target="_blank" rel="noreferrer" className="truncate text-sky-400 hover:underline">
                  {f.title}
                </a>
              ) : (
                <span className="truncate">{f.title}</span>
              )}
            </div>
          )
        })}
      </div>
    </Panel>
  )
}

function NoteBody({ note, refresh }) {
  const top3 = note.sections['Top 3 for Today']?.checkboxes || []
  const also = note.sections['Also Do Today']?.checkboxes || []
  const carry = note.sections['Carry Forward']?.checkboxes || []
  return (
    <>
      <h3 className="mb-1 text-xs font-semibold uppercase text-zinc-500">Top 3 for Today</h3>
      {top3.length === 0 && <div className="text-sm text-zinc-600 italic">empty</div>}
      {top3.map((cb) => (
        <Checkbox key={cb.line} noteDate={note.date} cb={cb} refresh={refresh} />
      ))}
      {also.length > 0 && (
        <>
          <h3 className="mt-3 mb-1 text-xs font-semibold uppercase text-zinc-500">Also Do Today</h3>
          {also.map((cb) => (
            <Checkbox key={cb.line} noteDate={note.date} cb={cb} refresh={refresh} />
          ))}
        </>
      )}
      {carry.length > 0 && (
        <>
          <h3 className="mt-3 mb-1 text-xs font-semibold uppercase text-zinc-500">Carry Forward</h3>
          {carry.map((cb) => (
            <Checkbox key={cb.line} noteDate={note.date} cb={cb} refresh={refresh} />
          ))}
        </>
      )}
    </>
  )
}
