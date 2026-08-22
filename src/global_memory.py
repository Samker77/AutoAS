"""E3 — cross-run global memory index.

Persists distilled findings into a project-level JSONL index so a run on the
same benchmark inherits the lessons of all prior runs without re-scanning every
session folder. Write side: ``distill_to_session`` appends each run's findings
with domain/session/kind/score metadata. Read side: ``recall.find_similar_findings``
loads the persisted index (instead of scanning sessions) and can filter by
``domain`` to prevent cross-domain pollution.

Durability: every line is self-contained JSON and appends rewrite the file
atomically (temp + rename), so a torn write drops at most one malformed line on
the next load (skipped) and never corrupts the whole index. The index can always
be rebuilt from session dirs via ``GlobalMemory.rebuild``.

Best-effort throughout: append/rebuild never raise on unreadable state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .memory_index import (
    Finding,
    MemoryIndex,
    MemoryMatch,
    _dedup_key,
    findings_from_session,
)

GLOBAL_MEMORY_FILENAME = "global_memory.jsonl"


class GlobalMemory:
    """A project-level, append-only, queryable store of distilled findings."""

    def __init__(self, path: Path):
        self._path = Path(path)

    @classmethod
    def at_cwd(cls, cwd: str | Path) -> "GlobalMemory":
        """The index for a project rooted at ``cwd``: ``<cwd>/.arbor/...``.

        This is the read-side view — recall resolves it from the project root.
        """
        return cls(Path(cwd).resolve() / ".arbor" / GLOBAL_MEMORY_FILENAME)

    @classmethod
    def for_session(cls, session_dir: str | Path) -> "GlobalMemory":
        """The index owning a session dir, recovered from its ``.arbor`` ancestor.

        ``distill_to_session`` receives the session dir; walking up to the
        ``.arbor`` directory yields the same file ``at_cwd`` computes, so the
        write side and read side always agree.
        """
        session_dir = Path(session_dir).resolve()
        for parent in session_dir.parents:
            if parent.name == ".arbor":
                return cls(parent / GLOBAL_MEMORY_FILENAME)
        return cls(session_dir.parent / GLOBAL_MEMORY_FILENAME)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    # ── disk ───────────────────────────────────────────────────────────────

    def _read_records(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn last line — drop it, keep the rest
        return out

    def _write_atomic(self, records: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8")
        os.replace(tmp, self._path)

    # ── write side ─────────────────────────────────────────────────────────

    def append(self, findings: Iterable[Finding]) -> int:
        """Append findings not already present; returns how many were added.

        Dedup is per-session (the same lesson logged live and distilled once),
        while the same lesson across runs is kept once per run so recall can
        count cross-run recurrence ([xN]).
        """
        existing = self._read_records()
        keys = {
            (r.get("session"), _dedup_key(Finding.from_record(r).text))
            for r in existing
        }
        new_records: list[dict[str, Any]] = []
        for f in findings:
            key = _dedup_key(f.text)
            if (f.session, key) in keys:
                continue
            keys.add((f.session, key))
            new_records.append(f.to_dict())
        if not new_records:
            return 0
        self._write_atomic(existing + new_records)
        return len(new_records)

    def rebuild(self, session_dirs: Iterable[Path]) -> int:
        """Recreate the index from session dirs (repair or bootstrap)."""
        index = MemoryIndex()
        for sd in session_dirs:
            index.extend(findings_from_session(sd))
        records = [f.to_dict() for f in index.findings]
        self._write_atomic(records)
        return len(records)

    # ── read side ──────────────────────────────────────────────────────────

    def load(self) -> MemoryIndex:
        """Rebuild the in-memory index from the persisted records."""
        index = MemoryIndex()
        for rec in self._read_records():
            index.add(Finding.from_record(rec))
        return index

    def query(
        self,
        topic: str,
        *,
        k: int = 5,
        domain: str | None = None,
        min_score: float = 0.0,
    ) -> list[MemoryMatch]:
        return self.load().query(topic, k=k, domain=domain, min_score=min_score)

    def __len__(self) -> int:
        return len(self._read_records())
