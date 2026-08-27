import React, { Suspense, lazy, useEffect, useState, useCallback, useRef } from 'react'
import {
  LayoutDashboard, MessageSquareText, CheckCheck, RefreshCw, CalendarDays,
  GitPullRequest, FolderGit2, SquareKanban, FileText, ShieldCheck, ScrollText,
  ClipboardList, CalendarRange, Wallet, Gauge, HeartPulse, Terminal,
  PanelLeftClose, PanelLeftOpen, Brain, Network, BookOpen, Activity, Bot,
  Wrench, GitBranch,
} from 'lucide-react'
import { getBoard, editLock } from './api.js'
import { ToastProvider } from './ui.jsx'
import CommandPalette from './CommandPalette.jsx'
import StatusBar from './panels/StatusBar.jsx'

// Every tab panel is code-split — only the active tab's chunk is fetched,
// instead of bundling all 21 panels (incl. heavy ones like ArchMap/Obsidian)
// into one multi-hundred-KB chunk loaded up front.
//
// A rebuild replaces dist/ chunks with new hashes, so an app instance loaded
// before the rebuild 404s on every lazy tab import — without handling, that
// unmounts the React root and the window goes black. Reload once to pick up
// the fresh index; the sessionStorage guard stops a reload loop when the
// failure is something else (e.g. backend down), letting the ErrorBoundary
// show a proper message instead.
const lazyPanel = (load) => lazy(() =>
  load()
    .then((m) => { sessionStorage.removeItem('stale-chunk-reload'); return m })
    .catch((e) => {
      if (!sessionStorage.getItem('stale-chunk-reload')) {
        sessionStorage.setItem('stale-chunk-reload', '1')
        window.location.reload()
        return new Promise(() => {}) // reload takes over; never resolve
      }
      throw e
    }),
)

export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error) {
    return { error }
  }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="grid h-full min-h-[300px] place-items-center p-6">
        <div className="max-w-md rounded-2xl border border-red-900/50 bg-zinc-900 p-5 text-center shadow-[var(--shadow)]">
          <div className="text-[13px] font-semibold text-red-300">This view crashed</div>
          <div className="mt-1.5 break-words font-mono text-[11px] text-zinc-500">
            {String(this.state.error?.message || this.state.error).slice(0, 300)}
          </div>
          <button
            onClick={() => window.location.reload()}
            className="press mt-3 rounded-lg bg-zinc-800 px-3 py-1.5 text-[12px] font-medium text-zinc-200 hover:bg-zinc-700"
          >
            Reload the app
          </button>
        </div>
      </div>
    )
  }
}

const TodayPanel = lazyPanel(() => import('./panels/TodayPanel.jsx'))
const PingsPanel = lazyPanel(() => import('./panels/PingsPanel.jsx'))
const ApprovalsPanel = lazyPanel(() => import('./panels/ApprovalsPanel.jsx'))
const MeetingsPanel = lazyPanel(() => import('./panels/MeetingsPanel.jsx'))
const OpenLoopsPanel = lazyPanel(() => import('./panels/OpenLoopsPanel.jsx'))
const ActivityPanel = lazyPanel(() => import('./panels/ActivityPanel.jsx'))
const WriteAccessPanel = lazyPanel(() => import('./panels/WriteAccessPanel.jsx'))
const ReposPanel = lazyPanel(() => import('./panels/ReposPanel.jsx'))
const PrApprovalsPanel = lazyPanel(() => import('./panels/PrApprovalsPanel.jsx'))
const PrReadinessPanel = lazyPanel(() => import('./panels/PrReadinessPanel.jsx'))
const AdoPanel = lazyPanel(() => import('./panels/AdoPanel.jsx'))
const ConfluencePanel = lazyPanel(() => import('./panels/ConfluencePanel.jsx'))
const WeeklyStatusPanel = lazyPanel(() => import('./panels/WeeklyStatusPanel.jsx'))
const ScrumPanel = lazyPanel(() => import('./panels/ScrumPanel.jsx'))
const RoadmapPanel = lazyPanel(() => import('./panels/RoadmapPanel.jsx'))
const ExecStatusPanel = lazyPanel(() => import('./panels/ExecStatusPanel.jsx'))
const ScorecardPanel = lazyPanel(() => import('./panels/ScorecardPanel.jsx'))
const CostPanel = lazyPanel(() => import('./panels/CostPanel.jsx'))
const HealthPanel = lazyPanel(() => import('./panels/HealthPanel.jsx'))
const LogsPanel = lazyPanel(() => import('./panels/LogsPanel.jsx'))
const ObsidianPanel = lazyPanel(() => import('./panels/ObsidianPanel.jsx'))
const ArchMapPanel = lazyPanel(() => import('./panels/ArchMapPanel.jsx'))
const KnowledgePanel = lazyPanel(() => import('./panels/KnowledgePanel.jsx'))
const SwarmPanel = lazyPanel(() => import('./panels/SwarmPanel.jsx'))
const WorktreesPanel = lazyPanel(() => import('./panels/WorktreesPanel.jsx'))
const WatchersPanel = lazyPanel(() => import('./panels/WatchersPanel.jsx'))
const CompanionPanel = lazyPanel(() => import('./panels/CompanionPanel.jsx'))

export default function App() {
  return (
    <ToastProvider>
      <Board />
    </ToastProvider>
  )
}

function Board() {
  const [board, setBoard] = useState(null)
  const [error, setError] = useState(null)
  const [lastSync, setLastSync] = useState(null)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [sseConnected, setSseConnected] = useState(false)
  const [runningJobs, setRunningJobs] = useState(new Set())
  const [refreshTick, setRefreshTick] = useState(0)
  const [lastSweepMsg, setLastSweepMsg] = useState(null)

  // Auto-clear sweep result message after 8 seconds
  useEffect(() => {
    if (!lastSweepMsg) return
    const t = setTimeout(() => setLastSweepMsg(null), 8000)
    return () => clearTimeout(t)
  }, [lastSweepMsg])

  const refresh = useCallback(async () => {
    try {
      setBoard(await getBoard())
      setError(null)
      setLastSync(Date.now())
    } catch (e) {
      setError(String(e))
    }
  }, [])

  // Browser notifications for urgent tier-C items (opt-in, fire-and-forget)
  const notifiedRef = useRef(new Set())
  const notify = useCallback((pings) => {
    if (!('Notification' in window)) return
    const urgent = (pings || []).filter(
      (p) => p.urgent && p.status !== 'dismissed' && p.status !== 'done',
    )
    if (!urgent.length) return
    if (Notification.permission === 'default') {
      Notification.requestPermission()
      return
    }
    if (Notification.permission !== 'granted') return
    for (const p of urgent) {
      if (notifiedRef.current.has(p.id)) continue
      notifiedRef.current.add(p.id)
      try {
        const n = new Notification(`Urgent: ${p.sender}`, {
          body: (p.content || '').slice(0, 120),
          tag: String(p.id),
        })
        n.onclick = () => { window.focus(); n.close() }
      } catch {}
    }
  }, [])

  useEffect(() => {
    refresh()

    // SSE: instant push when backend state changes. Falls back to 5s polling on error.
    let es = null
    let fallback = null
    let reconnectTimer = null

    const startFallback = () => {
      if (fallback) return
      fallback = setInterval(() => { if (!editLock.active()) refresh() }, 5000)
    }

    const connect = () => {
      try {
        es = new EventSource('/api/stream')
        es.onopen = () => {
          setSseConnected(true)
          if (fallback) { clearInterval(fallback); fallback = null }
        }
        es.onmessage = (e) => {
          try {
            const data = JSON.parse(e.data)
            if (data.type === 'sweep_started') {
              setRunningJobs((s) => new Set([...s, data.job]))
            } else if (data.type === 'board_changed') {
              if (data.job) setRunningJobs((s) => { const n = new Set(s); n.delete(data.job); return n })
              if (data.msg) setLastSweepMsg({ job: data.job, text: data.msg })
              setRefreshTick((t) => t + 1)
              if (!editLock.active()) refresh()
            }
          } catch {}
        }
        es.onerror = () => {
          setSseConnected(false)
          es?.close()
          es = null
          startFallback()
          reconnectTimer = setTimeout(connect, 8000)
        }
      } catch {
        startFallback()
      }
    }

    connect()

    return () => {
      es?.close()
      if (fallback) clearInterval(fallback)
      if (reconnectTimer) clearTimeout(reconnectTimer)
    }
  }, [refresh])

  // Fire browser notifications when urgent pings arrive.
  useEffect(() => {
    if (board) notify(board.pings)
  }, [board, notify])

  // Browser notifications for new high-severity pipeline problems —
  // fires no matter which tab is open; the Live Monitor tab has the detail.
  useEffect(() => {
    const fresh = board?.osca_new
    if (!fresh?.length || !('Notification' in window)) return
    if (Notification.permission === 'default') { Notification.requestPermission(); return }
    if (Notification.permission !== 'granted') return
    for (const p of fresh) {
      const tag = `osca:${p.key}`
      if (notifiedRef.current.has(tag)) continue
      notifiedRef.current.add(tag)
      try {
        const n = new Notification('Pipeline agent — failure detected', {
          body: `${(p.title || '').slice(0, 110)}${p.count > 1 ? ` (${p.count} affected)` : ''}`,
          tag,
        })
        n.onclick = () => { window.focus(); setTab('archmap'); n.close() }
      } catch {}
    }
  }, [board])

  // Update document title with active ping count so the browser tab shows unread.
  useEffect(() => {
    if (!board) return
    const active = (board.pings || []).filter(
      (p) => p.status !== 'dismissed' && p.status !== 'done',
    ).length
    const pending = board.drafts?.length || 0
    const parts = []
    if (active > 0) parts.push(`${active} ping${active !== 1 ? 's' : ''}`)
    if (pending > 0) parts.push(`${pending} draft${pending !== 1 ? 's' : ''}`)
    document.title = parts.length ? `(${parts.join(' · ')}) Chief` : 'Chief'
  }, [board])

  const [tab, setTab] = useState(() => {
    try { return localStorage.getItem('tab') || 'today' } catch { return 'today' }
  })
  useEffect(() => { try { localStorage.setItem('tab', tab) } catch {} }, [tab])

  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('sidebar') === 'collapsed' } catch { return false }
  })
  useEffect(() => {
    try { localStorage.setItem('sidebar', collapsed ? 'collapsed' : 'open') } catch {}
  }, [collapsed])

  // ⌘K palette; ⌘B sidebar; ⌘↑/⌘↓ move between tabs (never while typing)
  const tabIdsRef = useRef([])
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((v) => !v)
        return
      }
      const typing = /input|textarea|select/i.test(document.activeElement?.tagName || '')
      if (typing || paletteOpen) return
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault()
        setCollapsed((v) => !v)
        return
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
        e.preventDefault()
        const ids = tabIdsRef.current
        setTab((cur) => {
          const i = ids.indexOf(cur)
          return ids[(i + (e.key === 'ArrowDown' ? 1 : ids.length - 1)) % ids.length] || cur
        })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [paletteOpen])

  if (!board)
    return (
      <div className="flex h-screen items-center justify-center gap-2 text-zinc-500">
        <RefreshCw size={16} className="animate-spin" />
        <span className="text-sm">{error ? `backend unreachable: ${error}` : 'loading'}</span>
      </div>
    )

  const tabGroups = [
    {
      label: 'Companion',
      tabs: [
        { id: 'companion', label: 'CoS', icon: Bot },
      ],
    },
    {
      label: 'Inbox',
      tabs: [
        { id: 'today', label: 'Today', icon: LayoutDashboard },
        {
          id: 'pings',
          label: 'Pings',
          icon: MessageSquareText,
          badge: (board.pings || []).filter((p) => p.status !== 'dismissed' && p.status !== 'done').length,
        },
        { id: 'approvals', label: 'Approvals', icon: CheckCheck, badge: board.drafts?.length || 0, accent: true },
        { id: 'loops', label: 'Open loops', icon: RefreshCw, badge: board.open_loops?.length || 0 },
      ],
    },
    { label: 'Meetings', tabs: [{ id: 'meetings', label: 'Meetings', icon: CalendarDays }] },
    { label: 'Scrum', tabs: [{ id: 'scrum', label: 'Scrum', icon: Activity }] },
    {
      label: 'GitHub',
      tabs: [
        { id: 'prs', label: 'PR approvals', icon: GitPullRequest },
        {
          id: 'readiness',
          label: 'PR readiness',
          icon: ShieldCheck,
          badge: board.pr_readiness?.needs_attention || 0,
          accent: (board.pr_readiness?.needs_attention || 0) > 0,
        },
        { id: 'repos', label: 'Repos', icon: FolderGit2 },
      ],
    },
    { label: 'Azure DevOps', tabs: [{ id: 'ado', label: 'My items', icon: SquareKanban }] },
    {
      label: 'Swarms',
      tabs: [
        { id: 'swarm', label: 'ADO Swarm', icon: Bot },
        { id: 'prswarm', label: 'PR Swarm', icon: GitPullRequest },
        { id: 'debtswarm', label: 'Debt Swarm', icon: Wrench },
        { id: 'worktrees', label: 'Worktrees', icon: GitBranch },
      ],
    },
    {
      label: 'Watchers',
      tabs: [
        { id: 'watchers', label: 'Teams', icon: MessageSquareText },
      ],
    },
    {
      label: 'Confluence',
      tabs: [
        { id: 'confluence', label: 'Updates', icon: FileText },
        {
          id: 'weekly',
          label: 'Weekly status',
          icon: ClipboardList,
          badge: board.weekly_status_draft ? 1 : 0,
          accent: !!board.weekly_status_draft,
        },
        { id: 'exec', label: 'Exec status', icon: Gauge, accent: true },
        { id: 'scorecard', label: 'Scorecard', icon: ClipboardList },
        { id: 'roadmap', label: 'Roadmap', icon: CalendarRange },
      ],
    },
    {
      label: 'Knowledge',
      tabs: [
        { id: 'knowledge', label: 'AI Platform', icon: BookOpen },
        { id: 'obsidian', label: 'Obsidian', icon: Brain },
      ],
    },
    {
      label: 'AI Landscape',
      tabs: [
        { id: 'archmap', label: 'Arch Map', icon: Network,
          badge: board.osca_alerts || 0, accent: (board.osca_alerts || 0) > 0 },
      ],
    },
    {
      label: 'System',
      tabs: [
        { id: 'health', label: 'Health', icon: HeartPulse },
        { id: 'cost', label: 'Cost', icon: Wallet },
        { id: 'write', label: 'Write access', icon: ShieldCheck },
        { id: 'activity', label: 'Activity', icon: ScrollText },
        { id: 'logs', label: 'Logs', icon: Terminal },
      ],
    },
  ]

  const allTabs = tabGroups.flatMap((g) => g.tabs)
  tabIdsRef.current = allTabs.map((t) => t.id)

  const panels = {
    today: <TodayPanel board={board} refresh={refresh} setTab={setTab} />,
    pings: <PingsPanel board={board} refresh={refresh} />,
    approvals: <ApprovalsPanel board={board} refresh={refresh} />,
    meetings: <MeetingsPanel board={board} refresh={refresh} />,
    scrum: <ScrumPanel />,
    loops: <OpenLoopsPanel board={board} refresh={refresh} />,
    write: <WriteAccessPanel />,
    repos: <ReposPanel />,
    prs: <PrApprovalsPanel />,
    readiness: <PrReadinessPanel board={board} refresh={refresh} />,
    ado: <AdoPanel />,
    swarm: <SwarmPanel mode="ado" />,
    prswarm: <SwarmPanel mode="pr" />,
    debtswarm: <SwarmPanel mode="debt" />,
    worktrees: <WorktreesPanel />,
    watchers: <WatchersPanel />,
    companion: <CompanionPanel />,
    confluence: <ConfluencePanel />,
    weekly: <WeeklyStatusPanel />,
    exec: <ExecStatusPanel />,
    scorecard: <ScorecardPanel />,
    roadmap: <RoadmapPanel />,
    cost: <CostPanel />,
    activity: <ActivityPanel board={board} refreshTick={refreshTick} />,
    health: <HealthPanel refreshTick={refreshTick} />,
    logs: <LogsPanel refreshTick={refreshTick} />,
    knowledge: <KnowledgePanel />,
    obsidian: <ObsidianPanel />,
    archmap: <ArchMapPanel />,
  }

  return (
    <div className="flex h-screen w-full flex-col px-2 pt-2 pb-2">
      <StatusBar
        board={board}
        refresh={refresh}
        lastSync={lastSync}
        sseConnected={sseConnected}
        runningJobs={runningJobs}
        lastSweepMsg={lastSweepMsg}
        openPalette={() => setPaletteOpen(true)}
        setTab={setTab}
      />
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        tabs={allTabs}
        setTab={setTab}
        board={board}
        refresh={refresh}
      />
      {error && (
        <div className="mt-2 rounded-lg border border-red-800 bg-red-950 px-3 py-1.5 text-sm text-red-300">
          {error}
        </div>
      )}
      {board.integration_error && (
        <div className="mt-2 rounded-lg border border-amber-800 bg-amber-950 px-3 py-1.5 text-[13px] text-amber-200">
          {/access token|authenticate/i.test(board.integration_error)
            ? <>Microsoft Graph token expired — calendar/Teams/email are paused. Re-auth: <code className="rounded bg-black/30 px-1">cd ~/RP/mcp-ms-graph && node auth-node.js</code></>
            : <>Integration issue: {board.integration_error}</>}
        </div>
      )}

      <div className="mt-2 flex min-h-0 flex-1 gap-2.5">
        <Sidebar
          groups={tabGroups}
          tab={tab}
          setTab={setTab}
          collapsed={collapsed}
          setCollapsed={setCollapsed}
        />
        <main key={tab} className="animate-fade-up min-h-0 flex-1 overflow-y-auto pr-1">
          <ErrorBoundary>
            <Suspense fallback={<PanelSkeleton />}>{panels[tab]}</Suspense>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}

function Sidebar({ groups, tab, setTab, collapsed, setCollapsed }) {
  // Per-group fold state (persisted). A group holding the active tab never hides it.
  const [folded, setFolded] = useState(() => {
    try { return JSON.parse(localStorage.getItem('sidebarGroups') || '{}') } catch { return {} }
  })
  const toggleGroup = (label) => {
    setFolded((f) => {
      const next = { ...f, [label]: !f[label] }
      try { localStorage.setItem('sidebarGroups', JSON.stringify(next)) } catch {}
      return next
    })
  }
  // Menu customization (owner request, 2026-07-10): every tab and section can be shown
  // or hidden from the customize list at the top. Persisted; PR Swarm and
  // Debt Swarm start hidden.
  const [hidden, setHidden] = useState(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem('sidebarHidden') || '["prswarm","debtswarm"]'))
    } catch { return new Set(['prswarm', 'debtswarm']) }
  })
  const [customize, setCustomize] = useState(false)
  const toggleHidden = (key) => {
    setHidden((cur) => {
      const next = new Set(cur)
      next.has(key) ? next.delete(key) : next.add(key)
      try { localStorage.setItem('sidebarHidden', JSON.stringify([...next])) } catch {}
      if (key === tab && next.has(key)) setTab('today')
      return next
    })
  }
  const visibleGroups = groups
    .map((g) => ({ ...g, tabs: g.tabs.filter((t) => !hidden.has(t.id)) }))
    .filter((g) => g.tabs.length > 0 && !hidden.has(`group:${g.label}`))
  return (
    <aside
      className={`flex shrink-0 flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900 shadow-[var(--shadow)] transition-[width] duration-200 ease-out ${
        collapsed ? 'w-[56px]' : 'w-[208px]'
      }`}
    >
      {!collapsed && (
        <div className="shrink-0 px-2 pt-2">
          <button
            onClick={() => setCustomize((v) => !v)}
            title="Choose which sections and items the menu shows"
            className={`press flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-[11px] font-medium ${
              customize ? 'bg-[var(--accent-fill)] text-zinc-100' : 'text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300'
            }`}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="4" y1="6" x2="20" y2="6" /><circle cx="9" cy="6" r="2" fill="currentColor" />
              <line x1="4" y1="12" x2="20" y2="12" /><circle cx="15" cy="12" r="2" fill="currentColor" />
              <line x1="4" y1="18" x2="20" y2="18" /><circle cx="7" cy="18" r="2" fill="currentColor" />
            </svg>
            Customize menu
          </button>
          {customize && (
            <div className="mt-1 max-h-[45vh] space-y-1.5 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950/60 p-2">
              {groups.map((g) => {
                const gKey = `group:${g.label}`
                const gOn = !hidden.has(gKey)
                return (
                  <div key={g.label}>
                    <label className="flex cursor-pointer items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
                      <input
                        type="checkbox"
                        checked={gOn}
                        onChange={() => toggleHidden(gKey)}
                        className="h-3 w-3 accent-[var(--accent)]"
                      />
                      {g.label}
                    </label>
                    {gOn && g.tabs.map((t) => (
                      <label key={t.id} className="ml-4 flex cursor-pointer items-center gap-1.5 py-0.5 text-[11px] text-zinc-400 hover:text-zinc-200">
                        <input
                          type="checkbox"
                          checked={!hidden.has(t.id)}
                          onChange={() => toggleHidden(t.id)}
                          className="h-3 w-3 accent-[var(--accent)]"
                        />
                        <t.icon size={11} className="text-zinc-500" />
                        {t.label}
                      </label>
                    ))}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
      <nav className="min-h-0 flex-1 space-y-2.5 overflow-y-auto overflow-x-hidden p-2">
        {visibleGroups.map((group, gi) => {
          const isFolded = !collapsed && folded[group.label] && !group.tabs.some((t) => t.id === tab)
          const foldedBadge = isFolded ? group.tabs.reduce((n, t) => n + (t.badge || 0), 0) : 0
          return (
          <div key={group.label}>
            {collapsed ? (
              gi > 0 && <div className="mx-2 mb-2 h-px bg-zinc-800" />
            ) : (
              <button
                onClick={() => toggleGroup(group.label)}
                className="group/hdr flex w-full items-center gap-1 px-2 pb-1 text-[9px] font-semibold uppercase tracking-[0.13em] text-zinc-600 hover:text-zinc-400"
              >
                <span>{group.label}</span>
                {foldedBadge > 0 && (
                  <span className="rounded-full bg-zinc-800 px-1 text-[8px] font-semibold normal-case tabular-nums text-zinc-400">
                    {foldedBadge}
                  </span>
                )}
                <svg width="8" height="8" viewBox="0 0 8 8"
                  className={`ml-auto opacity-0 transition-all group-hover/hdr:opacity-100 ${isFolded ? '-rotate-90' : ''}`}>
                  <path d="M1 2.5 L4 5.5 L7 2.5" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinecap="round" />
                </svg>
              </button>
            )}
            <div className="space-y-0.5">
              {(isFolded ? [] : group.tabs).map((t) => {
                const active = tab === t.id
                const Icon = t.icon
                return (
                  <button
                    key={t.id}
                    onClick={() => setTab(t.id)}
                    title={collapsed ? `${t.label}${t.badge > 0 ? ` (${t.badge})` : ''}` : undefined}
                    className={`press group relative flex w-full items-center rounded-lg text-[13px] font-medium ${
                      collapsed ? 'justify-center py-2' : 'gap-2.5 px-2.5 py-[7px]'
                    } ${
                      active
                        ? 'bg-[var(--accent-fill)] text-zinc-100'
                        : 'text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200'
                    }`}
                  >
                    {active && (
                      <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-[var(--accent)]" />
                    )}
                    <span className="relative shrink-0">
                      {Icon && (
                        <Icon
                          size={16}
                          className={active ? 'text-[var(--accent)]' : 'text-zinc-600 group-hover:text-zinc-400'}
                        />
                      )}
                      {collapsed && t.badge > 0 && (
                        <span
                          className={`absolute -right-2 -top-1.5 min-w-[15px] rounded-full px-1 text-center text-[9px] font-semibold leading-[15px] ${
                            t.accent ? 'bg-green-800 text-green-200' : 'bg-zinc-700 text-zinc-300'
                          }`}
                        >
                          {t.badge > 99 ? '99+' : t.badge}
                        </span>
                      )}
                    </span>
                    {!collapsed && <span className="min-w-0 flex-1 truncate text-left">{t.label}</span>}
                    {!collapsed && t.badge > 0 && (
                      <span
                        className={`shrink-0 rounded-full px-1.5 py-px text-[10px] font-semibold tabular-nums ${
                          t.accent
                            ? 'bg-green-800 text-green-200'
                            : active
                              ? 'bg-zinc-700 text-zinc-200'
                              : 'bg-zinc-800 text-zinc-400'
                        }`}
                      >
                        {t.badge}
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          </div>
          )
        })}
      </nav>
      <button
        onClick={() => setCollapsed((v) => !v)}
        title={`${collapsed ? 'Expand' : 'Collapse'} sidebar (⌘B)`}
        className={`press flex items-center gap-2 border-t border-zinc-800 py-2.5 text-[12px] text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 ${
          collapsed ? 'justify-center' : 'px-3'
        }`}
      >
        {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
        {!collapsed && <span>Collapse</span>}
      </button>
    </aside>
  )
}

function PanelSkeleton() {
  return (
    <div className="space-y-2.5 p-1">
      <div className="skeleton h-9 w-full rounded-xl" />
      <div className="skeleton h-24 w-full rounded-xl" />
      <div className="skeleton h-24 w-full rounded-xl" />
      <div className="skeleton h-40 w-full rounded-xl" />
    </div>
  )
}

export function Panel({ title, badge, actions, children, fill }) {
  // fill: the panel becomes exactly viewport-height (flex column, content
  // flexes inside, nothing scrolls at the card level) — used by board views.
  return (
    <section className={`overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900 shadow-[var(--shadow)] ${
      fill ? 'flex h-full min-h-0 flex-col' : ''
    }`}>
      <header className="flex shrink-0 items-center justify-between border-b border-zinc-800 px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <h2 className="text-[12px] font-semibold uppercase tracking-[0.1em] text-zinc-400">{title}</h2>
          {badge != null && (
            <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-[11px] font-medium tabular-nums text-zinc-300">
              {badge}
            </span>
          )}
        </div>
        {actions}
      </header>
      <div className={fill ? 'flex min-h-0 flex-1 flex-col p-3.5' : 'p-3.5'}>{children}</div>
    </section>
  )
}
