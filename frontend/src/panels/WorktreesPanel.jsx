import React, { useCallback, useEffect, useState } from 'react'
import {
  GitBranch, GitPullRequest, Loader2, CheckCircle2, XCircle, Trash2,
  Hammer, Wrench, ExternalLink, AlertTriangle, Timer, User,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'
import { useToast, TimeAgo, timeAgo } from '../ui.jsx'
import { Empty, RefreshButton } from './AdoPanel.jsx'

const WT_STATUS = {
  active: { label: 'coder working', icon: Loader2, cls: 'border-sky-800 bg-sky-950 text-sky-300', ic: 'animate-spin' },
  ready: { label: 'awaiting your approval', icon: AlertTriangle, cls: 'border-amber-800 bg-amber-950 text-amber-300', ic: '' },
  pr_created: { label: 'PR opened', icon: CheckCircle2, cls: 'border-emerald-800 bg-emerald-950 text-emerald-300', ic: '' },
  merged: { label: 'merged', icon: CheckCircle2, cls: 'border-emerald-800 bg-emerald-950 text-emerald-300', ic: '' },
  closed: { label: 'closed unmerged', icon: XCircle, cls: 'border-red-900 bg-red-950/60 text-red-300', ic: '' },
  discarded: { label: 'dumped', icon: Trash2, cls: 'border-zinc-700 bg-zinc-800 text-zinc-400', ic: '' },
  error: { label: 'error', icon: XCircle, cls: 'border-red-900 bg-red-950/60 text-red-300', ic: '' },
}
const KIND_ICON = { ticket: Hammer, tech_debt: Wrench }
const KIND_LABEL = { ticket: 'Ticket swarm', tech_debt: 'Debt swarm' }

// hours until the TTL dumps this worktree into the bin
const expiresIn = (wt, ttlHours) => {
  if (!['ready', 'error', 'active'].includes(wt.status) || !ttlHours) return null
  const left = ttlHours * 3600000 - (Date.now() - new Date(wt.updated_at))
  if (left <= 0) return 'expiring now'
  const h = Math.floor(left / 3600000)
  return h >= 1 ? `expires in ${h}h` : `expires in ${Math.max(1, Math.round(left / 60000))}m`
}

function Worktree({ wt, ttlHours, refresh }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(wt.status === 'ready')
  const S = WT_STATUS[wt.status] || WT_STATUS.error
  const K = KIND_ICON[wt.kind] || GitBranch
  const exp = expiresIn(wt, ttlHours)

  const act = async (action) => {
    if (action === 'discard' && !window.confirm(`Dump ${wt.branch} into the bin? The worktree and its commits are deleted.`)) return
    setBusy(true)
    try {
      const r = await post(`/api/worktrees/${wt.id}/${action}`)
      if (action === 'approve') {
        toast(r.status === 'dry_run'
          ? 'Dry run — push + PR were audit-logged, not sent. Flip dry run off to publish.'
          : `Draft PR opened: ${r.url}`, r.status === 'dry_run' ? 'info' : 'success')
      } else {
        toast('Worktree dumped to the bin', 'info')
      }
      refresh()
    } catch (e) {
      toast(`${action} failed: ${String(e).slice(0, 140)}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`rounded-xl border p-2.5 ${wt.status === 'ready' ? 'border-amber-800/60 bg-amber-950/10' : 'border-zinc-800'}`}>
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <K size={13} className="shrink-0 text-[var(--accent)]" />
        <span className="font-medium text-zinc-200">{(wt.repo || '').split('/').pop()}</span>
        <button
          onClick={() => setOpen((v) => !v)}
          className="press flex items-center gap-1 rounded-md bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300 hover:bg-zinc-700"
          title={wt.path}
        >
          <GitBranch size={10} /> {wt.branch}
        </button>
        <span className="text-[10px] text-zinc-500">{wt.target_ref}</span>
        {exp && (
          <span className="flex items-center gap-1 rounded-md bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-400">
            <Timer size={10} /> {exp}
          </span>
        )}
        <span className={`ml-auto flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${S.cls}`}>
          <S.icon size={11} className={S.ic} />
          {S.label}
        </span>
        <TimeAgo iso={wt.updated_at} className="text-[10px] text-zinc-600" />
      </div>

      {open && (
        <div className="animate-fade-in mt-2 space-y-2">
          <div className="text-[12px] text-zinc-300">{wt.title}</div>
          <div className="text-[10px] text-zinc-500">
            spun by {KIND_LABEL[wt.kind] || wt.kind} · run #{wt.run_id} · created {timeAgo(wt.created_at)}
          </div>
          {wt.summary && (
            <pre className="whitespace-pre-wrap rounded-lg bg-zinc-950 px-2.5 py-2 font-sans text-[12px] leading-relaxed text-zinc-400">{wt.summary}</pre>
          )}
          {wt.diff_stat && (
            <pre className="overflow-x-auto rounded-lg bg-zinc-950 px-2.5 py-2 font-mono text-[10px] leading-relaxed text-zinc-500">{wt.diff_stat}</pre>
          )}
          {wt.error && <p className="text-[12px] text-amber-300/90">{wt.error}</p>}
          <div className="flex items-center gap-2">
            {wt.status === 'ready' && (
              <button
                onClick={() => act('approve')}
                disabled={busy}
                className="press flex items-center gap-1.5 rounded-lg bg-emerald-700 px-3 py-1.5 text-[12px] font-medium text-emerald-50 hover:bg-emerald-600 disabled:opacity-40"
              >
                <GitPullRequest size={13} /> Approve → push + draft PR
              </button>
            )}
            {['ready', 'error'].includes(wt.status) && (
              <button
                onClick={() => act('discard')}
                disabled={busy}
                className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-3 py-1.5 text-[12px] text-zinc-300 hover:bg-red-900/50 hover:text-red-200 disabled:opacity-40"
              >
                <Trash2 size={13} /> Dump to bin
              </button>
            )}
            {wt.pr_url && (
              <a
                href={wt.pr_url}
                target="_blank"
                rel="noreferrer"
                className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-3 py-1.5 text-[12px] text-sky-300 hover:bg-zinc-700"
              >
                <ExternalLink size={12} /> open PR
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// PRs created: every PR the swarms shipped, and by whom.
function PrRow({ wt }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-zinc-800 px-2.5 py-2 text-[12px]">
      <GitPullRequest size={13} className="shrink-0 text-violet-400" />
      {wt.pr_url ? (
        <a href={wt.pr_url} target="_blank" rel="noreferrer" className="min-w-0 flex-1 truncate font-medium text-zinc-200 hover:underline">
          {(wt.repo || '').split('/').pop()} — {wt.title}
        </a>
      ) : (
        <span className="min-w-0 flex-1 truncate font-medium text-zinc-200">{(wt.repo || '').split('/').pop()} — {wt.title}</span>
      )}
      <span className="shrink-0 rounded-md bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
        by {KIND_LABEL[wt.kind] || wt.kind} · run #{wt.run_id}
      </span>
      {wt.status === 'merged' ? (
        <span className="flex shrink-0 items-center gap-1 rounded-md bg-emerald-950 px-1.5 py-0.5 text-[10px] text-emerald-300">
          <CheckCircle2 size={9} /> merged · ticket updated
        </span>
      ) : wt.status === 'closed' ? (
        <span className="flex shrink-0 items-center gap-1 rounded-md bg-red-950/60 px-1.5 py-0.5 text-[10px] text-red-300">
          <XCircle size={9} /> closed without merge
        </span>
      ) : (
        <span className="flex shrink-0 items-center gap-1 rounded-md bg-emerald-950 px-1.5 py-0.5 text-[10px] text-emerald-300">
          <User size={9} /> approved by you
        </span>
      )}
      <span className="shrink-0 text-[10px] text-zinc-500">{wt.target_ref}</span>
      <TimeAgo iso={wt.updated_at} className="shrink-0 text-[10px] text-zinc-600" />
    </div>
  )
}

// The bin: everything that was dumped, when, and why.
function BinRow({ wt }) {
  const expired = /expired/.test(wt.error || '')
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-zinc-800/60 px-2.5 py-2 text-[12px] opacity-75">
      <Trash2 size={12} className="shrink-0 text-zinc-500" />
      <span className="font-mono text-[10px] text-zinc-400">{wt.branch}</span>
      <span className="min-w-0 flex-1 truncate text-zinc-400">{wt.title}</span>
      <span className={`shrink-0 rounded-md px-1.5 py-0.5 text-[10px] ${
        expired ? 'bg-amber-950/60 text-amber-300/90' : wt.status === 'error' ? 'bg-red-950/60 text-red-300/90' : 'bg-zinc-800 text-zinc-400'
      }`}>
        {wt.error || (wt.status === 'error' ? 'errored' : 'discarded')}
      </span>
      <span className="shrink-0 text-[10px] text-zinc-600">run #{wt.run_id}</span>
      <TimeAgo iso={wt.updated_at} className="shrink-0 text-[10px] text-zinc-600" />
    </div>
  )
}

const TABS = [
  { id: 'active', label: 'Active', icon: GitBranch },
  { id: 'prs', label: 'PRs created', icon: GitPullRequest },
  { id: 'bin', label: 'Bin', icon: Trash2 },
]

export default function WorktreesPanel() {
  const [wts, setWts] = useState(null)
  const [ttl, setTtl] = useState(72)
  const [tab, setTab] = useState(() => {
    try { return localStorage.getItem('wttab') || 'active' } catch { return 'active' }
  })
  const pick = (id) => { setTab(id); try { localStorage.setItem('wttab', id) } catch {} }

  const load = useCallback(() => {
    fetch('/api/worktrees?limit=100')
      .then((r) => r.json())
      .then((d) => { setWts(d.worktrees || []); if (d.ttl_hours) setTtl(d.ttl_hours) })
      .catch(() => {})
  }, [])

  useEffect(() => {
    load()
    let es
    try {
      es = new EventSource('/api/stream')
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.type === 'worktree_update' || d.type === 'swarm_run_update') load()
        } catch {}
      }
    } catch {}
    const t = setInterval(load, 10000)
    return () => { es?.close(); clearInterval(t) }
  }, [load])

  const groups = {
    active: (wts || []).filter((w) => ['active', 'ready'].includes(w.status)),
    prs: (wts || []).filter((w) => ['pr_created', 'merged', 'closed'].includes(w.status)),
    bin: (wts || []).filter((w) => ['discarded', 'error'].includes(w.status)),
  }

  return (
    <Panel
      title="Swarm worktrees"
      badge={groups.active.length}
      actions={
        <div className="flex items-center gap-2">
          <span className="hidden text-[11px] text-zinc-600 sm:block">
            isolated coder sandboxes · unapproved work expires after {ttl}h · nothing ships without you
          </span>
          <RefreshButton busy={false} onClick={load} title="Refresh" />
        </div>
      }
    >
      <div className="mb-2.5 flex w-fit gap-0.5 rounded-lg bg-zinc-950 p-0.5">
        {TABS.map((t) => {
          const active = tab === t.id
          const n = groups[t.id].length
          return (
            <button
              key={t.id}
              onClick={() => pick(t.id)}
              className={`press flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors ${
                active ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300'
              }`}
            >
              <t.icon size={13} className={active ? 'text-[var(--accent)]' : ''} />
              {t.label}
              {n > 0 && <span className="tabular-nums text-zinc-600">{n}</span>}
            </button>
          )
        })}
      </div>

      {!wts && <div className="skeleton h-20 w-full rounded-xl" />}

      {wts && tab === 'active' && (
        groups.active.length === 0
          ? <Empty text="No active worktrees. The ADO Swarm's ticket runs and the Debt Swarm spin coder sandboxes here; each waits for your approval before anything is pushed." />
          : <div className="stagger space-y-1.5">
              {groups.active.map((w) => <Worktree key={w.id} wt={w} ttlHours={ttl} refresh={load} />)}
            </div>
      )}

      {wts && tab === 'prs' && (
        groups.prs.length === 0
          ? <Empty text="No PRs shipped yet — approve a ready worktree and its draft PR lands here." />
          : <div className="stagger space-y-1.5">
              {groups.prs.map((w) => <PrRow key={w.id} wt={w} />)}
            </div>
      )}

      {wts && tab === 'bin' && (
        groups.bin.length === 0
          ? <Empty text="The bin is empty — discarded and expired worktrees end up here with the reason they were dumped." />
          : <div className="stagger space-y-1">
              {groups.bin.map((w) => <BinRow key={w.id} wt={w} />)}
            </div>
      )}
    </Panel>
  )
}
