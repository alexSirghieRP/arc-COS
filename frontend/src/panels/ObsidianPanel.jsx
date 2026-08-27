import React, {
  useEffect,
  useState,
  useRef,
  useCallback,
} from 'react'
import { X, Search, RefreshCw, BookOpen, Plus } from 'lucide-react'
import { Panel } from '../App.jsx'
import { useToast } from '../ui.jsx'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtK(n) {
  if (n == null) return '–'
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

function relTime(mtime) {
  if (!mtime) return ''
  const ms = typeof mtime === 'number' ? mtime * 1000 : new Date(mtime).getTime()
  const d = (Date.now() - ms) / 1000
  if (d < 60) return 'just now'
  if (d < 3600) return `${Math.floor(d / 60)}m ago`
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`
  return `${Math.floor(d / 86400)}d ago`
}

function stripFrontmatter(content) {
  if (!content) return ''
  return content.replace(/^---[\s\S]*?---\n?/, '').trim()
}

function folderOf(path) {
  if (!path) return ''
  const parts = path.split('/')
  return parts.length > 1 ? parts.slice(0, -1).join('/') : ''
}

function nodeColor(folder) {
  if (!folder) return '#74B9FF'
  const f = folder.toLowerCase()
  if (f.includes('personal')) return '#6C5CE7'
  if (f.includes('chieff') || f.includes('chief')) return '#00B894'
  return '#74B9FF'
}

function mapRadius(size) {
  const clamped = Math.max(10, Math.min(30, size || 10))
  return 4 + ((clamped - 10) / 20) * 10
}

// ---------------------------------------------------------------------------
// Force-directed simulation — pure JS, no D3
// ---------------------------------------------------------------------------

function runSimulation(nodes, edges, width, height) {
  const n = nodes.length
  const positions = Array.from({ length: n }, () => ({
    x: Math.random() * width,
    y: Math.random() * height,
    vx: 0,
    vy: 0,
  }))

  // Pre-index edges to integer indices
  const idToIdx = {}
  nodes.forEach((node, i) => { idToIdx[node.id] = i })
  const edgePairs = (edges || [])
    .map((e) => ({ s: idToIdx[e.source] ?? -1, t: idToIdx[e.target] ?? -1 }))
    .filter((e) => e.s >= 0 && e.t >= 0)

  const cx = width / 2
  const cy = height / 2

  for (let tick = 0; tick < 200; tick++) {
    // --- Repulsion (all-pairs O(n²)) ---
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = positions[j].x - positions[i].x
        const dy = positions[j].y - positions[i].y
        const dist2 = dx * dx + dy * dy + 1
        const dist = Math.sqrt(dist2)
        const force = 5000 / dist2
        const fx = (force * dx) / dist
        const fy = (force * dy) / dist
        positions[i].vx -= fx
        positions[i].vy -= fy
        positions[j].vx += fx
        positions[j].vy += fy
      }
    }

    // --- Spring attraction along edges ---
    for (const { s, t } of edgePairs) {
      const dx = positions[t].x - positions[s].x
      const dy = positions[t].y - positions[s].y
      const dist = Math.sqrt(dx * dx + dy * dy) || 1
      const force = 0.05 * (dist - 120)
      const fx = (force * dx) / dist
      const fy = (force * dy) / dist
      positions[s].vx += fx
      positions[s].vy += fy
      positions[t].vx -= fx
      positions[t].vy -= fy
    }

    // --- Center gravity ---
    for (const p of positions) {
      p.vx += (cx - p.x) * 0.02
      p.vy += (cy - p.y) * 0.02
    }

    // --- Velocity damping + integration ---
    for (const p of positions) {
      p.vx *= 0.85
      p.vy *= 0.85
      p.x += p.vx
      p.y += p.vy
    }
  }

  return positions
}

// ---------------------------------------------------------------------------
// Shared micro-components
// ---------------------------------------------------------------------------

function Spinner() {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-zinc-500">
      <RefreshCw size={16} className="animate-spin" />
      <span className="text-sm">Loading…</span>
    </div>
  )
}

function Empty({ text }) {
  return (
    <div className="flex items-center justify-center py-12 text-sm text-zinc-500">
      {text}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Snippet highlighter for Search tab
// ---------------------------------------------------------------------------

function HighlightSnippet({ text, query }) {
  if (!query || !text) return <span className="text-zinc-500 text-[11px]">{text}</span>
  const idx = text.toLowerCase().indexOf(query.toLowerCase())
  if (idx === -1) return <span className="text-zinc-500 text-[11px]">{text}</span>
  return (
    <span className="text-[11px] text-zinc-500">
      {text.slice(0, idx)}
      <mark className="bg-violet-900/60 text-violet-200 rounded px-0.5">
        {text.slice(idx, idx + query.length)}
      </mark>
      {text.slice(idx + query.length)}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Note detail slide-in panel
// ---------------------------------------------------------------------------

function NoteDetail({ note, onClose }) {
  if (!note) return null
  const displayName = (note.name || '').replace(/\.md$/, '')
  const body = stripFrontmatter(note.content || '')

  return (
    <div className="fixed inset-y-0 right-0 z-50 flex w-[480px] flex-col overflow-hidden border-l border-zinc-800 bg-zinc-950 shadow-2xl">
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-zinc-800 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <BookOpen size={14} className="shrink-0 text-violet-400" />
          <h1 className="truncate text-sm font-semibold text-zinc-100">{displayName}</h1>
        </div>
        <div className="ml-3 flex shrink-0 items-center gap-2">
          <a
            href={`obsidian://open?vault=OBSIDIAN&file=${encodeURIComponent((note.path || '').replace(/\.md$/, ''))}`}
            title="Open in Obsidian app"
            className="rounded-lg px-2 py-1 text-[11px] text-violet-400 hover:bg-violet-900/30 hover:text-violet-200"
          >
            Open ↗
          </a>
          <button
            onClick={onClose}
            title="Close"
            aria-label="Close"
            className="rounded-lg p-1.5 text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
          >
            <X size={15} />
          </button>
        </div>
      </div>

      {/* Scrollable body */}
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {/* Tags */}
        {(note.tags || []).length > 0 && (
          <div className="flex flex-wrap gap-1">
            {note.tags.map((tag) => (
              <span
                key={tag}
                className="rounded-full bg-violet-900/50 px-2 py-0.5 text-[10px] text-violet-300"
              >
                #{tag}
              </span>
            ))}
          </div>
        )}

        {/* Content */}
        <pre className="whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-zinc-300">
          {body || '(empty note)'}
        </pre>

        {/* Wikilinks */}
        {(note.wikilinks || []).length > 0 && (
          <div>
            <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-widest text-zinc-500">
              Links
            </h3>
            <div className="flex flex-wrap gap-1">
              {note.wikilinks.map((link) => (
                <span
                  key={link}
                  className="rounded border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[11px] text-zinc-300"
                >
                  [[{link}]]
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Meta footer */}
        <div className="space-y-0.5 border-t border-zinc-800 pt-3 text-[10px] text-zinc-600">
          <div>Path: {note.path}</div>
          {note.mtime && <div>Modified: {relTime(note.mtime)}</div>}
          {note.size != null && <div>Size: {note.size} bytes</div>}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Overview tab
// ---------------------------------------------------------------------------

function OverviewTab({ stats, loading, error, activity }) {
  if (loading) return <Spinner />
  if (error) return <Empty text="Vault unreachable" />
  if (!stats) return null

  const maxCount = Math.max(...(stats.folders || []).map((f) => f.count), 1)

  return (
    <div className="space-y-5">
      {/* Stats row */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: 'Notes', value: fmtK(stats.total_notes) },
          { label: 'Words', value: fmtK(stats.total_words) },
          { label: 'Folders', value: String(stats.folders?.length ?? '–') },
        ].map(({ label, value }) => (
          <div
            key={label}
            className="rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-3 text-center"
          >
            <div className="text-2xl font-bold tabular-nums text-zinc-100">{value}</div>
            <div className="mt-0.5 text-[11px] uppercase tracking-wider text-zinc-500">{label}</div>
          </div>
        ))}
      </div>

      {/* Top folders */}
      {(stats.folders || []).length > 0 && (
        <div>
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-widest text-zinc-500">
            Top Folders
          </h3>
          <div className="space-y-1.5">
            {stats.folders.slice(0, 10).map((f) => (
              <div key={f.name} className="flex items-center gap-2">
                <span className="w-32 shrink-0 truncate text-right text-xs text-zinc-400">
                  {f.name || '(root)'}
                </span>
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-800">
                  <div
                    className="h-full rounded-full bg-violet-500 transition-all"
                    style={{ width: `${(f.count / maxCount) * 100}%` }}
                  />
                </div>
                <span className="w-8 text-right text-xs tabular-nums text-zinc-500">
                  {f.count}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Top tags */}
      {(stats.top_tags || []).length > 0 && (
        <div>
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-widest text-zinc-500">
            Top Tags
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {stats.top_tags.slice(0, 20).map((t) => (
              <span
                key={t.tag}
                className="flex items-center gap-1 rounded-full border border-zinc-700 bg-zinc-800 px-2.5 py-0.5 text-[11px] text-zinc-300"
              >
                #{t.tag}
                <span className="tabular-nums text-zinc-500">{t.count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 7-day vault activity sparkline */}
      {activity && (
        <div className="mt-4">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-600">7-day vault activity</span>
            <span className="text-[10px] text-zinc-600">{activity.total_modified} notes modified</span>
          </div>
          <div className="flex items-end gap-1 h-12">
            {activity.counts.map((count, i) => {
              const max = Math.max(...activity.counts, 1)
              const h = Math.max(2, Math.round((count / max) * 44))
              const date = activity.dates[i]
              const label = new Date(date + 'T12:00:00').toLocaleDateString([], { weekday: 'short' })
              return (
                <div key={date} className="flex flex-1 flex-col items-center gap-0.5" title={`${label}: ${count} notes`}>
                  <div
                    className="w-full rounded-t bg-violet-600/70 hover:bg-violet-500 transition-colors"
                    style={{ height: h }}
                  />
                  <span className="text-[9px] text-zinc-600">{label.slice(0,1)}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Vault footer */}
      <p className="border-t border-zinc-800 pt-3 text-[10px] text-zinc-600">
        Vault path is set in policy.yaml (vault.root)
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Notes tab
// ---------------------------------------------------------------------------

function NotesTab({ notes, loading, error, onSelectNote }) {
  const [search, setSearch] = useState('')

  if (loading) return <Spinner />
  if (error) return <Empty text="Vault unreachable" />
  if (!notes) return null

  const filtered = notes.filter(
    (n) => !search || n.name.toLowerCase().includes(search.toLowerCase()),
  )

  return (
    <div className="space-y-3">
      {/* Search */}
      <div className="relative">
        <Search
          size={13}
          className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500"
        />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter notes…"
          className="w-full rounded-lg border border-zinc-700 bg-zinc-950 py-1.5 pl-8 pr-3 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-violet-600 focus:ring-1 focus:ring-violet-600/30"
        />
      </div>

      {/* List */}
      <div className="max-h-[60vh] space-y-0.5 overflow-y-auto pr-1">
        {filtered.length === 0 && (
          <div className="py-8 text-center text-sm text-zinc-600">No notes found</div>
        )}
        {filtered.map((n) => {
          const folder = folderOf(n.path)
          return (
            <button
              key={n.path}
              onClick={() => onSelectNote(n.path)}
              className="group w-full rounded-lg border border-transparent px-3 py-2 text-left transition-colors hover:border-zinc-800 hover:bg-zinc-800/50"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-sm font-medium text-zinc-200">
                  {n.name.replace(/\.md$/, '')}
                </span>
                <span className="shrink-0 tabular-nums text-[10px] text-zinc-600">
                  {relTime(n.mtime)}
                </span>
              </div>
              {folder && (
                <div className="truncate text-[10px] text-zinc-600">{folder}</div>
              )}
              {n.preview && (
                <div className="mt-0.5 truncate text-[11px] text-zinc-500">
                  {n.preview.slice(0, 80)}
                </div>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Graph tab — force-directed SVG
// ---------------------------------------------------------------------------

const SVG_W = 800
const SVG_H = 500

function GraphTab({ graphData, loading, error, onSelectNote }) {
  const [positions, setPositions] = useState(null)
  const [simDone, setSimDone] = useState(false)
  const [tooltip, setTooltip] = useState(null) // { x, y, label }
  const [transform, _setTransform] = useState({ x: 0, y: 0, scale: 1 })
  const transformRef = useRef({ x: 0, y: 0, scale: 1 })
  const svgRef = useRef(null)
  const dragRef = useRef(null)

  // Keep transformRef in sync so event handlers always see latest value
  const setTransform = useCallback((upd) => {
    _setTransform((prev) => {
      const next = typeof upd === 'function' ? upd(prev) : upd
      transformRef.current = next
      return next
    })
  }, [])

  // Run simulation once when graphData changes
  useEffect(() => {
    if (!graphData?.nodes?.length) return
    setSimDone(false)
    setPositions(null)
    const id = setTimeout(() => {
      const pos = runSimulation(graphData.nodes, graphData.edges || [], SVG_W, SVG_H)
      setPositions(pos)
      setSimDone(true)
    }, 0)
    return () => clearTimeout(id)
  }, [graphData])

  // Wheel zoom — must be non-passive to call preventDefault
  const onWheel = useCallback((e) => {
    e.preventDefault()
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect) return
    setTransform((prev) => {
      const factor = e.deltaY < 0 ? 1.1 : 0.9
      const newScale = Math.max(0.3, Math.min(3.0, prev.scale * factor))
      const mx = e.clientX - rect.left
      const my = e.clientY - rect.top
      // Zoom toward cursor
      const sx = (mx - prev.x) / prev.scale
      const sy = (my - prev.y) / prev.scale
      return { scale: newScale, x: mx - sx * newScale, y: my - sy * newScale }
    })
  }, [setTransform])

  useEffect(() => {
    const el = svgRef.current
    if (!el) return
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [onWheel, simDone]) // re-attach when simDone changes (SVG mounts)

  // Drag-to-pan — uses transformRef so no stale closure
  const onMouseDown = useCallback((e) => {
    const tag = (e.target?.tagName || '').toLowerCase()
    // Let node clicks pass through; only drag on background elements
    if (tag === 'circle' || tag === 'text') return
    const t = transformRef.current
    dragRef.current = { startX: e.clientX - t.x, startY: e.clientY - t.y }

    const onMove = (ev) => {
      if (!dragRef.current) return
      setTransform((prev) => ({
        ...prev,
        x: ev.clientX - dragRef.current.startX,
        y: ev.clientY - dragRef.current.startY,
      }))
    }
    const onUp = () => {
      dragRef.current = null
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [setTransform])

  const resetZoom = useCallback(() => setTransform({ x: 0, y: 0, scale: 1 }), [setTransform])

  if (loading) return <Spinner />
  if (error) return <Empty text="Vault unreachable" />
  if (!graphData) return null

  const { nodes = [], edges = [] } = graphData

  return (
    <div className="relative">
      {/* Zoom reset */}
      <button
        onClick={resetZoom}
        className="absolute right-2 top-2 z-10 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
      >
        Reset zoom
      </button>

      <div
        className="overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950"
        style={{ height: SVG_H }}
      >
        {!simDone && <Spinner />}
        {simDone && positions && (
          <svg
            ref={svgRef}
            width="100%"
            height={SVG_H}
            viewBox={`0 0 ${SVG_W} ${SVG_H}`}
            preserveAspectRatio="xMidYMid meet"
            className="cursor-grab select-none active:cursor-grabbing"
            onMouseDown={onMouseDown}
          >
            {/* Transparent background rect to capture drag events */}
            <rect width={SVG_W} height={SVG_H} fill="transparent" />

            <g
              transform={`translate(${transform.x},${transform.y}) scale(${transform.scale})`}
            >
              {/* Edges */}
              {edges.map((e, i) => {
                const si = nodes.findIndex((n) => n.id === e.source)
                const ti = nodes.findIndex((n) => n.id === e.target)
                if (si < 0 || ti < 0 || !positions[si] || !positions[ti]) return null
                return (
                  <line
                    key={i}
                    x1={positions[si].x}
                    y1={positions[si].y}
                    x2={positions[ti].x}
                    y2={positions[ti].y}
                    stroke="#555"
                    strokeWidth={0.8}
                    opacity={0.4}
                  />
                )
              })}

              {/* Nodes */}
              {nodes.map((node, i) => {
                if (!positions[i]) return null
                const { x, y } = positions[i]
                const r = mapRadius(node.size)
                const color = nodeColor(node.folder)
                const label = (node.label || node.id || '').replace(/\.md$/, '')
                const shortLabel = label.length > 15 ? `${label.slice(0, 14)}…` : label

                return (
                  <g
                    key={node.id}
                    transform={`translate(${x},${y})`}
                    className="cursor-pointer"
                    onClick={() => node.id && onSelectNote(node.id)}
                    onMouseEnter={(e) => setTooltip({ x: e.clientX, y: e.clientY, label })}
                    onMouseLeave={() => setTooltip(null)}
                    onMouseMove={(e) =>
                      setTooltip((prev) => (prev ? { ...prev, x: e.clientX, y: e.clientY } : prev))
                    }
                  >
                    <circle r={r} fill={color} opacity={0.85} />
                    <text
                      x={0}
                      y={r + 10}
                      textAnchor="middle"
                      fontSize={9}
                      fill="#aaa"
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                      {shortLabel}
                    </text>
                  </g>
                )
              })}
            </g>
          </svg>
        )}
      </div>

      {/* Floating tooltip */}
      {tooltip && (
        <div
          style={{
            position: 'fixed',
            left: tooltip.x + 12,
            top: tooltip.y - 8,
            pointerEvents: 'none',
            zIndex: 9999,
          }}
          className="max-w-[220px] truncate rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-200 shadow-lg"
        >
          {tooltip.label}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Search tab
// ---------------------------------------------------------------------------

function SearchTab({ searchQ, setSearchQ, searchResults, searchLoading, onSelectNote, inputRef }) {
  return (
    <div className="space-y-3">
      {/* Prominent full-width search input */}
      <div className="relative">
        <Search
          size={15}
          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500"
        />
        <input
          ref={inputRef}
          type="text"
          value={searchQ}
          onChange={(e) => setSearchQ(e.target.value)}
          placeholder="Search across 347+ vault notes…"
          className="w-full rounded-xl border border-zinc-700 bg-zinc-950 py-2.5 pl-9 pr-9 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-violet-600 focus:ring-1 focus:ring-violet-600/30"
        />
        {searchLoading && (
          <RefreshCw
            size={13}
            className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 animate-spin text-violet-400"
          />
        )}
      </div>

      {/* Initial state — no query typed yet */}
      {!searchQ && (
        <div className="flex items-center justify-center py-12 text-sm text-zinc-600">
          Search across 347+ vault notes…
        </div>
      )}

      {/* Loading state — query present but results not yet arrived */}
      {searchQ && searchLoading && searchResults === null && <Spinner />}

      {/* Empty state — query returned no results */}
      {searchQ && !searchLoading && searchResults !== null && searchResults.length === 0 && (
        <Empty text="No matches in vault" />
      )}

      {/* Results list */}
      {searchResults !== null && searchResults.length > 0 && (
        <div className="max-h-[60vh] space-y-0.5 overflow-y-auto pr-1">
          {searchResults.map((result) => {
            const name = (result.name || '').replace(/\.md$/, '')
            return (
              <button
                key={result.path}
                onClick={() => onSelectNote(result.path)}
                className="group w-full rounded-lg border border-transparent px-3 py-2.5 text-left transition-colors hover:border-zinc-800 hover:bg-zinc-800/50"
              >
                {/* Name + folder row */}
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate text-sm font-medium text-zinc-200">{name}</span>
                  <span className="shrink-0 truncate text-right text-[10px] text-zinc-500">
                    {result.folder || '(root)'}
                  </span>
                </div>
                {/* Snippet + mtime row */}
                <div className="mt-0.5 flex items-baseline justify-between gap-2">
                  <span className="flex-1 overflow-hidden">
                    <HighlightSnippet text={result.snippet} query={searchQ} />
                  </span>
                  <span className="shrink-0 tabular-nums text-[10px] text-zinc-600">
                    {relTime(result.mtime)}
                  </span>
                </div>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Quick-capture modal
// ---------------------------------------------------------------------------

const CAPTURE_FOLDERS = ['', 'Personal', 'Chieff']

function QuickCaptureModal({ onClose, onCreated }) {
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [folder, setFolder] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  const submit = async () => {
    if (!title.trim()) return
    setBusy(true)
    try {
      const r = await fetch('/api/obsidian/note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title.trim(), content, folder }),
      })
      if (!r.ok) throw new Error(await r.text())
      toast('Note saved to vault', 'success')
      onCreated?.()
      onClose()
    } catch (e) {
      toast(`Failed: ${String(e).slice(0, 100)}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const onKeyDown = (e) => {
    if (e.key === 'Escape') onClose()
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit()
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onKeyDown={onKeyDown}
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="w-full max-w-lg rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center gap-2">
            <Plus size={14} className="text-violet-400" />
            <h2 className="text-sm font-semibold text-zinc-100">Quick Capture</h2>
          </div>
          <button
            onClick={onClose}
            title="Close"
            aria-label="Close"
            className="rounded-lg p-1 text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
          >
            <X size={15} />
          </button>
        </div>

        {/* Body */}
        <div className="space-y-3 p-4">
          {/* Title */}
          <div>
            <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-zinc-500">
              Title <span className="text-red-400">*</span>
            </label>
            <input
              autoFocus
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Note title…"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-violet-600 focus:ring-1 focus:ring-violet-600/30"
            />
          </div>

          {/* Content */}
          <div>
            <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-zinc-500">
              Content
            </label>
            <textarea
              rows={8}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="Write your note here…"
              className="w-full resize-none rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-violet-600 focus:ring-1 focus:ring-violet-600/30"
            />
          </div>

          {/* Folder */}
          <div>
            <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-zinc-500">
              Folder
            </label>
            <select
              value={folder}
              onChange={(e) => setFolder(e.target.value)}
              className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 outline-none focus:border-violet-600 focus:ring-1 focus:ring-violet-600/30"
            >
              {CAPTURE_FOLDERS.map((f) => (
                <option key={f} value={f}>
                  {f || '(vault root)'}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-zinc-800 px-4 py-3">
          <span className="text-[10px] text-zinc-600">⌘↵ to save</span>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 text-sm text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
            >
              Cancel
            </button>
            <button
              onClick={submit}
              disabled={busy || !title.trim()}
              className="flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? <RefreshCw size={13} className="animate-spin" /> : <Plus size={13} />}
              {busy ? 'Saving…' : 'Save note'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

const SUB_TABS = ['Overview', 'Notes', 'Graph', 'Search']

export default function ObsidianPanel() {
  const [subTab, setSubTab] = useState('Overview')

  // Quick-capture modal
  const [captureOpen, setCaptureOpen] = useState(false)
  const [capturing, setCapturing] = useState(false)

  // Overview
  const [stats, setStats] = useState(null)
  const [statsLoading, setStatsLoading] = useState(false)
  const [statsError, setStatsError] = useState(null)
  const [activity, setActivity] = useState(null)

  // Notes
  const [notes, setNotes] = useState(null)
  const [notesLoading, setNotesLoading] = useState(false)
  const [notesError, setNotesError] = useState(null)

  // Graph
  const [graphData, setGraphData] = useState(null)
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState(null)

  // Search
  const [searchQ, setSearchQ] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [searchLoading, setSearchLoading] = useState(false)
  const searchInputRef = useRef(null)

  // Note detail
  const [selectedPath, setSelectedPath] = useState(null)
  const [noteDetail, setNoteDetail] = useState(null)
  const [noteDetailLoading, setNoteDetailLoading] = useState(false)

  // Load stats once on mount
  useEffect(() => {
    setStatsLoading(true)
    fetch('/api/obsidian/stats')
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json() })
      .then((d) => { setStats(d); setStatsError(null) })
      .catch(() => setStatsError(true))
      .finally(() => setStatsLoading(false))
  }, [])

  // Load vault activity sparkline once on mount
  useEffect(() => {
    fetch('/api/obsidian/activity?days=7').then(r => r.json()).then(setActivity).catch(() => {})
  }, [])

  // Lazy-load notes on first visit to Notes tab
  useEffect(() => {
    if (subTab !== 'Notes' || notes !== null || notesLoading) return
    setNotesLoading(true)
    fetch('/api/obsidian/recent?limit=30')
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json() })
      .then((d) => { setNotes(d); setNotesError(null) })
      .catch(() => setNotesError(true))
      .finally(() => setNotesLoading(false))
  }, [subTab, notes, notesLoading])

  // Lazy-load graph on first visit to Graph tab
  useEffect(() => {
    if (subTab !== 'Graph' || graphData !== null || graphLoading) return
    setGraphLoading(true)
    fetch('/api/obsidian/graph?max_nodes=120')
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json() })
      .then((d) => { setGraphData(d); setGraphError(null) })
      .catch(() => setGraphError(true))
      .finally(() => setGraphLoading(false))
  }, [subTab, graphData, graphLoading])

  // Debounced vault search — fires 300 ms after searchQ settles
  useEffect(() => {
    if (!searchQ.trim()) {
      setSearchResults(null)
      setSearchLoading(false)
      return
    }
    setSearchLoading(true)
    const timer = setTimeout(() => {
      fetch(`/api/obsidian/search?q=${encodeURIComponent(searchQ)}&limit=15`)
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.json() })
        .then((d) => { setSearchResults(d); setSearchLoading(false) })
        .catch(() => { setSearchResults([]); setSearchLoading(false) })
    }, 300)
    return () => clearTimeout(timer)
  }, [searchQ])

  // Fetch full note when a path is selected
  const handleSelectNote = useCallback((path) => {
    setSelectedPath(path)
    setNoteDetail(null)
    setNoteDetailLoading(true)
    fetch(`/api/obsidian/note?path=${encodeURIComponent(path)}`)
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json() })
      .then(setNoteDetail)
      .catch(() => {
        // Show a stub so the panel still opens
        setNoteDetail({ name: path, path, content: 'Failed to load note.', wikilinks: [], tags: [] })
      })
      .finally(() => setNoteDetailLoading(false))
  }, [])

  const handleCloseDetail = useCallback(() => {
    setSelectedPath(null)
    setNoteDetail(null)
  }, [])

  // Refresh recent notes (called after a successful capture)
  const refreshNotes = useCallback(() => {
    setNotes(null)
    if (subTab === 'Notes') {
      setNotesLoading(true)
      fetch('/api/obsidian/recent?limit=30')
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.json() })
        .then((d) => { setNotes(d); setNotesError(null) })
        .catch(() => setNotesError(true))
        .finally(() => setNotesLoading(false))
    }
  }, [subTab])

  // Tab switch — auto-focuses search input when switching to Search
  const handleSubTabChange = useCallback((t) => {
    setSubTab(t)
    if (t === 'Search') {
      setTimeout(() => searchInputRef.current?.focus(), 50)
    }
  }, [])

  // Sub-tab pill buttons + Capture button rendered in the Panel actions area
  const subTabActions = (
    <div className="flex items-center gap-2">
      <div className="flex gap-0.5 rounded-lg bg-zinc-950 p-0.5">
        {SUB_TABS.map((t) => (
          <button
            key={t}
            onClick={() => handleSubTabChange(t)}
            className={`press rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
              subTab === t
                ? 'bg-zinc-800 text-zinc-100 shadow-sm'
                : 'text-zinc-500 hover:text-zinc-300'
            }`}
          >
            {t}
          </button>
        ))}
      </div>
      <button
        onClick={() => setCaptureOpen(true)}
        className="flex items-center gap-1 rounded-lg border border-violet-700/60 bg-violet-900/30 px-2.5 py-1 text-xs font-medium text-violet-300 transition-colors hover:bg-violet-800/40 hover:text-violet-100"
      >
        <Plus size={11} />
        Capture
      </button>
    </div>
  )

  // Detail note to display: while loading show a stub
  const detailNote = selectedPath
    ? noteDetailLoading
      ? { name: selectedPath, path: selectedPath, content: 'Loading…', wikilinks: [], tags: [] }
      : noteDetail
    : null

  return (
    <>
      <Panel
        title="Obsidian"
        badge={stats?.total_notes ?? '·'}
        actions={subTabActions}
      >
        {subTab === 'Overview' && (
          <OverviewTab stats={stats} loading={statsLoading} error={statsError} activity={activity} />
        )}
        {subTab === 'Notes' && (
          <NotesTab
            notes={notes}
            loading={notesLoading}
            error={notesError}
            onSelectNote={handleSelectNote}
          />
        )}
        {subTab === 'Graph' && (
          <GraphTab
            graphData={graphData}
            loading={graphLoading}
            error={graphError}
            onSelectNote={handleSelectNote}
          />
        )}
        {subTab === 'Search' && (
          <SearchTab
            searchQ={searchQ}
            setSearchQ={setSearchQ}
            searchResults={searchResults}
            searchLoading={searchLoading}
            onSelectNote={handleSelectNote}
            inputRef={searchInputRef}
          />
        )}
      </Panel>

      {/* Slide-in note detail */}
      {detailNote && <NoteDetail note={detailNote} onClose={handleCloseDetail} />}

      {/* Quick-capture modal */}
      {captureOpen && (
        <QuickCaptureModal
          onClose={() => { setCaptureOpen(false); setCapturing(false) }}
          onCreated={refreshNotes}
        />
      )}
    </>
  )
}
