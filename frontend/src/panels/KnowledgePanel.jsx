import React, { useEffect, useState, useCallback, useMemo } from 'react'
import {
  BookOpen, BarChart3, Search, ExternalLink, RefreshCw, AlertTriangle,
  CheckCircle2, FileText, Code2, Image, GitBranch, Tag, TrendingUp,
  Users, Clock, Database, Layers, Zap, BookMarked, Map, Target,
  ChevronRight, ChevronDown, Activity,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { useToast, SkeletonRows } from '../ui.jsx'

const TABS = [
  { id: 'overview', label: 'Overview', icon: BookOpen },
  { id: 'usecases', label: 'Use Cases', icon: Target },
  { id: 'plans', label: 'Plans', icon: Map },
  { id: 'docgaps', label: 'Doc Gaps', icon: AlertTriangle },
  { id: 'stats', label: 'Insights', icon: BarChart3 },
]

const USE_CASE_COLOR = {
  Intake: 'sky', Pipeline: 'emerald', Docs: 'violet', Platform: 'amber', Other: 'zinc',
}
// Fully-literal per-color classes — Tailwind's scanner does static text
// search on this file's source, not JS evaluation, so even a computed
// `bg-${c}-950` (built from a small fixed color list) never matches; every
// class name has to appear spelled out somewhere for its CSS to be generated.
const USE_CASE_CLS = {
  sky: {
    badge: 'bg-sky-950 text-sky-400 border border-sky-900',
    card: 'border-sky-900 bg-sky-950/10',
    heading: 'text-sky-300',
    count: 'bg-sky-900 text-sky-500',
    tag: 'bg-sky-950 text-sky-500 border border-sky-900',
    icon: 'text-sky-600',
  },
  emerald: {
    badge: 'bg-emerald-950 text-emerald-400 border border-emerald-900',
    card: 'border-emerald-900 bg-emerald-950/10',
    heading: 'text-emerald-300',
    count: 'bg-emerald-900 text-emerald-500',
    tag: 'bg-emerald-950 text-emerald-500 border border-emerald-900',
    icon: 'text-emerald-600',
  },
  violet: {
    badge: 'bg-violet-950 text-violet-400 border border-violet-900',
    card: 'border-violet-900 bg-violet-950/10',
    heading: 'text-violet-300',
    count: 'bg-violet-900 text-violet-500',
    tag: 'bg-violet-950 text-violet-500 border border-violet-900',
    icon: 'text-violet-600',
  },
  amber: {
    badge: 'bg-amber-950 text-amber-400 border border-amber-900',
    card: 'border-amber-900 bg-amber-950/10',
    heading: 'text-amber-300',
    count: 'bg-amber-900 text-amber-500',
    tag: 'bg-amber-950 text-amber-500 border border-amber-900',
    icon: 'text-amber-600',
  },
  zinc: {
    badge: 'bg-zinc-950 text-zinc-400 border border-zinc-900',
    card: 'border-zinc-900 bg-zinc-950/10',
    heading: 'text-zinc-300',
    count: 'bg-zinc-900 text-zinc-500',
    tag: 'bg-zinc-950 text-zinc-500 border border-zinc-900',
    icon: 'text-zinc-600',
  },
}
const DOC_TYPE_ICON = {
  architecture: Layers, runbook: BookMarked, guide: BookOpen,
  'meeting-notes': Users, other: FileText,
}

// Tailwind's scanner needs full literal class strings per color — a template
// literal like `bg-${color}-950/30` only generates CSS if that exact string
// also appears elsewhere verbatim, so most colors here silently rendered
// unstyled. Static maps keep every variant visible to the scanner.
const SCORE_BAR_CLS = {
  emerald: { bar: 'bg-emerald-500', text: 'text-emerald-400' },
  amber: { bar: 'bg-amber-500', text: 'text-amber-400' },
  red: { bar: 'bg-red-500', text: 'text-red-400' },
}

const WORK_ITEM_CLS = {
  violet: { label: 'text-violet-400', bar: 'bg-violet-600', count: 'text-violet-500' },
  sky: { label: 'text-sky-400', bar: 'bg-sky-600', count: 'text-sky-500' },
  emerald: { label: 'text-emerald-400', bar: 'bg-emerald-600', count: 'text-emerald-500' },
  teal: { label: 'text-teal-400', bar: 'bg-teal-600', count: 'text-teal-500' },
  red: { label: 'text-red-400', bar: 'bg-red-600', count: 'text-red-500' },
  zinc: { label: 'text-zinc-400', bar: 'bg-zinc-600', count: 'text-zinc-500' },
}

function ScoreBar({ score, max = 100 }) {
  const pct = Math.round((score / max) * 100)
  const cls = SCORE_BAR_CLS[score >= 80 ? 'emerald' : score >= 50 ? 'amber' : 'red']
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 flex-1 rounded-full bg-zinc-800">
        <div className={`h-1.5 rounded-full ${cls.bar} transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-xs tabular-nums ${cls.text}`}>{score}</span>
    </div>
  )
}

// Same static-literal requirement as USE_CASE_CLS above.
const STAT_CARD_CLS = {
  sky: { card: 'border-sky-900 bg-sky-950/30', label: 'text-sky-600', value: 'text-sky-300', sub: 'text-sky-600', icon: 'text-sky-700' },
  violet: { card: 'border-violet-900 bg-violet-950/30', label: 'text-violet-600', value: 'text-violet-300', sub: 'text-violet-600', icon: 'text-violet-700' },
  emerald: { card: 'border-emerald-900 bg-emerald-950/30', label: 'text-emerald-600', value: 'text-emerald-300', sub: 'text-emerald-600', icon: 'text-emerald-700' },
  amber: { card: 'border-amber-900 bg-amber-950/30', label: 'text-amber-600', value: 'text-amber-300', sub: 'text-amber-600', icon: 'text-amber-700' },
  red: { card: 'border-red-900 bg-red-950/30', label: 'text-red-600', value: 'text-red-300', sub: 'text-red-600', icon: 'text-red-700' },
  zinc: { card: 'border-zinc-900 bg-zinc-950/30', label: 'text-zinc-600', value: 'text-zinc-300', sub: 'text-zinc-600', icon: 'text-zinc-700' },
}

function StatCard({ label, value, sub, icon: Icon, color = 'zinc' }) {
  const cls = STAT_CARD_CLS[color] || STAT_CARD_CLS.zinc
  return (
    <div className={`rounded-xl border ${cls.card} px-4 py-3`}>
      <div className="flex items-start justify-between">
        <div>
          <div className={`text-[10px] uppercase tracking-wide ${cls.label}`}>{label}</div>
          <div className={`text-2xl font-bold tabular-nums ${cls.value}`}>{value}</div>
          {sub && <div className={`text-[11px] ${cls.sub}`}>{sub}</div>}
        </div>
        {Icon && <Icon size={20} className={cls.icon} />}
      </div>
    </div>
  )
}

// ── Overview tab ─────────────────────────────────────────────────────────────
function OverviewTab({ pages, stats, onRefresh, busy }) {
  const [q, setQ] = useState('')
  const [uc, setUc] = useState('all')
  const [dt, setDt] = useState('all')

  const filtered = useMemo(() => {
    const ql = q.toLowerCase()
    return (pages || []).filter((p) =>
      (!ql || p.title?.toLowerCase().includes(ql) || (p.linked_repos || []).some((r) => r.includes(ql))) &&
      (uc === 'all' || p.use_case === uc) &&
      (dt === 'all' || p.doc_type === dt)
    )
  }, [pages, q, uc, dt])

  const useCases = ['all', ...Object.keys(USE_CASE_COLOR)]
  const docTypes = ['all', 'architecture', 'runbook', 'guide', 'meeting-notes', 'other']

  return (
    <div className="space-y-4">
      {/* Summary strip */}
      <div className="grid grid-cols-4 gap-2">
        <StatCard label="Pages" value={stats?.total_pages ?? '—'} icon={FileText} color="sky" />
        <StatCard label="w/ Code" value={stats?.pages_with_code ?? '—'} icon={Code2} color="violet" />
        <StatCard label="Avg words" value={stats?.avg_word_count ? Math.round(stats.avg_word_count) : '—'} icon={BookOpen} color="emerald" />
        <StatCard label="Authors" value={stats?.top_authors?.length ?? '—'} icon={Users} color="amber" />
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
          <input
            value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search pages…"
            className="w-full rounded-lg border border-zinc-800 bg-zinc-900 py-1.5 pl-8 pr-3 text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-600"
          />
        </div>
        <select value={uc} onChange={(e) => setUc(e.target.value)}
          className="rounded-lg border border-zinc-800 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-300 focus:outline-none">
          {useCases.map((u) => <option key={u} value={u}>{u === 'all' ? 'All use cases' : u}</option>)}
        </select>
        <select value={dt} onChange={(e) => setDt(e.target.value)}
          className="rounded-lg border border-zinc-800 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-300 focus:outline-none">
          {docTypes.map((d) => <option key={d} value={d}>{d === 'all' ? 'All types' : d}</option>)}
        </select>
        <button onClick={onRefresh} disabled={busy}
          className="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-400 hover:text-zinc-200 disabled:opacity-50">
          <RefreshCw size={12} className={busy ? 'animate-spin' : ''} />Full sync
        </button>
      </div>

      {/* Page list */}
      <div className="space-y-1">
        {filtered.length === 0 && <div className="py-8 text-center text-sm text-zinc-600">No pages{q ? ` matching "${q}"` : ''}</div>}
        {filtered.map((p) => {
          const DIcon = DOC_TYPE_ICON[p.doc_type] || FileText
          const ucCls = USE_CASE_CLS[USE_CASE_COLOR[p.use_case]] || USE_CASE_CLS.zinc
          const repos = Array.isArray(p.linked_repos) ? p.linked_repos : (p.linked_repos ? JSON.parse(p.linked_repos) : [])
          return (
            <a key={p.id} href={p.url} target="_blank" rel="noreferrer"
              className="group flex items-start gap-3 rounded-lg border border-transparent px-3 py-2.5 hover:border-zinc-800 hover:bg-zinc-900/50">
              <DIcon size={15} className="mt-0.5 shrink-0 text-zinc-600" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-zinc-200">{p.title}</span>
                  <span className={`shrink-0 rounded px-1 py-0.5 text-[10px] ${ucCls.badge}`}>
                    {p.use_case}
                  </span>
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-zinc-600">
                  {p.doc_type && <span className="capitalize">{p.doc_type}</span>}
                  {p.word_count > 0 && <span>{p.word_count} words</span>}
                  {p.has_code > 0 && <span className="flex items-center gap-0.5"><Code2 size={10} />code</span>}
                  {p.last_edited_by && p.last_edited_by !== '?' && <span>{p.last_edited_by}</span>}
                  {p.last_edited_at && <span>{p.last_edited_at?.slice(0, 10)}</span>}
                  {repos.slice(0, 2).map((r) => (
                    <span key={r} className="flex items-center gap-0.5 text-violet-600"><GitBranch size={9} />{r}</span>
                  ))}
                </div>
              </div>
              <ExternalLink size={12} className="mt-1 shrink-0 text-zinc-700 opacity-0 transition-opacity group-hover:opacity-100" />
            </a>
          )
        })}
      </div>
    </div>
  )
}

// ── Use Cases tab ─────────────────────────────────────────────────────────────
function UseCasesTab({ stats, pages }) {
  const byUC = useMemo(() => {
    const m = {}
    for (const uc of ['Intake', 'Pipeline', 'Docs', 'Platform', 'Other']) {
      const ucPages = (pages || []).filter((p) => p.use_case === uc)
      const adoEpics = (stats?.ado_stats?.active_epics || []).filter((e) =>
        e.tags?.toLowerCase().includes(uc.toLowerCase().slice(0, 6)) ||
        e.title?.toLowerCase().includes(uc.toLowerCase().slice(0, 6))
      )
      m[uc] = { pages: ucPages, epics: adoEpics }
    }
    return m
  }, [stats, pages])

  const adoByState = stats?.ado_stats?.by_state || {}
  const adoTotal = Object.values(adoByState).reduce((a, b) => a + b, 0) || 1

  return (
    <div className="space-y-5">
      {/* ADO health strip */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">ADO Work Item Health</div>
        <div className="flex gap-2 flex-wrap">
          {Object.entries(adoByState).sort(([,a],[,b]) => b-a).map(([state, n]) => {
            const pct = Math.round((n / adoTotal) * 100)
            const cKey = state === 'Active' ? 'emerald' : state === 'Closed' ? 'zinc' : state === 'New' ? 'sky' : 'amber'
            const c = STAT_CARD_CLS[cKey]
            return (
              <div key={state} className={`rounded-xl border ${c.card} px-3 py-2 min-w-[80px]`}>
                <div className={`text-lg font-bold tabular-nums ${c.value}`}>{n}</div>
                <div className={`text-[10px] ${c.sub}`}>{state} · {pct}%</div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Per-use-case cards */}
      {Object.entries(byUC).map(([uc, { pages: ucPages, epics }]) => {
        const c = USE_CASE_CLS[USE_CASE_COLOR[uc]] || USE_CASE_CLS.zinc
        const docScore = ucPages.length === 0 ? 0 : ucPages.length >= 3 ? 90 : 60
        const hasArch = ucPages.some((p) => p.doc_type === 'architecture')
        const hasRunbook = ucPages.some((p) => p.doc_type === 'runbook')
        const hasGuide = ucPages.some((p) => p.doc_type === 'guide')
        return (
          <div key={uc} className={`rounded-xl border ${c.card} p-4`}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <span className={`text-base font-bold ${c.heading}`}>{uc}</span>
                <span className={`rounded px-1.5 py-0.5 text-[10px] ${c.count}`}>
                  {ucPages.length} page{ucPages.length !== 1 ? 's' : ''}
                </span>
                {epics.length > 0 && (
                  <span className="rounded px-1.5 py-0.5 text-[10px] bg-zinc-900 text-zinc-500">
                    {epics.length} epic{epics.length !== 1 ? 's' : ''}
                  </span>
                )}
              </div>
              <div className="w-32">
                <ScoreBar score={docScore} />
              </div>
            </div>

            {/* Doc coverage pills */}
            <div className="flex gap-1.5 mb-3">
              {[['arch', hasArch, 'architecture'], ['runbook', hasRunbook, 'runbook'], ['guide', hasGuide, 'guide']].map(([lbl, has, type]) => (
                <span key={lbl} className={`flex items-center gap-1 rounded px-2 py-0.5 text-[10px] ${has ? `bg-emerald-900 text-emerald-300` : 'bg-zinc-900 text-zinc-600'}`}>
                  {has ? <CheckCircle2 size={9} /> : <AlertTriangle size={9} />} {lbl}
                </span>
              ))}
            </div>

            {/* Pages */}
            {ucPages.length > 0 && (
              <div className="space-y-1">
                {ucPages.map((p) => (
                  <a key={p.id} href={p.url} target="_blank" rel="noreferrer"
                    className="flex items-center gap-2 text-[11px] text-zinc-500 hover:text-zinc-300">
                    <FileText size={10} className="shrink-0" />
                    <span className="truncate">{p.title}</span>
                    <span className="ml-auto shrink-0 capitalize text-zinc-700">{p.doc_type}</span>
                  </a>
                ))}
              </div>
            )}

            {/* Active epics */}
            {epics.length > 0 && (
              <div className="mt-2 border-t border-zinc-800 pt-2 space-y-1">
                <div className="text-[10px] uppercase tracking-wide text-zinc-600 mb-1">Active Epics</div>
                {epics.slice(0, 3).map((e) => (
                  <div key={e.id} className="flex items-center gap-2 text-[11px]">
                    <Target size={9} className={c.icon} />
                    <span className="truncate text-zinc-400">{e.title?.replace(/^Epic \d+: /, '')}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Plans tab ─────────────────────────────────────────────────────────────────
function PlansTab({ stats }) {
  const byType = stats?.ado_stats?.by_type || {}
  const epics = stats?.ado_stats?.active_epics || []
  const typeOrder = ['Epic', 'Feature', 'Product Backlog Item', 'User Story', 'Bug', 'Task']
  const total = Object.values(byType).reduce((a, b) => a + b, 0) || 1

  return (
    <div className="space-y-5">
      {/* Work item type breakdown */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Work Item Breakdown</div>
        <div className="space-y-2">
          {typeOrder.filter((t) => byType[t]).map((t) => {
            const n = byType[t] || 0
            const pct = Math.round((n / total) * 100)
            const colors = { Epic: 'violet', Feature: 'sky', 'Product Backlog Item': 'emerald', 'User Story': 'teal', Bug: 'red', Task: 'zinc' }
            const c = WORK_ITEM_CLS[colors[t]] || WORK_ITEM_CLS.zinc
            return (
              <div key={t} className="flex items-center gap-3">
                <div className={`w-28 shrink-0 text-[11px] ${c.label}`}>{t}</div>
                <div className="flex-1 h-2 rounded-full bg-zinc-800">
                  <div className={`h-2 rounded-full ${c.bar}`} style={{ width: `${pct}%` }} />
                </div>
                <span className={`w-12 text-right text-xs tabular-nums ${c.count}`}>{n}</span>
              </div>
            )
          })}
        </div>
      </div>

      {/* Active epics detail */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Active Epics</div>
        <div className="space-y-2">
          {epics.length === 0 && <div className="text-sm text-zinc-600">No active epics found</div>}
          {epics.map((e) => {
            const tags = (e.tags || '').split(';').map((t) => t.trim()).filter(Boolean)
            const uc = tags.includes('intake') ? 'Intake' : tags.includes('use-case') ? 'Pipeline' :
              e.title?.toLowerCase().includes('intake') ? 'Intake' :
              e.title?.toLowerCase().includes('pipeline') ? 'Pipeline' : 'Platform'
            const c = USE_CASE_CLS[USE_CASE_COLOR[uc]] || USE_CASE_CLS.zinc
            return (
              <div key={e.id} className="rounded-lg border border-zinc-800 bg-zinc-900/30 px-3 py-2.5">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <span className={`shrink-0 text-[10px] rounded px-1 ${c.tag}`}>{uc}</span>
                      <span className="text-sm text-zinc-200 truncate">{e.title}</span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {tags.filter((t) => !['use-case', 'arc', uc.toLowerCase()].includes(t)).slice(0, 4).map((t) => (
                        <span key={t} className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">{t}</span>
                      ))}
                    </div>
                  </div>
                  <span className="shrink-0 rounded bg-emerald-950 px-1.5 py-0.5 text-[10px] text-emerald-500">Active</span>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ── Doc Gaps tab ──────────────────────────────────────────────────────────────
function DocGapsTab({ docGaps }) {
  const repos = docGaps?.repos || []
  const undoc = repos.filter((r) => !r.has_doc)
  const partial = repos.filter((r) => r.has_doc && r.score < 80)
  const full = repos.filter((r) => r.has_doc && r.score >= 80)

  const coveragePct = repos.length ? Math.round((full.length / repos.length) * 100) : 0

  return (
    <div className="space-y-5">
      {/* Summary strip */}
      <div className="grid grid-cols-3 gap-2">
        <StatCard label="Fully covered" value={full.length} icon={CheckCircle2} color="emerald" />
        <StatCard label="Partial docs" value={partial.length} icon={AlertTriangle} color="amber" />
        <StatCard label="No docs" value={undoc.length} icon={AlertTriangle} color="red" />
      </div>

      {/* Coverage bar */}
      <div>
        <div className="mb-1 flex items-center justify-between text-xs">
          <span className="text-zinc-500 uppercase tracking-wide font-semibold">Documentation coverage</span>
          <span className={`tabular-nums font-bold ${coveragePct >= 70 ? 'text-emerald-400' : coveragePct >= 40 ? 'text-amber-400' : 'text-red-400'}`}>{coveragePct}%</span>
        </div>
        <div className="h-3 rounded-full bg-zinc-800 overflow-hidden">
          <div className="h-3 rounded-full bg-gradient-to-r from-emerald-600 to-emerald-400 transition-all" style={{ width: `${coveragePct}%` }} />
        </div>
      </div>

      {/* Undocumented repos */}
      {undoc.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-red-600">
            <AlertTriangle size={11} /> No documentation
          </div>
          <div className="space-y-1">
            {undoc.map((r) => (
              <div key={r.name} className="flex items-center gap-2 rounded-lg border border-red-950 bg-red-950/10 px-3 py-2">
                <GitBranch size={13} className="shrink-0 text-red-800" />
                <span className="flex-1 text-sm text-zinc-400">{r.name}</span>
                <div className="flex gap-1">
                  {(r.gaps || []).slice(0, 3).map((g) => (
                    <span key={g} className="rounded bg-red-950 px-1.5 py-0.5 text-[10px] text-red-600">{g}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Partial docs */}
      {partial.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-amber-600">
            <AlertTriangle size={11} /> Partial documentation
          </div>
          <div className="space-y-1">
            {partial.map((r) => (
              <div key={r.name} className="rounded-lg border border-amber-950 bg-amber-950/10 px-3 py-2.5">
                <div className="flex items-center gap-2 mb-1.5">
                  <GitBranch size={13} className="shrink-0 text-amber-700" />
                  <span className="flex-1 text-sm text-zinc-400">{r.name}</span>
                  <div className="w-24">
                    <ScoreBar score={r.score} />
                  </div>
                </div>
                {r.pages?.length > 0 && (
                  <div className="ml-5 space-y-0.5">
                    {r.pages.slice(0, 2).map((pg) => (
                      <div key={pg} className="flex items-center gap-1.5 text-[11px] text-zinc-600">
                        <FileText size={9} />{pg}
                      </div>
                    ))}
                  </div>
                )}
                {r.gaps?.length > 0 && (
                  <div className="ml-5 mt-1 flex gap-1 flex-wrap">
                    {r.gaps.map((g) => (
                      <span key={g} className="rounded bg-amber-950 px-1.5 py-0.5 text-[10px] text-amber-700">missing: {g}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Well-documented */}
      {full.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-emerald-600">
            <CheckCircle2 size={11} /> Well documented
          </div>
          <div className="space-y-1">
            {full.map((r) => (
              <div key={r.name} className="flex items-center gap-2 rounded-lg border border-emerald-950 bg-emerald-950/10 px-3 py-2">
                <GitBranch size={13} className="shrink-0 text-emerald-700" />
                <span className="flex-1 text-sm text-zinc-400">{r.name}</span>
                <div className="flex gap-1">
                  {(r.pages || []).slice(0, 2).map((pg) => (
                    <span key={pg} className="rounded bg-emerald-950 px-1.5 py-0.5 text-[10px] text-emerald-700 truncate max-w-[120px]">{pg}</span>
                  ))}
                </div>
                <div className="w-20">
                  <ScoreBar score={r.score} />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Insights tab ──────────────────────────────────────────────────────────────
function InsightsTab({ stats }) {
  const staleness = stats?.staleness || {}
  const topAuthors = stats?.top_authors || []
  const byUC = stats?.by_use_case || {}
  const byDT = stats?.by_doc_type || {}
  const totalPages = stats?.total_pages || 0

  // Simple donut segments via SVG
  function MiniDonut({ data, colors }) {
    const total = Object.values(data).reduce((a, b) => a + b, 0) || 1
    let angle = 0
    const r = 28, cx = 32, cy = 32, strokeW = 10
    const circumference = 2 * Math.PI * r
    const segments = Object.entries(data).map(([k, v]) => {
      const pct = v / total
      const dash = pct * circumference
      const gap = circumference - dash
      const offset = -angle * circumference
      angle += pct
      return { key: k, dash, gap, offset, pct }
    })
    const colorList = Object.keys(data).map((k, i) => colors[k] || colors[i] || '#52525b')
    return (
      <svg width={64} height={64} className="shrink-0">
        {segments.map((s, i) => (
          <circle key={s.key} cx={cx} cy={cy} r={r} fill="none"
            stroke={colorList[i]} strokeWidth={strokeW}
            strokeDasharray={`${s.dash} ${s.gap}`}
            strokeDashoffset={circumference * 0.25 + s.offset}
            style={{ transform: 'rotate(-90deg)', transformOrigin: '50% 50%' }} />
        ))}
        <text x={cx} y={cy + 1} textAnchor="middle" dominantBaseline="middle"
          className="fill-zinc-400" style={{ fontSize: 11, fontWeight: 600 }}>{totalPages}</text>
      </svg>
    )
  }

  const ucColors = { Intake: '#0ea5e9', Pipeline: '#10b981', Docs: '#8b5cf6', Platform: '#f59e0b', Other: '#71717a' }
  const dtColors = { architecture: '#8b5cf6', runbook: '#0ea5e9', guide: '#10b981', 'meeting-notes': '#f59e0b', other: '#52525b' }

  return (
    <div className="space-y-6">
      {/* Staleness */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Page freshness</div>
        <div className="grid grid-cols-3 gap-2">
          {[
            { label: 'Fresh (7d)', key: 'fresh_7d', color: 'emerald' },
            { label: '7–30 days', key: 'fresh_30d', color: 'amber' },
            { label: 'Stale 30d+', key: 'stale_30d_plus', color: 'red' },
          ].map(({ label, key, color }) => {
            const c = STAT_CARD_CLS[color]
            return (
              <div key={key} className={`rounded-xl border ${c.card} px-3 py-2.5`}>
                <div className={`text-xl font-bold tabular-nums ${c.value}`}>{staleness[key] ?? 0}</div>
                <div className={`text-[10px] ${c.sub}`}>{label}</div>
              </div>
            )
          })}
        </div>
      </div>

      {/* By use case + doc type */}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">By use case</div>
          <div className="flex items-center gap-3">
            <MiniDonut data={byUC} colors={ucColors} />
            <div className="space-y-1">
              {Object.entries(byUC).sort(([,a],[,b]) => b-a).map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 text-[11px]">
                  <div className="h-2 w-2 rounded-full shrink-0" style={{ background: ucColors[k] || '#52525b' }} />
                  <span className="text-zinc-400 w-20 truncate">{k}</span>
                  <span className="tabular-nums text-zinc-600">{v}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">By doc type</div>
          <div className="flex items-center gap-3">
            <MiniDonut data={byDT} colors={dtColors} />
            <div className="space-y-1">
              {Object.entries(byDT).sort(([,a],[,b]) => b-a).map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 text-[11px]">
                  <div className="h-2 w-2 rounded-full shrink-0" style={{ background: dtColors[k] || '#52525b' }} />
                  <span className="text-zinc-400 w-24 truncate capitalize">{k}</span>
                  <span className="tabular-nums text-zinc-600">{v}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Top authors */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Top contributors</div>
        <div className="space-y-1.5">
          {topAuthors.slice(0, 8).map((a, i) => {
            const max = topAuthors[0]?.count || 1
            const pct = Math.round((a.count / max) * 100)
            return (
              <div key={a.name} className="flex items-center gap-2.5">
                <span className="w-4 text-[10px] tabular-nums text-zinc-700">{i + 1}</span>
                <span className="w-36 truncate text-xs text-zinc-400">{a.name}</span>
                <div className="flex-1 h-1.5 rounded-full bg-zinc-800">
                  <div className="h-1.5 rounded-full bg-sky-700" style={{ width: `${pct}%` }} />
                </div>
                <span className="w-8 text-right text-xs tabular-nums text-zinc-600">{a.count}</span>
              </div>
            )
          })}
        </div>
      </div>

      {/* Data gems */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-900/30 p-3 space-y-2">
        <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500 flex items-center gap-1.5">
          <Zap size={11} className="text-amber-500" />Data gems
        </div>
        {[
          stats?.pages_with_code > 0 && `${stats.pages_with_code} page${stats.pages_with_code !== 1 ? 's' : ''} include code samples — these are the most developer-actionable docs`,
          totalPages > 0 && stats?.avg_word_count && `Average ${Math.round(stats.avg_word_count)} words/page — ${stats.avg_word_count > 500 ? 'detailed and thorough' : 'lean; consider expanding key pages'}`,
          (staleness.stale_30d_plus || 0) > 0 && `${staleness.stale_30d_plus} page${staleness.stale_30d_plus !== 1 ? 's' : ''} haven't been updated in 30+ days — schedule a doc review sprint`,
          topAuthors[0] && `${topAuthors[0].name} is your top contributor with ${topAuthors[0].count} page${topAuthors[0].count !== 1 ? 's' : ''} — bus-factor risk?`,
        ].filter(Boolean).map((gem, i) => (
          <div key={i} className="flex items-start gap-2 text-[11px] text-zinc-500">
            <span className="mt-0.5 text-amber-500">→</span>{gem}
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────────
export default function KnowledgePanel() {
  const [tab, setTab] = useState('overview')
  const [pages, setPages] = useState(null)
  const [stats, setStats] = useState(null)
  const [docGaps, setDocGaps] = useState(null)
  const [loading, setLoading] = useState(true)
  const [syncBusy, setSyncBusy] = useState(false)
  const toast = useToast()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [pRes, sRes, gRes] = await Promise.all([
        fetch('/api/knowledge/pages').then((r) => r.json()),
        fetch('/api/knowledge/stats').then((r) => r.json()),
        fetch('/api/knowledge/doc-gaps').then((r) => r.json()),
      ])
      setPages(pRes.pages || [])
      setStats(sRes)
      setDocGaps(gRes)
    } catch {
      toast('Failed to load knowledge data', 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  const fullSync = useCallback(async () => {
    setSyncBusy(true)
    try {
      const r = await fetch('/api/knowledge/full-sync', { method: 'POST' }).then((r) => r.json())
      toast(`Sync complete — ${r.total || 0} pages`, 'success')
      await load()
    } catch {
      toast('Sync failed', 'error')
    } finally {
      setSyncBusy(false)
    }
  }, [load])

  useEffect(() => {
    load()
    const t = setInterval(load, 300000)
    return () => clearInterval(t)
  }, [load])

  const totalPages = pages?.length ?? stats?.total_pages ?? 0

  return (
    <Panel
      title="AI Platform Knowledge"
      badge={totalPages || '·'}
      actions={
        <button onClick={fullSync} disabled={syncBusy}
          className="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-400 hover:text-zinc-200 disabled:opacity-50">
          <RefreshCw size={12} className={syncBusy ? 'animate-spin' : ''} />
          {syncBusy ? 'Syncing…' : 'Full sync'}
        </button>
      }
    >
      {/* Tab bar */}
      <div className="mb-4 flex gap-0.5 rounded-xl bg-zinc-950 p-1">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button key={id} onClick={() => setTab(id)}
            className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium transition-colors ${
              tab === id ? 'bg-zinc-800 text-zinc-100 shadow' : 'text-zinc-500 hover:text-zinc-300'
            }`}>
            <Icon size={11} />
            <span className="hidden sm:inline">{label}</span>
          </button>
        ))}
      </div>

      {loading ? (
        <SkeletonRows n={6} />
      ) : (
        <>
          {tab === 'overview' && <OverviewTab pages={pages} stats={stats} onRefresh={fullSync} busy={syncBusy} />}
          {tab === 'usecases' && <UseCasesTab stats={stats} pages={pages} />}
          {tab === 'plans' && <PlansTab stats={stats} />}
          {tab === 'docgaps' && <DocGapsTab docGaps={docGaps} />}
          {tab === 'stats' && <InsightsTab stats={stats} />}
        </>
      )}
    </Panel>
  )
}
