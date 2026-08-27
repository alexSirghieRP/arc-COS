import React, { useEffect, useRef, useState } from 'react'
import { Pause, Play, Search, ArrowDownToLine, Copy } from 'lucide-react'
import { Panel } from '../App.jsx'

const LEVEL_CLS = {
  DEBUG: 'text-zinc-500',
  INFO: 'text-sky-300',
  WARNING: 'text-amber-300',
  ERROR: 'text-red-300',
  CRITICAL: 'text-red-200 bg-red-950/50',
}

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR']

export default function LogsPanel({ refreshTick }) {
  const [data, setData] = useState({ logs: [], counts: {} })
  const [level, setLevel] = useState('INFO')
  const [q, setQ] = useState('')
  const [paused, setPaused] = useState(false)
  const [follow, setFollow] = useState(true)
  const scrollRef = useRef(null)
  const lastIdRef = useRef(0)

  useEffect(() => {
    // Full reset when filters change — not when pause/resume toggles, or
    // clicking Resume flashes an empty "no matching log lines" state before
    // the full reload below repopulates it.
    lastIdRef.current = 0
    setData({ logs: [], counts: {} })
  }, [level, q])

  useEffect(() => {
    if (paused) return
    const load = (incremental) => {
      const params = new URLSearchParams({ limit: '400' })
      if (level) params.set('level', level)
      if (q) params.set('q', q)
      // Incremental: only fetch records newer than the last seen ID.
      if (incremental && lastIdRef.current > 0) params.set('after_id', String(lastIdRef.current))
      fetch(`/api/logs?${params}`)
        .then((r) => r.json())
        .then((d) => {
          if (incremental && lastIdRef.current > 0) {
            // Append new lines to existing data, keep last 400.
            setData((prev) => {
              const merged = [...prev.logs, ...d.logs].slice(-400)
              return { logs: merged, counts: d.counts }
            })
          } else {
            setData(d)
          }
          if (d.last_id) lastIdRef.current = d.last_id
        })
        .catch(() => {})
    }
    load(false)  // initial load: full fetch
    const t = setInterval(() => load(true), 3000)  // polls: incremental
    return () => clearInterval(t)
  }, [level, q, paused, refreshTick])

  useEffect(() => {
    if (follow && scrollRef.current)
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [data, follow])

  const counts = data.counts || {}

  return (
    <Panel
      title="Backend logs"
      badge={data.logs.length}
      actions={
        <div className="flex items-center gap-1.5">
          <div className="relative">
            <Search size={12} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-zinc-500" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="filter…"
              className="w-44 rounded-lg bg-zinc-800 py-1 pl-7 pr-2 text-[12px] text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
            />
          </div>
          {LEVELS.map((l) => (
            <button
              key={l}
              onClick={() => setLevel(level === l ? '' : l)}
              className={`press rounded-md px-2 py-1 text-[10px] font-semibold tracking-wide ${
                level === l
                  ? 'bg-zinc-700 text-zinc-100 ring-1 ring-zinc-500'
                  : 'bg-zinc-900 text-zinc-500 ring-1 ring-zinc-800 hover:text-zinc-300'
              }`}
              title={`show ${l}+ ${counts[l] ? `(${counts[l]} buffered)` : ''}`}
            >
              {l.slice(0, 4)}
              {l === 'ERROR' && counts.ERROR > 0 && (
                <span className="ml-1 rounded bg-red-950 px-1 text-red-300">{counts.ERROR}</span>
              )}
            </button>
          ))}
          <button
            onClick={() => setFollow((v) => !v)}
            title={follow ? 'Following tail — click to stop' : 'Follow tail'}
            className={`press grid h-6 w-6 place-items-center rounded-md ${
              follow ? 'bg-zinc-700 text-zinc-100' : 'bg-zinc-900 text-zinc-500 ring-1 ring-zinc-800'
            }`}
          >
            <ArrowDownToLine size={12} />
          </button>
          <button
            onClick={() => setPaused((v) => !v)}
            title={paused ? 'Resume live tail' : 'Pause'}
            className={`press grid h-6 w-6 place-items-center rounded-md ${
              paused ? 'bg-amber-900 text-amber-200' : 'bg-zinc-900 text-zinc-500 ring-1 ring-zinc-800'
            }`}
          >
            {paused ? <Play size={12} /> : <Pause size={12} />}
          </button>
        </div>
      }
    >
      <div
        ref={scrollRef}
        onWheel={() => setFollow(false)}
        className="max-h-[70vh] overflow-y-auto rounded-lg bg-zinc-950 p-2 font-mono text-[11px] leading-[1.55]"
      >
        {data.logs.length === 0 && (
          <div className="p-3 text-center text-zinc-600">no matching log lines in the buffer</div>
        )}
        {data.logs.map((l) => (
          <div key={l.id} className="group flex gap-2 whitespace-pre-wrap break-all px-1 py-px hover:bg-zinc-900/70">
            <span className="shrink-0 tabular-nums text-zinc-600">
              {new Date(l.ts).toLocaleTimeString([], { hour12: false })}
            </span>
            <span className={`w-14 shrink-0 font-semibold ${LEVEL_CLS[l.level] || 'text-zinc-400'}`}>
              {l.level}
            </span>
            <span className="shrink-0 text-zinc-500">{l.logger}</span>
            <span className="min-w-0 flex-1 text-zinc-300">{l.message}</span>
            <button
              onClick={() => navigator.clipboard?.writeText(`[${l.level}] ${l.logger} ${l.message}`)}
              title="Copy line"
              className="ml-auto shrink-0 opacity-0 group-hover:opacity-60 hover:!opacity-100 text-zinc-500 hover:text-zinc-300"
            >
              <Copy size={10} />
            </button>
          </div>
        ))}
      </div>
    </Panel>
  )
}
