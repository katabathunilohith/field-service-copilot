"""Local lexical memory used when Hindsight is unconfigured, slow or down.

It indexes the seeded service history plus a write-through journal of every
record the app retains at runtime, and ranks with BM25 plus tag boosts. It has
no semantic understanding; it exists so the diagnostic pipeline never breaks.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from pathlib import Path

from memory.bank_schemas import RepairRecord
from memory.insights import MemoryHit

_TOKEN = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "on", "in", "at", "for", "is", "it", "was", "with", "by", "this", "that"}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class LocalMemoryStore:
    K1 = 1.4
    B = 0.75

    def __init__(self, seed_records: list[RepairRecord], journal_path: Path) -> None:
        self._journal = journal_path
        self._lock = threading.Lock()
        self._records: list[RepairRecord] = []
        self._tokens: list[Counter[str]] = []
        self._df: Counter[str] = Counter()
        self._total_len = 0
        for record in seed_records:
            self._index(record)
        for record in self._read_journal():
            self._index(record)

    # ------------------------------------------------------------- persistence
    def _read_journal(self) -> list[RepairRecord]:
        if not self._journal.exists():
            return []
        records = []
        for line in self._journal.read_text().splitlines():
            if line.strip():
                try:
                    records.append(RepairRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue  # a torn write must not take the fallback down
        return records

    def add(self, record: RepairRecord) -> None:
        with self._lock:
            self._journal.parent.mkdir(parents=True, exist_ok=True)
            with self._journal.open("a") as fh:
                fh.write(json.dumps(record.to_dict()) + "\n")
            self._index(record)

    def _index(self, record: RepairRecord) -> None:
        tokens = Counter(tokenize(record.content))
        self._records.append(record)
        self._tokens.append(tokens)
        self._df.update(tokens.keys())
        self._total_len += sum(tokens.values())

    # ---------------------------------------------------------------- queries
    @property
    def size(self) -> int:
        return len(self._records)

    def matching(self, model_key: str | None = None, error_code: str | None = None, unit_id: str | None = None) -> list[RepairRecord]:
        return [
            r
            for r in self._records
            if (not model_key or r.model_key == model_key)
            and (not error_code or r.error_code == error_code)
            and (not unit_id or r.unit_id == unit_id)
        ]

    def search(self, query: str, tags: list[str] | None = None, limit: int = 12) -> list[MemoryHit]:
        with self._lock:
            records, token_rows = list(self._records), list(self._tokens)
        if not records:
            return []
        tags = tags or []
        wanted_model = next((t for t in tags if t.startswith("model:")), None)
        n = len(records)
        avg_len = self._total_len / n
        q_tokens = tokenize(query)

        scored: list[tuple[float, RepairRecord]] = []
        for record, tokens in zip(records, token_rows):
            record_tags = set(record.tags)
            # Keep fallback recall on the equipment family under diagnosis.
            if wanted_model and wanted_model not in record_tags:
                continue
            length = sum(tokens.values())
            score = 0.0
            for term in q_tokens:
                tf = tokens.get(term)
                if not tf:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += idf * tf * (self.K1 + 1) / (tf + self.K1 * (1 - self.B + self.B * length / avg_len))
            for tag in tags:
                if tag in record_tags:
                    score += {"error": 4.0, "unit": 2.0, "model": 1.0}.get(tag.split(":", 1)[0], 0.5)
            if record.outcome_held is True and record.action_category == "field":
                score += 0.5  # verified field knowledge is the most useful thing to surface
            if score > 0:
                scored.append((score, record))

        scored.sort(key=lambda pair: (-pair[0], pair[1].occurred_at))
        top = scored[:limit]
        best = top[0][0] if top else 1.0
        return [MemoryHit.from_record(record, score / best) for score, record in top]

    def known_actions(self, model_key: str, error_code: str) -> list[str]:
        """Distinct actions ever recorded for a model+error (excluding no-defect inspections)."""
        seen: dict[str, None] = {}
        for record in self.matching(model_key, error_code):
            if not record.action_taken.lower().startswith("inspected recalled field finding"):
                seen.setdefault(record.action_taken, None)
        return list(seen)

    def outcome_hits(self, model_key: str, error_code: str) -> list[MemoryHit]:
        """Every recorded outcome for a model and error, for exhaustive fix statistics."""
        return [MemoryHit.from_record(r, 1.0) for r in self.matching(model_key, error_code)]

