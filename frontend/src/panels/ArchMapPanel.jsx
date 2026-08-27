import React, { useEffect, useState, useRef, useCallback, useMemo } from 'react'
import { ChevronLeft, Search } from 'lucide-react'
import { Panel } from '../App.jsx'
import OscaMonitorPanel from './OscaMonitorPanel.jsx'

// React attaches its synthetic onWheel as a *passive* native listener (for scroll perf),
// so e.preventDefault() inside a JSX onWheel handler silently no-ops — the trackpad's
// wheel/pinch gesture still scrolls/zooms the surrounding WKWebView instead of our SVG.
// Attaching the listener directly with passive:false makes preventDefault actually work.
function useNonPassiveWheel(ref, handler) {
  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [ref, handler])
}

// ─── Embedded fallback data ───────────────────────────────────────────────────
// Sample/fictional systems so the panel renders out of the box — the real
// catalog is served by /api/arch-map (backend/app/arch_map.py); real content
// is expected to be maintained there for your own system landscape.

const USECASES = [
  {
    id: 'support_copilot',
    label: 'Support Copilot',
    status: 'UAT',
    description: 'End-to-end support-case pipeline: Salesforce → L2 Orchestrator → 4 L1 agents → data platform',
    color: '#6C5CE7',
    size: 48,
  },
  {
    id: 'docs_pipeline',
    label: 'Docs Pipeline',
    status: 'Active',
    description: 'Inbound document intake, classification, and routing pipeline (part of Support Copilot)',
    color: '#FDCB6E',
    size: 36,
  },
  {
    id: 'billing_recon',
    label: 'Billing Reconciler',
    status: 'Alpha',
    description: 'Automated missing-invoice detection and resolution agent',
    color: '#00B894',
    size: 32,
  },
  {
    id: 'chief_of_staff',
    label: 'Chief of Staff',
    status: 'Active',
    description: 'This app — meta-layer: pings, approvals, sweeps, roadmap, Obsidian brain',
    color: '#FD79A8',
    size: 30,
  },
]

const SUPPORT_COPILOT_NODES = [
  { id: 'salesforce', label: 'Salesforce', type: 'external', color: '#00A1E0', size: 32, x: 80, y: 250,
    desc: 'Support-case source + status sync back after delivery' },
  { id: 'l2_orchestrator', label: 'L2 Orchestrator', type: 'agent', color: '#6C5CE7', size: 42, x: 240, y: 250,
    desc: 'Hub-and-spoke supervisor. Dispatches L1 work orders, advances milestones via callbacks.' },
  { id: 'comms_agent', label: 'Node A: Comms', type: 'agent', color: '#00B894', size: 34, x: 420, y: 100,
    desc: 'Questionnaire provision + HITL email drafting via LangGraph. Sends welcome email via SendGrid.' },
  { id: 'doc_intel', label: 'Node B: DocIntel', type: 'agent', color: '#FDCB6E', size: 34, x: 420, y: 210,
    desc: 'Vision extraction of PDFs/CSVs (uploaded reports and forms) via a vision model on Vertex AI.' },
  { id: 'case_assembly', label: 'Node C: Assembly', type: 'agent', color: '#E17055', size: 34, x: 420, y: 320,
    desc: 'Merges extracted records into a case file, validates, detects overflow. PII stays in GCS.' },
  { id: 'delivery_agent', label: 'Node D: Delivery', type: 'agent', color: '#74B9FF', size: 34, x: 420, y: 430,
    desc: 'Imports the case file into the data platform. Syncs status back to Salesforce.' },
  { id: 'integration_service', label: 'Integration Svc', type: 'service', color: '#A29BFE', size: 30, x: 600, y: 250,
    desc: 'External API gateway for all outbound calls: Salesforce, SendGrid, data platform, Firestore, GCS, Outlook.' },
  { id: 'control_tower', label: 'Control Tower', type: 'ui', color: '#FD79A8', size: 28, x: 240, y: 430,
    desc: 'Next.js HITL dashboard: intake wizard, email review, document review queue, escalation queue.' },
  { id: 'eval_reviewer', label: 'Eval Reviewer', type: 'ui', color: '#636E72', size: 24, x: 240, y: 100,
    desc: 'Internal review tool for validating document extraction eval runs and mapping rules.' },
  { id: 'data_platform', label: 'Data Platform', type: 'external', color: '#00CEC9', size: 32, x: 760, y: 250,
    desc: 'Case-file import target and downstream system of record.' },
  { id: 'firestore', label: 'Firestore', type: 'infra', color: '#FF7675', size: 26, x: 600, y: 100,
    desc: 'Primary state store: job configs, document tasks, communication snapshots, idempotency log.' },
  { id: 'gcs', label: 'Cloud Storage', type: 'infra', color: '#55EFC4', size: 26, x: 600, y: 380,
    desc: 'PII-bearing artifact staging: extracted records, case snapshots, sidecars.' },
  { id: 'vertex_ai', label: 'Vertex AI', type: 'infra', color: '#FDCB6E', size: 24, x: 760, y: 130,
    desc: 'Vision model for document extraction. Fast model for special-instructions parsing.' },
  { id: 'dlp', label: 'Cloud DLP', type: 'infra', color: '#B2BEC3', size: 22, x: 760, y: 370,
    desc: 'PII redaction before Firestore writes. Egress guard blocks redacted sentinels from leaving the pipeline.' },
  { id: 'sendgrid', label: 'SendGrid', type: 'external', color: '#1A73E8', size: 26, x: 600, y: 430,
    desc: 'Transactional email delivery (welcome emails). Inbound parse webhook.' },
]

const SUPPORT_COPILOT_EDGES = [
  { source: 'salesforce', target: 'integration_service', label: 'CDC stream', type: 'data' },
  { source: 'integration_service', target: 'l2_orchestrator', label: 'ingress events', type: 'call' },
  { source: 'l2_orchestrator', target: 'comms_agent', label: 'Node A work order', type: 'dispatch' },
  { source: 'l2_orchestrator', target: 'doc_intel', label: 'Node B work order', type: 'dispatch' },
  { source: 'l2_orchestrator', target: 'case_assembly', label: 'Node C work order', type: 'dispatch' },
  { source: 'l2_orchestrator', target: 'delivery_agent', label: 'Node D work order', type: 'dispatch' },
  { source: 'comms_agent', target: 'integration_service', label: 'email + provision', type: 'call' },
  { source: 'comms_agent', target: 'l2_orchestrator', label: 'callback', type: 'callback' },
  { source: 'doc_intel', target: 'vertex_ai', label: 'vision extraction', type: 'call' },
  { source: 'doc_intel', target: 'gcs', label: 'artifacts write', type: 'data' },
  { source: 'doc_intel', target: 'l2_orchestrator', label: 'callback', type: 'callback' },
  { source: 'case_assembly', target: 'gcs', label: 'case write', type: 'data' },
  { source: 'case_assembly', target: 'l2_orchestrator', label: 'callback', type: 'callback' },
  { source: 'delivery_agent', target: 'gcs', label: 'read case', type: 'data' },
  { source: 'delivery_agent', target: 'integration_service', label: 'case import', type: 'call' },
  { source: 'integration_service', target: 'data_platform', label: 'import case file', type: 'call' },
  { source: 'integration_service', target: 'salesforce', label: 'status sync', type: 'call' },
  { source: 'integration_service', target: 'sendgrid', label: 'send email', type: 'call' },
  { source: 'integration_service', target: 'firestore', label: 'snapshots', type: 'data' },
  { source: 'l2_orchestrator', target: 'firestore', label: 'state', type: 'data' },
  { source: 'l2_orchestrator', target: 'dlp', label: 'PII redaction', type: 'call' },
  { source: 'control_tower', target: 'comms_agent', label: 'HITL approve', type: 'ui' },
  { source: 'control_tower', target: 'l2_orchestrator', label: 'HITL controls', type: 'ui' },
  { source: 'eval_reviewer', target: 'doc_intel', label: 'eval runs', type: 'ui' },
]

const DETAILS_FALLBACK = {
  support_copilot: {
    nodes: SUPPORT_COPILOT_NODES,
    edges: SUPPORT_COPILOT_EDGES,
    description: 'End-to-end support-case pipeline (sample data)',
    status: 'UAT',
  },
}

// ─── Constants ────────────────────────────────────────────────────────────────

const EDGE_COLORS = {
  dispatch: '#6C5CE7',
  call:     '#74B9FF',
  callback: '#00B894',
  data:     '#FDCB6E',
  ui:       '#FD79A8',
}

const STATUS_COLORS_MAP = {
  uat:    { border: '#FDCB6E', text: '#FDCB6E', bg: 'rgba(253,203,110,0.15)' },
  active: { border: '#00B894', text: '#00B894', bg: 'rgba(0,184,148,0.15)' },
  alpha:  { border: '#71717a', text: '#a1a1aa', bg: 'rgba(113,113,122,0.15)' },
  early:  { border: '#71717a', text: '#a1a1aa', bg: 'rgba(113,113,122,0.15)' },
}
const STATUS_COLORS = { UAT: STATUS_COLORS_MAP.uat, Active: STATUS_COLORS_MAP.active, Alpha: STATUS_COLORS_MAP.alpha }
const statusColor = (s) => STATUS_COLORS_MAP[(s || '').toLowerCase()] || STATUS_COLORS_MAP.alpha

const NODE_ABBR = {
  salesforce: 'SF', l2_orchestrator: 'L2', comms_agent: 'NA', doc_intel: 'NB',
  case_assembly: 'NC', delivery_agent: 'ND', integration_service: 'API',
  control_tower: 'CT', eval_reviewer: 'ER', data_platform: 'DP',
  firestore: 'FS', gcs: 'GCS', vertex_ai: 'AI', dlp: 'DLP', sendgrid: 'SG',
  cos_backend: 'API', cos_frontend: 'UI', ms_graph: 'MSG', github: 'GH',
  slack: 'SLK', sqlite: 'DB', obsidian_vault: 'OBS', apscheduler: 'SCH',
}

const TILE_H = { agent: 54, external: 48, service: 46, ui: 44, infra: 42 }

const GAL_W = 820
const GAL_H = 420
const DET_W = 920
const DET_H = 540

// ─── Force simulation ─────────────────────────────────────────────────────────

const NODE_SIZE_DEFAULTS = { agent: 38, external: 30, service: 26, ui: 26, infra: 22 }

function runSim(nodes, edges, width, height, ticks = 200) {
  const pos = nodes.map((n, i) => ({
    ...n,
    size: n.size ?? NODE_SIZE_DEFAULTS[n.type] ?? 28,
    color: n.color ?? '#6C5CE7',
    x: n.x != null ? n.x : width * 0.15 + (i % 5) * (width * 0.16) + (Math.floor(i / 5) % 2) * 40,
    y: n.y != null ? n.y : height * 0.2 + Math.floor(i / 5) * (height * 0.25),
    vx: 0, vy: 0,
  }))
  const byId = {}
  pos.forEach((p) => { byId[p.id] = p })
  for (let t = 0; t < ticks; t++) {
    const alpha = 1 - t / ticks
    for (let i = 0; i < pos.length; i++) {
      for (let j = i + 1; j < pos.length; j++) {
        const a = pos[i]; const b = pos[j]
        const dx = b.x - a.x; const dy = b.y - a.y
        const dist = Math.max(Math.hypot(dx, dy), 1)
        const minDist = (a.size + b.size) * 1.5 + 40
        const force = Math.min((minDist * minDist) / (dist * dist), 180) * alpha
        const fx = (dx / dist) * force; const fy = (dy / dist) * force
        a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy
      }
    }
    const restLen = 165
    for (const e of edges) {
      const a = byId[e.source]; const b = byId[e.target]
      if (!a || !b) continue
      const dx = b.x - a.x; const dy = b.y - a.y
      const dist = Math.max(Math.hypot(dx, dy), 1)
      const force = (dist - restLen) * 0.035 * alpha
      const fx = (dx / dist) * force; const fy = (dy / dist) * force
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy
    }
    for (const p of pos) {
      p.vx += (width / 2 - p.x) * 0.008 * alpha
      p.vy += (height / 2 - p.y) * 0.008 * alpha
      p.vx *= 0.84; p.vy *= 0.84
      p.x += p.vx; p.y += p.vy
      const m = p.size + 16
      p.x = Math.max(m, Math.min(width - m, p.x))
      p.y = Math.max(m, Math.min(height - m, p.y))
    }
  }
  return pos
}

// ─── SVG defs: markers, glow filters, flow animation ─────────────────────────

function MapDefs() {
  return (
    <defs>
      <style>{`
        @keyframes flowdash {
          from { stroke-dashoffset: 28; }
          to { stroke-dashoffset: 0; }
        }
        .flow-anim { animation: flowdash 1.6s linear infinite; }
        @keyframes pulseRing {
          0%,100% { opacity: 0.18; r: 38; }
          50% { opacity: 0.38; r: 46; }
        }
        .pulse-ring { animation: pulseRing 2.6s ease-in-out infinite; }
      `}</style>
      <filter id="map-edge-glow" x="-30%" y="-30%" width="160%" height="160%">
        <feGaussianBlur stdDeviation="3.5" result="blur" />
        <feMerge>
          <feMergeNode in="blur" />
          <feMergeNode in="SourceGraphic" />
        </feMerge>
      </filter>
      <filter id="map-node-glow" x="-40%" y="-40%" width="180%" height="180%">
        <feGaussianBlur stdDeviation="6" result="blur" />
        <feMerge>
          <feMergeNode in="blur" />
          <feMergeNode in="SourceGraphic" />
        </feMerge>
      </filter>
      {/* Circuit board background pattern */}
      <pattern id="circuit-grid" x="0" y="0" width="40" height="40" patternUnits="userSpaceOnUse">
        <line x1="40" y1="0" x2="40" y2="40" stroke="#1e3a5f" strokeWidth="0.4" />
        <line x1="0" y1="40" x2="40" y2="40" stroke="#1e3a5f" strokeWidth="0.4" />
        <circle cx="40" cy="40" r="1.2" fill="#1e3a5f" />
        <circle cx="0" cy="0" r="1.2" fill="#1e3a5f" />
      </pattern>
      {/* Arrow markers per edge type */}
      {Object.entries(EDGE_COLORS).map(([type, color]) => (
        <marker key={type} id={`tip-${type}`}
          markerWidth="7" markerHeight="6" refX="5" refY="3" orient="auto" markerUnits="userSpaceOnUse">
          <path d="M0,0 L0,6 L7,3 z" fill={color} opacity={0.9} />
        </marker>
      ))}
    </defs>
  )
}

// ─── Circuit board background ─────────────────────────────────────────────────

function CircuitBg({ width, height }) {
  return (
    <>
      <rect width={width} height={height} fill="#060e1c" />
      <rect width={width} height={height} fill="url(#circuit-grid)" opacity={0.9} />
      {/* Radial center glow */}
      <radialGradient id="center-glow" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor="#0f2044" stopOpacity="0.6" />
        <stop offset="100%" stopColor="#060e1c" stopOpacity="0" />
      </radialGradient>
      <rect width={width} height={height} fill="url(#center-glow)" />
    </>
  )
}

// ─── Glowing edge ─────────────────────────────────────────────────────────────

function GlowEdge({ x1, y1, x2, y2, color, type, label, isHov, onEnter, onLeave }) {
  const mx = (x1 + x2) / 2
  const my = (y1 + y2) / 2
  const dx = x2 - x1; const dy = y2 - y1
  const len = Math.max(Math.hypot(dx, dy), 1)
  // slight perpendicular bend for visual separation
  const px = (-dy / len) * 14; const py = (dx / len) * 14
  const d = `M ${x1} ${y1} Q ${mx + px} ${my + py} ${x2} ${y2}`

  return (
    <g>
      {/* Invisible wide hit area */}
      <path d={d} stroke="transparent" strokeWidth={18} fill="none"
        onMouseEnter={onEnter} onMouseLeave={onLeave} />
      {/* Wide outer blur halo */}
      <path d={d} stroke={color} strokeWidth={9} opacity={0.12} fill="none"
        filter="url(#map-edge-glow)" pointerEvents="none" />
      {/* Mid glow */}
      <path d={d} stroke={color} strokeWidth={4} opacity={isHov ? 0.55 : 0.28} fill="none"
        pointerEvents="none" style={{ transition: 'opacity 0.2s' }} />
      {/* Core animated flow line */}
      <path d={d} stroke={color} strokeWidth={1.5} opacity={isHov ? 1 : 0.7} fill="none"
        strokeDasharray="7 5" className="flow-anim"
        markerEnd={`url(#tip-${type})`} pointerEvents="none"
        style={{ transition: 'opacity 0.2s' }} />
      {/* Hover label */}
      {isHov && label && (
        <g>
          <rect x={mx + px - 38} y={my + py - 14} width={76} height={17} rx={5}
            fill="#0d1b35" stroke={color} strokeWidth={0.7} opacity={0.95} />
          <text x={mx + px} y={my + py - 2} textAnchor="middle"
            fill={color} fontSize={9} fontWeight={700} pointerEvents="none">
            {label}
          </text>
        </g>
      )}
    </g>
  )
}

// ─── Node platform tile ───────────────────────────────────────────────────────

function NodeTile({ node, isHov, highlight, isDimmed, onMouseDown, onMouseEnter, onMouseLeave, dragging, liveStatus }) {
  const { x, y, color, label, type, size } = node
  const abbr = NODE_ABBR[node.id] || label.split(/[\s:]+/).map((w) => w[0] || '').join('').slice(0, 3).toUpperCase()
  const tw = Math.max(64, size * 1.85)
  const th = TILE_H[type] || 50
  const isAgent = type === 'agent'
  const lsColor = liveStatus ? (liveStatus.status === 'fail' ? '#ef4444' : '#f59e0b') : null

  const glowStrength = highlight ? '14px' : isHov ? '10px' : '4px'
  const tileOpacity = highlight ? 1 : isHov ? 0.96 : 0.82
  const borderWidth = isAgent ? 2 : 1.5
  const borderDash = type === 'external' ? '5 3' : type === 'service' ? '3 3' : undefined

  return (
    <g
      opacity={isDimmed ? 0.12 : 1}
      onMouseDown={onMouseDown}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      style={{ cursor: dragging ? 'grabbing' : 'grab', transition: 'opacity 0.25s ease' }}
    >
      {/* Outer glow aura */}
      {(isHov || highlight) && (
        <rect
          x={x - tw / 2 - 8} y={y - th / 2 - 8}
          width={tw + 16} height={th + 16} rx={18}
          fill={color} opacity={highlight ? 0.22 : 0.14}
          style={{ filter: `blur(${glowStrength})` }}
          pointerEvents="none"
        />
      )}
      {/* Ambient glow always */}
      <rect
        x={x - tw / 2 - 4} y={y - th / 2 - 4}
        width={tw + 8} height={th + 8} rx={16}
        fill={color} opacity={0.06}
        style={{ filter: 'blur(6px)' }}
        pointerEvents="none"
      />
      {/* Live problem ring — this component is failing/degraded right now */}
      {lsColor && (
        <>
          <rect
            x={x - tw / 2 - 3.5} y={y - th / 2 - 3.5}
            width={tw + 7} height={th + 7} rx={14}
            fill="none" stroke={lsColor} strokeWidth={2}
            style={{ filter: `drop-shadow(0 0 7px ${lsColor}cc)` }}
            pointerEvents="none"
          >
            {liveStatus.status === 'fail' && (
              <animate attributeName="opacity" values="1;0.35;1" dur="1.4s" repeatCount="indefinite" />
            )}
          </rect>
          <text
            x={x} y={y - th / 2 - 9} textAnchor="middle"
            fill={lsColor} fontSize={7.5} fontWeight={800}
            pointerEvents="none"
          >
            {(liveStatus.label || '').slice(0, 34)}
          </text>
        </>
      )}
      {/* Tile body */}
      <rect
        x={x - tw / 2} y={y - th / 2}
        width={tw} height={th} rx={12}
        fill="#0a1628" stroke={color}
        strokeWidth={borderWidth} strokeDasharray={borderDash}
        opacity={tileOpacity}
        filter={isHov || highlight ? 'url(#map-node-glow)' : undefined}
        style={{ transition: 'opacity 0.2s' }}
      />
      {/* Inner highlight line at top edge */}
      <line
        x1={x - tw / 2 + 12} y1={y - th / 2 + 1}
        x2={x + tw / 2 - 12} y2={y - th / 2 + 1}
        stroke={color} strokeWidth={0.6} opacity={0.35}
        pointerEvents="none"
      />
      {/* Abbreviation / icon text */}
      <text
        x={x} y={y + 5}
        textAnchor="middle"
        fill={color}
        fontSize={isAgent ? 15 : 13}
        fontWeight={800}
        fontFamily="'SF Mono', 'Fira Mono', monospace"
        opacity={isHov ? 1 : 0.9}
        pointerEvents="none"
        style={{ transition: 'opacity 0.15s', letterSpacing: '0.04em' }}
      >
        {abbr}
      </text>
      {/* Label below tile */}
      <text
        x={x} y={y + th / 2 + 15}
        textAnchor="middle"
        fill={isHov ? '#e2e8f0' : '#64748b'}
        fontSize={9}
        fontWeight={500}
        pointerEvents="none"
        style={{ transition: 'fill 0.15s' }}
      >
        {label}
      </text>
    </g>
  )
}

// ─── Galaxy view (all use-cases overview) ─────────────────────────────────────

function GalaxyView({ usecases, onSelect, live }) {
  const [hovered, setHovered] = useState(null)
  const [xform, setXform] = useState({ x: 0, y: 0, scale: 1 })
  const xformRef = useRef(xform)
  useEffect(() => { xformRef.current = xform }, [xform])
  const svgRef = useRef(null)
  const isPanning = useRef(false)
  const panStart = useRef(null)

  const CX = GAL_W / 2
  const CY = GAL_H / 2

  const positions = useMemo(() =>
    usecases.map((uc, i) => {
      const angle = (i / usecases.length) * 2 * Math.PI - Math.PI / 2
      return {
        ...uc,
        x: CX + Math.cos(angle) * 158,
        y: CY + Math.sin(angle) * 128,
      }
    }),
  [usecases])

  const onWheel = useCallback((e) => {
    e.preventDefault()
    const factor = e.deltaY < 0 ? 1.1 : 0.9
    setXform((t) => {
      const s = Math.max(0.3, Math.min(3, t.scale * factor))
      const rect = svgRef.current?.getBoundingClientRect()
      if (!rect) return { ...t, scale: s }
      const cx = e.clientX - rect.left; const cy = e.clientY - rect.top
      return { scale: s, x: cx - (cx - t.x) * (s / t.scale), y: cy - (cy - t.y) * (s / t.scale) }
    })
  }, [])
  useNonPassiveWheel(svgRef, onWheel)

  const onMouseDown = useCallback((e) => {
    if (e.target === svgRef.current || e.target.tagName === 'svg') {
      isPanning.current = true
      panStart.current = { x: e.clientX - xformRef.current.x, y: e.clientY - xformRef.current.y }
    }
  }, [])

  const onMouseMove = useCallback((e) => {
    if (isPanning.current && panStart.current)
      setXform((t) => ({ ...t, x: e.clientX - panStart.current.x, y: e.clientY - panStart.current.y }))
  }, [])

  const onMouseUp = useCallback(() => { isPanning.current = false }, [])

  return (
    <div className="overflow-hidden relative select-none"
      style={{ height: 'calc(100vh - 165px)', minHeight: GAL_H }}
      onMouseMove={onMouseMove} onMouseUp={onMouseUp} onMouseLeave={onMouseUp}>
      <svg ref={svgRef} width="100%" height="100%" viewBox={`0 0 ${GAL_W} ${GAL_H}`}
        overflow="visible" onMouseDown={onMouseDown}
        style={{ cursor: 'grab', background: '#060e1c' }}>
        <MapDefs />
        <CircuitBg width={GAL_W} height={GAL_H} />
        <g transform={`translate(${xform.x},${xform.y}) scale(${xform.scale})`}>

          {/* Spokes from center hub to each use-case */}
          {positions.map((uc) => {
            const isHov = hovered === uc.id
            return (
              <g key={`spoke-${uc.id}`} pointerEvents="none">
                {/* Outer blur halo */}
                <line x1={CX} y1={CY} x2={uc.x} y2={uc.y}
                  stroke={uc.color} strokeWidth={6} opacity={isHov ? 0.18 : 0.08}
                  style={{ filter: 'blur(4px)', transition: 'opacity 0.2s' }} />
                {/* Mid glow */}
                <line x1={CX} y1={CY} x2={uc.x} y2={uc.y}
                  stroke={uc.color} strokeWidth={2.5} opacity={isHov ? 0.5 : 0.22}
                  style={{ transition: 'opacity 0.2s' }} />
                {/* Core animated */}
                <line x1={CX} y1={CY} x2={uc.x} y2={uc.y}
                  stroke={uc.color} strokeWidth={1.2} opacity={isHov ? 0.9 : 0.45}
                  strokeDasharray="8 6" className="flow-anim"
                  style={{ transition: 'opacity 0.2s' }} />
              </g>
            )
          })}

          {/* Central hub (ARC PIPELINE) */}
          <g style={{ cursor: 'default' }}>
            {/* Pulsing rings */}
            <circle cx={CX} cy={CY} r={44} fill="none" stroke="#6C5CE7" strokeWidth={0.8}
              opacity={0.25} className="pulse-ring" />
            <circle cx={CX} cy={CY} r={30} fill="none" stroke="#6C5CE7" strokeWidth={0.5} opacity={0.4} />
            {/* Hub tile */}
            <rect x={CX - 34} y={CY - 28} width={68} height={56} rx={12}
              fill="#0d1628" stroke="#6C5CE7" strokeWidth={2.5}
              filter="url(#map-node-glow)" />
            <line x1={CX - 22} y1={CY - 27} x2={CX + 22} y2={CY - 27}
              stroke="#6C5CE7" strokeWidth={0.7} opacity={0.4} />
            <text x={CX} y={CY - 5} textAnchor="middle"
              fill="#8b7cf8" fontSize={11} fontWeight={900} fontFamily="'SF Mono','Fira Mono',monospace"
              letterSpacing="0.1em">
              ARC
            </text>
            <text x={CX} y={CY + 9} textAnchor="middle"
              fill="#5b52b8" fontSize={7} fontWeight={600} letterSpacing="0.12em">
              PIPELINE
            </text>
            <text x={CX} y={CY + 22} textAnchor="middle"
              fill="#4c3d9e" fontSize={6.5} letterSpacing="0.06em">
              AI Platform
            </text>
          </g>

          {/* Use-case platform tiles */}
          {positions.map((uc) => {
            const isHov = hovered === uc.id
            const sc = statusColor(uc.status)
            const TW = 110; const TH = 72
            return (
              <g key={uc.id}
                onClick={() => onSelect(uc.id)}
                onMouseEnter={() => setHovered(uc.id)}
                onMouseLeave={() => setHovered(null)}
                style={{ cursor: 'pointer' }}>
                {/* Outer aura on hover */}
                {isHov && (
                  <rect x={uc.x - TW / 2 - 8} y={uc.y - TH / 2 - 8}
                    width={TW + 16} height={TH + 16} rx={20}
                    fill={uc.color} opacity={0.16}
                    style={{ filter: 'blur(10px)' }} />
                )}
                {/* Ambient always */}
                <rect x={uc.x - TW / 2 - 4} y={uc.y - TH / 2 - 4}
                  width={TW + 8} height={TH + 8} rx={18}
                  fill={uc.color} opacity={0.05}
                  style={{ filter: 'blur(6px)' }} />
                {/* Live telemetry ring — the flagship use case has a real-time monitor */}
                {uc.id === 'support_copilot' && live?.ok && live.status !== 'green' && (
                  <>
                    <rect x={uc.x - TW / 2 - 4} y={uc.y - TH / 2 - 4}
                      width={TW + 8} height={TH + 8} rx={17}
                      fill="none" stroke={FL_LIVE_COLORS[live.status] || '#64748b'} strokeWidth={2}
                      style={{ filter: `drop-shadow(0 0 7px ${FL_LIVE_COLORS[live.status]}cc)` }}
                      pointerEvents="none">
                      {live.status === 'red' && (
                        <animate attributeName="opacity" values="1;0.35;1" dur="1.4s" repeatCount="indefinite" />
                      )}
                    </rect>
                    <text x={uc.x} y={uc.y - TH / 2 - 10} textAnchor="middle"
                      fill={FL_LIVE_COLORS[live.status]} fontSize={7.5} fontWeight={800}
                      pointerEvents="none">
                      ● LIVE · {(live.totals?.escalations || 0)} escalated · {(live.totals?.checks_failing || 0)} checks failing
                    </text>
                  </>
                )}
                {/* Tile */}
                <rect x={uc.x - TW / 2} y={uc.y - TH / 2}
                  width={TW} height={TH} rx={14}
                  fill="#0a1628" stroke={uc.color}
                  strokeWidth={isHov ? 2.5 : 1.8}
                  opacity={isHov ? 1 : 0.85}
                  filter={isHov ? 'url(#map-node-glow)' : undefined}
                  style={{ transition: 'all 0.2s' }} />
                {/* Top highlight */}
                <line x1={uc.x - TW / 2 + 14} y1={uc.y - TH / 2 + 1.5}
                  x2={uc.x + TW / 2 - 14} y2={uc.y - TH / 2 + 1.5}
                  stroke={uc.color} strokeWidth={0.7} opacity={0.3} />
                {/* Status badge */}
                <rect x={uc.x + TW / 2 - 34} y={uc.y - TH / 2 - 1}
                  width={36} height={14} rx={5}
                  fill={sc.bg} stroke={sc.border} strokeWidth={0.8} />
                <text x={uc.x + TW / 2 - 16} y={uc.y - TH / 2 + 9}
                  textAnchor="middle" fill={sc.text} fontSize={7} fontWeight={700}>
                  {uc.status}
                </text>
                {/* Label */}
                <text x={uc.x} y={uc.y + 5}
                  textAnchor="middle" fill={isHov ? '#f1f5f9' : '#94a3b8'}
                  fontSize={11} fontWeight={700}
                  style={{ transition: 'fill 0.2s' }}>
                  {uc.label}
                </text>
                {/* Sub-hint on hover */}
                {isHov && (
                  <text x={uc.x} y={uc.y + 18}
                    textAnchor="middle" fill="#475569" fontSize={8}>
                    click to explore →
                  </text>
                )}
              </g>
            )
          })}
        </g>
      </svg>
    </div>
  )
}

// ─── Detail view (agent/system graph for one use-case) ────────────────────────

function DetailView({ detail, nodeQuery, liveNodes }) {
  const [positions, setPositions] = useState([])
  const [hovered, setHovered] = useState(null)
  const [hovEdge, setHovEdge] = useState(null)
  const [tooltip, setTooltip] = useState(null)
  const [xform, setXform] = useState({ x: 0, y: 0, scale: 1 })
  const xformRef = useRef(xform)
  useEffect(() => { xformRef.current = xform }, [xform])
  const [dragging, setDragging] = useState(null)
  const svgRef = useRef(null)
  const dragRef = useRef(null)
  const isPanning = useRef(false)
  const panStart = useRef(null)

  useEffect(() => {
    if (!detail?.nodes) return
    setPositions(runSim(detail.nodes, detail.edges || [], DET_W, DET_H, 180))
  }, [detail])

  const nodeMap = useMemo(() => {
    const m = {}
    positions.forEach((p) => { m[p.id] = p })
    return m
  }, [positions])

  const onWheel = useCallback((e) => {
    e.preventDefault()
    const factor = e.deltaY < 0 ? 1.1 : 0.9
    setXform((t) => {
      const s = Math.max(0.2, Math.min(4, t.scale * factor))
      const rect = svgRef.current?.getBoundingClientRect()
      if (!rect) return { ...t, scale: s }
      const cx = e.clientX - rect.left; const cy = e.clientY - rect.top
      return { scale: s, x: cx - (cx - t.x) * (s / t.scale), y: cy - (cy - t.y) * (s / t.scale) }
    })
  }, [])
  useNonPassiveWheel(svgRef, onWheel)

  const onNodeMouseDown = useCallback((e, nodeId) => {
    e.stopPropagation()
    dragRef.current = { id: nodeId, cx: e.clientX, cy: e.clientY }
    setDragging(nodeId)
    setTooltip(null)
  }, [])

  const onSvgMouseDown = useCallback((e) => {
    if (!dragRef.current) {
      isPanning.current = true
      panStart.current = { x: e.clientX - xformRef.current.x, y: e.clientY - xformRef.current.y }
    }
  }, [])

  const onMouseMove = useCallback((e) => {
    if (dragRef.current) {
      const scale = xformRef.current.scale
      const dx = (e.clientX - dragRef.current.cx) / scale
      const dy = (e.clientY - dragRef.current.cy) / scale
      dragRef.current.cx = e.clientX; dragRef.current.cy = e.clientY
      const id = dragRef.current.id
      setPositions((prev) => prev.map((p) => (p.id === id ? { ...p, x: p.x + dx, y: p.y + dy } : p)))
    } else if (isPanning.current && panStart.current) {
      setXform((t) => ({ ...t, x: e.clientX - panStart.current.x, y: e.clientY - panStart.current.y }))
    }
  }, [])

  const onMouseUp = useCallback(() => {
    dragRef.current = null; isPanning.current = false; setDragging(null)
  }, [])

  if (!positions.length || !detail?.nodes) {
    return (
      <div className="flex items-center justify-center text-zinc-500 text-sm" style={{ height: DET_H }}>
        Computing layout…
      </div>
    )
  }

  return (
    <div className="overflow-hidden relative select-none"
      style={{ height: 'calc(100vh - 165px)', minHeight: DET_H }}
      onMouseMove={onMouseMove} onMouseUp={onMouseUp} onMouseLeave={onMouseUp}>

      {/* Floating tooltip */}
      {tooltip && (
        <div className="pointer-events-none absolute z-20 rounded-xl border border-slate-700/60 bg-slate-900/96 p-3 shadow-2xl max-w-[220px] backdrop-blur-md"
          style={{
            left: tooltip.px > 640 ? tooltip.px - 234 : tooltip.px + 16,
            top: Math.max(tooltip.py - 52, 4),
            boxShadow: `0 0 20px ${tooltip.node.color}22`,
          }}>
          <div className="text-xs font-semibold text-slate-100 mb-1">{tooltip.node.label}</div>
          <div className="text-[11px] leading-relaxed text-slate-400">{tooltip.node.desc}</div>
          <div className="mt-1.5 text-[10px] uppercase tracking-widest font-semibold"
            style={{ color: tooltip.node.color }}>
            {tooltip.node.type}
          </div>
        </div>
      )}

      <svg ref={svgRef} width="100%" height="100%" viewBox={`0 0 ${DET_W} ${DET_H}`}
        overflow="visible" onMouseDown={onSvgMouseDown}
        style={{ cursor: dragging ? 'grabbing' : 'default', background: '#060e1c' }}>
        <MapDefs />
        <CircuitBg width={DET_W} height={DET_H} />
        <g transform={`translate(${xform.x},${xform.y}) scale(${xform.scale})`}>

          {/* Edges (behind nodes) */}
          {(detail.edges || []).map((e, i) => {
            const a = nodeMap[e.source]; const b = nodeMap[e.target]
            if (!a || !b) return null
            const color = EDGE_COLORS[e.type] || '#64748b'
            const dx = b.x - a.x; const dy = b.y - a.y
            const dist = Math.max(Math.hypot(dx, dy), 1)
            const ra = (a.size || 32) + 4; const rb = (b.size || 32) + 6
            const x1 = a.x + (dx / dist) * ra; const y1 = a.y + (dy / dist) * ra
            const x2 = b.x - (dx / dist) * rb; const y2 = b.y - (dy / dist) * rb
            return (
              <GlowEdge key={i}
                x1={x1} y1={y1} x2={x2} y2={y2}
                color={color} type={e.type} label={e.label}
                isHov={hovEdge === i}
                onEnter={() => setHovEdge(i)}
                onLeave={() => setHovEdge(null)}
              />
            )
          })}

          {/* Nodes */}
          {positions.map((node) => {
            const isHov = hovered === node.id
            const isMatch = !nodeQuery || node.label.toLowerCase().includes(nodeQuery.toLowerCase())
            const isDimmed = !!nodeQuery && !isMatch
            return (
              <NodeTile key={node.id} node={node}
                isHov={isHov} highlight={!!nodeQuery && isMatch} isDimmed={isDimmed}
                liveStatus={liveNodes?.[node.id]}
                dragging={dragging === node.id}
                onMouseDown={(e) => onNodeMouseDown(e, node.id)}
                onMouseEnter={(e) => {
                  if (dragRef.current) return
                  setHovered(node.id)
                  const rect = svgRef.current?.getBoundingClientRect()
                  if (rect) setTooltip({ node, px: e.clientX - rect.left, py: e.clientY - rect.top })
                }}
                onMouseLeave={() => { setHovered(null); setTooltip(null) }}
              />
            )
          })}
        </g>
      </svg>
    </div>
  )
}

// ─── 16-Step Implementation Flow ─────────────────────────────────────────────

const FL_W = 1700
const FL_H = 680
// 14 column x-centers: cols 0-6=steps 1-7, col 7=D1, cols 8-9=steps 8-9, col 10=D2, cols 11-13=steps 10-12
const FL_XS = [145, 260, 375, 490, 605, 720, 835, 950, 1065, 1180, 1295, 1410, 1525, 1640]
const FL_SY  = 170  // main step row center y
const FL_CY  = 318  // customer lane center y
const FL_SCY = 436  // scheduler/cadence lane center y
const FL_SFY = 560  // salesforce lane center y
const FL_SBW = 90   // step box width
const FL_SBH = 118  // step box height — room for name + description + performer badge

// Performer types: who executes each step
const FL_PERF = {
  agent: { color: '#6C5CE7', label: 'AI Agent' },
  hitl:  { color: '#f59e0b', label: 'Human Review' },
  auto:  { color: '#10b981', label: 'Automated' },
}

const FL_PHASES = [
  { label: 'ENGAGE', sub: 'Start the conversation', timing: 'Day 0 · ~15 min total', color: '#2d6a4f', x1: 90, x2: 540 },
  { label: 'COLLECT', sub: 'Gather customer inputs', timing: 'Day 0 → Day 21 · ongoing cadence', color: '#1d4d9c', x1: 540, x2: 1000 },
  { label: 'PROCESS', sub: 'Extract, validate & digitize', timing: 'Hours to days · fully automated', color: '#5e2db5', x1: 1000, x2: 1462 },
  { label: 'DELIVER', sub: 'Import to data platform & close', timing: 'Day 21 · ~1 hr automated', color: '#2d6a4f', x1: 1462, x2: FL_W - 6 },
]
const FL_PHASE_COLS = FL_PHASES.map((p) => p.color)

// Each step: col=column index, n=step number, lines=box label, shortDesc=always-visible subtitle,
// fullDesc=tooltip lines (max 3), performer=agent|hitl|auto, timing=when it runs, phase=phase index
const FL_STEPS = [
  { col: 0, n: 1, lines: ['Workflow', 'start'],
    shortDesc: 'SF CDC triggers L2 init',
    fullDesc: ['Salesforce CDC event triggers the workflow.', 'L2 Orchestrator initializes job state in', 'Firestore and assigns a rep + milestone plan.'],
    performer: 'auto', timing: 'T+0 · <1 min', phase: 0 },

  { col: 1, n: 2, lines: ['Client', 'intake'],
    shortDesc: 'Rep registers case in wizard',
    fullDesc: ['Rep fills intake wizard in Control Tower.', 'Records account metadata, contacts,', 'and document type requirements.'],
    performer: 'hitl', timing: 'T+0 · ~10 min', phase: 0 },

  { col: 2, n: 3, lines: ['Case', 'setup'],
    shortDesc: 'Provision questionnaire',
    fullDesc: ['Comms Agent provisions the customer', 'questionnaire for the case and syncs', 'the questionnaire link back to Firestore.'],
    performer: 'agent', timing: 'T+0 · ~2 min', phase: 0 },

  { col: 3, n: 4, lines: ['Email', 'review'],
    shortDesc: 'HITL: approve AI-drafted email',
    fullDesc: ['Rep reviews the welcome email drafted', 'by Comms Agent in Control Tower before', 'sending. Can edit and approve.'],
    performer: 'hitl', timing: 'T+0 · ~2 min', phase: 0 },

  { col: 4, n: 5, lines: ['Welcome', 'email sent'],
    shortDesc: 'Auto-sent via SendGrid',
    fullDesc: ['Welcome email + questionnaire link sent', 'to customer via SendGrid. Delivery', 'activity logged to the Salesforce case.'],
    performer: 'auto', timing: 'T+0 · <1 min', phase: 1 },

  { col: 5, n: 6, lines: ['Reminder', 'emails'],
    shortDesc: 'Cadence: T+3 & T+7 emails',
    fullDesc: ['Scheduler auto-sends follow-up emails', 'at T+3 and T+7 if no customer response.', 'Each send logs a Salesforce activity.'],
    performer: 'auto', timing: 'T+3 & T+7', phase: 1 },

  { col: 6, n: 7, lines: ['Parse', 'inbound', 'emails'],
    shortDesc: 'AI reads & routes replies',
    fullDesc: ['Comms Agent reads replies via MS Graph', 'or SendGrid webhook. Extracts intent,', 'links attachments, updates Firestore.'],
    performer: 'agent', timing: 'Ongoing', phase: 1 },

  { col: 8, n: 8, lines: ['Parse docs &', 'attachments'],
    shortDesc: 'Vision model: PDFs → data',
    fullDesc: ['DocIntel uses a vision model to extract', 'records from uploaded PDFs and forms.', 'Results staged to GCS with PII guard.'],
    performer: 'agent', timing: 'Varies · parallel', phase: 2 },

  { col: 9, n: 9, lines: ['Data', 'completeness'],
    shortDesc: 'Validate fields, request gaps',
    fullDesc: ['Case Assembly checks all required fields.', 'Missing data auto-requested from customer.', 'Gaps logged to Firestore for tracking.'],
    performer: 'agent', timing: '~5 min', phase: 2 },

  { col: 11, n: 10, lines: ['Case', 'digitization'],
    shortDesc: 'Merge records → case snapshot',
    fullDesc: ['Validated records merged into the case', 'file. DLP redacts PII sentinels.', 'Case snapshot written to GCS.'],
    performer: 'agent', timing: '~10 min', phase: 2 },

  { col: 12, n: 11, lines: ['Delivery'],
    shortDesc: 'Import case to data platform',
    fullDesc: ['Delivery Agent imports the case file', 'into the data platform. Success validated;', 'Salesforce case updated.'],
    performer: 'agent', timing: 'T+21 · ~15 min', phase: 3 },

  { col: 13, n: 12, lines: ['Complete'],
    shortDesc: 'Case closed, handoff done',
    fullDesc: ['L2 Orchestrator marks workflow complete.', 'Salesforce case closed. Firestore', 'state archived for audit trail.'],
    performer: 'auto', timing: '<1 min', phase: 3 },
]

const FL_DECISIONS = [
  { col: 7, lines: ['Customer', 'responded?'], yesLabel: 'Attachments received', noLabel: 'No · start cadence timer' },
  { col: 10, lines: ['Data', 'complete?'], yesLabel: 'All fields present', noLabel: 'No · request missing data' },
]

// Customer interactions: what the customer actually does at each touchpoint
const FL_CUSTOMER = [
  { col: 3, line1: 'Receives email', line2: 'Welcome + questionnaire link' },
  { col: 4, line1: 'Completes questionnaire', line2: 'Property data & contacts' },
  { col: 5, line1: 'Replies to follow-ups', line2: 'Updates or questions' },
  { col: 8, line1: 'Uploads documents', line2: 'PDFs, forms, CSVs' },
  { col: 9, line1: 'Provides missing info', line2: 'Answers AI data requests' },
]

// Scheduler gates: triggered on no-response timer expiry
const FL_SCHEDULER = [
  { c1: 7, c2: 9,
    timing: 'T+14',
    title: '2-Week Follow-Up Gate',
    detail: 'No response → send reminder email · log Salesforce 2-wk touchpoint' },
  { c1: 10, c2: 12,
    timing: 'T+7',
    title: '1-Week Follow-Up Gate',
    detail: 'No response → send reminder email · log Salesforce 1-wk touchpoint' },
  { c1: 13, c2: 13,
    timing: 'T-1',
    title: 'Final Reminder Gate',
    detail: 'Day before deadline → send final reminder email to customer' },
]

// Salesforce data sync events that mirror each step
const FL_SALESFORCE = [
  { col: 1, line1: 'SF → Firestore', line2: 'Source of truth sync' },
  { col: 2, line1: 'Case created', line2: 'Record opened' },
  { col: 3, line1: 'Status: In Progress', line2: 'Case field updated' },
  { col: 5, line1: 'Activity logged', line2: 'Email send event' },
  { col: 8, line1: '2-week touchpoint', line2: 'Activity logged' },
  { col: 9, line1: '1-week touchpoint', line2: 'Activity logged' },
  { col: 12, line1: 'Case closure', line2: 'Status: Delivered' },
]

// Sample impact metrics — replace with your own measured numbers.
const FL_METRICS = [
  { value: '~30', unit: 'MIN', label: 'AI pipeline end-to-end', color: '#34d399' },
  { value: '~20', unit: 'HRS', label: 'Manual process (before)', color: '#94a3b8' },
  { value: '97%', unit: '', label: 'Faster processing time', color: '#10b981' },
  { value: '10 / 12', unit: '', label: 'Steps fully automated', color: '#6C5CE7' },
]

const IS_DEC = new Set([7, 10])

const FL_LIVE_COLORS = { green: '#10b981', amber: '#f59e0b', red: '#ef4444', unknown: '#64748b' }

// Poll /api/osca/health so the map actively traces the deployed pipeline.
function useOscaLive(enabled) {
  const [live, setLive] = useState(null)
  useEffect(() => {
    if (!enabled) return undefined
    let alive = true
    let staleTimer = null
    const pull = () => fetch('/api/osca/health')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!alive || !d) return
        setLive(d)
        // cold-start snapshot → live poll is warming; swap it in shortly
        if (d.stale && !staleTimer) staleTimer = setTimeout(() => { staleTimer = null; pull() }, 9000)
      })
      .catch(() => {})
    pull()
    const t = setInterval(pull, 30000)
    return () => { alive = false; clearInterval(t); if (staleTimer) clearTimeout(staleTimer) }
  }, [enabled])
  return live
}

// Which graph node does a k8s workload / milestone belong to?
const WORKLOAD_NODE_PATTERNS = [
  [/^integration-service/, 'integration_service'],
  [/^document-intelligence/, 'doc_intel'],
  [/^communications-agent/, 'comms_agent'],
  [/^case-assembly/, 'case_assembly'],
  [/l2-agent-orchestrator|^l2-orchestrator/, 'l2_orchestrator'],
  [/^delivery/, 'delivery_agent'],
  [/^control-tower/, 'control_tower'],
]
const MILESTONE_NODE = { A: 'comms_agent', B: 'doc_intel', C: 'case_assembly', D: 'delivery_agent' }

function FlowDiagram({ live }) {
  const [hov, setHov] = useState(null)
  const containerRef = useRef(null)
  const dragRef = useRef(null)
  const [panning, setPanning] = useState(false)

  const flowStatus = live?.flow || {}
  const liveColor = FL_LIVE_COLORS[live?.status] || FL_LIVE_COLORS.unknown
  const activity = live?.activity || []
  // data is "flowing" if the newest service event is < 2h old — animates the pipeline arrows
  const lastEventMs = activity[0]?.ts ? Date.parse(activity[0].ts) : null
  const dataFlowing = lastEventMs != null && (Date.now() - lastEventMs) < 2 * 3600 * 1000
  const failingChecks = (live?.checks || []).filter((c) => c.status === 'fail')

  // Pan (drag anywhere) + zoom (trackpad / wheel / +/- keys) via SVG transform.
  const [xform, setXform] = useState({ x: 0, y: 0, scale: 1 })
  const xformRef = useRef(xform)
  useEffect(() => { xformRef.current = xform }, [xform])

  const zoomAt = useCallback((factor, cx, cy) => {
    setXform((t) => {
      const s = Math.max(0.3, Math.min(3, t.scale * factor))
      return { scale: s, x: cx - (cx - t.x) * (s / t.scale), y: cy - (cy - t.y) * (s / t.scale) }
    })
  }, [])
  const zoomCenter = useCallback((factor) => {
    const el = containerRef.current
    zoomAt(factor, (el?.clientWidth || FL_W) / 2, (el?.clientHeight || FL_H) / 2)
  }, [zoomAt])

  // Fit to container: fill the width, center vertically
  const fitView = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    const s = Math.min(el.clientWidth / FL_W, 1.6)
    setXform({ x: 0, y: Math.max(0, (el.clientHeight - FL_H * s) / 2), scale: s })
  }, [])
  useEffect(() => { fitView() }, [fitView])

  // +/- to zoom, 0 to reset (ignored while typing)
  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey) return
      const typing = /input|textarea|select/i.test(document.activeElement?.tagName || '')
      if (typing) return
      if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomCenter(1.2) }
      else if (e.key === '-' || e.key === '_') { e.preventDefault(); zoomCenter(1 / 1.2) }
      else if (e.key === '0') { e.preventDefault(); fitView() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [zoomCenter, fitView])

  const onWheel = useCallback((e) => {
    e.preventDefault()
    const rect = containerRef.current.getBoundingClientRect()
    zoomAt(e.deltaY < 0 ? 1.08 : 0.92, e.clientX - rect.left, e.clientY - rect.top)
  }, [zoomAt])
  useNonPassiveWheel(containerRef, onWheel)
  const onMouseDown = (e) => {
    if (e.button !== 0) return
    dragRef.current = { sx: e.clientX, sy: e.clientY, ox: xformRef.current.x, oy: xformRef.current.y }
    setPanning(true)
  }
  const onMouseMove = (e) => {
    if (!dragRef.current) return
    setXform((t) => ({ ...t,
      x: dragRef.current.ox + (e.clientX - dragRef.current.sx),
      y: dragRef.current.oy + (e.clientY - dragRef.current.sy) }))
  }
  const onMouseUp = () => { dragRef.current = null; setPanning(false) }

  return (
    <div
      ref={containerRef}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={onMouseUp}
      style={{ position: 'relative', overflow: 'hidden', background: '#060e1c',
               height: 'calc(100vh - 165px)', minHeight: 540,
               borderRadius: 8, cursor: panning ? 'grabbing' : 'grab', userSelect: 'none' }}
    >
      {/* Zoom controls */}
      <div style={{ position: 'absolute', right: 10, top: 10, zIndex: 6, display: 'flex', gap: 4 }}>
        {[['−', () => zoomCenter(1 / 1.2)], ['+', () => zoomCenter(1.2)],
          ['⤢', fitView]].map(([sym, fn]) => (
          <button key={sym} onMouseDown={(e) => e.stopPropagation()} onClick={fn}
            title={sym === '⤢' ? 'Reset view (0)' : sym === '+' ? 'Zoom in (+)' : 'Zoom out (−)'}
            style={{ width: 26, height: 26, borderRadius: 7, border: '1px solid #1e3a5f',
                     background: '#0a1628ee', color: '#94a3b8', fontSize: 13, fontWeight: 700,
                     cursor: 'pointer', lineHeight: 1 }}>
            {sym}
          </button>
        ))}
      </div>
      {/* Live status HUD — floats over the diagram */}
      {live && (
        <div style={{ position: 'absolute', left: 10, top: 10, zIndex: 5, width: 'fit-content',
                      maxWidth: 460, pointerEvents: 'none' }}>
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8,
                        padding: '5px 11px', borderRadius: 9, background: '#0a1628ee',
                        border: `1px solid ${liveColor}`, boxShadow: `0 0 14px ${liveColor}44` }}>
            <span style={{ width: 8, height: 8, borderRadius: 99, background: liveColor,
                           animation: live.status === 'red' ? 'flowdash 1.2s ease-in-out infinite' : undefined,
                           boxShadow: `0 0 6px ${liveColor}` }} />
            <span style={{ color: liveColor, fontSize: 11, fontWeight: 800, letterSpacing: '0.04em' }}>
              LIVE · {live.ok === false ? 'telemetry offline' : (live.status || '—').toUpperCase()}
            </span>
            {live.ok !== false && (
              <span style={{ color: '#94a3b8', fontSize: 10 }}>
                {live.totals?.active_clients ?? 0} active · {live.totals?.escalations ?? 0} escalated
                {live.totals?.checks_failing > 0 && ` · ${live.totals.checks_failing} check${live.totals.checks_failing !== 1 ? 's' : ''} failing`}
                {' · '}{live.environment}
              </span>
            )}
          </div>
          {/* Failing health checks — flagged directly on the map */}
          {failingChecks.slice(0, 2).map((c) => (
            <div key={c.name} style={{ marginTop: 4, display: 'flex', alignItems: 'center', gap: 6,
                          padding: '3px 9px', borderRadius: 7, background: '#1a0a0aee',
                          border: '1px solid #7f1d1d', width: 'fit-content', maxWidth: 440 }}>
              <span style={{ width: 5, height: 5, borderRadius: 99, background: '#ef4444',
                             animation: 'flowdash 1.2s ease-in-out infinite' }} />
              <span style={{ color: '#fca5a5', fontSize: 9.5, fontWeight: 700 }}>{c.name}</span>
              <span style={{ color: '#9c5555', fontSize: 9, overflow: 'hidden', textOverflow: 'ellipsis',
                             whiteSpace: 'nowrap' }}>{c.message}</span>
            </div>
          ))}
          {/* Data-flow ticker — what the services just did */}
          {activity.slice(0, 3).map((a, i) => (
            <div key={`${a.ts}-${i}`} style={{ marginTop: i === 0 ? 4 : 2, display: 'flex',
                          alignItems: 'center', gap: 6, padding: '2px 9px', borderRadius: 6,
                          background: '#0a1628cc', border: '1px solid #1e3a5f',
                          width: 'fit-content', maxWidth: 440, opacity: 1 - i * 0.25 }}>
              <span style={{ width: 4, height: 4, borderRadius: 99,
                             background: a.kind === 'email' ? '#10b981' : a.kind === 'agent' ? '#8b5cf6' : '#38bdf8' }} />
              <span style={{ color: '#64748b', fontSize: 8.5, fontFamily: 'monospace' }}>{String(a.entity || '').slice(0, 22)}</span>
              <span style={{ color: '#475569', fontSize: 8.5, overflow: 'hidden', textOverflow: 'ellipsis',
                             whiteSpace: 'nowrap' }}>{a.message}</span>
            </div>
          ))}
        </div>
      )}
      <svg width="100%" height="100%" style={{ display: 'block' }}>
        <defs>
          <style>{`
            @keyframes flowdash { from { stroke-dashoffset: 28; } to { stroke-dashoffset: 0; } }
            .fl-anim { animation: flowdash 1.6s linear infinite; }
          `}</style>
          <marker id="fl-tip" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
            <polygon points="0,0 6,2.5 0,5" fill="#475569" />
          </marker>
          <marker id="fl-cond" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
            <polygon points="0,0 6,2.5 0,5" fill="#f97316" />
          </marker>
          <marker id="fl-sf" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
            <polygon points="0,0 6,2.5 0,5" fill="#3b82f6" />
          </marker>
          <pattern id="fl-circuit" x="0" y="0" width="40" height="40" patternUnits="userSpaceOnUse">
            <line x1="40" y1="0" x2="40" y2="40" stroke="#1e3a5f" strokeWidth="0.4" />
            <line x1="0" y1="40" x2="40" y2="40" stroke="#1e3a5f" strokeWidth="0.4" />
            <circle cx="40" cy="40" r="1.2" fill="#1e3a5f" />
            <circle cx="0" cy="0" r="1.2" fill="#1e3a5f" />
          </pattern>
        </defs>

        {/* Background — fills the viewport regardless of pan/zoom */}
        <rect width="100%" height="100%" fill="#060e1c" />
        <rect width="100%" height="100%" fill="url(#fl-circuit)" opacity={0.9} />

        {/* Pannable/zoomable content */}
        <g transform={`translate(${xform.x},${xform.y}) scale(${xform.scale})`}>

        {/* Phase banners — taller with timing info */}
        {FL_PHASES.map((ph, i) => (
          <g key={i}>
            <rect x={ph.x1 + 2} y={3} width={ph.x2 - ph.x1 - 4} height={56} rx={7}
              fill={ph.color} opacity={0.18} />
            <rect x={ph.x1 + 2} y={3} width={ph.x2 - ph.x1 - 4} height={56} rx={7}
              fill="none" stroke={ph.color} strokeWidth={0.9} opacity={0.5} />
            <text x={(ph.x1 + ph.x2) / 2} y={24} textAnchor="middle"
              fill={ph.color} fontSize={12} fontWeight={800} letterSpacing="0.1em">{ph.label}</text>
            <text x={(ph.x1 + ph.x2) / 2} y={38} textAnchor="middle"
              fill={ph.color} fontSize={8.5} opacity={0.8}>{ph.sub}</text>
            <text x={(ph.x1 + ph.x2) / 2} y={52} textAnchor="middle"
              fill={ph.color} fontSize={7} opacity={0.5}>{ph.timing}</text>
          </g>
        ))}

        {/* Performer legend — top right */}
        {Object.entries(FL_PERF).map(([key, p], i) => (
          <g key={key}>
            <rect x={FL_W - 310 + i * 100} y={6} width={90} height={16} rx={4}
              fill={p.color + '22'} stroke={p.color} strokeWidth={0.7} opacity={0.9} />
            <text x={FL_W - 265 + i * 100} y={17} textAnchor="middle"
              fill={p.color} fontSize={7} fontWeight={700}>{p.label}</text>
          </g>
        ))}

        {/* Lane separator lines */}
        {[FL_SY + FL_SBH / 2 + 12, FL_CY + 38, FL_SCY + 38, FL_SFY + 34].map((y, i) => (
          <line key={i} x1={90} y1={y} x2={FL_W - 6} y2={y}
            stroke="#1e3a5f" strokeWidth={0.5} strokeDasharray="5 5" />
        ))}

        {/* Lane labels — larger and clearer */}
        {[
          { y: FL_SY - 14, lines: ['CONTROL TOWER', 'SUPPORT COPILOT'], color: '#94a3b8' },
          { y: FL_CY - 14, lines: ['CUSTOMER'], color: '#64748b' },
          { y: FL_SCY - 14, lines: ['SCHEDULER /', 'CADENCE ENGINE'], color: '#f97316' },
          { y: FL_SFY - 14, lines: ['SALESFORCE'], color: '#3b82f6' },
        ].map((l, li) =>
          l.lines.map((line, j) => (
            <text key={`${li}-${j}`} x={84} y={l.y + j * 12}
              textAnchor="end" fill={l.color} fontSize={8} fontWeight={700}>{line}</text>
          ))
        )}

        {/* Main flow arrows — animate when live telemetry shows data moving */}
        {[0,1,2,3,4,5,6,7,8,9,10,11,12].map((fromCol) => {
          const toCol = fromCol + 1
          const fx = FL_XS[fromCol] + (IS_DEC.has(fromCol) ? 32 : FL_SBW / 2)
          const tx = FL_XS[toCol] - (IS_DEC.has(toCol) ? 32 : FL_SBW / 2)
          const isAfterDec = IS_DEC.has(fromCol)
          const animated = isAfterDec || dataFlowing
          return (
            <g key={fromCol}>
              <line x1={fx} y1={FL_SY} x2={tx} y2={FL_SY}
                stroke={isAfterDec ? '#10b981' : dataFlowing ? '#3b82f6' : '#334155'}
                strokeWidth={isAfterDec ? 2 : 1.5}
                strokeDasharray={animated ? '6 4' : undefined}
                markerEnd="url(#fl-tip)" className={animated ? 'fl-anim' : ''} />
              {IS_DEC.has(fromCol) && (
                <text x={(fx + tx) / 2} y={FL_SY - 8} textAnchor="middle"
                  fill="#10b981" fontSize={8.5} fontWeight={800}>YES ✓</text>
              )}
            </g>
          )
        })}

        {/* NO arrows from decisions + wait label */}
        {FL_DECISIONS.map((d) => {
          const x = FL_XS[d.col]
          const noY = FL_SY + FL_SBH / 2 + 14
          return (
            <g key={d.col}>
              {/* Vertical dashed line down */}
              <line x1={x} y1={FL_SY + 32} x2={x} y2={FL_SCY - 42}
                stroke="#f97316" strokeWidth={1.5} strokeDasharray="5 3" markerEnd="url(#fl-cond)" />
              {/* NO badge */}
              <rect x={x + 6} y={FL_SY + 36} width={32} height={14} rx={4}
                fill="#1a0800" stroke="#f97316" strokeWidth={0.8} />
              <text x={x + 22} y={FL_SY + 46} textAnchor="middle"
                fill="#f97316" fontSize={7.5} fontWeight={800}>NO</text>
              {/* Await label */}
              <text x={x + 6} y={FL_SY + 68} fill="#f97316" fontSize={6.5} opacity={0.7}>{d.noLabel}</text>
            </g>
          )
        })}

        {/* Scheduler / cadence gates — redesigned with timing badge + title + detail */}
        {FL_SCHEDULER.map((s, i) => {
          const x1 = FL_XS[s.c1] - 46
          const x2 = FL_XS[s.c2] + 46
          const bw = x2 - x1
          const bh = 70
          const by = FL_SCY - bh / 2
          return (
            <g key={i}>
              {/* Box */}
              <rect x={x1} y={by} width={bw} height={bh} rx={8}
                fill="#140800" stroke="#f97316" strokeWidth={1.2} opacity={0.95} />
              {/* Timing badge */}
              <rect x={x1 + 8} y={by + 8} width={32} height={16} rx={4}
                fill="#f97316" opacity={0.95} />
              <text x={x1 + 24} y={by + 20} textAnchor="middle"
                fill="white" fontSize={8} fontWeight={900}>{s.timing}</text>
              {/* Title */}
              <text x={x1 + 48} y={by + 20} textAnchor="start"
                fill="#fb923c" fontSize={8} fontWeight={700}>{s.title}</text>
              {/* Detail line */}
              <text x={(x1 + x2) / 2} y={by + 40} textAnchor="middle"
                fill="#f97316" fontSize={7.5} opacity={0.8}>{s.detail}</text>
              {/* Animated flow line */}
              {bw > 100 && (
                <line x1={x1 + 10} y1={by + 54} x2={x2 - 10} y2={by + 54}
                  stroke="#f97316" strokeWidth={1} strokeDasharray="4 4" markerEnd="url(#fl-cond)"
                  className="fl-anim" opacity={0.6} />
              )}
            </g>
          )
        })}

        {/* Customer interaction boxes — larger with sub-label */}
        {FL_CUSTOMER.map((c) => {
          const x = FL_XS[c.col]
          const bh = 62
          const bw = 100
          return (
            <g key={c.col}>
              <line x1={x} y1={FL_SY + FL_SBH / 2} x2={x} y2={FL_CY - bh / 2}
                stroke="#475569" strokeWidth={1} strokeDasharray="3 3" markerEnd="url(#fl-tip)" />
              <rect x={x - bw / 2} y={FL_CY - bh / 2} width={bw} height={bh} rx={7}
                fill="#0d1b35" stroke="#475569" strokeWidth={1} />
              <text x={x} y={FL_CY - 8} textAnchor="middle"
                fill="#94a3b8" fontSize={8.5} fontWeight={600}>{c.line1}</text>
              <text x={x} y={FL_CY + 8} textAnchor="middle"
                fill="#475569" fontSize={7}>{c.line2}</text>
            </g>
          )
        })}

        {/* Salesforce data sync boxes — larger with sub-label */}
        {FL_SALESFORCE.map((sf) => {
          const x = FL_XS[sf.col]
          const bh = 58
          const bw = 100
          return (
            <g key={sf.col}>
              <line x1={x} y1={FL_SCY + 36} x2={x} y2={FL_SFY - bh / 2}
                stroke="#1e40af" strokeWidth={1} strokeDasharray="3 3" markerEnd="url(#fl-sf)" />
              <rect x={x - bw / 2} y={FL_SFY - bh / 2} width={bw} height={bh} rx={7}
                fill="#0d1b35" stroke="#1e40af" strokeWidth={1} />
              <text x={x} y={FL_SFY - 8} textAnchor="middle"
                fill="#3b82f6" fontSize={8.5} fontWeight={600}>{sf.line1}</text>
              <text x={x} y={FL_SFY + 8} textAnchor="middle"
                fill="#1e40af" fontSize={7}>{sf.line2}</text>
            </g>
          )
        })}

        {/* Step boxes — enlarged with description + performer badge always visible */}
        {FL_STEPS.map((s) => {
          const x = FL_XS[s.col]
          const color = FL_PHASE_COLS[s.phase]
          const perf = FL_PERF[s.performer]
          const isH = hov === s.n
          const lh = 12
          // Live status for this step (from /api/osca/health flow overlay)
          const ls = flowStatus[String(s.n)]
          const lsProblem = ls ? (ls.escalations || 0) + (ls.blocked || 0) : 0
          const lsPaused = ls ? (ls.paused || 0) : 0
          const lsIngress = ls ? (ls.ingress_issues || 0) : 0   // orphans + dead-letters at ingress
          const lsColor = lsProblem > 0 ? '#ef4444' : (lsPaused > 0 || lsIngress > 0) ? '#f59e0b'
            : (ls && ls.count > 0) ? '#38bdf8' : null
          const chipVal = ls ? (ls.count || lsIngress) : 0
          // Center the step name vertically in the upper 2/3 of the box
          const nameAreaCenter = FL_SY - 14
          const ty = nameAreaCenter - ((s.lines.length - 1) * lh) / 2
          const boxTop = FL_SY - FL_SBH / 2 + 14  // top of box body (below badge)
          const boxBot = FL_SY + FL_SBH / 2

          return (
            <g key={s.n} onMouseEnter={() => setHov(s.n)} onMouseLeave={() => setHov(null)}
              style={{ cursor: 'pointer' }}>
              {/* Hover glow aura */}
              {isH && (
                <rect x={x - FL_SBW / 2 - 8} y={boxTop - 4}
                  width={FL_SBW + 16} height={FL_SBH - 6} rx={16}
                  fill={color} opacity={0.2} style={{ filter: 'blur(10px)' }} />
              )}
              {/* Live status ring — clients are currently at this step */}
              {lsColor && (
                <rect x={x - FL_SBW / 2 - 2.5} y={boxTop - 2.5} width={FL_SBW + 5} height={FL_SBH - 9} rx={12}
                  fill="none" stroke={lsColor} strokeWidth={2}
                  style={{ filter: `drop-shadow(0 0 6px ${lsColor}cc)` }}>
                  {lsProblem > 0 && (
                    <animate attributeName="opacity" values="1;0.35;1" dur="1.4s" repeatCount="indefinite" />
                  )}
                </rect>
              )}
              {/* Box body */}
              <rect x={x - FL_SBW / 2} y={boxTop} width={FL_SBW} height={FL_SBH - 14} rx={10}
                fill="#0a1628" stroke={color} strokeWidth={isH ? 2.5 : 1.5}
                style={{ transition: 'all 0.15s', filter: isH ? `drop-shadow(0 0 10px ${color}88)` : undefined }} />
              {/* Live count chip — clients at this step, or ingress issues on step 1 */}
              {chipVal > 0 && (
                <g>
                  <rect x={x + FL_SBW / 2 - 22} y={boxTop - 9} width={30} height={15} rx={7}
                    fill={lsColor || '#38bdf8'} />
                  <text x={x + FL_SBW / 2 - 7} y={boxTop + 1.5} textAnchor="middle"
                    fill="#04101f" fontSize={8} fontWeight={900}>
                    {chipVal > 99 ? '99+' : chipVal}
                  </text>
                </g>
              )}
              {/* Ingress issue label under step 1 */}
              {lsIngress > 0 && !ls?.count && (
                <text x={x} y={boxBot + 22} textAnchor="middle"
                  fill="#f59e0b" fontSize={6.5} fontWeight={700}>
                  ⚠ {lsIngress} ingress issue{lsIngress !== 1 ? 's' : ''}
                </text>
              )}
              {/* Number badge */}
              <circle cx={x} cy={boxTop} r={13} fill={color}
                style={{ filter: isH ? `drop-shadow(0 0 6px ${color})` : undefined }} />
              <text x={x} y={boxTop + 5} textAnchor="middle"
                fill="white" fontSize={10} fontWeight={900}>{s.n}</text>
              {/* Step name */}
              {s.lines.map((line, j) => (
                <text key={j} x={x} y={ty + j * lh} textAnchor="middle"
                  fill={isH ? '#f1f5f9' : '#cbd5e1'} fontSize={8.5} fontWeight={600}>{line}</text>
              ))}
              {/* Short description — always visible */}
              <text x={x} y={FL_SY + 8} textAnchor="middle"
                fill={isH ? '#94a3b8' : '#475569'} fontSize={6.5} fontStyle="italic">
                {s.shortDesc}
              </text>
              {/* Performer badge — colored pill at bottom of box */}
              <rect x={x - 30} y={boxBot - 22} width={60} height={13} rx={4}
                fill={perf.color + '22'} stroke={perf.color} strokeWidth={0.7} />
              <text x={x} y={boxBot - 12} textAnchor="middle"
                fill={perf.color} fontSize={6.5} fontWeight={700}>{perf.label}</text>
              {/* Timing — below box */}
              <text x={x} y={boxBot + 12} textAnchor="middle"
                fill="#334155" fontSize={6.5}>{s.timing}</text>

              {/* Hover tooltip — rich panel above the step */}
              {isH && (
                <g>
                  {/* Tooltip positioned above: left-anchored for rightmost steps */}
                  {(() => {
                    const ttW = 230; const ttH = 82
                    const ttX = x > 1450 ? x - ttW - 4 : x - ttW / 2
                    const ttY = boxTop - ttH - 12
                    return (
                      <g>
                        <rect x={ttX} y={ttY} width={ttW} height={ttH} rx={8}
                          fill="#0d1b35" stroke={color} strokeWidth={1} opacity={0.98}
                          style={{ filter: `drop-shadow(0 4px 16px ${color}44)` }} />
                        {/* Header */}
                        <text x={ttX + 10} y={ttY + 17}
                          fill="#f1f5f9" fontSize={9} fontWeight={800}>
                          {`Step ${s.n}: ${s.lines.join(' ')}`}
                        </text>
                        <rect x={ttX + 8} y={ttY + 22} width={ttW - 16} height={0.6}
                          fill={color} opacity={0.4} />
                        {/* Description lines */}
                        {s.fullDesc.map((line, j) => (
                          <text key={j} x={ttX + 10} y={ttY + 36 + j * 12}
                            fill="#94a3b8" fontSize={7.5}>{line}</text>
                        ))}
                        {/* Footer: performer + timing */}
                        <rect x={ttX + 8} y={ttY + ttH - 20} width={ttW - 16} height={0.6}
                          fill={color} opacity={0.25} />
                        <text x={ttX + 10} y={ttY + ttH - 6}
                          fill={perf.color} fontSize={7} fontWeight={700}>{perf.label}</text>
                        <text x={ttX + ttW - 10} y={ttY + ttH - 6} textAnchor="end"
                          fill="#475569" fontSize={7}>{s.timing}</text>
                      </g>
                    )
                  })()}
                </g>
              )}
            </g>
          )
        })}

        {/* Decision diamonds — larger with YES/NO context */}
        {FL_DECISIONS.map((d) => {
          const x = FL_XS[d.col]
          const sz = 34
          const pts = `${x},${FL_SY - sz} ${x + sz},${FL_SY} ${x},${FL_SY + sz} ${x - sz},${FL_SY}`
          return (
            <g key={d.col}>
              {/* Glow */}
              <polygon points={pts} fill="none" stroke="#FDCB6E" strokeWidth={6} opacity={0.08}
                style={{ filter: 'blur(4px)' }} />
              <polygon points={pts} fill="#0d1b35" stroke="#FDCB6E" strokeWidth={2}
                style={{ filter: 'drop-shadow(0 0 6px #FDCB6E55)' }} />
              {d.lines.map((line, j) => (
                <text key={j} x={x} y={FL_SY + (j - d.lines.length / 2 + 0.5) * 11 + 2}
                  textAnchor="middle" fill="#FDCB6E" fontSize={8.5} fontWeight={800}>{line}</text>
              ))}
            </g>
          )
        })}

        {/* Metrics bar */}
        <rect x={90} y={FL_H - 56} width={FL_W - 98} height={50} rx={8}
          fill="#0d1b35" stroke="#1e3a5f" strokeWidth={1} />
        <line x1={90} y1={FL_H - 56} x2={FL_W - 8} y2={FL_H - 56}
          stroke="#10b981" strokeWidth={1.5} opacity={0.3} />
        <text x={112} y={FL_H - 34} fill="#e2e8f0" fontSize={9} fontWeight={700}>
          END-TO-END IMPACT — Automated. Intelligent. Built for speed.
        </text>
        <text x={112} y={FL_H - 18} fill="#475569" fontSize={7.5}>
          Support Copilot · 12-step pipeline · 4 AI nodes
        </text>
        {FL_METRICS.map((m, i) => {
          const mx = 480 + i * 300
          return (
            <g key={i}>
              <text x={mx} y={FL_H - 26} fill={m.color} fontSize={20} fontWeight={900}
                fontFamily="'SF Mono','Fira Mono',monospace">{m.value}</text>
              {m.unit && (
                <text x={mx + (m.value.length * 11.5) + 2} y={FL_H - 26} fill={m.color} fontSize={9} fontWeight={700}>{m.unit}</text>
              )}
              <text x={mx} y={FL_H - 12} fill="#475569" fontSize={7.5}>{m.label}</text>
            </g>
          )
        })}
        </g>
      </svg>
    </div>
  )
}

// ─── Edge legend ──────────────────────────────────────────────────────────────

function EdgeLegend() {
  return (
    <div className="flex items-center gap-2.5 flex-wrap">
      {Object.entries(EDGE_COLORS).map(([type, color]) => (
        <div key={type} className="flex items-center gap-1.5">
          <svg width="20" height="8" style={{ overflow: 'visible' }}>
            <line x1="0" y1="4" x2="14" y2="4" stroke={color} strokeWidth="2"
              style={{ filter: `drop-shadow(0 0 3px ${color})` }} />
            <polygon points="14,1.5 20,4 14,6.5" fill={color} />
          </svg>
          <span className="text-[10px] text-slate-500 capitalize">{type}</span>
        </div>
      ))}
    </div>
  )
}

// ─── Main panel ───────────────────────────────────────────────────────────────

export default function ArchMapPanel() {
  const [usecases, setUsecases] = useState(USECASES)
  const [details, setDetails] = useState(DETAILS_FALLBACK)
  const [selected, setSelected] = useState(null)
  const [nodeQuery, setNodeQuery] = useState('')
  const [view, setView] = useState('graph')  // 'graph' | 'flow'
  const live = useOscaLive(true)  // server-side cached; galaxy + graph + flow all react

  // Health-check + milestone state mapped onto graph node ids (fail > warn)
  const liveNodes = useMemo(() => {
    if (!live?.ok || selected !== 'support_copilot') return null
    const m = {}
    for (const c of live.checks || []) {
      if (c.status === 'ok') continue
      for (const [re, nodeId] of WORKLOAD_NODE_PATTERNS) {
        if (re.test(c.name)) {
          if (!m[nodeId] || (c.status === 'fail' && m[nodeId].status !== 'fail'))
            m[nodeId] = { status: c.status, label: (c.message || '').split('·')[0].trim() }
          break
        }
      }
    }
    for (const [ms, info] of Object.entries(live.milestones || {})) {
      const nodeId = MILESTONE_NODE[ms]
      const stuck = (info.escalations || 0) + (info.blocked || 0)
      if (nodeId && stuck > 0 && m[nodeId]?.status !== 'fail')
        m[nodeId] = { status: 'fail', label: `${stuck} client${stuck !== 1 ? 's' : ''} stuck` }
    }
    return m
  }, [live, selected])

  // Reset view and query when selection changes (the flagship use case opens on Flow)
  useEffect(() => {
    setView(selected === 'support_copilot' ? 'flow' : 'graph')
    setNodeQuery('')
  }, [selected])

  useEffect(() => {
    fetch('/api/arch-map')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return
        if (Array.isArray(data.usecases) && data.usecases.length) setUsecases(data.usecases)
        if (data.details && typeof data.details === 'object')
          setDetails((prev) => ({ ...prev, ...data.details }))
      })
      .catch(() => {})
  }, [])

  const selectedUc = usecases.find((u) => u.id === selected)
  const detail = selected ? details[selected] : null
  const showFlow = view === 'flow' && selected === 'support_copilot'
  const showMission = view === 'mission' && selected === 'support_copilot'

  const alertCount = live?.ok ? (live.totals?.escalations || 0) : 0
  const viewToggle = selected === 'support_copilot' && (
    <div className="flex overflow-hidden rounded-lg border border-slate-700 text-[11px]">
      <button
        onClick={() => setView('flow')}
        className={`px-2.5 py-1 transition-colors ${view === 'flow' ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}
      >
        Flow
      </button>
      <button
        onClick={() => setView('graph')}
        className={`px-2.5 py-1 transition-colors ${view === 'graph' ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}
      >
        Graph
      </button>
      <button
        onClick={() => setView('mission')}
        className={`flex items-center gap-1 px-2.5 py-1 transition-colors ${view === 'mission' ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}
      >
        Mission Control
        {alertCount > 0 && view !== 'mission' && (
          <span className="rounded-full bg-red-900 px-1 text-[9px] font-bold tabular-nums text-red-300">{alertCount}</span>
        )}
      </button>
    </div>
  )

  const galaxyBtn = (
    <button onClick={() => setSelected(null)}
      className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium text-slate-300 hover:text-slate-100 hover:bg-slate-800 transition-colors">
      <ChevronLeft size={12} /> Galaxy
    </button>
  )

  // Mission Control is its own full surface (own header) — render it directly.
  if (showMission) {
    return (
      <OscaMonitorPanel actions={
        <div className="flex items-center gap-2">{viewToggle}{galaxyBtn}</div>
      } />
    )
  }

  const panelActions = (
    <div className="flex items-center gap-3">
      {viewToggle}
      {selected && detail && !showFlow && (
        <div className="flex items-center gap-2 text-[10px] text-slate-600">
          <span>{(detail.nodes || []).length} nodes</span>
          <span>·</span>
          <span>{(detail.edges || []).length} edges</span>
          {['agent','external','service','ui','infra'].map((t) => {
            const cnt = (detail.nodes || []).filter((n) => n.type === t).length
            if (!cnt) return null
            return <span key={t} className="text-slate-700">{cnt} {t}</span>
          })}
        </div>
      )}
      {selected && !showFlow && (
        <div className="relative">
          <Search size={11} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-slate-500" />
          <input value={nodeQuery} onChange={(e) => setNodeQuery(e.target.value)}
            placeholder="find node…"
            className="h-6 w-28 rounded bg-slate-800 pl-6 pr-2 text-[11px] text-slate-300 placeholder:text-slate-600 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]" />
        </div>
      )}
      {selected && !showFlow && <EdgeLegend />}
      {selected ? galaxyBtn : (
        <span className="text-[10px] text-slate-600 italic">click a use-case to explore</span>
      )}
    </div>
  )

  return (
    <Panel title={selected ? (selectedUc?.label ?? selected) : 'Architecture Map'} actions={panelActions}>
      {selected ? (
        showFlow ? (
          <FlowDiagram live={live} />
        ) : detail ? (
          <DetailView detail={detail} nodeQuery={nodeQuery} liveNodes={liveNodes} />
        ) : (
          <div className="flex items-center justify-center text-slate-500 text-sm" style={{ minHeight: DET_H }}>
            No detail data for this use case yet.
          </div>
        )
      ) : (
        <GalaxyView usecases={usecases} onSelect={setSelected} live={live} />
      )}
    </Panel>
  )
}
