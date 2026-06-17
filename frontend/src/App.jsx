import React, { useEffect, useState, useCallback } from 'react'
import {
  LayoutDashboard, MessageSquareText, CheckCheck, RefreshCw, CalendarDays,
  GitPullRequest, FolderGit2, SquareKanban, FileText, ShieldCheck, ScrollText,
  ClipboardList, CalendarRange, Wallet,
} from 'lucide-react'
import { getBoard } from './api.js'
import StatusBar from './panels/StatusBar.jsx'
import TodayPanel from './panels/TodayPanel.jsx'
import PingsPanel from './panels/PingsPanel.jsx'
import ApprovalsPanel from './panels/ApprovalsPanel.jsx'
import MeetingsPanel from './panels/MeetingsPanel.jsx'
import OpenLoopsPanel from './panels/OpenLoopsPanel.jsx'
import ActivityPanel from './panels/ActivityPanel.jsx'
import WriteAccessPanel from './panels/WriteAccessPanel.jsx'
import ReposPanel from './panels/ReposPanel.jsx'
import PrApprovalsPanel from './panels/PrApprovalsPanel.jsx'
import PrReadinessPanel from './panels/PrReadinessPanel.jsx'
import AdoPanel from './panels/AdoPanel.jsx'
import ConfluencePanel from './panels/ConfluencePanel.jsx'
import WeeklyStatusPanel from './panels/WeeklyStatusPanel.jsx'
import RoadmapPanel from './panels/RoadmapPanel.jsx'
import CostPanel from './panels/CostPanel.jsx'

export default function App() {
  const [board, setBoard] = useState(null)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    try {
      setBoard(await getBoard())
      setError(null)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [refresh])

  const [tab, setTab] = useState('today')

  if (!board)
    return (
      <div className="flex h-screen items-center justify-center gap-2 text-zinc-500">
        <RefreshCw size={16} className="animate-spin" />
        <span className="text-sm">{error ? `backend unreachable: ${error}` : 'loading'}</span>
      </div>
    )

  const tabGroups = [
    {
      label: 'Inbox',
      tabs: [
        { id: 'today', label: 'Today', icon: LayoutDashboard },
        { id: 'pings', label: 'Pings', icon: MessageSquareText, badge: board.pings?.length || 0 },
        { id: 'approvals', label: 'Approvals', icon: CheckCheck, badge: board.drafts?.length || 0, accent: true },
        { id: 'loops', label: 'Open loops', icon: RefreshCw, badge: board.open_loops?.length || 0 },
      ],
    },
    { label: 'Meetings', tabs: [{ id: 'meetings', label: 'Meetings', icon: CalendarDays }] },
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
        { id: 'roadmap', label: 'Roadmap', icon: CalendarRange },
      ],
    },
    {
      label: 'System',
      tabs: [
        { id: 'cost', label: 'Cost', icon: Wallet },
        { id: 'write', label: 'Write access', icon: ShieldCheck },
        { id: 'activity', label: 'Activity', icon: ScrollText },
      ],
    },
  ]

  const panels = {
    today: <TodayPanel board={board} refresh={refresh} />,
    pings: <PingsPanel board={board} refresh={refresh} />,
    approvals: <ApprovalsPanel board={board} refresh={refresh} />,
    meetings: <MeetingsPanel board={board} refresh={refresh} />,
    loops: <OpenLoopsPanel board={board} />,
    write: <WriteAccessPanel />,
    repos: <ReposPanel />,
    prs: <PrApprovalsPanel />,
    readiness: <PrReadinessPanel board={board} />,
    ado: <AdoPanel />,
    confluence: <ConfluencePanel />,
    weekly: <WeeklyStatusPanel />,
    roadmap: <RoadmapPanel />,
    cost: <CostPanel />,
    activity: <ActivityPanel board={board} />,
  }

  return (
    <div className="mx-auto flex h-screen max-w-[1440px] flex-col px-4 pt-3 pb-4">
      <StatusBar board={board} refresh={refresh} />
      {error && (
        <div className="mt-2 rounded-lg border border-red-800 bg-red-950 px-3 py-1.5 text-sm text-red-300">
          {error}
        </div>
      )}

      <nav className="mt-4 flex items-end gap-2 overflow-x-auto pb-px">
        {tabGroups.map((group, gi) => (
          <React.Fragment key={group.label}>
            {gi > 0 && <div className="mx-1 mb-2.5 h-6 w-px shrink-0 self-center bg-zinc-800" />}
            <div className="flex shrink-0 flex-col gap-1">
              <span className="px-1 text-[9px] font-semibold uppercase tracking-[0.12em] text-zinc-600">
                {group.label}
              </span>
              <div className="flex gap-1">
                {group.tabs.map((t) => {
                  const active = tab === t.id
                  const Icon = t.icon
                  return (
                    <button
                      key={t.id}
                      onClick={() => setTab(t.id)}
                      className={`press group relative flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium ${
                        active
                          ? 'bg-zinc-800 text-zinc-100'
                          : 'text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200'
                      }`}
                    >
                      {Icon && (
                        <Icon
                          size={15}
                          className={active ? 'text-[var(--accent)]' : 'text-zinc-600 group-hover:text-zinc-400'}
                        />
                      )}
                      {t.label}
                      {t.badge > 0 && (
                        <span
                          className={`ml-0.5 rounded-full px-1.5 py-px text-[10px] font-semibold tabular-nums ${
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
                      {active && (
                        <span className="absolute -bottom-px left-3 right-3 h-0.5 rounded-full bg-[var(--accent)]" />
                      )}
                    </button>
                  )
                })}
              </div>
            </div>
          </React.Fragment>
        ))}
      </nav>

      <main key={tab} className="animate-fade-up mt-4 min-h-0 flex-1 overflow-y-auto pr-1">
        {panels[tab]}
      </main>
    </div>
  )
}

export function Panel({ title, badge, actions, children }) {
  return (
    <section className="overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900 shadow-[var(--shadow)]">
      <header className="flex items-center justify-between border-b border-zinc-800 px-4 py-2.5">
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
      <div className="p-3.5">{children}</div>
    </section>
  )
}
