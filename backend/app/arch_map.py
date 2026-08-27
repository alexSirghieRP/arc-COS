"""Agentic architecture map for the Chief-of-Staff board.

Returns a static-but-rich description of an agentic system landscape,
suitable for a galaxy-view (use cases as planets) and per-use-case drill-down
(node/edge graph).

NOTE: everything below is a compact, fully fictional SAMPLE catalog so the
feature renders and demos out of the box. Real content is expected to be
maintained here (or served from your own source) for your organization's
actual systems — replace the sample use cases and graphs with your own.
"""

from __future__ import annotations


def get_map() -> dict:
    """Return the full architecture map.

    Top-level structure::

        {
            "usecases": [...],   # galaxy-view summaries
            "details": {
                "support_copilot": {"nodes": [...], "edges": [...], ...},
                ...
            },
        }
    """
    return {
        "usecases": _USECASES,
        "details": {
            "support_copilot": _SUPPORT_COPILOT,
            "docs_pipeline": _DOCS_PIPELINE,
            "billing_recon": _BILLING_RECON,
            "chief_of_staff": _CHIEF_OF_STAFF,
        },
    }


# ---------------------------------------------------------------------------
# Galaxy-view use-case summaries (sample data)
# ---------------------------------------------------------------------------

_USECASES: list[dict] = [
    {
        "id": "support_copilot",
        "label": "Support Copilot",
        "description": (
            "End-to-end pipeline that ingests Salesforce support cases, "
            "provisions customer questionnaires, extracts uploaded documents "
            "via a vision model, assembles a case file, and imports it into "
            "the data platform. Human-in-the-loop at email review and "
            "delivery sign-off stages."
        ),
        "status": "UAT",
        "color": "#6C5CE7",
        "node_count": 15,
        "edge_count": 27,
        "repo": "acme-org/support-copilot",
    },
    {
        "id": "docs_pipeline",
        "label": "Docs Pipeline",
        "description": (
            "SendGrid inbound-parse webhook feeds into the Support Copilot "
            "Communications Agent. Handles attachment extraction, document "
            "routing, and rep notification when new files arrive."
        ),
        "status": "active",
        "color": "#00B894",
        "node_count": 5,
        "edge_count": 5,
        "repo": "acme-org/docs-pipeline",
    },
    {
        "id": "billing_recon",
        "label": "Billing Reconciler",
        "description": (
            "Detects missing AP bills by reconciling expected vendor "
            "invoices against posted transactions. Early stage; batch sweep "
            "approach with HITL exception triage."
        ),
        "status": "early",
        "color": "#FDCB6E",
        "node_count": 4,
        "edge_count": 3,
        "repo": "acme-org/billing-reconciler",
    },
    {
        "id": "chief_of_staff",
        "label": "Chief of Staff",
        "description": (
            "Meta-layer orchestrator (this app). Monitors Teams, email, "
            "GitHub PRs, ADO work items, and Confluence. Drafts replies, "
            "tracks open loops, surfaces daily briefs. Runs on FastAPI + "
            "LangGraph + MS Graph MCP."
        ),
        "status": "active",
        "color": "#FD79A8",
        "node_count": 10,
        "edge_count": 13,
        "repo": "chief-of-staff",
    },
]


# ---------------------------------------------------------------------------
# Support Copilot — detailed node/edge graph (sample data)
# ---------------------------------------------------------------------------

_SUPPORT_COPILOT_NODES: list[dict] = [
    {
        "id": "salesforce",
        "type": "external",
        "label": "Salesforce",
        "role": "Support-case source + status sync",
        "color": "#00A1E0",
    },
    {
        "id": "l2_orchestrator",
        "type": "agent",
        "label": "L2 Orchestrator",
        "role": (
            "Hub-and-spoke supervisor. Receives ingress, dispatches L1 work "
            "orders, advances milestones via callbacks."
        ),
        "color": "#6C5CE7",
    },
    {
        "id": "comms_agent",
        "type": "agent",
        "label": "Communications Agent",
        "role": (
            "Node A: questionnaire provision + HITL email drafting "
            "(LangGraph). Sends welcome email via SendGrid."
        ),
        "color": "#00B894",
    },
    {
        "id": "doc_intel",
        "type": "agent",
        "label": "Document Intelligence",
        "role": (
            "Node B: vision extraction of PDFs/CSVs (uploaded reports and "
            "forms) via a vision model on Vertex AI."
        ),
        "color": "#FDCB6E",
    },
    {
        "id": "case_assembly",
        "type": "agent",
        "label": "Case Assembly Agent",
        "role": (
            "Node C: merges extracted records into a case file, validates, "
            "detects overflow. PII stays in GCS."
        ),
        "color": "#E17055",
    },
    {
        "id": "delivery_agent",
        "type": "agent",
        "label": "Delivery Agent",
        "role": (
            "Node D: imports the case file into the data platform. Syncs "
            "status back to Salesforce."
        ),
        "color": "#74B9FF",
    },
    {
        "id": "integration_service",
        "type": "service",
        "label": "Integration Service",
        "role": (
            "External API gateway for all outbound calls: Salesforce, SendGrid, "
            "data platform, Firestore, GCS, Outlook."
        ),
        "color": "#A29BFE",
    },
    {
        "id": "control_tower",
        "type": "ui",
        "label": "Control Tower",
        "role": (
            "Next.js HITL dashboard: intake wizard, HITL email review, "
            "document review queue, escalation queue."
        ),
        "color": "#FD79A8",
    },
    {
        "id": "eval_reviewer",
        "type": "ui",
        "label": "Eval Reviewer",
        "role": (
            "Internal review tool for validating document extraction eval "
            "runs and mapping rules."
        ),
        "color": "#636E72",
    },
    {
        "id": "data_platform",
        "type": "external",
        "label": "Data Platform",
        "role": "Case-file import target and downstream system of record.",
        "color": "#00CEC9",
    },
    {
        "id": "firestore",
        "type": "infra",
        "label": "Firestore",
        "role": (
            "Primary state store: job configs, document tasks, communication "
            "snapshots, idempotency log, agentic state."
        ),
        "color": "#FF7675",
    },
    {
        "id": "gcs",
        "type": "infra",
        "label": "Cloud Storage (GCS)",
        "role": (
            "PII-bearing artifact staging: extracted records, case snapshots, "
            "sidecars (never in Firestore)."
        ),
        "color": "#55EFC4",
    },
    {
        "id": "vertex_ai",
        "type": "infra",
        "label": "Vertex AI",
        "role": (
            "Vision model for document extraction. Fast model for "
            "special-instructions parsing."
        ),
        "color": "#FDCB6E",
    },
    {
        "id": "dlp",
        "type": "infra",
        "label": "Cloud DLP",
        "role": (
            "PII redaction before Firestore writes. Egress guard blocks "
            "redacted sentinels from leaving the pipeline."
        ),
        "color": "#B2BEC3",
    },
    {
        "id": "sendgrid",
        "type": "external",
        "label": "SendGrid",
        "role": (
            "Transactional email delivery (welcome emails, attachments up to "
            "25 MB). Inbound parse webhook."
        ),
        "color": "#1A73E8",
    },
]

_SUPPORT_COPILOT_EDGES: list[dict] = [
    {
        "source": "salesforce",
        "target": "integration_service",
        "label": "CDC stream / case events",
        "type": "data",
    },
    {
        "source": "integration_service",
        "target": "l2_orchestrator",
        "label": "ingress/crm/cases",
        "type": "call",
    },
    {
        "source": "l2_orchestrator",
        "target": "comms_agent",
        "label": "Node A work order",
        "type": "dispatch",
    },
    {
        "source": "comms_agent",
        "target": "integration_service",
        "label": "questionnaire provision + SendGrid email",
        "type": "call",
    },
    {
        "source": "integration_service",
        "target": "sendgrid",
        "label": "send email",
        "type": "call",
    },
    {
        "source": "integration_service",
        "target": "data_platform",
        "label": "questionnaire provision",
        "type": "call",
    },
    {
        "source": "comms_agent",
        "target": "l2_orchestrator",
        "label": "L1 result callback",
        "type": "callback",
    },
    {
        "source": "l2_orchestrator",
        "target": "doc_intel",
        "label": "Node B work order",
        "type": "dispatch",
    },
    {
        "source": "doc_intel",
        "target": "vertex_ai",
        "label": "vision extraction",
        "type": "call",
    },
    {
        "source": "doc_intel",
        "target": "gcs",
        "label": "source docs read, artifacts write",
        "type": "data",
    },
    {
        "source": "doc_intel",
        "target": "firestore",
        "label": "document task write",
        "type": "data",
    },
    {
        "source": "doc_intel",
        "target": "l2_orchestrator",
        "label": "L1 result callback",
        "type": "callback",
    },
    {
        "source": "l2_orchestrator",
        "target": "case_assembly",
        "label": "Node C work order",
        "type": "dispatch",
    },
    {
        "source": "case_assembly",
        "target": "gcs",
        "label": "records, case snapshot, sidecars write",
        "type": "data",
    },
    {
        "source": "case_assembly",
        "target": "l2_orchestrator",
        "label": "L1 result callback",
        "type": "callback",
    },
    {
        "source": "l2_orchestrator",
        "target": "delivery_agent",
        "label": "Node D work order",
        "type": "dispatch",
    },
    {
        "source": "delivery_agent",
        "target": "gcs",
        "label": "read case snapshot + records",
        "type": "data",
    },
    {
        "source": "delivery_agent",
        "target": "integration_service",
        "label": "case-file import + Salesforce status",
        "type": "call",
    },
    {
        "source": "integration_service",
        "target": "data_platform",
        "label": "import case file",
        "type": "call",
    },
    {
        "source": "integration_service",
        "target": "salesforce",
        "label": "status sync",
        "type": "call",
    },
    {
        "source": "l2_orchestrator",
        "target": "firestore",
        "label": "state + audit snapshots",
        "type": "data",
    },
    {
        "source": "l2_orchestrator",
        "target": "dlp",
        "label": "PII redaction gate",
        "type": "call",
    },
    {
        "source": "control_tower",
        "target": "comms_agent",
        "label": "HITL email approve/reject",
        "type": "ui",
    },
    {
        "source": "control_tower",
        "target": "l2_orchestrator",
        "label": "escalation review, delivery triggers",
        "type": "ui",
    },
    {
        "source": "eval_reviewer",
        "target": "doc_intel",
        "label": "eval run parse",
        "type": "ui",
    },
    {
        "source": "eval_reviewer",
        "target": "l2_orchestrator",
        "label": "mapping registry",
        "type": "ui",
    },
    {
        "source": "integration_service",
        "target": "firestore",
        "label": "communication snapshots",
        "type": "data",
    },
]

_SUPPORT_COPILOT: dict = {
    "nodes": _SUPPORT_COPILOT_NODES,
    "edges": _SUPPORT_COPILOT_EDGES,
    "description": (
        "End-to-end Support Copilot pipeline. Salesforce case events trigger the "
        "L2 Orchestrator, which dispatches four sequential L1 agents (Communications, "
        "Document Intelligence, Case Assembly, Delivery). Human review is required at "
        "the email-drafting stage (Control Tower) and at escalation/delivery sign-off. "
        "PII never leaves GCS or traverses Firestore without DLP redaction."
    ),
    "status": "UAT",
}


# ---------------------------------------------------------------------------
# Stub details for the other use cases (sample data —
# enough to render a galaxy tooltip; drill-down can be expanded later)
# ---------------------------------------------------------------------------

_BILLING_RECON: dict = {
    "nodes": [
        {
            "id": "ap_system",
            "type": "external",
            "label": "AP System (ERP)",
            "role": "Source of truth for expected vendor invoices.",
            "color": "#FDCB6E",
        },
        {
            "id": "br_sweep_agent",
            "type": "agent",
            "label": "Billing Recon Sweep",
            "role": "Reconciles expected invoices against posted transactions; flags gaps.",
            "color": "#6C5CE7",
        },
        {
            "id": "br_triage_ui",
            "type": "ui",
            "label": "Exception Triage UI",
            "role": "HITL dashboard for reviewing and resolving flagged missing bills.",
            "color": "#FD79A8",
        },
        {
            "id": "br_firestore",
            "type": "infra",
            "label": "Firestore",
            "role": "State store for sweep runs and exception records.",
            "color": "#FF7675",
        },
    ],
    "edges": [
        {"source": "ap_system", "target": "br_sweep_agent", "label": "AP data pull", "type": "data"},
        {"source": "br_sweep_agent", "target": "br_firestore", "label": "exception write", "type": "data"},
        {"source": "br_firestore", "target": "br_triage_ui", "label": "exception read", "type": "data"},
    ],
    "description": (
        "Batch sweep agent that compares expected vendor bills in the AP system "
        "against posted transactions. Surfaces gaps to a HITL triage UI. Early-stage; "
        "reconciliation logic under active development."
    ),
    "status": "early",
}

_DOCS_PIPELINE: dict = {
    "nodes": [
        {
            "id": "sendgrid_inbound",
            "type": "external",
            "label": "SendGrid Inbound Parse",
            "role": "Webhook that forwards inbound emails to the pipeline.",
            "color": "#1A73E8",
        },
        {
            "id": "email_router",
            "type": "agent",
            "label": "Email Router",
            "role": "Classifies inbound emails, extracts attachments, routes to the correct agent.",
            "color": "#00B894",
        },
        {
            "id": "comms_agent_ref",
            "type": "agent",
            "label": "Communications Agent",
            "role": "Receives routed docs; triggers document intelligence if attachments present.",
            "color": "#00B894",
        },
        {
            "id": "dp_gcs",
            "type": "infra",
            "label": "Cloud Storage (GCS)",
            "role": "Attachment staging before document extraction.",
            "color": "#55EFC4",
        },
        {
            "id": "dp_firestore",
            "type": "infra",
            "label": "Firestore",
            "role": "Inbound email event log and routing state.",
            "color": "#FF7675",
        },
    ],
    "edges": [
        {"source": "sendgrid_inbound", "target": "email_router", "label": "POST webhook", "type": "call"},
        {"source": "email_router", "target": "dp_gcs", "label": "attachment staging", "type": "data"},
        {"source": "email_router", "target": "comms_agent_ref", "label": "routed email event", "type": "dispatch"},
        {"source": "email_router", "target": "dp_firestore", "label": "event log write", "type": "data"},
        {"source": "comms_agent_ref", "target": "dp_gcs", "label": "attachment read", "type": "data"},
    ],
    "description": (
        "SendGrid inbound-parse webhook feeds raw emails into the Email Router, "
        "which classifies them, stages attachments to GCS, and dispatches events "
        "to the Communications Agent. Part of the Support Copilot inbound surface."
    ),
    "status": "active",
}

_CHIEF_OF_STAFF: dict = {
    "nodes": [
        {
            "id": "cos_backend",
            "type": "service",
            "label": "CoS FastAPI Backend",
            "role": "Orchestrates sweeps, drafts, triage, and all agentic loops.",
            "color": "#FD79A8",
        },
        {
            "id": "cos_frontend",
            "type": "ui",
            "label": "CoS React Dashboard",
            "role": "Board UI: pings, approvals, open loops, roadmap, cost, health.",
            "color": "#74B9FF",
        },
        {
            "id": "ms_graph",
            "type": "external",
            "label": "MS Graph (Teams + Email)",
            "role": "Source of Teams mentions, chat messages, and Outlook emails.",
            "color": "#00A1E0",
        },
        {
            "id": "github",
            "type": "external",
            "label": "GitHub",
            "role": "PR review sweep: new PRs, review requests, CI status.",
            "color": "#636E72",
        },
        {
            "id": "ado",
            "type": "external",
            "label": "Azure DevOps",
            "role": "Work item assignment and state tracking.",
            "color": "#0078D4",
        },
        {
            "id": "confluence",
            "type": "external",
            "label": "Confluence",
            "role": "Knowledge sync and weekly status publication target.",
            "color": "#0052CC",
        },
        {
            "id": "cos_db",
            "type": "infra",
            "label": "SQLite (local)",
            "role": "Items, drafts, audit log, settings, job runs — all local.",
            "color": "#B2BEC3",
        },
        {
            "id": "claude_api",
            "type": "external",
            "label": "Claude API (Anthropic)",
            "role": "LLM backbone for triage, draft generation, summarization, PR review.",
            "color": "#A29BFE",
        },
        {
            "id": "obsidian_vault",
            "type": "infra",
            "label": "Obsidian Vault",
            "role": "Second-brain knowledge base. Read: stats, graph, search. Write: quick-capture, board snapshot, meeting notes.",
            "color": "#6C5CE7",
            "x": 600, "y": 420,
        },
        {
            "id": "apscheduler",
            "type": "service",
            "label": "APScheduler",
            "role": "Cron-style job runner: 14 scheduled sweeps (Teams 5min, email 10min, PRs 30min, daily briefs, etc.).",
            "color": "#55EFC4",
            "x": 240, "y": 420,
        },
    ],
    "edges": [
        {"source": "ms_graph", "target": "cos_backend", "label": "Teams sweep / email sweep", "type": "data"},
        {"source": "github", "target": "cos_backend", "label": "PR review sweep", "type": "data"},
        {"source": "ado", "target": "cos_backend", "label": "work item poll", "type": "data"},
        {"source": "confluence", "target": "cos_backend", "label": "knowledge sync", "type": "data"},
        {"source": "cos_backend", "target": "claude_api", "label": "triage + draft LLM calls", "type": "call"},
        {"source": "cos_backend", "target": "cos_db", "label": "items / drafts / audit", "type": "data"},
        {"source": "cos_backend", "target": "ms_graph", "label": "send Teams/email", "type": "call"},
        {"source": "cos_backend", "target": "confluence", "label": "publish weekly status", "type": "call"},
        {"source": "cos_frontend", "target": "cos_backend", "label": "REST API + SSE", "type": "call"},
        {"source": "cos_backend", "target": "github", "label": "post PR comments", "type": "call"},
        {"source": "cos_backend", "target": "obsidian_vault", "label": "read stats/notes/graph", "type": "data"},
        {"source": "cos_frontend", "target": "obsidian_vault", "label": "quick-capture / meeting notes", "type": "call"},
        {"source": "apscheduler", "target": "cos_backend", "label": "trigger 14 scheduled sweeps", "type": "dispatch"},
    ],
    "description": (
        "Chief of Staff meta-layer (this app). Monitors Teams, email, GitHub PRs, "
        "ADO work items, and Confluence. Drafts replies via Claude API, tracks open "
        "loops, surfaces daily briefs, and publishes weekly status reports. "
        "Obsidian vault integration: read/write notes, second-brain graph, board snapshots. "
        "All state is local SQLite; 14 scheduled sweeps via APScheduler. "
        "No cloud infra required."
    ),
    "status": "active",
}
