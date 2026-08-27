import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Send, Bot, Sparkles, Brain, Zap, Trash2, Loader2 } from 'lucide-react'
import { Panel } from '../App.jsx'
import { post } from '../api.js'
import { useToast } from '../ui.jsx'

// Provider/model picker. Both providers are authenticated CLIs, and they
// differ in a way the owner feels directly: Anthropic keeps a warm process (only
// the first turn pays the spawn), codex spawns per turn. `warm: false` is
// surfaced as a "slower" hint so picking a model explains its own latency
// rather than looking like a regression.
function ModelPicker({ value, onChange, disabled }) {
  const { providers, provider, model } = value
  if (!providers?.length) return null
  const opts = providers.flatMap((p) =>
    (p.models || []).map((m) => ({
      key: `${p.id}::${m}`,
      label: `${m}${p.warm === false ? '  (slower)' : ''}`,
      group: p.label || p.id,
      disabled: p.available === false,
    })))
  const current = providers.find((p) => p.id === provider)
  return (
    <select
      value={`${provider}::${model}`}
      disabled={disabled}
      onChange={(e) => {
        const [pid, mid] = e.target.value.split('::')
        onChange(pid, mid)
      }}
      title={current?.available === false
        ? `${current.cli} is not installed on PATH`
        : 'Which provider and model CoS answers with'}
      className={`max-w-[190px] truncate rounded-lg border bg-zinc-900 px-2 py-1.5 text-[11px] focus:outline-none disabled:opacity-40 ${
        current?.available === false
          ? 'border-amber-800 text-amber-300'
          : 'border-zinc-800 text-zinc-300 hover:border-zinc-700'
      }`}
    >
      {providers.map((p) => (
        <optgroup key={p.id} label={`${p.label || p.id}${p.available === false ? ' — not installed' : ''}`}>
          {opts.filter((o) => o.group === (p.label || p.id)).map((o) => (
            <option key={o.key} value={o.key} disabled={o.disabled}>{o.label}</option>
          ))}
        </optgroup>
      ))}
    </select>
  )
}

// The owner's instant, at-desk companion: talks to /api/companion directly (no
// Teams, no polling) so commands come back in tens of milliseconds.
function Bubble({ m }) {
  const me = m.who === 'me'
  return (
    <div className={`flex ${me ? 'justify-end' : 'justify-start'}`}>
      <div className={`max-w-[80%] rounded-2xl px-3 py-2 text-[13px] leading-relaxed ${
        me ? 'bg-[var(--accent-fill)] text-zinc-100'
           : 'border border-zinc-800 bg-zinc-900 text-zinc-200'
      }`}>
        {!me && (
          <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
            <Bot size={11} className="text-[var(--accent)]" /> CoS
            {m.fast != null && (
              <span className={`ml-1 inline-flex items-center gap-0.5 rounded-full px-1.5 py-px text-[8px] font-bold normal-case ${
                m.ms < 1000 ? 'bg-emerald-950 text-emerald-300' : 'bg-zinc-800 text-zinc-400'
              }`}>
                {m.fast ? <Zap size={8} /> : <Brain size={8} />}
                {m.ms < 1000 ? `${m.ms}ms` : `${(m.ms / 1000).toFixed(1)}s`}
              </span>
            )}
          </div>
        )}
        <div className="whitespace-pre-wrap">{m.text}</div>
      </div>
    </div>
  )
}

export default function CompanionPanel() {
  const [msgs, setMsgs] = useState(() => {
    try { return JSON.parse(sessionStorage.getItem('cosChat') || '[]') } catch { return [] }
  })
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [memCount, setMemCount] = useState(null)
  const [prov, setProv] = useState({ providers: [], provider: '', model: '' })
  const scroller = useRef(null)
  const toast = useToast()

  useEffect(() => {
    try { sessionStorage.setItem('cosChat', JSON.stringify(msgs.slice(-40))) } catch {}
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' })
  }, [msgs])

  const loadMem = useCallback(() => {
    fetch('/api/companion/memory').then((r) => r.json())
      .then((d) => setMemCount((d.memory || []).length)).catch(() => {})
  }, [])
  useEffect(() => { loadMem() }, [loadMem])

  useEffect(() => {
    fetch('/api/companion/providers').then((r) => r.json())
      .then(setProv).catch(() => {})
  }, [])

  const pickModel = async (provider, model) => {
    const prev = prov
    setProv((c) => ({ ...c, provider, model }))   // optimistic: the dropdown
    try {                                         // shouldn't lag the click
      await post('/api/companion/providers', { provider, model })
      toast(`CoS now answers with ${model}`, 'info')
    } catch (e) {
      setProv(prev)   // never leave the dropdown claiming a model that didn't stick
      toast(`Couldn't switch model: ${String(e).slice(0, 80)}`, 'error')
    }
  }

  const send = async (override) => {
    const t = (override ?? text).trim()
    if (!t || busy) return
    setText('')
    const history = msgs.slice(-8).map((m) => `[${m.who === 'me' ? 'Owner' : 'CoS'}] ${m.text}`)
    setMsgs((cur) => [...cur, { who: 'me', text: t, ts: Date.now() }])
    setBusy(true)
    try {
      const d = await post('/api/companion', { text: t, history })
      const outcomes = (d.outcomes || []).filter(Boolean)
      const body = d.reply || (outcomes.length ? outcomes.join('\n') : '(done)')
      setMsgs((cur) => [...cur, { who: 'cos', text: body, ms: d.ms, fast: d.fast, ts: Date.now() }])
      loadMem()
    } catch (e) {
      setMsgs((cur) => [...cur, { who: 'cos', text: `(couldn't reach me: ${String(e).slice(0, 100)})`, ts: Date.now() }])
    } finally {
      setBusy(false)
    }
  }

  const briefMe = async () => {
    setBusy(true)
    try {
      const d = await post('/api/companion/proactive', {})
      toast(d.posted === 'sent' ? 'Brief sent to your Teams chat' :
        d.idle ? 'Nothing new worth flagging right now' : `Proactive: ${JSON.stringify(d).slice(0, 80)}`,
        'info')
    } catch (e) { toast(`Brief failed: ${String(e).slice(0, 80)}`, 'error') }
    finally { setBusy(false) }
  }

  return (
    <Panel
      title="CoS"
      badge={memCount != null ? `${memCount} in memory` : null}
      fill
      actions={
        <div className="flex items-center gap-2">
          <span className="hidden text-[11px] text-zinc-600 xl:block">
            your instant companion · commands run for real · same brain as the Teams chat
          </span>
          <ModelPicker value={prov} onChange={pickModel} disabled={busy} />
          <button
            onClick={briefMe}
            disabled={busy}
            title="Ask CoS to look at everything and flag what needs you"
            className="press flex items-center gap-1.5 rounded-lg bg-zinc-800 px-2.5 py-1.5 text-[12px] font-medium text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
          >
            <Sparkles size={12} /> Brief me
          </button>
          <button
            onClick={() => { setMsgs([]); sessionStorage.removeItem('cosChat') }}
            title="Clear this conversation (memory is kept)"
            className="press grid h-8 w-8 place-items-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300"
          >
            <Trash2 size={14} />
          </button>
        </div>
      }
    >
      <div className="flex min-h-0 flex-1 flex-col">
        <div ref={scroller} className="min-h-0 flex-1 space-y-2 overflow-y-auto p-1">
          {msgs.length === 0 && (
            <div className="grid h-full place-items-center">
              <div className="max-w-sm text-center text-[12px] text-zinc-600">
                <Bot size={22} className="mx-auto mb-2 text-zinc-700" />
                Talk to me like a friend. Ask what's going on, tell me to spin up a swarm,
                run something on the desktop, or just think out loud. Try <span className="text-zinc-400">"status"</span> or <span className="text-zinc-400">"what should I focus on?"</span>
              </div>
            </div>
          )}
          {msgs.map((m, i) => <Bubble key={i} m={m} />)}
          {busy && (
            <div className="flex items-center gap-1.5 pl-1 text-[11px] text-zinc-500">
              <Loader2 size={12} className="animate-spin" /> thinking…
            </div>
          )}
        </div>
        <div className="mt-2 flex shrink-0 items-end gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            rows={1}
            placeholder="Message CoS…  (Enter to send, Shift+Enter for newline)"
            className="max-h-32 min-h-[40px] flex-1 resize-none rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-[13px] text-zinc-200 focus:border-zinc-600 focus:outline-none"
          />
          <button
            onClick={() => send()}
            disabled={busy || !text.trim()}
            className="press grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-fill)] text-zinc-100 hover:brightness-110 disabled:opacity-40"
          >
            <Send size={16} />
          </button>
        </div>
      </div>
    </Panel>
  )
}
