"""Realization ledger, read side: where a package's ledger lives, loading it and finding its latest event.

Moved unchanged from the single-file Realizer. The Analyzer reads prior history through these (read only); appending
stays in the engine until the Packager is extracted.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Sequence

from realizer.contracts import realization as rc


def ledger_path(output_dir: Path, rpkg_id: str) -> Path:
    return output_dir / "realization" / "ledger" / f"{rpkg_id}.jsonl"


def load_ledger(path: Path) -> List[rc.RealizationLedgerEntry]:
    if not path.exists():
        return []
    entries: List[rc.RealizationLedgerEntry] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        raw["event_type"] = rc.EventType(raw["event_type"])
        entry = rc.RealizationLedgerEntry(**raw)
        entry.validate()
        entries.append(entry)
    # recorded time governs projection order; RLE id is deterministic tie-break.
    return sorted(entries, key=lambda x: (x.recorded_at_utc, x.ledger_entry_id))


def latest_event(entries: Sequence[rc.RealizationLedgerEntry], *types: rc.EventType) -> Optional[rc.RealizationLedgerEntry]:
    allowed = {x.value for x in types}
    xs = [e for e in entries if e.event_type.value in allowed]
    return xs[-1] if xs else None
