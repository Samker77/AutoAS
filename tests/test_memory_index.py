"""Tests for the step-① memory substrate: Finding schema, LexicalScorer proxy,
MemoryIndex query/filter/dedup, and builders over the real artifact formats."""

from __future__ import annotations

import json
from pathlib import Path

from arbor.memory_index import (
    Finding,
    LexicalScorer,
    MemoryIndex,
    domain_from_session,
    findings_from_session,
    index_from_sessions,
    parse_experience_md,
    slug,
)


# ---------------------------------------------------------------------------
# Finding schema
# ---------------------------------------------------------------------------

def test_finding_text_combines_fields():
    f = Finding(kind="leverage", about="the eval harness", note="eval.py is protected",
                tags=("harness",), domain="knn")
    assert "leverage" in f.text and "eval.py" in f.text and "harness" in f.text


def test_finding_from_record_lenient_defaults():
    f = Finding.from_record({"kind": "LEVERAGE", "note": "drop noisy labels"})
    assert f.kind == "leverage"
    assert f.note == "drop noisy labels"
    assert f.domain == ""  # no domain on record and none passed -> unclassified
    assert f.tags == ()
    assert f.source == "agent"


def test_finding_from_record_tags_and_domain():
    f = Finding.from_record(
        {"kind": "pitfall", "note": "x", "domain": "ImageNet-1K", "tags": ["a", "b"]},
        domain="other",
    )
    assert f.domain == "imagenet-1k"  # record wins, slugged
    assert f.tags == ("a", "b")


def test_slug_normalizes():
    assert slug("ImageNet-1K") == "imagenet-1k"
    assert slug("  KNN   AlgoTune  ") == "knn-algotune"
    assert slug("") == "general"


# ---------------------------------------------------------------------------
# LexicalScorer proxy
# ---------------------------------------------------------------------------

def _scorer_on(texts):
    s = LexicalScorer()
    s.fit(texts)
    return s


def test_lexical_identical_is_one():
    s = _scorer_on(["gradient clipping mitigates exploding gradients"])
    assert s.score("gradient clipping mitigates exploding gradients",
                   "gradient clipping mitigates exploding gradients") == 1.0


def test_lexical_ranks_semantic_peers_above_unrelated():
    """The doc's motivating example: term-overlap that session Jaccard misses."""
    corpus = [
        "gradient clipping mitigates exploding gradients",
        "database row level indexing speeds up read queries",
    ]
    s = _scorer_on(corpus)
    query = "exploding gradient mitigation"
    similar = s.score(query, corpus[0])
    unrelated = s.score(query, corpus[1])
    assert similar > unrelated
    assert similar > 0.05
    # shared non-topic words alone no longer score high (TF-IDF down-weights)
    assert unrelated < 0.1


def test_lexical_unfitted_returns_zero():
    s = LexicalScorer()
    assert s.score("anything", "else") == 0.0


def test_lexical_stopwords_do_not_dominate():
    s = _scorer_on(["we tried the test and the run worked", "gradient clipping works"])
    a = s.score("gradient clipping works", "gradient clipping works")
    assert a > s.score("we tried the test and the run worked",
                       "gradient clipping works")


# ---------------------------------------------------------------------------
# MemoryIndex
# ---------------------------------------------------------------------------

def test_index_ranks_most_similar_first():
    index = MemoryIndex()
    index.add(Finding(note="gradient clipping mitigates exploding gradients"))
    index.add(Finding(note="database row indexing speeds up queries"))
    hits = index.query("exploding gradient mitigation")
    assert hits and hits[0].finding.note == "gradient clipping mitigates exploding gradients"


def test_index_k_cap():
    index = MemoryIndex()
    for i in range(10):
        index.add(Finding(note=f"same core concept number {i}"))
    hits = index.query("core concept", k=3)
    assert len(hits) == 3


def test_index_domain_filter_excludes_other_domains():
    index = MemoryIndex()
    index.add(Finding(note="gradient clipping helps", domain="ml"))
    index.add(Finding(note="gradient clipping helps", domain="web"))
    hits = index.query("gradient clipping", domain="ml")
    assert len(hits) == 1
    assert hits[0].finding.domain == "ml"
    # unclassified findings are still eligible under a domain filter
    index.add(Finding(note="untagged gradient insight"))
    assert len(index.query("gradient", domain="ml")) == 2


def test_index_dedupes_on_add():
    index = MemoryIndex()
    assert index.add(Finding(note="the exact same lesson repeated"))
    assert not index.add(Finding(note="the exact same lesson repeated"))
    assert len(index) == 1


def test_index_empty_query():
    assert MemoryIndex().query("anything") == []


# ---------------------------------------------------------------------------
# Builders over real artifact formats
# ---------------------------------------------------------------------------

def _make_session(tmp_path: Path, name: str = "run-001") -> Path:
    sd = tmp_path / name
    (sd / ".coordinator").mkdir(parents=True)
    return sd


def test_domain_from_session_prefers_tree_meta(tmp_path):
    sd = _make_session(tmp_path)
    tree = {"ROOT": {"meta": {"benchmark": "knn-algotune"}}}
    (sd / ".coordinator" / "idea_tree.json").write_text(
        json.dumps(tree), encoding="utf-8")
    assert domain_from_session(sd) == "knn-algotune"


def test_domain_from_session_falls_back_to_project(tmp_path):
    proj = tmp_path / "myproj" / ".arbor" / "sessions" / "run-9"
    proj.mkdir(parents=True)
    assert domain_from_session(proj) == "myproj"


def test_findings_from_session_combines_logged_and_live(tmp_path):
    sd = _make_session(tmp_path)
    (sd / "findings.jsonl").write_text(json.dumps(
        {"kind": "leverage", "about": "dataset", "note": "labels noisy above 9000"}),
        encoding="utf-8")
    (sd / "experience.jsonl").write_text(json.dumps(
        {"node_id": "1.1", "status": "done", "insight": "block-min tournament helps"}) + "\n",
        encoding="utf-8")
    found = findings_from_session(sd)
    notes = {f.note for f in found}
    assert "labels noisy above 9000" in notes
    assert "block-min tournament helps" in notes
    assert all(f.domain == "general" and f.session == "run-001" for f in found)


def test_findings_from_session_parses_experience_md(tmp_path):
    sd = _make_session(tmp_path)
    (sd / "EXPERIENCE.md").write_text(
        "# Findings\n\n- **[leverage] dataset** — labels are noisy above 9000\n",
        encoding="utf-8")
    found = findings_from_session(sd)
    assert len(found) == 1
    assert found[0].note == "labels are noisy above 9000"
    assert found[0].source == "distill"


def test_findings_from_session_dedupes_shared_bullets(tmp_path):
    sd = _make_session(tmp_path)
    (sd / "findings.jsonl").write_text(json.dumps(
        {"kind": "leverage", "about": "dataset", "note": "labels are noisy above 9000"}),
        encoding="utf-8")
    (sd / "EXPERIENCE.md").write_text(
        "# Findings\n\n- **[leverage] dataset** — labels are noisy above 9000\n",
        encoding="utf-8")
    # the same lesson logged live AND distilled must not double-count in an index
    index = MemoryIndex()
    index.extend(findings_from_session(sd))
    assert len(index) == 1


def test_findings_from_session_skips_blank_insights(tmp_path):
    sd = _make_session(tmp_path)
    (sd / "experience.jsonl").write_text(json.dumps(
        {"node_id": "2.1", "status": "pending", "insight": "  "}) + "\n",
        encoding="utf-8")
    assert findings_from_session(sd) == []


def test_parse_experience_md_bullets(tmp_path):
    md = """---
name: experience-knn
description: Concrete findings from a knn run.
---

# Findings: knn

- **[leverage] dataset** — labels are noisy above index 9000, drop them
- **[pitfall] harness** — the executor kept editing eval.py
- **a bare bullet with no kind**
"""
    found = parse_experience_md(md, session="run-1", domain="knn")
    assert len(found) == 3
    assert found[0].kind == "leverage" and found[0].about == "dataset"
    assert found[1].kind == "pitfall"
    assert found[2].kind == "finding"  # bare bullet falls back
    assert all(f.session == "run-1" and f.domain == "knn" for f in found)


def test_parse_experience_md_empty():
    assert parse_experience_md("") == []
    assert parse_experience_md("no bullets here") == []


def test_index_from_sessions(tmp_path):
    sd = _make_session(tmp_path)
    (sd / "findings.jsonl").write_text(json.dumps(
        {"kind": "leverage", "note": "gradient clipping mitigates exploding gradients"}),
        encoding="utf-8")
    index = index_from_sessions([sd])
    assert len(index) == 1
    hits = index.query("exploding gradient mitigation")
    assert hits and hits[0].score > 0


# ---------------------------------------------------------------------------
# Backward compatibility of record_finding metadata
# ---------------------------------------------------------------------------

def test_record_finding_with_and_without_metadata(tmp_path):
    from arbor.experience import load_findings, record_finding
    sd = tmp_path
    record_finding(str(sd), kind="leverage", about="", note="plain finding")
    record_finding(str(sd), kind="pitfall", about="harness", note="tagged finding",
                   domain="knn", tags=["harness"])
    recs = load_findings(sd)
    assert len(recs) == 2
    assert "domain" not in recs[0] and "tags" not in recs[0]
    assert recs[1]["domain"] == "knn"
    assert recs[1]["tags"] == ["harness"]
