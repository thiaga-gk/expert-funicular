"""Canonical Realizer outputs for the single-file compatibility baseline (realizer-repo-reorg-design.md 21.4, Phase 0).

One place defines what "the same realization output" means, for ``tests/fixtures/realizer/expected/``,
``tests/regression/realizer/test_single_file_compatibility.py``, ``scripts/update_realizer_goldens.py`` and
``scripts/compare_realizer_single_file.py``.

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
yields identical maps (``test_single_file_compatibility``). The workbook ``.xlsx`` is not part of the baseline: it
needs the proprietary ``artifact_tool`` runtime and ``RecommendationPackage`` objects of the retired single-file
publisher, which the frozen JSON fixtures cannot rebuild.
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
DEFAULT_CANDIDATE = "realizer.realizer"


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
# Engines: the frozen oracle and a pluggable candidate
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


def engine_from_module(name: str, module: Any, workbook_contract: Any) -> Engine:
    """An engine built from a module with the single-file API (``realize``, ``RealizerPolicy``, ``legacy_compatibility``,
    ``load_ledger``, ``ledger_path``) and a workbook contract module with ``project_realizer_workbook``."""

    def report(snapshot: Any, out: Path) -> Mapping[str, Any]:
        entries = module.load_ledger(module.ledger_path(out, snapshot.realization_package_id))
        projection = workbook_contract.project_realizer_workbook([snapshot], {snapshot.realization_package_id: entries})
        return dataclasses.asdict(projection)

    return Engine(
        name=name,
        realize=module.realize,
        policy=lambda mapping: None if mapping is None else module.RealizerPolicy.from_mapping(mapping),
        compatibility=module.legacy_compatibility,
        report=report,
    )


def oracle_engine() -> Engine:
    from tests.regression.realizer import oracle

    return engine_from_module("single-file oracle", oracle.engine(), oracle.realizer_workbook_contract())


def candidate_engine(spec: str = DEFAULT_CANDIDATE) -> Engine:
    """Resolve a candidate lazily. ``module:attr`` names an ``Engine`` (or a zero-argument factory returning one);
    a bare module must expose ``ENGINE`` or the single-file API, with ``project_realizer_workbook`` either in the
    module itself or in its package's ``contracts.report`` (the modular layout)."""
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    target = getattr(module, attr or "ENGINE", None)
    if callable(target) and not isinstance(target, Engine):
        target = target()
    if isinstance(target, Engine):
        return target
    if attr:
        raise TypeError(f"{spec} is not a Realizer Engine")
    workbook = module
    if not hasattr(module, "project_realizer_workbook"):
        workbook = importlib.import_module(module_name.split(".")[0] + ".contracts.report")
    name = "modular realizer" if module_name == DEFAULT_CANDIDATE else module_name
    return engine_from_module(name, module, workbook)


SRC = ROOT / "src" / "realizer"


def modular():
    """The modular engine module ``realizer.realizer``, checked to resolve to this repository's ``src/realizer``.

    Guards against a stray top-level ``realizer`` (an installed copy, a root folder, a test package on ``sys.path``)
    shadowing the package under test. The oracle is loaded under its private name and can never satisfy this import.
    """
    module = importlib.import_module("realizer.realizer")
    path = Path(module.__file__).resolve()
    if path.parent != SRC.resolve():
        raise ImportError(f"realizer.realizer resolved to {path}, not {SRC}")
    return module


def modular_engine() -> Engine:
    """The modular engine (``src/realizer``) as a baseline ``Engine``."""
    modular()
    return candidate_engine(DEFAULT_CANDIDATE)


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
