# arc-CoS — your personal Chief of Staff agent

arc-CoS is a self-hosted "chief of staff" that watches your work surfaces (Microsoft
Teams, Outlook, calendar, GitHub, Azure DevOps, Confluence, and an Obsidian vault),
triages what comes in, drafts replies for your approval, reviews PRs, keeps a daily
note, and produces weekly status reports and roadmaps — all behind a local control
tower at `http://localhost:7777`.

It is **read-by-default and guardrailed-by-design**: every outbound action goes
through a single code path with a kill switch, a dry-run default, an allowlist, and a
per-conversation rate limit. The LLM agents only ever *read and think*; the code decides
what (if anything) is sent.

> Built on the **Claude Agent SDK** for classification/drafting/synthesis and long-lived
> **MCP** sessions for deterministic reads/sends. Nothing here phones home; your tokens
> stay in your Claude Code settings file and never enter the repo.

---

## What it does

- **Triage** — classifies incoming Teams/Outlook messages into tiers (A trivial / B
  substantive / C VIP-or-sensitive) and acts per policy: auto-reply when away, draft for
  approval, or surface only.
- **@CoS / /CoS** — address the agent directly in any allowed chat and it replies in your
  voice (signed `~ CoS`), inside the guardrails.
- **PR review automation** — watches a Teams chat for GitHub PR links, reviews each diff,
  posts only critical/high findings as a GitHub comment, and summarizes back in chat.
- **PR-readiness watchdog** — checks *your own* open PRs (draft state, CI green, tests for
  new behavior, review clean) and nudges you privately in your Teams self-chat before you
  flip to Ready.
- **Weekly status** — drafts a status report from the week's merged PRs, ADO items and
  notes; you review/approve on the board, then it publishes to Confluence and posts a
  summary to a Teams chat.
- **Roadmap** — a swimlane timeline you can draft from data, edit (HITL), preview in the
  control tower, and **export to a polished PDF**.
- **Morning brief** — posts a daily brief (Top 3, meetings with links, tasks, watch items,
  this week's vision) to your Teams "Notes to self" chat, and writes the daily note.
- **Meeting summaries**, **open-loop tracking**, **knowledge sync** (Confluence/ADO →
  vault), and a **cost tab** that meters exactly what CoS spends to run.

## Guardrails (enforced in `backend/app/actions.py`, the only send path)

- Kill switch and a **dry-run default** (flip both on the board).
- Teams writes are **allowlist-only** (reads are unrestricted).
- One auto-reply per conversation per `rate_limit_minutes`; only while you're away.
- Email is **draft-only** — never auto-sent.
- A signature/attribution is appended to anything sent on your behalf; em dashes stripped.
- Everything lands in an `audit_log` with full content and reasoning.

---

## Architecture

```
backend/   FastAPI (Python 3.13, uv). MCP sessions + Claude Agent SDK + APScheduler.
           SQLite at data/ (gitignored). All rules live in policy.yaml (hot-reloaded).
frontend/  React + Vite + Tailwind control tower, served by the backend at :7777.
policy.example.yaml  → copy to policy.yaml and fill in. The only config you edit.
```

## Prerequisites

- **macOS** (uses the system keychain for corporate TLS trust and `launchctl` for
  autostart; the Teams "Notes to self" chat id is `48:notes`).
- [`uv`](https://docs.astral.sh/uv/), Node 18+, and the GitHub CLI [`gh`](https://cli.github.com/) (authenticated: `gh auth login`).
- **Claude Code** installed and authenticated (the Agent SDK uses it). The default agent
  model is `claude-haiku-4-5-20251001`; PR review / weekly / roadmap use `claude-sonnet-4-6`.
- **MCP servers configured in `~/.claude/settings.json`** under `mcpServers`. CoS reads
  their definitions from there (tokens never enter this repo). It expects:
  - `ms-graph` — a Microsoft Graph MCP server exposing `graph_*` tools (list/read chats,
    emails, calendar, presence, send chat message, etc.). **Required.**
  - `azure-devops` — optional; only for ADO work-item sync. Provide a PAT in its env.
  - `atlassian` — optional; for Confluence read/publish. Provide URL + email + API token.
  - `obsidian` — optional; the vault is also read/written directly from disk.

  If you don't use ADO/Confluence, leave their `policy.yaml` placeholders and those
  features no-op gracefully.

## Install

```bash
git clone <your-fork-url> arc-cos && cd arc-cos
cp policy.example.yaml policy.yaml      # then edit policy.yaml (see below)
make install                            # uv sync + npm install
make run                                # build frontend, serve at http://localhost:7777
```

First run boots in **dry-run** — it observes and drafts but sends nothing. Open the board,
review the Activity tab, then flip Live (and off dry-run) when you trust it.

Optional, start on login (macOS):

```bash
make autostart            # installs a launchd agent (make autostart-remove to undo)
```

## Configure `policy.yaml`

Everything personal/organizational lives in `policy.yaml` (gitignored). The essentials:

- `me` — your name, email, Entra/AAD object id, GitHub login(s). `self_chat_id` defaults
  to `48:notes` (the universal Teams self-chat) and is where the morning brief lands.
- `github.owner` / `azure_devops.org_url` — your org handles (used by PR review and ADO sync).
- `vault.root` — absolute path to your Obsidian vault (or any markdown folder).
- `teams_write.allow` — the only chats CoS may write to. Start empty/minimal.
- `vip` — people whose messages are always treated as sensitive (never auto-answered).
- `weekly_status` / `pr_readiness` / `pr_review` / `chieff` — feature-specific settings,
  each documented inline in `policy.example.yaml`.

`policy.yaml` is hot-reloaded — edits apply on the next sweep, no restart needed. You can
also flip dry-run, the kill switch, and per-chat write access from the board.

## Useful endpoints

`GET /api/board` — everything the UI shows · `GET /api/cost` — Claude/Codex/CoS spend ·
`POST /api/sweep/{teams,email,open_loops,pr_review,pr_readiness,weekly_status,morning_brief,...}`
— run any job now · `GET /api/roadmap/pdf` — export the roadmap PDF.

## Security

- No secrets in the repo. MCP tokens live only in `~/.claude/settings.json`.
- `policy.yaml`, `data/` (SQLite with your messages/drafts), `.venv`, `node_modules`, and
  build output are gitignored.
- Start in dry-run and add chats to the write allowlist deliberately.

## License

MIT — see `LICENSE`. Use at your own risk; you are responsible for what your instance
sends on your behalf.
