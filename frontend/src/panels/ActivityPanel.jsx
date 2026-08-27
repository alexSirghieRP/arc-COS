import React, { useEffect, useMemo, useState } from 'react'
import {
  Inbox, Tags, Send, Clock, FileEdit, GitPullRequest, BookOpen, BarChart3,
  ClipboardList, Settings2, AlertTriangle, CheckCircle2, Mail, Activity, Cpu,
} from 'lucide-react'
import { SiConfluence, SiObsidian, SiGithub } from 'react-icons/si'
import { PiMicrosoftTeamsLogoFill, PiMicrosoftOutlookLogoFill } from 'react-icons/pi'
import { VscAzureDevops } from 'react-icons/vsc'
import { Panel } from '../App.jsx'
import { Empty } from './AdoPanel.jsx'

// ---- categories: each defines color + icon + chip label -------------------
const CATS = {
  ingest:    { label: 'Inbox',     icon: Inbox,          text: 'text-sky-300',     chip: 'bg-sky-950 text-sky-300 ring-sky-900',          bar: 'bg-sky-500/60' },
  triage:    { label: 'Triage',    icon: Tags,           text: 'text-violet-300',  chip: 'bg-violet-950 text-violet-300 ring-violet-900', bar: 'bg-violet-500/60' },
  send:      { label: 'Sent',      icon: Send,           text: 'text-emerald-300', chip: 'bg-emerald-950 text-emerald-300 ring-emerald-900', bar: 'bg-emerald-500/60' },
  draft:     { label: 'Draft',     icon: FileEdit,       text: 'text-amber-300',   chip: 'bg-amber-950 text-amber-300 ring-amber-900',    bar: 'bg-amber-500/60' },
  review:    { label: 'PR review', icon: GitPullRequest, text: 'text-indigo-300',  chip: 'bg-indigo-950 text-indigo-300 ring-indigo-900', bar: 'bg-indigo-500/60' },
  knowledge: { label: 'Knowledge', icon: BookOpen,       text: 'text-cyan-300',    chip: 'bg-cyan-950 text-cyan-300 ring-cyan-900',       bar: 'bg-cyan-500/60' },
  weekly:    { label: 'Weekly',    icon: ClipboardList,  text: 'text-orange-300',  chip: 'bg-orange-950 text-orange-300 ring-orange-900', bar: 'bg-orange-500/60' },
  system:    { label: 'System',    icon: Settings2,      text: 'text-zinc-400',    chip: 'bg-zinc-800 text-zinc-400 ring-zinc-700',       bar: 'bg-zinc-600' },
  error:     { label: 'Error',     icon: AlertTriangle,  text: 'text-red-300',     chip: 'bg-red-950 text-red-300 ring-red-900',          bar: 'bg-red-500/70' },
}

// ---- MCP route / source each action came from -----------------------------
// Brand colors per the owner: Teams purple, Confluence light blue, Outlook darker
// blue, Obsidian darker purple, GitHub black/white. Logos from react-icons.
const ROUTES = {
  teams:      { label: 'Teams',      Logo: PiMicrosoftTeamsLogoFill,   text: 'text-[#7B83EB]' },
  confluence: { label: 'Confluence', Logo: SiConfluence,               text: 'text-[#4C9AFF]' },
  outlook:    { label: 'Outlook',    Logo: PiMicrosoftOutlookLogoFill, text: 'text-[#0364B8]' },
  obsidian:   { label: 'Obsidian',   Logo: SiObsidian,                 text: 'text-[#7C3AED]' },
  github:     { label: 'GitHub',     Logo: SiGithub,                   text: 'text-zinc-100' },
  ado:        { label: 'ADO',        Logo: VscAzureDevops,             text: 'text-[#0078D7]' },
  local:      { label: 'Local',      Logo: Cpu,                        text: 'text-zinc-500' },
}

function route(a, d) {
  const act = a.action, ch = d.channel, s = d.source
  if (act.startsWith('pr_review') || act === 'github_comment' || act === 'pr_digest_posted') return 'github'
  if (act === 'post_meeting_posted') return 'teams'
  if (act === 'weekly_status_post') return 'teams'
  if (act.startsWith('weekly_status')) return 'confluence'
  if (act === 'knowledge_synced' || act === 'knowledge_error')
    return d.ado?.length && !d.confluence?.length ? 'ado' : 'confluence'
  if (act === 'moved_email' || s === 'email' || ch === 'email') return 'outlook'
  if (act === 'note_written' || act === 'checkbox_toggled') return 'obsidian'
  if (act === 'setting_changed' || act === 'write_access_changed') return 'local'
  if (s === 'teams_chat' || s === 'teams_channel' || ch === 'teams') return 'teams'
  if (act === 'job_error') {
    const j = d.job || ''
    if (j.includes('email')) return 'outlook'
    if (j.includes('pr') || j.includes('github')) return 'github'
    if (j.includes('knowledge')) return 'confluence'
    return 'teams'
  }
  if (act === 'triage_error') return 'teams'
  // remaining triage/send actions operate on Teams chats by default
  if (['classified', 'item_ingested', 'draft_created', 'approved_send', 'auto_reply',
       'chieff_auto', 'holding_message', 'pr_review_post'].includes(act)) {
    if (ch === 'email') return 'outlook'
    return 'teams'
  }
  return 'local'
}

// ---- action -> {cat, primary, secondary} ----------------------------------
const MAP = {
  item_ingested:    (d) => ['ingest', `New ${src(d)} from ${d.sender || '?'}`, d.preview],
  classified:       (d) => ['triage', `Tier ${d.tier}${d.urgent ? ' · urgent' : ''} — ${d.sender || conv(d)}`, d.reasoning],
  auto_reply:       (d) => ['send', `Auto-replied to ${conv(d)}`, d.body],
  chieff_auto:      (d) => ['send', `Chieff replied in ${conv(d)}`, d.body],
  holding_message:  (d) => ['send', `Holding message to ${conv(d)}`, d.body],
  approved_send:    (d) => ['send', `Sent ${d.channel || ''} reply you approved${d.dry_run ? ' (dry run)' : ''}`, d.body],
  draft_created:    (d) => ['draft', `Drafted reply for ${d.sender || conv(d)}`, d.body],
  pr_review_picked: (d) => ['review', `Reviewing PR ${pr(d)}`, d.title],
  pr_reviewed:      (d) => ['review', `Reviewed PR ${pr(d)} — ${d.critical_count || 0} finding(s)`, d.summary],
  pr_review_post:   (d) => ['send', `Posted PR review summary to chat`, d.body],
  github_comment:   (d) => ['send', `Commented on PR ${pr(d)}`, d.body],
  pr_digest_posted: () => ['knowledge', `Posted GitHub pending-work digest`, null],
  post_meeting_posted: () => ['knowledge', `Posted post-meeting catch-up digest`, null],
  knowledge_synced: (d) => ['knowledge', `Synced knowledge`, `${d.confluence?.length || 0} Confluence · ${d.ado?.length || 0} ADO`],
  weekly_status_drafted:   (d) => ['weekly', `Weekly status drafted — awaiting approval`, `${d.metrics?.prs ?? '?'} PRs · ${d.title || ''}`],
  weekly_status_published: () => ['weekly', `Published weekly status + posted summary`, null],
  weekly_status_discarded: () => ['weekly', `Discarded weekly status draft`, null],
  weekly_status_post:      () => ['send', `Posted weekly status to chat`, null],
  moved_email:      (d) => ['system', `Filed email`, d.subject],
  note_written:     () => ['system', `Updated the daily note`, null],
  setting_changed:  (d) => ['system', `Set ${d.key} = ${d.value}`, null],
  checkbox_toggled: () => ['system', `Toggled a task in the daily note`, null],
  write_access_changed: (d) => ['system', `${d.allow ? 'Enabled' : 'Disabled'} writing in ${d.chat}`, null],
  item_status_changed: (d) => ['system', `Marked item ${d.to === 'new' ? 'reopened' : d.to}`, `was: ${d.from}`],
  connector_reconnect: (d) => ['system', `Restarted MCP server ${d.server} (${d.status})`, null],
}

function trim(s, n) { s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s }
function src(d) { return (d.source || 'item').replace('teams_chat', 'Teams message').replace('teams_channel', 'Teams channel').replace('email', 'email') }
function conv(d) { return trim(d.conversation || d.recipient || d.subject || 'a chat', 38) }
function pr(d) { const m = String(d.pr_url || '').match(/([^/]+)\/pull\/(\d+)/); return m ? `${m[1]} #${m[2]}` : trim(d.pr_url, 36) }
const isErr = (a) => /error|failed|blocked/i.test(a)

function parse(a) {
  try { return typeof a.detail === 'string' ? JSON.parse(a.detail) : a.detail || {} } catch { return {} }
}

function describe(a, d = parse(a)) {
  if (isErr(a.action)) return { cat: 'error', primary: a.action.replace(/_/g, ' '), secondary: d.error || d.reason || a.detail }
  const f = MAP[a.action]
  if (f) { const [cat, primary, secondary] = f(d); return { cat, primary, secondary } }
  return { cat: 'system', primary: a.action.replace(/_/g, ' '), secondary: null }
}

// What Chief "thought" vs what it "did", surfaced on expand.
function reasoning(a, d) {
  if (a.action === 'classified')
    return `Tier ${d.tier}${d.urgent ? ' · urgent' : ''}. ${d.reasoning || ''}` +
      (d.needs_reply !== undefined ? `  (needs reply: ${d.needs_reply ? 'yes' : 'no'}` +
        (d.waiting !== undefined ? `, sender waiting: ${d.waiting ? 'yes' : 'no'}` : '') + ')' : '')
  if (isErr(a.action)) return d.error || d.reason || a.detail
  return null
}
function actionText(a, d) {
  return d.body || d.summary || d.title || d.preview || null
}

const clock = (ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

function RouteChip({ active, onClick, Logo, iconClass, label }) {
  return (
    <button
      onClick={onClick}
      className={`press flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] ring-1 ${
        active ? 'bg-zinc-800 text-zinc-100 ring-zinc-600' : 'bg-zinc-900 text-zinc-400 ring-zinc-800 hover:text-zinc-200'
      }`}
    >
      {Logo ? <Logo size={12} className={iconClass} /> : <Activity size={12} className="text-zinc-400" />}
      {label}
    </button>
  )
}

function Block({ label, tone, body, mono }) {
  return (
    <div>
      <div className={`mb-0.5 text-[9px] uppercase tracking-wide ${tone}`}>{label}</div>
      <div className={`whitespace-pre-wrap break-words text-zinc-300 ${mono ? 'font-mono text-[10px]' : ''}`}>
        {String(body)}
      </div>
    </div>
  )
}

export default function ActivityPanel({ refreshTick }) {
  const [audit, setAudit] = useState([])
  const [hideErrors, setHideErrors] = useState(false)
  const [openId, setOpenId] = useState(null)
  const [routeFilter, setRouteFilter] = useState(null)
  const lastIdRef = React.useRef(0)

  useEffect(() => {
    // Full fetch on mount; incremental on subsequent ticks.
    const incremental = lastIdRef.current > 0
    const url = incremental
      ? `/api/audit?limit=200&after_id=${lastIdRef.current}`
      : '/api/audit?limit=150'
    fetch(url)
      .then((r) => r.json())
      .then((d) => {
        const rows = d.log ?? (Array.isArray(d) ? d : [])
        if (d.last_id) lastIdRef.current = d.last_id
        if (incremental) {
          // Overlapping fetches (two refreshTicks racing before lastIdRef
          // advances) can return the same rows twice — dedupe by id.
          setAudit((prev) => {
            const seen = new Set(prev.map((r) => r.id))
            const fresh = rows.filter((r) => !seen.has(r.id))
            return [...fresh, ...prev].slice(0, 300)
          })
        } else {
          setAudit(rows)
        }
      })
      .catch(() => {})
  }, [refreshTick])

  // Parse each row's JSON detail blob once per audit fetch, not on every
  // render — expanding a row or flipping a filter used to re-run JSON.parse
  // and re-derive the route for up to 300 rows on every keystroke/click.
  const enriched = useMemo(
    () => audit.map((a) => {
      const d = parse(a)
      return { a, d, route: route(a, d), err: isErr(a.action) }
    }),
    [audit],
  )

  // routes present in the current data, in a stable display order
  const order = ['teams', 'confluence', 'outlook', 'obsidian', 'github', 'ado', 'local']
  const present = useMemo(
    () => order.filter((k) => enriched.some((e) => e.route === k)),
    [enriched],
  )
  const errorCount = useMemo(() => enriched.filter((e) => e.err).length, [enriched])

  const rows = useMemo(() => {
    let r = hideErrors ? enriched.filter((e) => !e.err) : enriched
    if (routeFilter) r = r.filter((e) => e.route === routeFilter)
    return r
  }, [enriched, hideErrors, routeFilter])

  // think -> act chain: every audit event sharing an item_id, oldest first
  const byItem = useMemo(() => {
    const map = {}
    audit.forEach((a) => { if (a.item_id != null) (map[a.item_id] ||= []).push(a) })
    Object.values(map).forEach((arr) => arr.sort((x, y) => x.id - y.id))
    return map
  }, [audit])

  // group consecutive rows by minute for a top-down, time-clustered read
  const groups = useMemo(() => {
    const g = []
    rows.forEach((e) => {
      const key = clock(e.a.ts)
      const last = g[g.length - 1]
      if (last && last.key === key) last.items.push(e)
      else g.push({ key, items: [e] })
    })
    return g
  }, [rows])

  return (
    <Panel
      title="Activity"
      badge={rows.length}
      actions={
        <button
          onClick={() => setHideErrors((v) => !v)}
          className={`press flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[11px] ring-1 transition-colors ${
            hideErrors ? 'bg-zinc-800 text-zinc-300 ring-zinc-700 hover:bg-zinc-700' : 'bg-red-950 text-red-300 ring-red-900 hover:bg-red-900/60'
          }`}
        >
          <AlertTriangle size={12} />
          {hideErrors ? `Errors hidden (${errorCount})` : `${errorCount} error${errorCount === 1 ? '' : 's'}`}
        </button>
      }
    >
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <RouteChip active={routeFilter === null} onClick={() => setRouteFilter(null)} label="All" />
        {present.map((k) => (
          <RouteChip
            key={k}
            active={routeFilter === k}
            onClick={() => setRouteFilter(routeFilter === k ? null : k)}
            Logo={ROUTES[k].Logo}
            iconClass={ROUTES[k].text}
            label={ROUTES[k].label}
          />
        ))}
      </div>
      <div className="stagger space-y-3">
        {rows.length === 0 && <Empty text="No activity yet" />}
        {groups.map((g, gi) => (
          <div key={gi}>
            <div className="mb-1 flex items-center gap-2">
              <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">{g.key}</span>
              <span className="h-px flex-1 bg-zinc-800/70" />
            </div>
            <div className="space-y-1">
              {g.items.map(({ a, d, route: rt }) => {
                const { cat, primary, secondary } = describe(a, d)
                const C = CATS[cat] || CATS.system
                const Icon = C.icon
                const err = cat === 'error'
                const RT = ROUTES[rt] || ROUTES.local
                const isOpen = openId === a.id
                const chain = a.item_id != null ? byItem[a.item_id] || [] : []
                const think = reasoning(a, d)
                const act = actionText(a, d)
                return (
                  <div
                    key={a.id}
                    className={`overflow-hidden rounded-lg border-l-2 ${C.bar.replace('bg-', 'border-')} ${
                      err ? 'bg-red-950/30' : isOpen ? 'bg-zinc-900' : ''
                    }`}
                  >
                    <button
                      onClick={() => setOpenId(isOpen ? null : a.id)}
                      className="row-hover flex w-full items-start gap-2.5 py-1.5 pl-2.5 pr-2 text-left"
                    >
                      <Icon size={14} className={`mt-0.5 shrink-0 ${C.text}`} />
                      <span className="mt-px flex shrink-0 items-center gap-1 rounded bg-zinc-900 px-1.5 text-[9px] font-semibold uppercase leading-4 tracking-wide ring-1 ring-zinc-700"
                            title={`Source: ${RT.label}`}>
                        <RT.Logo size={11} className={RT.text} />
                        <span className={RT.text}>{RT.label}</span>
                      </span>
                      <span className={`mt-px shrink-0 rounded px-1.5 text-[9px] font-medium uppercase leading-4 tracking-wide ring-1 ${C.chip}`}>
                        {C.label}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className={`truncate text-xs font-medium ${err ? 'text-red-200' : 'text-zinc-200'}`}>
                          {primary}
                        </div>
                        {secondary && (
                          <div className="truncate text-[11px] text-zinc-500" title={String(secondary)}>
                            {trim(secondary, 120)}
                          </div>
                        )}
                      </div>
                      {(a.actor === 'user' || a.actor === 'alex') && (
                        <span className="mt-0.5 shrink-0 rounded bg-zinc-800 px-1.5 text-[9px] text-zinc-500">you</span>
                      )}
                    </button>

                    {isOpen && (
                      <div className="animate-fade-in space-y-2.5 border-t border-zinc-800 px-3 py-2.5 text-[11px]">
                        {think && (
                          <Block label="Chief's reasoning" tone="text-violet-300" body={think} />
                        )}
                        {act && (
                          <Block label={act === d.body ? 'What it wrote' : 'Detail'} tone="text-emerald-300" body={act} mono />
                        )}
                        {chain.length > 1 && (
                          <div>
                            <div className="mb-1 text-[9px] uppercase tracking-wide text-zinc-600">
                              How Chief handled this ({chain.length} steps)
                            </div>
                            <div className="space-y-1 border-l border-zinc-800 pl-2.5">
                              {chain.map((c) => {
                                const cd = describe(c)
                                const CC = CATS[cd.cat] || CATS.system
                                return (
                                  <div key={c.id} className="flex items-center gap-2">
                                    <span className="font-mono text-[9px] text-zinc-600">{clock(c.ts)}</span>
                                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${CC.bar}`} />
                                    <span className={`shrink-0 text-[10px] ${CC.text}`}>{CC.label}</span>
                                    <span className="truncate text-zinc-400">{cd.primary}</span>
                                  </div>
                                )
                              })}
                            </div>
                          </div>
                        )}
                        {!think && !act && chain.length <= 1 && (
                          <pre className="whitespace-pre-wrap break-words text-[10px] text-zinc-500">
                            {JSON.stringify(d, null, 1)}
                          </pre>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </Panel>
  )
}
