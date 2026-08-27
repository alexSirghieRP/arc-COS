"""Board gates: tunable loop counts between pipeline lanes.

Each gate sits between two lanes on the swarm board. Pass-through gates are
free transitions; loop gates bound how many times the swarm re-tries a stage
before the unit drops through (to Blocked / Needs you). The user adjusts loop
counts from gate chips on the board itself; overrides live in the settings
table and beat the policy.yaml defaults. Values are clamped to 0..5.

Lives in its own module because both swarm.py and worktrees.py consume gate
values and swarm already imports worktrees (a helper there would be circular).
"""

from . import db
from .config import policy

MAX_LOOPS = 5

# gate key -> (policy scope, policy key, default)
GATES = {
    # scan rows: how often a failed worker retries before the unit blocks
    "retries": ("swarm", "retries", 1),
    # coding row: repair passes for a coder whose attempt failed
    "repair_attempts": ("code", "repair_attempts", 1),
    # review row: fix coders spawned for red CI checks
    "ci_fix_attempts": ("code", "ci_fix_attempts", 2),
    # review row: fix rounds for reviewer comments before "Needs you"
    "review_fix_attempts": ("code", "review_fix_attempts", 3),
}


def _clamp(v) -> int:
    return max(0, min(MAX_LOOPS, int(v)))


def gate_value(key: str) -> int:
    scope, name, default = GATES[key]
    raw = db.get_setting(f"swarm_gate:{key}")
    if raw is not None:
        try:
            return _clamp(raw)
        except (TypeError, ValueError):
            pass
    cfg = policy().get("swarm", {})
    if scope == "code":
        cfg = cfg.get("code", {})
    try:
        return _clamp(cfg.get(name, default))
    except (TypeError, ValueError):
        return default


def gates_state() -> dict:
    return {k: gate_value(k) for k in GATES}


def set_gate(key: str, loops) -> dict:
    if key not in GATES:
        raise KeyError(key)
    v = _clamp(loops)
    db.set_setting(f"swarm_gate:{key}", str(v))
    db.audit("swarm_gate", {"gate": key, "loops": v})
    return gates_state()
