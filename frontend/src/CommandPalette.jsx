import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Search, CornerDownLeft, Clock } from 'lucide-react'
import { post, setSetting } from './api.js'
import { useToast } from './ui.jsx'

async function searchItems(q) {
  const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=8`)
  if (!r.ok) return { items: [], audit: [] }
  return r.json()
}

async function searchObsidian(q) {
  const r = await fetch(`/api/obsidian/recent?limit=12&q=${encodeURIComponent(q)}`)
  if (!r.ok) return []
  return r.json()
}

const RECENT_KEY = 'chief_cmd_recent'
const MAX_RECENT = 5
function getRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]') } catch { return [] }
}
function addRecent(id) {
  const prev = getRecent().filter((x) => x !== id)
  try { localStorage.setItem(RECENT_KEY, JSON.stringify([id, ...prev].slice(0, MAX_RECENT))) } catch {}
}

// Fuzzy-ish match: all query chars must appear in order.
function matches(q, text) {
  q = q.toLowerCase()
  text = text.toLowerCase()
  let i = 0
  for (const c of text) if (c === q[i]) i++
  return i >= q.length
}

const SWEEPS = [
  ['teams', 'Run Teams sweep'],
  ['email', 'Run email sweep'],
  ['open_loops', 'Run open-loops sweep'],
  ['meeting_summaries', 'Run meeting summaries'],
  ['pr_readiness', 'Run PR readiness check'],
  ['pr_status', 'Run PR status sweep'],
  ['knowledge', 'Run knowledge sync'],
  ['morning_brief', 'Run morning brief'],
  ['evening_shutdown', 'Run evening shutdown'],
  ['weekly_status', 'Generate weekly status draft'],
  ['chieff_daily_update', 'Run Chieff daily update'],
  ['backfill_week', 'Backfill week (last 7d)'],
  ['hygiene', 'Run hygiene cleanup'],
  ['pr_review_recent', 'Review recent PRs (last 2d)'],
  ['pr_review_orphans', 'Requeue orphaned PR reviews'],
  ['chieff', 'Run @Chieff mention handler'],
  ['pre_meeting', 'Run pre-meeting brief check'],
  ['post_meeting', 'Run post-meeting catch-up'],
  ['pr_digest', 'Run GitHub pending-work digest'],
]

export default function CommandPalette({ open, onClose, tabs, setTab, board, refresh }) {
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(0)
  const [searchResults, setSearchResults] = useState(null)
  const [searching, setSearching] = useState(false)
  const [obsidianResults, setObsidianResults] = useState(null)
  const inputRef = useRef(null)
  const searchTimer = useRef(null)
  const searchSeq = useRef(0)
  const toast = useToast()

  useEffect(() => {
    if (open) {
      setQ('')
      setSel(0)
      setSearchResults(null)
      setObsidianResults(null)
      setTimeout(() => inputRef.current?.focus(), 10)
    }
  }, [open])

  // Live search when query starts with '/' (items) or '@' (Obsidian notes)
  useEffect(() => {
    if (q.startsWith('@')) {
      setSearchResults(null)
      const needle = q.slice(1).trim()
      if (needle.length < 1) { setObsidianResults(null); return }
      clearTimeout(searchTimer.current)
      const seq = ++searchSeq.current
      searchTimer.current = setTimeout(async () => {
        setSearching(true)
        try {
          const r = await searchObsidian(needle)
          // A slower in-flight fetch for an earlier keystroke can resolve
          // after a faster later one — ignore it if a newer search started.
          if (seq === searchSeq.current) setObsidianResults(r)
        } finally {
          if (seq === searchSeq.current) setSearching(false)
        }
      }, 200)
      return () => clearTimeout(searchTimer.current)
    }
    setObsidianResults(null)
    if (!q.startsWith('/')) { setSearchResults(null); return }
    const needle = q.slice(1).trim()
    if (needle.length < 2) { setSearchResults(null); return }
    clearTimeout(searchTimer.current)
    const seq = ++searchSeq.current
    searchTimer.current = setTimeout(async () => {
      setSearching(true)
      try {
        const r = await searchItems(needle)
        if (seq === searchSeq.current) setSearchResults(r)
      } finally {
        if (seq === searchSeq.current) setSearching(false)
      }
    }, 300)
    return () => clearTimeout(searchTimer.current)
  }, [q])

  const commands = useMemo(() => {
    const go = (id, label) => ({
      id: `go-${id}`,
      group: 'Go to',
      label,
      run: () => setTab(id),
    })
    const sweep = (name, label) => ({
      id: `sweep-${name}`,
      group: 'Run',
      label,
      run: async () => {
        toast(`${label}…`, 'info', 2000)
        try {
          await post(`/api/sweep/${name}`)
          toast(`${label} done`, 'success')
          refresh()
        } catch (e) {
          toast(`${label} failed: ${String(e).slice(0, 120)}`, 'error')
        }
      },
    })
    return [
      ...tabs.map((t) => go(t.id, t.label)),
      ...SWEEPS.map(([n, l]) => sweep(n, l)),
      {
        id: 'toggle-dry-run',
        group: 'Toggle',
        label: board?.dry_run ? 'Go LIVE (disable dry run)' : 'Enable dry run',
        run: async () => {
          await setSetting('dry_run', board?.dry_run ? 'false' : 'true')
          toast(board?.dry_run ? 'LIVE mode: sends are real now' : 'Dry run enabled', board?.dry_run ? 'error' : 'success')
          refresh()
        },
      },
      {
        id: 'toggle-kill',
        group: 'Toggle',
        label: board?.kill_switch ? 'Resume sends (kill switch off)' : 'KILL SWITCH — stop all sends',
        run: async () => {
          await setSetting('kill_switch', board?.kill_switch ? 'false' : 'true')
          toast(board?.kill_switch ? 'Sends resumed' : 'Kill switch ON', 'info')
          refresh()
        },
      },
      {
        id: 'toggle-theme',
        group: 'Toggle',
        label: 'Toggle light / dark theme',
        run: () => {
          const light = !document.documentElement.classList.contains('light')
          document.documentElement.classList.toggle('light', light)
          try { localStorage.setItem('theme', light ? 'light' : 'dark') } catch {}
        },
      },
    ]
  }, [tabs, setTab, board, refresh, toast])

  const filtered = useMemo(() => {
    if (q) return commands.filter((c) => matches(q, `${c.group} ${c.label}`))
    // No query: bubble recently used commands to top with a "Recent" group label.
    const recent = getRecent()
    if (!recent.length) return commands
    const recentCmds = recent
      .map((id) => commands.find((c) => c.id === id))
      .filter(Boolean)
      .map((c) => ({ ...c, group: 'Recent' }))
    const rest = commands.filter((c) => !recent.includes(c.id))
    return [...recentCmds, ...rest]
  }, [q, commands])
  const clamped = Math.min(sel, Math.max(0, filtered.length - 1))

  useEffect(() => setSel(0), [q])

  if (!open) return null

  const runSel = () => {
    const cmd = filtered[clamped]
    if (cmd) {
      addRecent(cmd.id)
      onClose()
      cmd.run()
    }
  }

  const onKey = (e) => {
    if (e.key === 'Escape') onClose()
    else if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, filtered.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)) }
    else if (e.key === 'Enter') { e.preventDefault(); runSel() }
  }

  let lastGroup = null

  return (
    <div
      className="animate-fade-in fixed inset-0 z-40 bg-black/50 backdrop-blur-[2px]"
      onMouseDown={onClose}
    >
      <div
        className="palette-in mx-auto mt-[12vh] w-[560px] max-w-[92vw] overflow-hidden rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 border-b border-zinc-800 px-4 py-3">
          <Search size={16} className="shrink-0 text-zinc-500" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKey}
            placeholder="Tab/sweep/switch… · /keyword search items · @note Obsidian"
            className="w-full bg-transparent text-[14px] text-zinc-100 placeholder:text-zinc-600 focus:outline-none"
          />
          <kbd className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">
            esc
          </kbd>
        </div>
        <div className="max-h-[46vh] overflow-y-auto p-1.5">
          {/* Obsidian note search when query starts with @ */}
          {q.startsWith('@') ? (
            searching ? (
              <div className="px-3 py-4 text-center text-[12px] text-zinc-600">searching vault…</div>
            ) : obsidianResults ? (
              obsidianResults.length === 0 ? (
                <div className="px-3 py-4 text-center text-[12px] text-zinc-600">no notes match "{q.slice(1)}"</div>
              ) : (
                <>
                  <div className="px-2.5 pb-0.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-600">Obsidian notes</div>
                  {obsidianResults.map((note) => (
                    <button
                      key={note.path}
                      onClick={() => { setTab('obsidian'); onClose() }}
                      className="flex w-full flex-col gap-0.5 rounded-lg px-2.5 py-1.5 text-left hover:bg-zinc-800"
                    >
                      <div className="flex items-center gap-2 text-[12px]">
                        <span className="font-medium text-zinc-200">{note.name.replace(/\.md$/, '')}</span>
                        <span className="ml-auto text-[10px] text-zinc-600">{note.path.split('/').slice(0, -1).join('/')}</span>
                      </div>
                      {note.preview && (
                        <div className="truncate text-[11px] text-zinc-500">{note.preview.slice(0, 100)}</div>
                      )}
                    </button>
                  ))}
                </>
              )
            ) : (
              <div className="px-3 py-4 text-center text-[12px] text-zinc-600">type a note name after @</div>
            )
          ) : q.startsWith('/') ? (
            searching ? (
              <div className="px-3 py-4 text-center text-[12px] text-zinc-600">searching…</div>
            ) : searchResults ? (
              <>
                {searchResults.items.length === 0 && searchResults.audit.length === 0 ? (
                  <div className="px-3 py-4 text-center text-[12px] text-zinc-600">no results</div>
                ) : null}
                {searchResults.items.length > 0 && (
                  <>
                    <div className="px-2.5 pb-0.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-600">Items</div>
                    {searchResults.items.map((item) => (
                      <div key={item.id} className="flex flex-col gap-0.5 rounded-lg px-2.5 py-1.5 hover:bg-zinc-800">
                        <div className="flex items-center gap-2 text-[12px]">
                          <span className="font-medium text-zinc-200">{item.sender || item.source}</span>
                          {item.tier && <span className="rounded bg-zinc-700 px-1 text-[9px] text-zinc-300">{item.tier}</span>}
                          <span className="ml-auto text-[10px] text-zinc-600">{item.status}</span>
                        </div>
                        <div className="truncate text-[11px] text-zinc-500">{item.subject} — {(item.content || '').slice(0, 80)}</div>
                      </div>
                    ))}
                  </>
                )}
                {searchResults.audit.length > 0 && (
                  <>
                    <div className="px-2.5 pb-0.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-600">Audit log</div>
                    {searchResults.audit.map((row) => (
                      <div key={row.id} className="flex flex-col gap-0.5 rounded-lg px-2.5 py-1.5 hover:bg-zinc-800">
                        <div className="flex items-center gap-2 text-[12px]">
                          <span className="font-mono text-zinc-400">{row.action}</span>
                          <span className="ml-auto text-[10px] text-zinc-600">{(row.ts || '').slice(0, 16).replace('T', ' ')}</span>
                        </div>
                        <div className="truncate text-[11px] text-zinc-500">{(row.detail || '').slice(0, 120)}</div>
                      </div>
                    ))}
                  </>
                )}
              </>
            ) : (
              <div className="px-3 py-4 text-center text-[12px] text-zinc-600">type 2+ chars after /</div>
            )
          ) : (
            <>
              {filtered.length === 0 && (
                <div className="px-3 py-6 text-center text-[13px] text-zinc-600">nothing matches</div>
              )}
              {filtered.map((c, i) => {
                const header = c.group !== lastGroup
                lastGroup = c.group
                return (
                  <React.Fragment key={c.id}>
                    {header && (
                      <div className="px-2.5 pb-0.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-600">
                        {c.group}
                      </div>
                    )}
                    <button
                      onClick={() => { addRecent(c.id); onClose(); c.run() }}
                      onMouseEnter={() => setSel(i)}
                      className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] ${
                        i === clamped ? 'bg-[var(--accent-fill)] text-zinc-100' : 'text-zinc-300'
                      }`}
                    >
                      {c.group === 'Recent' && <Clock size={11} className="shrink-0 text-zinc-600" />}
                      <span className="flex-1">{c.label}</span>
                      {i === clamped && <CornerDownLeft size={13} className="text-zinc-500" />}
                    </button>
                  </React.Fragment>
                )
              })}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
