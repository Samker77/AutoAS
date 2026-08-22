"""Experience recall: find prior runs whose lessons may help the current topic.

Each finished run leaves an ``EXPERIENCE.md`` in its session folder. When a new
run starts, intake scans past sessions, scores each against the current research
topic, and (if a strong match exists) asks the user whether to reuse it. Accepted
matches are composed into a tailored experience block for the agent.

Matching is deterministic keyword overlap here — cheap and good enough to gate
the ask; an embedding/LLM judge can replace ``_score`` later.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .memory_index import MemoryIndex, findings_from_session, iter_session_dirs

_STOP = {"the", "a", "an", "to", "of", "and", "for", "on", "in", "with", "without",
         "maximize", "minimize", "improve", "optimize", "score", "test", "dev", "task"}


def _safe_experience_files(cwd: str) -> list[Path]:
    """Return non-symlink experience files contained by this project's root."""

    sessions = Path(cwd).resolve() / ".arbor" / "sessions"
    if not sessions.is_dir() or sessions.is_symlink():
        return []
    out: list[Path] = []
    for session in sessions.iterdir():
        if not session.is_dir() or session.is_symlink():
            continue
        exp = session / "EXPERIENCE.md"
        if not exp.is_file() or exp.is_symlink():
            continue
        try:
            exp.resolve(strict=True).relative_to(sessions)
        except (OSError, ValueError):
            continue
        out.append(exp)
    return out


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def _score(topic: set[str], experience: set[str]) -> float:
    if not topic or not experience:
        return 0.0
    return len(topic & experience) / len(topic)  # fraction of topic terms covered


def find_similar(cwd: str, topic: str, *, limit: int = 3, threshold: float = 0.25) -> list[dict[str, Any]]:
    """Return prior sessions ranked by topic overlap: [{name, path, score, text}]."""
    tt = _tokens(topic)
    hits: list[dict[str, Any]] = []
    for exp in _safe_experience_files(cwd):
        text = exp.read_text(encoding="utf-8")
        s = _score(tt, _tokens(text))
        if s >= threshold:
            hits.append({"name": exp.parent.name, "path": str(exp), "score": round(s, 3), "text": text})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def list_experiences(cwd: str, limit: int = 8) -> list[tuple[str, str]]:
    """[(session_name, first-line summary)] of prior runs that left experience."""
    out: list[tuple[str, str]] = []
    for exp in sorted(_safe_experience_files(cwd), reverse=True)[:limit]:
        desc = ""
        for ln in exp.read_text(encoding="utf-8").splitlines():
            if ln.startswith("description:"):
                desc = ln.split(":", 1)[1].strip()
                break
        out.append((exp.parent.name, desc))
    return out


def compose_for_topic(cwd: str, topic: str) -> str:
    """Compose one tailored experience block from sessions matching the topic."""
    hits = find_similar(cwd, topic)
    return _compose(hits)


def compose_from_sessions(cwd: str, names: list[str]) -> str:
    """Compose from sessions the intake agent (an LLM) judged relevant.

    LLM selection beats keyword matching: the intake agent reads the goal and the
    project, so it picks which prior runs actually transfer. Falls back to nothing
    if the named sessions lack experience.
    """
    available = {exp.parent.name: exp for exp in _safe_experience_files(cwd)}
    hits = []
    for name in names or []:
        exp = available.get(str(name))
        if exp is not None:
            hits.append({"name": name, "score": "selected", "text": exp.read_text(encoding="utf-8")})
    return _compose(hits)


# ---------------------------------------------------------------------------
# E2: bullet-level semantic recall via MemoryIndex
# ---------------------------------------------------------------------------

def find_similar_findings(
    cwd: str,
    topic: str,
    *,
    limit: int = 5,
    threshold: float = 0.1,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    """Bullet-level semantic recall: find individual past findings about ``topic``.

    Upgrades the session-level keyword gate: instead of scoring whole
    EXPERIENCE.md files by term overlap, every finding (from findings.jsonl,
    experience.jsonl, or distilled EXPERIENCE.md) is indexed and the topic is
    queried against the individual lessons. A single relevant bullet buried in a
    large session file now surfaces even when the file as a whole doesn't share
    many topic words (E2's granularity win).

    Deterministic today (MemoryIndex's lexical scorer); the embedding scorer
    slots in later without changing this function.
    """
    index = _recall_index(cwd)
    hits: list[dict[str, Any]] = []
    for match in index.query(topic, k=limit, domain=domain, min_score=threshold):
        hits.append({"finding": match.finding, "score": round(match.score, 3)})
    return hits


def _recall_index(cwd: str) -> MemoryIndex:
    """Prefer the persistent cross-run index (E3); scan sessions as fallback.

    Once distillation has populated ``<cwd>/.arbor/global_memory.jsonl``, recall
    loads that instead of re-scanning every session folder — the index is the
    accumulated memory across runs. Without it (cold start) we scan session dirs
    exactly as before.
    """
    try:
        from .global_memory import GlobalMemory
        gm = GlobalMemory.at_cwd(cwd)
        if gm.exists():
            return gm.load()
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    index = MemoryIndex()
    for sd in iter_session_dirs(cwd):
        index.extend(findings_from_session(sd))
    return index


def compose_for_topic_semantic(cwd: str, topic: str, *, domain: str | None = None) -> str:
    """Compose an experience block from bullet-level semantic matches."""
    return _compose_findings(find_similar_findings(cwd, topic, domain=domain))


def _compose_findings(matches: list[dict[str, Any]]) -> str:
    """Merge matched findings into a block, deduped with a recurrence count.

    Same shape as ``_compose`` (cross-finding confidence via [xN], ranked by how
    often a lesson reappears), but operating on structured findings rather than
    re-parsing whole session files.
    """
    if not matches:
        return ""
    merged: dict[str, list[Any]] = {}  # norm -> [count, bullet, session]
    for m in matches:
        f = m["finding"]
        body = f"**[{f.kind}] {f.about}** — {f.note}" if f.about else f"**[{f.kind}]** {f.note}"
        key = re.sub(r"\W+", "", (f.about + " " + f.note).lower())[:80]
        if not key:
            continue
        if key in merged:
            merged[key][0] += 1
        else:
            merged[key] = [1, body, f.session]
    ranked = sorted(merged.values(), key=lambda x: -x[0])
    lines = ["# Prior experience (candidate priors — verify, don't blindly apply)",
             f"_merged from {len(matches)} matched finding(s); [xN] = seen in N findings_\n"]
    lines += [f"- [x{c}] {b}" for c, b, _s in ranked]
    return "\n".join(lines)


def _compose(hits: list[dict[str, Any]]) -> str:
    """Merge findings across the matched sessions, deduped with a recurrence count.

    A finding seen in several past runs is more trustworthy, so it's tagged [xN]
    and ranked first — cross-session confidence without a global library.
    """
    if not hits:
        return ""
    merged: dict[str, list[Any]] = {}  # norm -> [count, bullet, first-session]
    for h in hits:
        for ln in h["text"].splitlines():
            ln = ln.strip()
            if not ln.startswith("- "):
                continue
            note = re.sub(r"\*\*\[.*?\]\s*[^—]*\*\*\s*—?\s*", "", ln[2:]).strip()
            key = re.sub(r"\W+", "", note.lower())[:80]
            if not key:
                continue
            if key in merged:
                merged[key][0] += 1
            else:
                merged[key] = [1, ln[2:].strip(), h["name"]]
    if not merged:  # nothing parseable — fall back to raw concatenation
        return "\n".join(["# Prior experience (candidate priors — verify, don't blindly apply)"]
                         + [f"\n## from {h['name']}\n{h['text']}" for h in hits])
    ranked = sorted(merged.values(), key=lambda x: -x[0])
    lines = ["# Prior experience (candidate priors — verify, don't blindly apply)",
             f"_merged from {len(hits)} past run(s); [xN] = seen in N runs_\n"]
    lines += [f"- [x{c}] {bullet}" for c, bullet, _src in ranked]
    return "\n".join(lines)
