"""Memory tests (goal pillar 1 + learning loop pillar 8): persistence,
recall/relevance, directive parsing, and dedupe."""

from app import companion_memory as mem


def test_add_and_recall_by_topic():
    mem.add("The owner prefers squashed commits on merge", kind="preference",
            topic="commits", source="owner")
    hits = mem.recall("how should I do the commit")
    assert any("squashed" in h["content"] for h in hits)


def test_corrections_outrank_facts():
    mem.add("the sky is blue", kind="fact", topic="misc")
    mem.add("never force-push to shared branches", kind="correction",
            topic="git")
    # a git-related query should surface the correction near the top
    hits = mem.recall("should I push to the branch")
    kinds = [h["kind"] for h in hits[:3]]
    assert "correction" in kinds


def test_forget_removes():
    mem.add("temporary throwaway note about xyzzy", kind="fact", topic="xyzzy")
    assert any("xyzzy" in h["content"] for h in mem.recall("xyzzy"))
    removed = mem.forget("xyzzy")
    assert removed >= 1
    assert not any("xyzzy" in h["content"] for h in mem.recall("xyzzy"))


def test_dedupe_same_content():
    c = "The owner likes short PR descriptions"
    a = mem.add(c, kind="preference")
    b = mem.add(c, kind="preference")
    assert a == b  # same row refreshed, not duplicated


def test_directive_parsing():
    assert mem.parse_directive("remember that Jane owns the rollout") == \
        ("remember", "Jane owns the rollout")
    assert mem.parse_directive("remember: squash my commits") == \
        ("remember", "squash my commits")
    assert mem.parse_directive("forget about the xyzzy thing") == \
        ("forget", "the xyzzy thing")
    assert mem.parse_directive("what do you know about Jane")[0] == "recall"
    assert mem.parse_directive("kick off a swarm on 12345") is None


def test_context_marks_used_and_returns_block():
    mem.add("Project Alpha import finished clean", kind="fact", topic="alpha")
    block = mem.context("how did project alpha go")
    assert "Project Alpha" in block


def test_classify():
    assert mem._classify("don't ever force push") == "correction"
    assert mem._classify("I prefer squashed commits") == "preference"
    assert mem._classify("we decided to go with the uat branch") == "decision"
    assert mem._classify("the repo is example-repo") == "fact"
