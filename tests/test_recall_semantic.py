"""E2 semantic recall: bullet-level MemoryIndex recall over the intake flow.

Demonstrates the granularity win — a relevant lesson buried in a session full
of unrelated bullets surfaces as *the* match, and the composed block is short
instead of dumping every bullet — plus cross-session recurrence and the intake
wiring (the old whole-file keyword gate is replaced as the fallback).
"""

from __future__ import annotations

import json
from pathlib import Path

from arbor.recall import compose_for_topic_semantic, compose_from_sessions, find_similar_findings


def _write_session(project: Path, name: str, md: str, *, meta: dict | None = None) -> Path:
    sd = project / ".arbor" / "sessions" / name
    (sd / ".coordinator").mkdir(parents=True)
    if meta is not None:
        (sd / ".coordinator" / "idea_tree.json").write_text(
            json.dumps({"ROOT": {"meta": meta}}), encoding="utf-8")
    (sd / "EXPERIENCE.md").write_text(md, encoding="utf-8")
    return sd


def test_bullet_granularity_surfaces_relevant_lesson(tmp_path):
    """A relevant bullet in a file full of kNN-related noise still wins."""
    project = tmp_path / "proj"
    noise = "\n".join(
        f"- **[pitfall]** noise about kNN search variant {i} — unrelated data quirk"
        for i in range(30))
    md = "# Findings\n\n" + noise + (
        "\n- **[leverage]** block-min tournament — "
        "block-min tournament speeds up kNN nearest neighbor search\n")
    _write_session(project, "run-a", md)

    topic = "block-min tournament to speed up kNN search"
    hits = find_similar_findings(str(project), topic)
    assert hits
    assert "block-min tournament" in hits[0]["finding"].note
    assert hits[0]["score"] > 0.4

    block = compose_for_topic_semantic(str(project), topic)
    assert "block-min tournament" in block
    # the 30 noise bullets are not all dumped into the block
    assert block.count("- [") < 10


def test_cross_session_recurrence_is_counted(tmp_path):
    project = tmp_path / "proj"
    bullet = ("- **[leverage]** gradient clipping — "
              "gradient clipping mitigates exploding gradients during training\n")
    _write_session(project, "run-a", bullet)
    _write_session(project, "run-b", bullet)

    block = compose_for_topic_semantic(str(project), "exploding gradient mitigation")
    assert "[x2]" in block


def test_semantic_recall_empty_when_no_matches(tmp_path):
    project = tmp_path / "proj"
    _write_session(project, "run-a", "- **[leverage]** about — database indexing notes\n")
    assert compose_for_topic_semantic(str(project), "quantum chemistry of boron") == ""


def test_domain_filter_limits_recall(tmp_path):
    project = tmp_path / "proj"
    _write_session(project, "a", "- **[leverage]** gradient clipping helps\n", meta={"domain": "ml"})
    _write_session(project, "b", "- **[leverage]** gradient clipping helps\n", meta={"domain": "web"})

    block = compose_for_topic_semantic(str(project), "gradient clipping", domain="ml")
    # only the ml session's finding is eligible -> [x1]
    assert "[x1]" in block and "[x2]" not in block


def test_compose_from_sessions_unchanged(tmp_path):
    """The LLM-selected path (intake primary) is untouched by E2."""
    project = tmp_path / "proj"
    _write_session(project, "run-a", "- **[leverage]** about — the dataset labels are noisy\n")
    block = compose_from_sessions(str(project), ["run-a"])
    assert "labels are noisy" in block


def test_with_experience_wires_semantic_fallback(tmp_path):
    from arbor.cli.intake.launch_tool import _with_experience

    project = tmp_path / "proj"
    _write_session(project, "run-a",
                   "- **[leverage]** gradient clipping — gradient clipping helps here\n")
    out = _with_experience(str(project), "exploding gradient mitigation", apply=True)
    assert "gradient clipping helps" in out
    # apply=False leaves the instruction untouched
    assert _with_experience(str(project), "anything", apply=False) == "anything"


def test_with_experience_no_match_returns_instruction(tmp_path):
    from arbor.cli.intake.launch_tool import _with_experience

    project = tmp_path / "proj"
    _write_session(project, "run-a", "- **[leverage]** database indexing notes\n")
    assert _with_experience(str(project), "quantum chemistry", apply=True) == "quantum chemistry"
