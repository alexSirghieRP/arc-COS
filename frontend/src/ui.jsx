import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { CheckCircle2, AlertTriangle, Info, X } from 'lucide-react'

// ---------------------------------------------------------------------------
// Toasts: small, self-dismissing notices replacing silent failures/alert().
// ---------------------------------------------------------------------------
const ToastCtx = createContext(() => {})

export const useToast = () => useContext(ToastCtx)

const TONE = {
  success: { icon: CheckCircle2, cls: 'border-emerald-700/60 text-emerald-200', iconCls: 'text-emerald-400' },
  error: { icon: AlertTriangle, cls: 'border-red-700/60 text-red-200', iconCls: 'text-red-400' },
  info: { icon: Info, cls: 'border-zinc-700 text-zinc-200', iconCls: 'text-sky-400' },
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const idRef = useRef(0)

  const push = useCallback((message, tone = 'info', ttl = 4200) => {
    const id = ++idRef.current
    setToasts((t) => [...t.slice(-3), { id, message: String(message), tone }])
    if (ttl) setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), ttl)
    return id
  }, [])

  const dismiss = (id) => setToasts((t) => t.filter((x) => x.id !== id))

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[380px] flex-col gap-2">
        {toasts.map((t) => {
          const T = TONE[t.tone] || TONE.info
          const Icon = T.icon
          return (
            <div
              key={t.id}
              className={`toast-in pointer-events-auto flex items-start gap-2.5 rounded-xl border bg-zinc-900 px-3.5 py-2.5 text-[13px] shadow-[var(--shadow)] ${T.cls}`}
            >
              <Icon size={15} className={`mt-0.5 shrink-0 ${T.iconCls}`} />
              <span className="min-w-0 flex-1 break-words">{t.message}</span>
              <button onClick={() => dismiss(t.id)} className="press shrink-0 text-zinc-500 hover:text-zinc-200">
                <X size={13} />
              </button>
            </div>
          )
        })}
      </div>
    </ToastCtx.Provider>
  )
}

// ---------------------------------------------------------------------------
// Skeleton shimmer for loading states.
// ---------------------------------------------------------------------------
export function Skeleton({ className = '' }) {
  return <div className={`skeleton rounded-md ${className}`} />
}

export function SkeletonRows({ n = 4, className = 'h-9' }) {
  return (
    <div className="space-y-1.5">
      {Array.from({ length: n }, (_, i) => (
        <Skeleton key={i} className={className} />
      ))}
    </div>
  )
}

// Whole-panel initial-load placeholder — a header strip + a couple of content
// blocks, used in place of a bare "loading…" string.
export function PanelLoading() {
  return (
    <div className="space-y-2.5">
      <div className="flex gap-2">
        <Skeleton className="h-14 flex-1" />
        <Skeleton className="h-14 flex-1" />
        <Skeleton className="h-14 flex-1" />
      </div>
      <Skeleton className="h-28 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Relative time that stays fresh.
// ---------------------------------------------------------------------------
export function timeAgo(iso) {
  if (!iso) return ''
  const diff = (Date.now() - new Date(iso)) / 1000
  const s = Math.abs(diff)
  const fmt = (n, unit) => (diff < 0 ? `in ${n}${unit}` : `${n}${unit} ago`)
  if (s < 45) return diff < 0 ? 'in <1m' : 'just now'
  const m = s / 60
  if (m < 60) return fmt(Math.round(m), 'm')
  const h = m / 60
  if (h < 24) return fmt(Math.round(h), 'h')
  return fmt(Math.round(h / 24), 'd')
}

export function TimeAgo({ iso, className = '' }) {
  const [, force] = useState(0)
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 30000)
    return () => clearInterval(t)
  }, [])
  return (
    <span className={className} title={iso ? new Date(iso).toLocaleString() : ''}>
      {timeAgo(iso)}
    </span>
  )
}

export const fmtMs = (ms) =>
  ms == null ? '—' : ms < 1000 ? `${ms}ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)}s` : `${(ms / 60000).toFixed(1)}m`

// Teams/Graph message bodies keep HTML entities after tag-stripping; decode the
// common ones for display so pings don't read "2 -&gt; 1".
const ENTITIES = { '&gt;': '>', '&lt;': '<', '&amp;': '&', '&quot;': '"', '&#39;': "'", '&apos;': "'", '&nbsp;': ' ' }
export const decodeEntities = (s) =>
  (s || '').replace(/&(?:gt|lt|amp|quot|#39|apos|nbsp);/g, (m) => ENTITIES[m])

// ---------------------------------------------------------------------------
// Animated number — eases from the previously shown value to a new one
// whenever `value` changes, instead of snapping. Respects reduced-motion.
// ---------------------------------------------------------------------------
const reduceMotion = () =>
  typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

export function useCountUp(value, duration = 450) {
  const target = Number(value)
  const [display, setDisplay] = useState(target)
  const fromRef = useRef(target)
  const rafRef = useRef(null)

  useEffect(() => {
    if (!Number.isFinite(target)) { setDisplay(value); return }
    if (reduceMotion() || !Number.isFinite(fromRef.current)) {
      fromRef.current = target
      setDisplay(target)
      return
    }
    const from = fromRef.current
    if (from === target) return
    const start = performance.now()
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration)
      const eased = 1 - Math.pow(1 - t, 3)
      setDisplay(from + (target - from) * eased)
      if (t < 1) rafRef.current = requestAnimationFrame(tick)
      else fromRef.current = target
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, duration])

  return Number.isFinite(target) ? display : value
}

export function CountUp({ value, duration, format, className }) {
  const display = useCountUp(value, duration)
  const shown = format ? format(display) : Math.round(display)
  return <span className={className}>{shown}</span>
}
