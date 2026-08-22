"""Memory index — the similarity substrate for the semantic-memory direction.

Step ①: metadata schema + scoring interface + a deterministic proxy scorer.

Turns the project's scattered experience artifacts (``findings.jsonl``,
``experience.jsonl``, distilled ``EXPERIENCE.md``) into one queryable index.
The scorer is behind a small protocol so an embedding scorer (E2/E3) can be
swapped in later without touching callers; the shipped ``LexicalScorer`` is a
dependency-free TF-IDF cosine proxy — deterministic, testable, and strictly
better than the session-level Jaccard in ``recall.py`` at *bullet* granularity.

Nothing here is wired into the run loop yet. That happens in E2 (semantic
recall) and E1 (dedup warning). Best-effort throughout: builders never raise
on malformed input.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

_STOP = {
    "the", "a", "an", "to", "of", "and", "for", "on", "in", "with", "without",
    "that", "this", "these", "those", "is", "are", "was", "were", "be", "been",
    "it", "its", "from", "by", "as", "at", "we", "you", "they", "our", "their",
    "but", "or", "if", "then", "than", "so", "not", "no", "do", "did", "does",
    "can", "could", "should", "would", "will", "may", "might", "when", "while",
    "where", "which", "who", "what", "why", "how", "all", "any", "some", "each",
    "more", "most", "other", "only", "just", "also", "use", "used", "using",
    "make", "makes", "made", "get", "got", "gotcha", "has", "have", "had",
    "note", "notes", "kind", "about", "score", "status", "result", "results",
    "finding", "findings", "was", "were", "run", "runs", "running", "worked",
    "works", "working", "try", "tries", "tried", "trying", "maximize",
    "minimize", "improve", "improved", "improves", "optimize", "optimized",
    "test", "tests", "testing", "dev", "task", "tasking", "one", "two", "three",
}

_FINDING_KINDS = {"leverage", "pitfall", "finding"}


def slug(s: str) -> str:
    """Normalize an arbitrary string into a lowercase-kebab domain slug."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "general").lower()).strip("-") or "general"


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords dropped, length > 2 kept."""
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP]


def _ngrams(tokens: list[str], n: int) -> list[str]:
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def _dedup_key(text: str) -> str:
    """Character-level key matching recall._compose's convention ([:80])."""
    return re.sub(r"\W+", "", (text or "").lower())[:80]


# ---------------------------------------------------------------------------
# Metadata schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """One concrete, situational lesson — the unit of searchable memory.

    ``kind`` is free-form ('leverage' | 'pitfall' | ...); ``about`` is the
    specific thing it concerns (dataset, harness, a numpy call...); ``note`` is
    the finding itself. ``domain`` is the normalized session/benchmark slug the
    finding belongs to (used for cross-domain anti-pollution, E3); ``tags`` are
    optional free-form labels. ``session``/``score``/``status``/``source`` are
    provenance so a retrieved finding can point back at where it came from.
    """

    kind: str = "finding"
    about: str = ""
    note: str = ""
    domain: str = ""
    session: str = ""
    score: float | None = None
    status: str | None = None
    source: str = ""
    tags: tuple[str, ...] = ()

    @classmethod
    def from_record(
        cls, rec: dict[str, Any], *, domain: str = "", session: str = "",
        source: str = "agent",
    ) -> "Finding":
        """Build from a findings.jsonl / experience.jsonl record (lenient)."""
        kind = (rec.get("kind") or "finding").strip().lower()
        if kind not in _FINDING_KINDS:
            kind = "finding"
        raw_tags = rec.get("tags") or ()
        tags = tuple(str(t).strip() for t in raw_tags if str(t).strip())
        raw_domain = rec.get("domain") or domain
        return cls(
            kind=kind,
            about=(rec.get("about") or "").strip(),
            note=(rec.get("note") or rec.get("insight") or "").strip(),
            domain=slug(raw_domain) if raw_domain else "",
            session=(rec.get("session") or session or ""),
            score=rec.get("score"),
            status=rec.get("status"),
            source=(rec.get("source") or source or ""),
            tags=tags,
        )

    @property
    def text(self) -> str:
        """The searchable surface: kind + about + note + tags.

        ``domain`` is deliberately *not* part of the text: it is a filter
        dimension (query with ``MemoryIndex.query(domain=...)``), not similarity
        content. Keeping it out also makes ``to_dict``/``from_record`` round-trips
        identity-preserving, so dedup keys stay stable across persistence.
        """
        parts = [self.kind, self.about, self.note, *self.tags]
        return " ".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "about": self.about,
            "note": self.note,
            "domain": self.domain,
            "session": self.session,
            "score": self.score,
            "status": self.status,
            "source": self.source,
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------------
# Scoring interface + deterministic proxy
# ---------------------------------------------------------------------------

class SimilarityScorer(Protocol):
    """Swap-in point for embeddings (E2+). score() must return [0, 1]."""

    def score(self, query: str, candidate: str) -> float: ...

    def fit(self, texts: list[str]) -> None: ...


class LexicalScorer:
    """Deterministic TF-IDF cosine proxy — no dependencies, no network.

    Fitted on the index corpus to weight terms by document frequency, then
    scores a query against a candidate by TF-IDF cosine similarity. Catches
    term-level overlap that the keyword Jaccard in ``recall.py`` misses (shared
    non-topic words no longer dominate); it does *not* catch true synonyms —
    that is precisely the gap embeddings close in E2.
    """

    def __init__(self, *, ngram_max: int = 2):
        self._ngram_max = ngram_max
        self._idf: dict[str, float] = {}

    def fit(self, texts: list[str]) -> None:
        docs = [self._terms(t) for t in texts]
        n = len(docs)
        df: dict[str, int] = {}
        for terms in docs:
            for term in set(terms):
                df[term] = df.get(term, 0) + 1
        self._idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}

    def _terms(self, text: str) -> list[str]:
        toks = _tokens(text)
        out = list(toks)
        for n in range(2, self._ngram_max + 1):
            out += _ngrams(toks, n)
        return out

    def _vector(self, terms: list[str]) -> dict[str, float]:
        tf: dict[str, float] = {}
        for t in terms:
            tf[t] = tf.get(t, 0.0) + 1.0
        n = len(terms) or 1
        return {t: (c / n) * self._idf.get(t, 0.0) for t, c in tf.items()}

    def score(self, query: str, candidate: str) -> float:
        if not self._idf:
            return 0.0
        q = self._vector(self._terms(query))
        c = self._vector(self._terms(candidate))
        if not q or not c:
            return 0.0
        dot = sum(q.get(t, 0.0) * c.get(t, 0.0) for t in q)
        qn = math.sqrt(sum(v * v for v in q.values()))
        cn = math.sqrt(sum(v * v for v in c.values()))
        if qn == 0 or cn == 0:
            return 0.0
        return dot / (qn * cn)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryMatch:
    finding: Finding
    score: float


class MemoryIndex:
    """Holds findings and answers similarity queries.

    ``query`` ranks findings by the scorer (auto-fitted on first query) with an
    optional ``domain`` filter for cross-domain anti-pollution: a finding with a
    non-empty domain that differs from the filter is excluded; unclassified
    findings (empty domain) are always eligible. Dedupes on add per-session by
    normalized text key, mirroring recall._compose's convention.
    """

    def __init__(self, *, scorer: SimilarityScorer | None = None):
        self._scorer = scorer if scorer is not None else LexicalScorer()
        self._findings: list[Finding] = []
        self._keys: set[tuple[str, str]] = set()
        self._fitted = False

    def add(self, finding: Finding) -> bool:
        """Add one finding; returns False if it's a near-duplicate of one kept.

        Dedup is *per-session*: the same lesson logged live and distilled within
        one session collapses to a single finding, but the same lesson appearing
        across several sessions is kept once per session — so recall can count
        cross-session recurrence ([xN]) the way ``recall._compose`` does.
        """
        key = _dedup_key(finding.text)
        skey = (finding.session, key)
        if skey in self._keys:
            return False
        self._keys.add(skey)
        self._findings.append(finding)
        self._fitted = False
        return True

    def extend(self, findings: Iterable[Finding]) -> int:
        """Add many findings; returns how many were actually kept."""
        added = 0
        for f in findings:
            added += 1 if self.add(f) else 0
        return added

    def _ensure_fitted(self) -> None:
        if self._fitted:
            return
        fit = getattr(self._scorer, "fit", None)
        if fit is not None:
            fit([f.text for f in self._findings])
        self._fitted = True

    def query(
        self,
        query: str,
        *,
        k: int = 5,
        domain: str | None = None,
        min_score: float = 0.0,
    ) -> list[MemoryMatch]:
        """Rank findings by similarity to ``query``, best first, capped at ``k``."""
        self._ensure_fitted()
        scored: list[MemoryMatch] = []
        for f in self._findings:
            if domain is not None and f.domain and f.domain != domain:
                continue
            s = self._scorer.score(query, f.text)
            if s >= min_score:
                scored.append(MemoryMatch(finding=f, score=s))
        scored.sort(key=lambda m: (-m.score, m.finding.text))
        return scored[:k]

    @property
    def findings(self) -> list[Finding]:
        return list(self._findings)

    def __len__(self) -> int:
        return len(self._findings)


# ---------------------------------------------------------------------------
# Builders — load real experience artifacts into the index (never raise)
# ---------------------------------------------------------------------------

def domain_from_session(session_dir: Path) -> str:
    """Recover the domain slug for a session, mirroring distill._domain.

    Prefers explicit domain/benchmark/cwd metadata on the tree root, else the
    containing project name from the ``.arbor/sessions`` layout.
    """
    session_dir = Path(session_dir)
    tree_path = session_dir / ".coordinator" / "idea_tree.json"
    meta: dict[str, Any] = {}
    try:
        if tree_path.exists():
            data = json.loads(tree_path.read_text(encoding="utf-8"))
            root = data.get("ROOT") or {}
            meta = root.get("meta", {}) if isinstance(root.get("meta"), dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    d = meta.get("domain") or meta.get("benchmark") or meta.get("cwd")
    if d:
        return slug(Path(str(d)).name)
    parts = session_dir.resolve().parts
    if ".arbor" in parts:
        return slug(parts[parts.index(".arbor") - 1])
    return "general"


def parse_experience_md(
    text: str, *, session: str = "", domain: str = "",
) -> list[Finding]:
    """Parse distilled EXPERIENCE.md bullets back into Findings.

    Bullets are ``- **[kind] about** — note`` (see distill._frag). Lenient:
    unparseable lines are skipped, never an error.
    """
    out: list[Finding] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("- "):
            continue
        body = ln[2:]
        m = re.match(r"\*\*\[([^\]]*)\]\s*([^*]*?)\*\*\s*[—–-]?\s*(.*)", body)
        if m:
            kind = (m.group(1).strip().lower() or "finding")
            if kind not in _FINDING_KINDS:
                kind = "finding"
            about = m.group(2).strip()
            note = m.group(3).strip()
        else:
            kind, about, note = "finding", "", body
        if not note:
            continue
        out.append(Finding(kind=kind, about=about, note=note,
                           domain=slug(domain) or "general", session=session,
                           source="distill"))
    return out


def iter_session_dirs(cwd: str) -> list[Path]:
    """Safely list the session directories under ``<cwd>/.arbor/sessions``.

    Skips symlinks (mirroring recall._safe_experience_files) so hostile or
    accidentally-linked sessions are never read.
    """
    sessions = Path(cwd).resolve() / ".arbor" / "sessions"
    if not sessions.is_dir() or sessions.is_symlink():
        return []
    out: list[Path] = []
    for s in sessions.iterdir():
        if s.is_dir() and not s.is_symlink():
            out.append(s)
    return out


def findings_from_session(session_dir: Path) -> list[Finding]:
    """Load a session's logged, live, and distilled findings into the index.

    Combines explicitly logged findings (``findings.jsonl``), insights captured
    live during the run (``experience.jsonl``), and the distilled lessons in
    ``EXPERIENCE.md``. Best-effort: any unreadable or missing artifact is
    skipped.
    """
    session_dir = Path(session_dir)
    domain = domain_from_session(session_dir)
    session = session_dir.name
    out: list[Finding] = []
    try:
        from .experience import load_experience, load_findings
    except Exception:  # pragma: no cover - import fallback
        load_experience = load_findings = None

    if load_findings is not None:
        for rec in load_findings(session_dir):
            f = Finding.from_record(rec, domain=domain, session=session, source="agent")
            if f.note:
                out.append(f)

    if load_experience is not None:
        for rec in load_experience(session_dir):
            f = Finding.from_record(rec, domain=domain, session=session, source="experience")
            if f.note:
                out.append(f)

    md = session_dir / "EXPERIENCE.md"
    if md.exists() and not md.is_symlink():
        try:
            out.extend(parse_experience_md(
                md.read_text(encoding="utf-8", errors="replace"),
                session=session, domain=domain))
        except OSError:  # pragma: no cover - unreadable file
            pass
    return out


def index_from_sessions(
    session_dirs: Iterable[Path], *, scorer: SimilarityScorer | None = None,
) -> MemoryIndex:
    """Build a MemoryIndex over one or more sessions (E3's persistent seed)."""
    index = MemoryIndex(scorer=scorer)
    for sd in session_dirs:
        index.extend(findings_from_session(sd))
    return index
