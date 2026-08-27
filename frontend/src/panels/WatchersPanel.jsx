import React, { useCallback, useEffect, useState } from 'react'
import {
  Eye, Plus, Play, Trash2, Loader2, ShieldCheck, MessageSquareText, Activity,
  ChevronDown, ChevronRight, AlertTriangle, Info, Zap, Save,
} from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'
import { useToast, TimeAgo } from '../ui.jsx'
import { Empty, RefreshButton } from './AdoPanel.jsx'

const KINDS = {
  teams_chat: { label: 'Teams chat', icon: MessageSquareText, desc: 'reads new messages, applies the guardrails, surfaces events' },
  teams_channel: { label: 'Teams channel', icon: MessageSquareText, desc: 'watches a team CHANNEL — paste its Teams link (the one with groupId)' },
  process: { label: 'Process', icon: Activity, desc: 'watches one scheduler job for repeated failures' },
}

const SEV = {
  urgent: { icon: Zap, cls: 'border-red-900 bg-red-950/60 text-red-300' },
  attention: { icon: AlertTriangle, cls: 'border-amber-800 bg-amber-950/60 text-amber-300' },
  info: { icon: Info, cls: 'border-zinc-700 bg-zinc-900 text-zinc-400' },
}

function Toggle({ on, onChange, title }) {
  return (
    <button
      onClick={onChange}
      title={title}
      className={`press relative h-5 w-9 shrink-0 rounded-full transition-colors ${
        on ? 'bg-[var(--accent-fill)]' : 'bg-zinc-700'
      }`}
    >
      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-zinc-100 transition-all ${
        on ? 'left-[18px]' : 'left-0.5'
      }`} />
    </button>
  )
}

function WatcherCard({ w, refresh, defaultPrompt }) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [rails, setRails] = useState(w.guardrails || '')
  const [prompt, setPrompt] = useState(w.prompt || '')
  const [busy, setBusy] = useState(false)
  const K = KINDS[w.kind] || KINDS.teams_chat
  const dirty = rails !== (w.guardrails || '')
  const promptDirty = prompt !== (w.prompt || '')

  const update = async (fields, note) => {
    try {
      await post(`/api/watchers/${w.id}`, fields)
      toast(note || 'Watcher updated', 'success')
      refresh()
    } catch (e) {
      toast(`Update failed: ${String(e).slice(0, 120)}`, 'error')
    }
  }

  const runNow = async () => {
    setBusy(true)
    try {
      const r = await post(`/api/watchers/${w.id}/run`, {})
      toast(`${w.name}: ${JSON.stringify(r.result?.[w.name] ?? r.result).slice(0, 140)}`, 'success')
      refresh()
    } catch (e) {
      toast(`Run failed: ${String(e).slice(0, 120)}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    try {
      await fetch(`/api/watchers/${w.id}`, { method: 'DELETE' })
      toast('Watcher deleted', 'info')
      refresh()
    } catch {}
  }

  return (
    <div className={`rounded-xl border p-2.5 ${w.enabled ? 'border-zinc-800' : 'border-zinc-800/60 opacity-70'}`}>
      <div className="flex items-center gap-2">
        <button onClick={() => setOpen((v) => !v)} className="press shrink-0 text-zinc-500 hover:text-zinc-300">
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </button>
        <K.icon size={13} className="shrink-0 text-[var(--accent)]" />
        <span className="shrink-0 text-[13px] font-medium text-zinc-200">{w.name}</span>
        <span className="shrink-0 rounded-md bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">{K.label}</span>
        {w.target_label && w.target_label !== w.name ? (
          <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-400" title={w.target}>
            {w.target_label}
          </span>
        ) : (
          <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-zinc-600" title={w.target}>
            {w.target_label ? '' : w.target}
          </span>
        )}
        <span className="hidden shrink-0 text-[10px] text-zinc-600 md:block">
          every {w.interval_minutes}m
          {w.last_run && <> · ran <TimeAgo iso={w.last_run} /></>}
        </span>
        <button
          onClick={runNow}
          disabled={busy}
          title="Run this watcher now"
          className="press grid h-6 w-6 shrink-0 place-items-center rounded-md text-zinc-400 hover:bg-zinc-800 hover:text-sky-300"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
        </button>
        <button
          onClick={remove}
          title="Delete this watcher"
          className="press grid h-6 w-6 shrink-0 place-items-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-red-300"
        >
          <Trash2 size={12} />
        </button>
        <Toggle
          on={!!w.enabled}
          title={w.enabled ? 'Watching — click to pause' : 'Paused — click to watch'}
          onChange={() => update({ enabled: !w.enabled }, w.enabled ? `${w.name} paused` : `${w.name} watching`)}
        />
      </div>
      {w.last_note && (
        <div className="mt-1 pl-6 text-[10px] italic text-zinc-500">{w.last_note}</div>
      )}
      {open && (
        <div className="mt-2 space-y-2 pl-6">
          <div>
            <div className="mb-1 flex items-center gap-1.5">
              <Eye size={11} className="text-sky-400" />
              <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
                Prompt — how this watcher thinks (clear + save to reset to the default)
              </span>
            </div>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={4}
              spellCheck={false}
              placeholder={defaultPrompt}
              className="w-full rounded-lg border border-zinc-800 bg-zinc-950 p-2 font-mono text-[11px] leading-relaxed text-zinc-300 placeholder:text-zinc-600 focus:border-zinc-600 focus:outline-none"
            />
            {promptDirty && (
              <button
                onClick={() => update({ prompt }, prompt.trim() ? 'Prompt saved' : 'Prompt reset to default')}
                className="press mt-1 flex items-center gap-1.5 rounded-lg bg-[var(--accent-fill)] px-2.5 py-1 text-[11px] font-medium text-zinc-100 hover:brightness-110"
              >
                <Save size={11} /> Save prompt
              </button>
            )}
          </div>
          <div>
            <div className="mb-1 flex items-center gap-1.5">
              <ShieldCheck size={11} className="text-emerald-400" />
              <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
                Guardrails — what this watcher cares about, ignores, and may never do
              </span>
            </div>
            <textarea
              value={rails}
              onChange={(e) => setRails(e.target.value)}
              rows={5}
              spellCheck={false}
              className="w-full rounded-lg border border-zinc-800 bg-zinc-950 p-2 font-mono text-[11px] leading-relaxed text-zinc-300 focus:border-zinc-600 focus:outline-none"
            />
            {dirty && (
              <button
                onClick={() => update({ guardrails: rails }, 'Guardrails saved')}
                className="press mt-1 flex items-center gap-1.5 rounded-lg bg-[var(--accent-fill)] px-2.5 py-1 text-[11px] font-medium text-zinc-100 hover:brightness-110"
              >
                <Save size={11} /> Save guardrails
              </button>
            )}
          </div>
          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
              Recent events
            </div>
            {(w.events || []).length === 0 && (
              <div className="text-[11px] italic text-zinc-600">none yet — events appear when the guardrails match</div>
            )}
            <div className="space-y-1">
              {(w.events || []).map((e, i) => {
                const S = SEV[e.severity] || SEV.info
                return (
                  <div key={i} className={`flex items-start gap-1.5 rounded-lg border px-2 py-1 text-[11px] ${S.cls}`}>
                    <S.icon size={11} className="mt-0.5 shrink-0" />
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5 text-zinc-200">
                        <span className="min-w-0">{e.headline}</span>
                        {e.needs_alex && (
                          <span className="shrink-0 rounded-full bg-red-950/80 px-1.5 py-px text-[8px] font-bold uppercase tracking-wide text-red-300 ring-1 ring-red-900">
                            needs you
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] opacity-70">{e.why} · {(e.ts || '').slice(0, 16).replace('T', ' ')}</div>
                      {e.checked && (
                        <div className="mt-0.5 text-[10px] italic text-sky-300/90">
                          ↳ ephemeral check: {e.checked}
                        </div>
                      )}
                      {e.draft_id && (
                        <div className="mt-0.5 text-[10px] font-medium text-emerald-300">
                          ↳ reply drafted for you → Approvals tab
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function AddWatcher({ refresh }) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState('teams_chat')
  const [target, setTarget] = useState('')       // selected chat id / job name
  const [targetName, setTargetName] = useState('')
  const [search, setSearch] = useState('')
  const [chats, setChats] = useState(null)
  const [interval, setInterval_] = useState(5)

  useEffect(() => {
    if (!open || kind !== 'teams_chat' || chats !== null) return
    fetch('/api/watchers/chats')
      .then((r) => r.json())
      .then((d) => setChats(d.chats || []))
      .catch(() => setChats([]))
  }, [open, kind, chats])

  const create = async () => {
    try {
      await post('/api/watchers', {
        name: targetName, kind, target, interval_minutes: interval,
      })
      toast(`Watching '${targetName || target}' — default guardrails applied, edit them on the card`, 'success')
      setOpen(false); setTarget(''); setTargetName(''); setSearch('')
      refresh()
    } catch (e) {
      toast(`Create failed: ${String(e).slice(0, 120)}`, 'error')
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-3 py-1.5 text-[12px] font-medium text-zinc-200 hover:bg-zinc-700"
      >
        <Plus size={13} /> Add watcher
      </button>
    )
  }
  // the search box doubles as manual entry: a pasted id/link is accepted as-is
  const pasted = /19:[^\s]+@thread\.(v2|tacv2)/.test(search) || /teams\.microsoft\.com/.test(search)
  const q = search.trim().toLowerCase()
  const filtered = (chats || [])
    .filter((c) => c.topic) // unnamed 1:1s are poor watcher targets
    .filter((c) => !q || c.topic.toLowerCase().includes(q))
    .slice(0, 12)
  return (
    <div className="w-full rounded-xl border border-zinc-700 bg-zinc-950/60 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={kind}
          onChange={(e) => { setKind(e.target.value); setTarget(''); setTargetName('') }}
          className="cursor-pointer rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[12px] text-zinc-300"
        >
          {Object.entries(KINDS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
        {kind === 'teams_chat' ? (
          target ? (
            <span className="flex items-center gap-1.5 rounded-lg border border-[var(--accent-strong)] bg-[var(--accent-fill)] px-2 py-1 text-[12px] text-zinc-100">
              <MessageSquareText size={12} />
              {targetName || target}
              <button onClick={() => { setTarget(''); setTargetName('') }} className="press ml-1 text-zinc-300 hover:text-zinc-100">✕</button>
            </span>
          ) : (
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              autoFocus
              placeholder="search your chats… (or paste a chat id / Teams link)"
              className="min-w-[300px] flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[12px] text-zinc-200 focus:border-zinc-600 focus:outline-none"
            />
          )
        ) : (
          <input
            value={target}
            onChange={(e) => { setTarget(e.target.value); setTargetName(kind === 'teams_channel' ? '' : e.target.value) }}
            placeholder={kind === 'teams_channel'
              ? 'paste the channel link from Teams (…/l/channel/19:…@thread.tacv2/…?groupId=…)'
              : 'job name (e.g. email_sweep, swarm_housekeeping)'}
            className="min-w-[260px] flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-[11px] text-zinc-300 focus:border-zinc-600 focus:outline-none"
          />
        )}
        <label className="flex items-center gap-1 text-[11px] text-zinc-500">
          every
          <input
            type="number"
            min={1}
            value={interval}
            onChange={(e) => setInterval_(Number(e.target.value) || 5)}
            className="w-14 rounded-lg border border-zinc-800 bg-zinc-950 px-1.5 py-1 text-center text-[12px] text-zinc-300"
          />
          min
        </label>
        <button
          onClick={() => {
            if (!target && pasted) { setTarget(search.trim()); setTargetName(''); setTimeout(create, 0); return }
            create()
          }}
          disabled={!target.trim() && !pasted}
          className="press rounded-lg bg-[var(--accent-fill)] px-3 py-1.5 text-[12px] font-medium text-zinc-100 hover:brightness-110 disabled:opacity-40"
        >
          Create
        </button>
        <button onClick={() => setOpen(false)} className="press px-2 text-[12px] text-zinc-500 hover:text-zinc-300">
          cancel
        </button>
      </div>
      {kind === 'teams_chat' && !target && (
        <div className="mt-2 max-h-56 space-y-0.5 overflow-y-auto rounded-lg border border-zinc-800/70 p-1">
          {chats === null && <div className="p-2 text-[11px] italic text-zinc-600">loading your chats…</div>}
          {chats !== null && filtered.length === 0 && !pasted && (
            <div className="p-2 text-[11px] italic text-zinc-600">no chats match — try another search, or paste a chat id / Teams link</div>
          )}
          {pasted && (
            <div className="p-2 text-[11px] text-emerald-300">↩ press Create to use the pasted chat id</div>
          )}
          {filtered.map((c) => (
            <button
              key={c.id}
              onClick={() => { setTarget(c.id); setTargetName(c.topic); setSearch('') }}
              className="press flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-zinc-800"
            >
              <MessageSquareText size={12} className="shrink-0 text-[var(--accent)]" />
              <span className="min-w-0 flex-1 truncate text-[12px] text-zinc-200">{c.topic}</span>
              <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[9px] text-zinc-500">{c.type}</span>
            </button>
          ))}
        </div>
      )}
      <div className="mt-1.5 text-[10px] text-zinc-600">
        {KINDS[kind].desc}
        {kind === 'teams_chat' && ' · chats listed by recent activity · the watcher takes the chat name from Teams'}
      </div>
    </div>
  )
}

export default function WatchersPanel() {
  const [ws, setWs] = useState(null)
  const [defaultPrompt, setDefaultPrompt] = useState('')

  const load = useCallback(() => {
    fetch('/api/watchers')
      .then((r) => r.json())
      .then((d) => { setWs(d.watchers || []); setDefaultPrompt(d.default_prompt || '') })
      .catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  return (
    <Panel
      title="Watchers"
      badge={ws ? ws.filter((w) => w.enabled).length : null}
      actions={
        <div className="flex items-center gap-2">
          <span className="hidden text-[11px] text-zinc-600 lg:block">
            eyes on channels and processes · guardrails editable per watcher · toggle on/off freely
          </span>
          <RefreshButton busy={false} onClick={load} title="Refresh" />
        </div>
      }
    >
      <div className="space-y-2.5">
        <AddWatcher refresh={load} />
        {ws === null && <div className="skeleton h-24 w-full rounded-xl" />}
        {ws?.length === 0 && <Empty text="No watchers yet — add one above" />}
        {(ws || []).map((w) => <WatcherCard key={w.id} w={w} refresh={load} defaultPrompt={defaultPrompt} />)}
      </div>
    </Panel>
  )
}
