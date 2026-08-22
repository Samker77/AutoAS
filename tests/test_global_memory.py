"""E3 cross-run global memory: persistence, atomicity, rebuild, and the
write side (distill) / read side (recall) wiring."""

from __future__ import annotations

import json
from pathlib import Path

from arbor.distill import distill_to_session
from arbor.global_memory import GLOBAL_MEMORY_FILENAME, GlobalMemory
from arbor.memory_index import Finding
from arbor.recall import compose_for_topic_semantic


def _gm(tmp_path: Path) -> GlobalMemory:
    return GlobalMemory.at_cwd(str(tmp_path / "proj"))


def _finding(note: str, *, session: str = "run-1", domain: str = "knn",
             kind: str = "leverage") -> Finding:
    return Finding(note=note, session=session, domain=domain, kind=kind)


# ── append / load round-trip ────────────────────────────────────────────

def test_append_then_load_round_trips(tmp_path):
    gm = _gm(tmp_path)
    assert gm.append([_finding("gradient clipping helps")]) == 1
    assert gm.exists()
    notes = {f.note for f in gm.load().findings}
    assert notes == {"gradient clipping helps"}


def test_append_is_per_session_dedup(tmp_path):
    gm = _gm(tmp_path)
    assert gm.append([_finding("same lesson")]) == 1
    # identical lesson in the SAME session → deduped, nothing added
    assert gm.append([_finding("same lesson")]) == 0
    assert len(gm) == 1
    # identical lesson in a DIFFERENT session → kept (cross-run recurrence)
    assert gm.append([_finding("same lesson", session="run-2")]) == 1
    assert len(gm) == 2


def test_file_is_valid_jsonl(tmp_path):
    gm = _gm(tmp_path)
    gm.append([_finding("a"), _finding("b", session="run-2")])
    lines = gm.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for ln in lines:
        rec = json.loads(ln)
        assert rec["note"] in ("a", "b")
        assert rec["session"] and rec["domain"] and rec["kind"]


def test_torn_last_line_is_skipped_on_load(tmp_path):
    gm = _gm(tmp_path)
    gm.append([_finding("good lesson")])
    gm.path.write_text(
        gm.path.read_text(encoding="utf-8") + '{"note": "torn', encoding="utf-8")
    notes = {f.note for f in gm.load().findings}
    assert notes == {"good lesson"}


# ── query / domain filter ───────────────────────────────────────────────

def test_query_with_domain_filter(tmp_path):
    gm = _gm(tmp_path)
    gm.append([_finding("gradient clipping helps", domain="ml"),
               _finding("gradient clipping helps", domain="web")])
    hits = gm.query("gradient clipping", domain="ml")
    assert len(hits) == 1
    assert hits[0].finding.domain == "ml"


# ── rebuild ─────────────────────────────────────────────────────────────

def test_rebuild_from_session_dirs(tmp_path):
    proj = tmp_path / "proj"
    sd1 = proj / ".arbor" / "sessions" / "run-1"
    sd2 = proj / ".arbor" / "sessions" / "run-2"
    for sd in (sd1, sd2):
        (sd / ".coordinator").mkdir(parents=True)
        (sd / "findings.jsonl").write_text(json.dumps(
            {"kind": "leverage", "note": "a shared lesson"}) + "\n", encoding="utf-8")
    gm = GlobalMemory.at_cwd(str(proj))
    assert gm.rebuild([sd1, sd2]) == 2  # same lesson across two runs is kept
    assert len(gm) == 2
    assert gm.exists()


# ── path resolution consistency ─────────────────────────────────────────

def test_for_session_matches_at_cwd(tmp_path):
    proj = tmp_path / "proj"
    session_dir = proj / ".arbor" / "sessions" / "run-1"
    assert GlobalMemory.for_session(session_dir).path == \
        GlobalMemory.at_cwd(str(proj)).path
    assert GlobalMemory.at_cwd(str(proj)).path.name == GLOBAL_MEMORY_FILENAME


# ── read side: recall prefers the index ─────────────────────────────────

def test_recall_uses_global_memory_when_present(tmp_path):
    proj = tmp_path / "proj"
    gm = GlobalMemory.at_cwd(str(proj))
    gm.append([_finding("gradient clipping mitigates exploding gradients",
                        domain="ml")])
    # no session dirs exist — the block can only come from the persisted index
    block = compose_for_topic_semantic(str(proj), "exploding gradient mitigation")
    assert "gradient clipping" in block


def test_recall_falls_back_to_sessions_without_index(tmp_path):
    proj = tmp_path / "proj"
    sd = proj / ".arbor" / "sessions" / "run-1"
    (sd / ".coordinator").mkdir(parents=True)
    (sd / "findings.jsonl").write_text(json.dumps(
        {"kind": "leverage", "note": "block-min tournament helps"}) + "\n",
        encoding="utf-8")
    # no global_memory.jsonl yet — scans sessions, same as E2
    block = compose_for_topic_semantic(str(proj), "block-min tournament")
    assert "block-min tournament helps" in block


# ── write side: distill populates the index ─────────────────────────────

def _session_with_logged_finding(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    sd = proj / ".arbor" / "sessions" / "run-1"
    (sd / ".coordinator").mkdir(parents=True)
    (sd / ".coordinator" / "idea_tree.json").write_text(json.dumps(
        {"ROOT": {"meta": {"benchmark": "knn"}}, "1": {
            "id": "1", "parent_id": "ROOT", "depth": 1,
            "hypothesis": "x", "status": "done", "insight": "block-min helps"}}),
        encoding="utf-8")
    (sd / "findings.jsonl").write_text(json.dumps(
        {"kind": "leverage", "about": "dataset", "note": "labels noisy above 9000"}) + "\n",
        encoding="utf-8")
    return proj, sd


def test_distill_populates_global_memory(tmp_path):
    proj, sd = _session_with_logged_finding(tmp_path)
    assert distill_to_session(sd, provider=None) is not None
    gm = GlobalMemory.at_cwd(str(proj))
    assert gm.exists()
    notes = {f.note for f in gm.load().findings}
    assert "labels noisy above 9000" in notes


def test_distill_is_idempotent_for_global_memory(tmp_path):
    proj, sd = _session_with_logged_finding(tmp_path)
    assert distill_to_session(sd, provider=None) is not None
    before = len(GlobalMemory.at_cwd(str(proj)))
    # a second distill on the same session must not double-append
    assert distill_to_session(sd, provider=None) is not None
    assert len(GlobalMemory.at_cwd(str(proj))) == before
