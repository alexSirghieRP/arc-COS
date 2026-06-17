"""Agent registry and the Claude Agent SDK harness.

Agents are read/think-only: their tool access never includes send/write tools.
Every outbound action goes through actions.py where the guardrails live.
"""

from dataclasses import dataclass, field

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .config import mcp_server_defs, policy

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
        f"You are CoS, the chief-of-staff agent for {who}, {role}. You triage their "
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


async def run_agent(
    agent: AgentDef,
    prompt: str,
    schema: dict | None = None,
    with_tools: bool = True,
    max_turns: int = 12,
    model: str | None = None,
    label: str = "agent",
):
    """One-shot agent run. Returns structured_output dict if schema given, else text.

    model overrides the agent's default for this call (use a cheaper model for
    high-volume work, a stronger one for quality-sensitive synthesis). label tags
    the call's purpose for CoS's own operating-cost breakdown."""
    defs = mcp_server_defs()
    sys_prompt = chief_system_prompt() if agent.name == "chief" else agent.system_prompt
    options = ClaudeAgentOptions(
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
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            # meter CoS's own operating cost (best-effort, never blocks the run)
            try:
                from . import cost
                cost.record_run(label, model or agent.model,
                                getattr(message, "total_cost_usd", 0.0),
                                getattr(message, "usage", None))
            except Exception:
                pass
            if message.subtype == "success":
                if schema:
                    if message.structured_output is None:
                        # e.g. an inaccessible model returns subtype=success but
                        # no structured output, with the reason in result.
                        raise RuntimeError(
                            "agent returned no structured output (model "
                            f"{model or agent.model}?): {(message.result or '')[:200]}")
                    return message.structured_output
                return message.result
            raise RuntimeError(f"agent run failed: {message.subtype}")
    raise RuntimeError("agent run produced no result")
