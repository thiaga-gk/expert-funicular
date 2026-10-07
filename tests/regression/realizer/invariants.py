"""Ledger invariants of a Realizer state (docs/proposals/realizer/golden-scenarios.md section 4), checked over the files a
run leaves, whatever produced them.

A state is read as a map of relative path -> text under ``realization/`` (``read_state``; the stored-baseline file maps
of ``canonical.py`` have the same form). The checks:

1. Append-only: every ledger of the state before a run is a byte prefix of the same file after it, other lineages'
   ledgers included.
2. Identity: entry ids are unique, and every entry of a ledger names one workspace and one warehouse.
3. Lineage id: the RPKG id derives from the PKG id (``make_rpkg_id``) for every entry, and matches the ledger file;
   every realization package names its own ledger.
4. References: ``caused_by_entry_id`` and ``corrects_entry_id`` resolve to earlier entries of the same ledger.
5. Cross-lineage ids: a successor, an owner (claimed-by), a rollback or a lineage-derived replacement names a lineage
   present in the state (a source package under ``realization/sources/`` and its ledger), with its RPKG id; a
   replacement the caller named is checked for its form only (``PKG-`` and 24 hex digits, not the package itself).
6. Replay: each ledger replayed (cut at ``replay_through`` for the run's own lineage when the run used it) projects the
   realization package written for it: the same snapshot id and the same canonical content.
7. Ownership: the owned application intervals per workspace, warehouse and knob are half-open ``[from, to)`` and do
   not overlap. An interval starts at the application's effective time and ends at the first drift or observed
   rollback that closed its episode; an application another lineage claims (``APPLICATION_CLAIMED_BY_OTHER_LINEAGE``
   for that episode and knob) is not owned.

``violations`` returns what failed (empty when the state holds); ``assert_state`` raises with the list; ``RunChecker``
checks after each run into one directory, the state after the previous run being the state before the next.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from tests.regression.realizer import canonical

canonical.modular()  # src/realizer, never a shadowing copy
from realizer.contracts import realization as rc  # noqa: E402
from realizer.contracts.realization import make_rpkg_id  # noqa: E402
from realizer.packager.ledger import in_ledger_order  # noqa: E402
from realizer.packager.snapshot import project_snapshot  # noqa: E402

LEDGER = "realization/ledger/"
PACKAGES = "realization/packages/"
SOURCES = "realization/sources/"
PKG_ID = re.compile(r"^PKG-[0-9A-F]{24}$")
APPLICATIONS = {"APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED"}
APPLIED = {"EXACT", "MODIFIED"}


def read_state(*roots: Path) -> Dict[str, str]:
    """The ``realization/`` files (JSON and JSONL) under each root, merged: a fleet run's state and run folder, or one
    output directory."""
    files: Dict[str, str] = {}
    for root in roots:
        for path in sorted((Path(root) / "realization").rglob("*")):
            if path.is_file() and path.suffix in {".json", ".jsonl"}:
                files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return files


def ledgers(files: Mapping[str, Optional[str]]) -> Dict[str, str]:
    """{RPKG id: ledger text} of a state."""
    return {rel[len(LEDGER):-len(".jsonl")]: text for rel, text in files.items()
            if rel.startswith(LEDGER) and rel.endswith(".jsonl") and text is not None}


def _utc(value: Any) -> dt.datetime:
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _entry(raw: Mapping[str, Any]) -> rc.RealizationLedgerEntry:
    return rc.RealizationLedgerEntry(**{**raw, "event_type": rc.EventType(raw["event_type"])})


def _sources(files: Mapping[str, Optional[str]], packages: Iterable[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    out = {p["recommendation_package_id"]: p for p in packages}
    for rel, text in files.items():
        if rel.startswith(SOURCES) and rel.endswith(".json") and text is not None:
            package = json.loads(text)
            out.setdefault(package["recommendation_package_id"], package)
    return out


def _step_knob(package: Mapping[str, Any], scenario_id: Any, step: Any) -> Optional[str]:
    scenario = next((s for s in package.get("scenarios") or [] if s.get("scenario_id") == scenario_id), None) or {}
    found = next((s for s in scenario.get("execution_plan") or [] if s.get("step_type") == "CANDIDATE" and s.get("step") == step), None)
    return None if found is None else found.get("knob")


def owned_intervals(rpkg: str, lines: Sequence[Mapping[str, Any]], package: Optional[Mapping[str, Any]]) -> List[Tuple[str, str, str, Any, Any, str]]:
    """The owned application intervals of one ledger: (workspace, warehouse, knob, start, end or None, episode)."""
    ordered = sorted(lines, key=lambda e: _utc(e["recorded_at_utc"]))  # recorded order, append order at a tie
    first: Dict[str, Mapping[str, Any]] = {}
    closers: Dict[str, List[Mapping[str, Any]]] = {}
    for e in ordered:
        episode, kind = e.get("application_episode_id"), e["event_type"]
        if kind in APPLICATIONS and episode and episode not in first:
            first[episode] = e
            closers[episode] = []
        elif kind == "DRIFT_DETECTED":  # a drift ends every episode still open, and its own
            for ep in [ep for ep in first if not closers[ep]] + ([episode] if episode in first else []):
                closers[ep].append(e)
        elif kind == "ROLLBACK_OBSERVED":
            for ep in {episode, *(e["payload"].get("rolled_back_episode_ids") or [])}:
                if ep in first:
                    closers[ep].append(e)
    claimed = {(e.get("application_episode_id"), e["payload"].get("knob")) for e in lines
               if e["event_type"] == "APPLICATION_CLAIMED_BY_OTHER_LINEAGE"}
    out = []
    for episode, e in first.items():
        payload = e["payload"]
        if not payload.get("authority_eligible"):
            continue
        if payload.get("application_scope") == "SCENARIO":
            knobs = [_step_knob(package or {}, r.get("scenario_id"), r.get("step")) for r in payload.get("applied_step_refs") or []
                     if r.get("conformance") in APPLIED]
        else:
            knobs = [payload.get("knob")] if payload.get("implementation_conformance") in APPLIED else []
        start = _utc(payload.get("effective_from_utc") or e["effective_at_utc"])
        close = closers[episode][0] if closers[episode] else None
        end = None if close is None else _utc(close["payload"].get("effective_from_utc") or close["effective_at_utc"])
        for knob in knobs:
            if (episode, knob) not in claimed:
                out.append((e["workspace_id"], e["warehouse_id"], str(knob), start, end, f"{rpkg}/{episode}"))
    return out


def violations(files: Mapping[str, Optional[str]], *, before: Optional[Mapping[str, str]] = None,
               packages: Iterable[Mapping[str, Any]] = (), replay_through: Optional[str] = None,
               replay_lineage: Optional[str] = None) -> List[str]:
    """The invariants a state breaks (empty when it holds).

    *files*: the state after the run (``read_state``); *before*: its ledgers before the run ({RPKG id: text},
    ``ledgers``), none for a fresh state; *packages*: source packages the map does not hold (a stored map leaves out
    the source copies); *replay_through* and *replay_lineage*: the run's cut and its own lineage's RPKG id.
    """
    problems: List[str] = []
    after = ledgers(files)
    sources = _sources(files, packages)
    present = {pkg: make_rpkg_id(pkg) for pkg in sources if make_rpkg_id(pkg) in after}

    # (1) append-only, every lineage of the state
    for rpkg, text in (before or {}).items():
        if rpkg not in after:
            problems.append(f"(1) {rpkg}: the ledger is gone after the run")
        elif not after[rpkg].encode("utf-8").startswith(text.encode("utf-8")):
            problems.append(f"(1) {rpkg}: the ledger before the run is not a prefix of it after")

    parsed: Dict[str, List[Dict[str, Any]]] = {}
    seen_ids: Dict[str, str] = {}
    for rpkg, text in sorted(after.items()):
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        parsed[rpkg] = lines
        # (2) unique ids, one workspace and warehouse
        for e in lines:
            if e["ledger_entry_id"] in seen_ids:
                problems.append(f"(2) {rpkg}: entry {e['ledger_entry_id']} repeats an entry of {seen_ids[e['ledger_entry_id']]}")
            seen_ids[e["ledger_entry_id"]] = rpkg
        identities = {(e["workspace_id"], e["warehouse_id"]) for e in lines}
        if len(identities) != 1:
            problems.append(f"(2) {rpkg}: {len(identities)} workspace/warehouse identities {sorted(identities)}")
        # (3) the RPKG id derives from the PKG id and names the file
        for e in lines:
            if e["realization_package_id"] != rpkg or make_rpkg_id(e["recommendation_package_id"]) != rpkg:
                problems.append(f"(3) {rpkg}: entry {e['ledger_entry_id']} names {e['realization_package_id']} of "
                                f"{e['recommendation_package_id']} (RPKG {make_rpkg_id(e['recommendation_package_id'])})")
        # (4) references resolve to earlier entries of the same ledger
        earlier: set = set()
        for e in lines:
            for ref in ("caused_by_entry_id", "corrects_entry_id"):
                if e.get(ref) is not None and e[ref] not in earlier:
                    problems.append(f"(4) {rpkg}: {ref} {e[ref]} of {e['ledger_entry_id']} is not an earlier entry of the ledger")
            earlier.add(e["ledger_entry_id"])

    # (5) cross-lineage ids name a lineage of the state
    def lineage_ref(rpkg: str, e: Mapping[str, Any], pkg_key: str, rpkg_key: Optional[str]) -> None:
        pkg = e["payload"].get(pkg_key)
        if pkg not in present:
            problems.append(f"(5) {rpkg}: {e['event_type']} {pkg_key} {pkg} names no lineage of the state")
        elif rpkg_key is not None and e["payload"].get(rpkg_key) != present[pkg]:
            problems.append(f"(5) {rpkg}: {e['event_type']} {rpkg_key} {e['payload'].get(rpkg_key)} is not {present[pkg]}")

    for rpkg, lines in parsed.items():
        for e in lines:
            kind, payload = e["event_type"], e["payload"]
            if kind == "SUCCESSOR_PACKAGE_LINKED":
                lineage_ref(rpkg, e, "successor_package_id", "successor_realization_package_id")
            elif kind == "APPLICATION_CLAIMED_BY_OTHER_LINEAGE":
                lineage_ref(rpkg, e, "owner_recommendation_package_id", "owner_realization_package_id")
                owner = parsed.get(payload.get("owner_realization_package_id")) or []
                if payload.get("owner_application_episode_id") not in {x.get("application_episode_id") for x in owner}:
                    problems.append(f"(5) {rpkg}: owner episode {payload.get('owner_application_episode_id')} is not in the owner's ledger")
            elif kind == "ROLLBACK_OBSERVED" and "rollback_recommendation_package_id" in payload:  # a later package's rollback
                lineage_ref(rpkg, e, "rollback_recommendation_package_id", "rollback_realization_package_id")
            elif kind in ("SOURCE_PACKAGE_SUPERSEDED", "REPLACEMENT_PACKAGE_LINKED"):
                if "replacement_realization_package_id" in payload:  # decided across lineages
                    lineage_ref(rpkg, e, "replacement_package_id", "replacement_realization_package_id")
                elif not PKG_ID.match(str(payload.get("replacement_package_id"))) or payload.get("replacement_package_id") == e["recommendation_package_id"]:
                    problems.append(f"(5) {rpkg}: {kind} names {payload.get('replacement_package_id')!r}, not another package id")

    # (6) the ledger replayed projects the written realization package
    for rpkg, lines in parsed.items():
        rel = f"{PACKAGES}{rpkg}.json"
        if rel not in files:
            problems.append(f"(6) {rpkg}: no realization package written for the ledger")
            continue
        written = json.loads(files[rel])
        if written.get("realization_package_id") != rpkg:
            problems.append(f"(6) {rel} names {written.get('realization_package_id')}")
        package = sources.get(lines[0]["recommendation_package_id"]) if lines else None
        if package is None:
            problems.append(f"(6) {rpkg}: no source package to replay the ledger with")
            continue
        entries = in_ledger_order([_entry(e) for e in lines])
        if replay_through and rpkg == replay_lineage:
            entries = [e for e in entries if _utc(e.recorded_at_utc) <= _utc(replay_through)]
        replayed = project_snapshot(package, entries)
        if replayed.snapshot_id != written.get("snapshot_id"):
            problems.append(f"(6) {rpkg}: the ledger replays to {replayed.snapshot_id}, the package written is {written.get('snapshot_id')}")
        elif json.loads(rc.canonical_json(replayed.to_dict())) != written:
            problems.append(f"(6) {rpkg}: the ledger replays to other content than the package written")
    for rel in files:
        if rel.startswith(PACKAGES) and rel[len(PACKAGES):-len(".json")] not in parsed:
            problems.append(f"(6) {rel}: a realization package with no ledger")

    # (7) owned intervals per workspace, warehouse and knob: half-open, never overlapping
    by_knob: Dict[Tuple[str, str, str], List[Tuple[Any, Any, str]]] = {}
    for rpkg, lines in parsed.items():
        package = sources.get(lines[0]["recommendation_package_id"]) if lines else None
        for ws, wh, knob, start, end, who in owned_intervals(rpkg, lines, package):
            if end is not None and not start < end:
                problems.append(f"(7) {who}: {knob} interval [{start}, {end}) is empty or reversed")
                continue
            by_knob.setdefault((ws, wh, knob), []).append((start, end, who))
    for key, intervals in sorted(by_knob.items()):
        for i, (s1, e1, w1) in enumerate(intervals):
            for s2, e2, w2 in intervals[i + 1:]:
                if (e2 is None or s1 < e2) and (e1 is None or s2 < e1):
                    problems.append(f"(7) {key}: {w1} [{s1}, {e1}) overlaps {w2} [{s2}, {e2})")
    return problems


def assert_state(files: Mapping[str, Optional[str]], label: str = "state", **kwargs: Any) -> None:
    """Raise ``AssertionError`` listing every invariant the state breaks."""
    problems = violations(files, **kwargs)
    if problems:
        raise AssertionError(f"{label}: ledger invariants broken:\n  " + "\n  ".join(problems))


class RunChecker:
    """Checks a state after each run into it, from a fresh state: the state after one run is the state before the next.

    ``RunChecker(label)(package, out, replay_through=None, state=None)`` after each run; *state* is the state root when it
    is not *out* (a fleet run's state and run folder). *before* is the state's ledgers before the first run, when the
    state is not fresh (``ledgers(read_state(root))``)."""

    def __init__(self, label: str = "state", packages: Iterable[Mapping[str, Any]] = (), before: Optional[Mapping[str, str]] = None):
        self.label = label
        self.packages = list(packages)
        self.before: Dict[str, str] = dict(before or {})
        self.runs = 0

    def __call__(self, package: Optional[Mapping[str, Any]], out: Path, *, replay_through: Optional[str] = None,
                 state: Optional[Path] = None) -> None:
        files = read_state(*([state] if state is not None else []), out)
        lineage = make_rpkg_id(package["recommendation_package_id"]) if package is not None else None
        assert_state(files, f"{self.label} run {self.runs}", before=self.before, packages=self.packages,
                     replay_through=replay_through, replay_lineage=lineage)
        self.before = ledgers(files)
        self.runs += 1
