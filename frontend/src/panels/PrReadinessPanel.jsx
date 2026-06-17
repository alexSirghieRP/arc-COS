import React from 'react'
import {
  CheckCircle2, XCircle, Clock, ExternalLink, ShieldAlert, GitPullRequestDraft,
  GitPullRequest,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'

const repoOf = (url) => {
  const m = String(url).match(/github\.com\/[^/]+\/([^/]+)\/pull\/(\d+)/)
  return m ? `${m[1]} #${m[2]}` : url
}

function CheckPill({ c }) {
  const pending = c.pending || c.advisory
  const Icon = c.ok ? CheckCircle2 : pending ? Clock : XCircle
  const cls = c.ok
    ? 'text-emerald-300 bg-emerald-950 ring-emerald-900'
    : pending
      ? 'text-zinc-400 bg-zinc-800 ring-zinc-700'
      : 'text-red-300 bg-red-950 ring-red-900'
  return (
    <span className={`flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] ring-1 ${cls}`} title={c.detail}>
      <Icon size={11} /> {c.label}
    </span>
  )
}

export default function PrReadinessPanel({ board }) {
  const data = board?.pr_readiness || { prs: [] }
  const prs = data.prs || []
  // needs-attention first, then drafts, then ready
  const sorted = [...prs].sort((a, b) => (b.needs_attention ? 1 : 0) - (a.needs_attention ? 1 : 0))

  return (
    <Panel
      title="PR readiness"
      badge={prs.length}
      actions={
        <span className="text-[11px] text-zinc-500">
          {data.needs_attention || 0} need attention
        </span>
      }
    >
      <div className="stagger space-y-1.5">
        {prs.length === 0 && <Empty text="No open PRs of yours right now" />}
        {sorted.map((r) => {
          const Icon = r.is_draft ? GitPullRequestDraft : GitPullRequest
          return (
            <div
              key={r.url}
              className={`rounded-xl border px-3 py-2.5 ${
                r.needs_attention ? 'border-red-900/60 bg-red-950/20' : 'border-zinc-800 bg-zinc-900'
              }`}
            >
              <div className="flex items-center gap-2 text-sm">
                <Icon size={15} className={r.is_draft ? 'text-zinc-400' : 'text-emerald-400'} />
                <span className="shrink-0 font-medium text-zinc-200">{repoOf(r.url)}</span>
                <span className="truncate text-zinc-400">{r.title}</span>
                {r.needs_attention ? (
                  <span className="ml-auto flex shrink-0 items-center gap-1 rounded-md bg-red-950 px-2 py-0.5 text-[10px] text-red-300 ring-1 ring-red-900">
                    <ShieldAlert size={11} /> not ready
                  </span>
                ) : r.ready ? (
                  <span className="ml-auto shrink-0 rounded-md bg-emerald-950 px-2 py-0.5 text-[10px] text-emerald-300 ring-1 ring-emerald-900">
                    {r.is_draft ? 'ready to flip' : 'clean'}
                  </span>
                ) : (
                  <span className="ml-auto shrink-0 rounded-md bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">
                    in progress
                  </span>
                )}
                <a
                  href={r.url}
                  target="_blank"
                  rel="noreferrer"
                  className="press shrink-0 text-zinc-500 hover:text-zinc-300"
                  title="Open on GitHub"
                >
                  <ExternalLink size={13} />
                </a>
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {r.checks?.map((c) => <CheckPill key={c.key} c={c} />)}
              </div>
            </div>
          )
        })}
        {data.at && (
          <div className="pt-1 text-right text-[10px] text-zinc-600">
            checked {new Date(data.at).toLocaleTimeString()}
          </div>
        )}
      </div>
    </Panel>
  )
}
