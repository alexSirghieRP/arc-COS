import React, { useEffect, useState } from 'react'
import { FolderGit2 } from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'

export default function ReposPanel() {
  const [repos, setRepos] = useState([])
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState(null)

  const load = () =>
    fetch('/api/repos')
      .then((r) => r.json())
      .then((data) => setRepos(Array.isArray(data) ? data : []))
      .catch(() => setRepos([]))

  useEffect(() => {
    load()
  }, [])

  const toggle = async (r) => {
    setBusy(r.name)
    try {
      await post('/api/repos', { repo: r.name, enabled: !r.enabled })
      await load()
    } finally {
      setBusy(null)
    }
  }

  const shown = repos.filter((r) => !filter || r.name.toLowerCase().includes(filter.toLowerCase()))
  const on = shown.filter((r) => r.enabled)
  const off = shown.filter((r) => !r.enabled)

  return (
    <Panel title="Review repos" badge={`${repos.filter((r) => r.enabled).length} enabled`}>
      <p className="mb-2 text-xs text-zinc-500">
        Chieff reviews PRs (GitHub comments for critical findings + chat summaries) only for
        repos enabled here. Disabling a repo stops new reviews instantly; already-posted
        comments stay.
      </p>
      <input
        className="mb-2 w-full rounded border border-transparent bg-zinc-800 px-2 py-1 text-sm outline-none transition-colors focus:border-[var(--accent)]"
        placeholder="filter repos…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <h3 className="mb-1 mt-2 text-xs font-semibold uppercase text-green-500">
        Reviews enabled ({on.length})
      </h3>
      <div className="space-y-1">
        {on.map((r) => (
          <RepoRow key={r.name} r={r} busy={busy} toggle={toggle} />
        ))}
        {on.length === 0 && <div className="text-sm text-zinc-500">none, reviews are off everywhere</div>}
      </div>
      <h3 className="mb-1 mt-4 text-xs font-semibold uppercase text-zinc-500">
        Reviews disabled ({off.length})
      </h3>
      <div className="space-y-1">
        {off.map((r) => (
          <RepoRow key={r.name} r={r} busy={busy} toggle={toggle} />
        ))}
        {off.length === 0 && <div className="text-sm text-zinc-500">none disabled</div>}
      </div>
    </Panel>
  )
}

function RepoRow({ r, busy, toggle }) {
  return (
    <div className="row-hover flex items-center gap-2 rounded-lg border border-transparent px-2.5 py-1.5 text-sm hover:border-zinc-800">
      <FolderGit2 size={14} className="shrink-0 text-zinc-600" />
      <span className="truncate text-zinc-300">{r.name}</span>
      <button
        onClick={() => toggle(r)}
        disabled={busy === r.name}
        title={r.enabled ? 'Reviews on — click to disable' : 'Reviews off — click to enable'}
        className="press ml-auto shrink-0 disabled:opacity-40"
      >
        <span
          className={`relative block h-[18px] w-8 rounded-full transition-colors ${
            r.enabled ? 'bg-[var(--accent-strong)]' : 'bg-zinc-700'
          }`}
        >
          <span
            className={`absolute left-0 top-0.5 h-[14px] w-[14px] rounded-full bg-white shadow transition-transform duration-200 ${
              r.enabled ? 'translate-x-[16px]' : 'translate-x-[2px]'
            }`}
          />
        </span>
      </button>
    </div>
  )
}
