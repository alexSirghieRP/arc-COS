"""Agent registry and the Claude Agent SDK harness.

Agents are read/think-only: their tool access never includes send/write tools.
Every outbound action goes through actions.py where the guardrails live.
"""

import shutil
from dataclasses import dataclass, field

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .config import mcp_server_defs, policy

# The SDK bundles its own Claude Code CLI, which lags the org's enforced
# minimum version (org policy started rejecting the bundled 2.1.173 on
# 2026-07-14: "older than the minimum version required by your organization").
# The system CLI (kept current via the org's update channel) works; None
# falls back to the bundled one if it's ever missing from PATH.
CLI_PATH = shutil.which("claude")

# Cost-optimal default: Haiku handles the high-volume triage/classification
# calls (every few minutes). Quality-sensitive callers (PR review, weekly
# status) pass model= explicitly. NOTE: claude-fable-5 is NOT accessible on
# this account and returns empty structured output, so never use it here.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

NO_EM_DASH_RULE = (
    "Hard style rules for anything written on the user's behalf: never use em dashes. "
    "Use commas, periods, or parentheses instead. Be brief and natural, match the "
    "register of the conversation, and never pretend to be the user themselves."
)


def chief_system_prompt() -> str:
    """The CoS system prompt, built from policy.me so its identity matches you."""
    me = policy().get("me", {})
    name = me.get("name", "the user")
    email = me.get("email", "")
    role = me.get("role", "a professional")
    who = f"{name} ({email})" if email else name
    return (
        f"You are Chief, the chief-of-staff agent for {who}, {role}. You triage their "
        "messages, draft replies for their approval, summarize meetings, and keep their "
        "daily note alive. You never send anything yourself; you produce classifications, "
        "drafts, and summaries that the system or the user acts on. " + NO_EM_DASH_RULE
    )


@dataclass
class AgentDef:
    name: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)  # read-only tools only
    mcp_servers: list[str] = field(default_factory=list)
    schedule: str | None = None

    @property
    def model(self) -> str:
        cfg = policy().get("agents", {}).get(self.name, {})
        return cfg.get("model", DEFAULT_MODEL)


CHIEF = AgentDef(
    name="chief",
    system_prompt="",  # resolved per-call from policy.me via chief_system_prompt()
    allowed_tools=[
        "mcp__ms-graph__graph_read_chat_messages",
        "mcp__ms-graph__graph_read_email",
        "mcp__ms-graph__graph_list_calendar_events",
        "mcp__ms-graph__graph_check_calendar_availability",
        "mcp__ms-graph__graph_search_users",
    ],
    mcp_servers=["ms-graph"],
)

REGISTRY: dict[str, AgentDef] = {a.name: a for a in [CHIEF]}


DEFAULT_TIMEOUT_S = 300  # a hung SDK call must never wedge a sweep


async def run_agent(
    agent: AgentDef,
    prompt: str,
    schema: dict | None = None,
    with_tools: bool = True,
    max_turns: int = 12,
    model: str | None = None,
    label: str = "agent",
    timeout: float = DEFAULT_TIMEOUT_S,
):
    """One-shot agent run. Returns structured_output dict if schema given, else text.

    model overrides the agent's default for this call (use a cheaper model for
    high-volume work, a stronger one for quality-sensitive synthesis). label tags
    the call's purpose for CoS's own operating-cost breakdown. Every run, success
    or failure, is metered with its duration so the Health tab can show agent
    error rates and latency; timeout aborts a stuck run."""
    import asyncio
    import time as _time

    defs = mcp_server_defs()
    sys_prompt = chief_system_prompt() if agent.name == "chief" else agent.system_prompt
    options = ClaudeAgentOptions(
        cli_path=CLI_PATH,
        model=model or agent.model,
        system_prompt=sys_prompt,
        mcp_servers={n: defs[n] for n in agent.mcp_servers if n in defs} if with_tools else {},
        allowed_tools=list(agent.allowed_tools) if with_tools else [],
        disallowed_tools=["Bash", "Write", "Edit", "WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        output_format={"type": "json_schema", "schema": schema} if schema else None,
        setting_sources=[],
    )

    t0 = _time.monotonic()

    def _meter(message=None, error: str | None = None):
        # meter CoS's own operating cost (best-effort, never blocks the run)
        try:
            from . import cost
            cost.record_run(
                label, model or agent.model,
                getattr(message, "total_cost_usd", 0.0) if message else 0.0,
                getattr(message, "usage", None) if message else None,
                duration_ms=int((_time.monotonic() - t0) * 1000),
                ok=error is None, error=error)
        except Exception:
            pass

    try:
        async with asyncio.timeout(timeout):
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    if message.subtype == "success":
                        if schema and message.structured_output is None:
                            # e.g. an inaccessible model returns subtype=success but
                            # no structured output, with the reason in result.
                            err = ("agent returned no structured output (model "
                                   f"{model or agent.model}?): {(message.result or '')[:200]}")
                            _meter(message, error=err)
                            raise RuntimeError(err)
                        _meter(message)
                        return message.structured_output if schema else message.result
                    _meter(message, error=f"agent run failed: {message.subtype}")
                    raise RuntimeError(f"agent run failed: {message.subtype}")
    except TimeoutError:
        _meter(error=f"timed out after {timeout:.0f}s")
        raise RuntimeError(f"agent run timed out after {timeout:.0f}s (label={label})")
    _meter(error="agent run produced no result")
    raise RuntimeError("agent run produced no result")
