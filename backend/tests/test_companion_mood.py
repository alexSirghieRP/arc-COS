"""Mood/EQ tests (pillar 5) and report-back change-line (pillar 3)."""

from app import companion


def test_mood_frustrated():
    name, steer = companion._mood("ugh why is this still broken")
    assert name == "frustrated" and "on their side" in steer


def test_mood_happy():
    name, steer = companion._mood("nice, PR finally passed lets go")
    assert name == "happy" and "energy" in steer.lower()


def test_mood_stressed():
    name, steer = companion._mood("I'm slammed today, deadline is tonight")
    assert name == "stressed"


def test_mood_headsdown_terse():
    name, _ = companion._mood("status")
    assert name == "heads_down"


def test_mood_neutral():
    name, steer = companion._mood("can you walk me through how the uat pipeline decides deployment")
    assert name == "neutral" and steer == ""


def test_report_back_line_success():
    line = companion._change_line("companion_work_done",
                                  {"task": "fix the flaky test", "ok": True, "pr": "http://x/pull/9"})
    assert "finished" in line and "pull/9" in line


def test_report_back_line_failure():
    line = companion._change_line("companion_work_done",
                                  {"task": "fix the flaky test", "ok": False, "error": "boom"})
    assert "snag" in line and "boom" in line
