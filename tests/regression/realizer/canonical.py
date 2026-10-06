"""Canonical Realizer outputs for the stored baseline (docs/architecture/realizer/repo-reorg-design.md 21.4, Phase 0).

One place defines what "the same realization output" means, for ``tests/fixtures/realizer/expected/``,
``tests/regression/realizer/test_stored_baseline.py``, ``scripts/update_realizer_goldens.py`` and
``scripts/compare_realizer_with_ref.py``.

A *scenario* (``tests/fixtures/realizer/observations/<name>.json``) names a frozen Publisher package under
``tests/fixtures/realizer/packages/`` and a sequence of realize runs into one output directory, each with an
observation (or none), an optional Realizer policy mapping and an optional ``replay_through`` cut. The canonical output
of a scenario is a map of relative path -> text:

- ``realization/{ledger,packages,snapshots,evidence,regeneration}/*``: every file the engine wrote, byte for byte
  (RLE JSONL, RPKG, RSNAP, REVID evidence, RERUN regeneration request);
- ``runs.json``: per run, the returned snapshot's RPKG/RSNAP IDs and its legacy Reporter compatibility projection;
- ``report.json``: the Realizer Workbook Extension projection (summary and timeline rows) of the final snapshot.

Variable fields: the engine reads no wall clock (ledger times, ``as_of_utc`` and lineage come from the package and
the observation) and writes no paths into its files, so the only run-dependent value is the output directory itself.
``normalize`` replaces it with ``<out>`` should it ever appear; running a scenario twice into different directories
yields identical maps (``test_stored_baseline``). There is no ``.xlsx`` in the baseline: the Realizer's workbook
projection is the ``report.json`` above, and the single-file renderer was dropped (see the parity record).
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

FIXTURES = ROOT / "tests" / "fixtures" / "realizer"
PACKAGES = FIXTURES / "packages"
OBSERVATIONS = FIXTURES / "observations"
EXPECTED = FIXTURES / "expected"
OUT_PLACEHOLDER = "<out>"


def dumps(value: Any) -> str:
    """Serialization of every derived expectation (indent 2, sorted keys, trailing newline; same as the engine's writer)."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_names() -> List[str]:
    return sorted(p.stem for p in OBSERVATIONS.glob("*.json"))


def load_scenario(name: str) -> Dict[str, Any]:
    """The scenario with its ``package`` file name resolved to the parsed frozen PKG (as ``package_doc``)."""
    scenario = json.loads((OBSERVATIONS / f"{name}.json").read_text(encoding="utf-8"))
    scenario["package_doc"] = json.loads((PACKAGES / scenario["package"]).read_text(encoding="utf-8"))
    return scenario


# ---------------------------------------------------------------------------
# Engines: the modular engine, and the single-file API of old git refs
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Engine:
    """What the baseline needs from a Realizer implementation.

    ``realize(package, observation, out, policy=, replay_through=)`` returns a snapshot object with ``to_dict()``;
    ``policy(mapping)`` builds the engine's policy from a scenario mapping (``None`` = defaults);
    ``compatibility(snapshot)`` is the legacy Reporter compatibility projection;
    ``report(snapshot, out)`` is the workbook-extension projection of a snapshot and its ledger, as primitives.
    """

    name: str
    realize: Callable[..., Any]
    policy: Callable[[Optional[Mapping[str, Any]]], Any]
    compatibility: Callable[[Any], Mapping[str, Any]]
    report: Callable[[Any, Path], Mapping[str, Any]]


def engine_from_functions(name: str, *, realize: Callable[..., Any], policy_class: Any, legacy_compatibility: Callable[..., Any],
                          load_ledger: Callable[..., Any], ledger_path: Callable[..., Any], project_realizer_workbook: Callable[..., Any]) -> Engine:
    """An engine built from the functions of one implementation (the capability modules, or the single-file module)."""

    def report(snapshot: Any, out: Path) -> Mapping[str, Any]:
        entries = load_ledger(ledger_path(out, snapshot.realization_package_id))
        return dataclasses.asdict(project_realizer_workbook([snapshot], {snapshot.realization_package_id: entries}))

    return Engine(
        name=name,
        realize=realize,
        policy=lambda mapping: None if mapping is None else policy_class.from_mapping(mapping),
        compatibility=legacy_compatibility,
        report=report,
    )


SRC = ROOT / "src" / "realizer"


def modular(src: Optional[Path] = None):
    """The modular engine module ``realizer.realizer``, checked to resolve to ``src/realizer`` (this repository's, or *src*).

    Guards against a stray top-level ``realizer`` (an installed copy, a root folder, a test package on ``sys.path``)
    shadowing the package under test.
    """
    expected = (src or SRC).resolve()
    module = importlib.import_module("realizer.realizer")
    path = Path(module.__file__).resolve()
    if path.parent != expected:
        raise ImportError(f"realizer.realizer resolved to {path}, not {expected}")
    return module


def modular_engine(src: Optional[Path] = None) -> Engine:
    """The modular engine (``src/realizer``) as a baseline ``Engine``, built from the capability modules."""
    facade = modular(src)
    from realizer.contracts.report import project_realizer_workbook
    from realizer.governor.policy import RealizerPolicy
    from realizer.packager.ledger import ledger_path, load_ledger
    from realizer.reporter.reporter import legacy_compatibility

    return engine_from_functions("modular realizer", realize=facade.realize, policy_class=RealizerPolicy,
                                 legacy_compatibility=legacy_compatibility, load_ledger=load_ledger, ledger_path=ledger_path,
                                 project_realizer_workbook=project_realizer_workbook)


def single_file_engine(module: Any, workbook_contract: Any) -> Engine:
    """An engine from a module with the single-file API (``realize``, ``RealizerPolicy``, ``legacy_compatibility``,
    ``load_ledger``, ``ledger_path``) and its workbook contract module: the layout of ``legacy/realizer_single_file/``
    in the git tag ``realizer-reorg-oracle-1.0.0``."""
    return engine_from_functions("single-file realizer", realize=module.realize, policy_class=module.RealizerPolicy,
                                 legacy_compatibility=module.legacy_compatibility, load_ledger=module.load_ledger,
                                 ledger_path=module.ledger_path, project_realizer_workbook=workbook_contract.project_realizer_workbook)


# ---------------------------------------------------------------------------
# Canonical output
# ---------------------------------------------------------------------------

def normalize(text: str, out: Path) -> str:
    """Replace the run's output directory (the only run-dependent value) with a placeholder."""
    return text.replace(str(out), OUT_PLACEHOLDER)


def run_scenario(engine: Engine, scenario: Mapping[str, Any], out: Path) -> Dict[str, str]:
    """Run every step of *scenario* into *out* and return its canonical file map (relative path -> text)."""
    package = scenario["package_doc"]
    runs = []
    snapshot = None
    for step in scenario["runs"]:
        snapshot = engine.realize(package, step.get("observation"), out,
                                  policy=engine.policy(step.get("policy")),
                                  replay_through=step.get("replay_through"))
        runs.append({
            "realization_package_id": snapshot.realization_package_id,
            "snapshot_id": snapshot.snapshot_id,
            "legacy_compatibility": engine.compatibility(snapshot),
        })
    files: Dict[str, str] = {}
    for path in sorted((out / "realization").rglob("*")):
        if path.is_file():
            files[path.relative_to(out).as_posix()] = normalize(path.read_text(encoding="utf-8"), out)
    files["runs.json"] = normalize(dumps(runs), out)
    files["report.json"] = normalize(dumps(engine.report(snapshot, out)), out)
    return files


def produce(engine: Engine, name: str) -> Dict[str, str]:
    """Canonical output of scenario *name* run by *engine* in a fresh temporary directory."""
    with tempfile.TemporaryDirectory(prefix=f"realizer-{name}-") as tmp:
        return run_scenario(engine, load_scenario(name), Path(tmp))


def stored_expected(name: str) -> Dict[str, str]:
    base = EXPECTED / name
    return {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8") for p in sorted(base.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# Semantic comparison
# ---------------------------------------------------------------------------

def _final_snapshot(files: Mapping[str, str]) -> Dict[str, Any]:
    rpkg = [rel for rel in files if rel.startswith("realization/packages/")]
    return json.loads(files[rpkg[0]]) if len(rpkg) == 1 else {}


def _ledger(files: Mapping[str, str]) -> List[Dict[str, Any]]:
    rel = [r for r in files if r.startswith("realization/ledger/")]
    return [json.loads(line) for r in rel for line in files[r].splitlines() if line.strip()]


ASPECTS: Dict[str, Callable[[Mapping[str, str]], Any]] = {
    "lifecycle state": lambda f: _final_snapshot(f).get("current_state"),
    "blockers": lambda f: (_final_snapshot(f).get("attribution") or {}).get("hard_disqualifiers"),
    "plan/step status": lambda f: {k: (_final_snapshot(f).get("application") or {}).get(k)
                                   for k in ("state", "plan_progress", "application_scope", "applied_step_refs",
                                             "implementation_conformance", "modification_relation", "scenario_id", "step")},
    "realized economics": lambda f: _final_snapshot(f).get("economics"),
    "attribution": lambda f: _final_snapshot(f).get("attribution"),
    "review result": lambda f: _final_snapshot(f).get("validation"),
    "RLE sequence": lambda f: [(e["event_type"], e["ledger_entry_id"]) for e in _ledger(f)],
    "RSNAP": lambda f: {r: f[r] for r in f if r.startswith("realization/snapshots/")},
    "RPKG": lambda f: {r: f[r] for r in f if r.startswith("realization/packages/")},
    "report": lambda f: (f.get("report.json"), f.get("runs.json")),
}


def semantic_diff(label: str, expected: Mapping[str, str], actual: Mapping[str, str]) -> List[str]:
    """Differences named by aspect (lifecycle state, blockers, ... report), then any remaining file-level difference."""
    problems = []
    for aspect, view in ASPECTS.items():
        try:
            a, b = view(expected), view(actual)
        except (KeyError, ValueError, TypeError) as exc:  # malformed candidate output
            problems.append(f"{label}: {aspect}: cannot read output ({exc})")
            continue
        if a != b:
            problems.append(f"{label}: {aspect} differs\n  expected: {json.dumps(a, sort_keys=True, default=str)[:600]}"
                            f"\n  actual:   {json.dumps(b, sort_keys=True, default=str)[:600]}")
    for rel in sorted(set(expected) | set(actual)):
        if rel not in expected or rel not in actual:
            problems.append(f"{label}: {rel} only in {'expected' if rel in expected else 'actual'}")
        elif expected[rel] != actual[rel]:
            problems.append(f"{label}: {rel} differs")
    return problems
