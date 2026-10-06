"""Helpers for the Realizer unit tests that compare a moved function or a whole run with the frozen oracle."""
import contextlib
import copy
import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests.regression.realizer import canonical, oracle  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer import realizer as engine  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402

oengine = oracle.engine()


class Opaque:
    """A value no code can serialize or interpret; unlike ``object()`` two instances compare equal and print alike, so a
    result that merely carries it can be compared between the two engines."""
    def __eq__(self, other):
        return isinstance(other, Opaque)

    def __hash__(self):
        return 0

    def __repr__(self):
        return "Opaque()"


class Unprintable:
    """An observation value that cannot be turned into text (``str`` raises); truthy."""
    def __str__(self):
        raise ValueError("unprintable observation value")

    def __eq__(self, other):
        return isinstance(other, Unprintable)

    def __hash__(self):
        return 1


class Untestable:
    """An observation value whose truth cannot be tested (``bool`` raises)."""
    def __bool__(self):
        raise ValueError("observation flag has no truth value")

    def __eq__(self, other):
        return isinstance(other, Untestable)

    def __hash__(self):
        return 2


def outcome(fn: Callable[..., Any], *args: Any, **kwargs: Any):
    """``("ok", value)``, or ``(exception type name, message)`` when *fn* raises."""
    try:
        return "ok", fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - compared, not handled
        return type(exc).__name__, str(exc)


def fixture_package() -> Dict[str, Any]:
    return json.loads(next(canonical.PACKAGES.glob("PKG-*.json")).read_text(encoding="utf-8"))


def tree(out: Path) -> Dict[str, Optional[str]]:
    """Every file (text) and directory (``None``) under *out*, relative: what a run, finished or crashed, left behind."""
    return {p.relative_to(out).as_posix(): (p.read_text(encoding="utf-8") if p.is_file() else None) for p in sorted(out.rglob("*"))}


def events(tree: Mapping[str, Optional[str]]) -> List[str]:
    """The ledger event types of a run tree, in file (append) order."""
    lines = [line for rel, text in tree.items() if rel.startswith("realization/ledger/") and text for line in text.splitlines() if line.strip()]
    return [json.loads(line)["event_type"] for line in lines]


def files(tree: Mapping[str, Optional[str]], folder: str) -> List[str]:
    """The files a run tree holds under ``realization/<folder>/``."""
    return sorted(rel for rel, text in tree.items() if rel.startswith(f"realization/{folder}/") and text is not None)


def ledger_lines(out: Path) -> List[Dict[str, Any]]:
    """The ledger entries under *out*, parsed, in file (append) order."""
    paths = sorted((out / "realization" / "ledger").glob("*.jsonl"))
    return [json.loads(line) for path in paths for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_sequence(module: Any, package: Mapping[str, Any], observations: Sequence[Optional[Mapping[str, Any]]], policy: Any = None):
    """Run each observation through *module*'s ``realize`` into one fresh directory, continuing past a failure.

    Returns ``(outcomes, tree)``: per observation ``("ok", snapshot id)`` or ``(exception type, message)``, and the
    files and directories left behind (so a crash is compared together with the ledger it left half written).
    """
    with tempfile.TemporaryDirectory(prefix="parity-") as tmp:
        out = Path(tmp)
        outcomes: List[Any] = []
        for observation in observations:
            kwargs = {} if policy is None else {"policy": module.RealizerPolicy.from_mapping(policy)}
            result = outcome(module.realize, copy.deepcopy(package), copy.deepcopy(observation), out, **kwargs)
            outcomes.append(("ok", result[1].snapshot_id) if result[0] == "ok" else result)
        return outcomes, tree(out)


STAGES = ("reconciler", "estimator", "reviewer", "packager")


@dataclasses.dataclass
class StagedRun:
    """One realize run of a scenario through both engines: the oracle's ledger lines before it and appended by it,
    each modular stage's ``(args, result)`` calls, and both output trees after it."""
    scenario: str
    index: int
    observation: Optional[Mapping[str, Any]]
    before: List[Dict[str, Any]]
    appended: List[Dict[str, Any]]
    calls: Dict[str, List[Any]]
    modular_tree: Dict[str, Optional[str]]
    oracle_tree: Dict[str, Optional[str]]
    replay_through: Optional[str] = None


def _capturing(original: Callable[..., Any], calls: List[Any]) -> Callable[..., Any]:
    def run(*args: Any) -> Any:
        result = original(*args)
        calls.append((args, result))
        return result
    return run


def staged_scenario_runs() -> Iterator[StagedRun]:
    """Every run of every scenario, oracle and modular engine side by side, with the modular stages' calls captured."""
    for name in canonical.scenario_names():
        scenario = canonical.load_scenario(name)
        package = scenario["package_doc"]
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            oracle_out, modular_out = Path(a), Path(b)
            for index, step in enumerate(scenario["runs"]):
                observation = step.get("observation")
                policy = step.get("policy")
                before = ledger_lines(oracle_out)
                oengine.realize(copy.deepcopy(package), copy.deepcopy(observation), oracle_out, replay_through=step.get("replay_through"),
                                policy=None if policy is None else oengine.RealizerPolicy.from_mapping(policy))
                appended = ledger_lines(oracle_out)[len(before):]
                calls: Dict[str, List[Any]] = {stage: [] for stage in STAGES}
                with contextlib.ExitStack() as stack:
                    for stage in STAGES:
                        module = getattr(engine, stage)
                        stack.enter_context(mock.patch.object(module, "run", _capturing(module.run, calls[stage])))
                    engine.realize(copy.deepcopy(package), copy.deepcopy(observation), modular_out, replay_through=step.get("replay_through"),
                                   policy=None if policy is None else engine.RealizerPolicy.from_mapping(policy))
                yield StagedRun(name, index, observation, before, appended, calls, tree(modular_out), tree(oracle_out), step.get("replay_through"))


VALIDATION_SLOTS = ("VALIDATION_READY", "VALIDATION_STARTED", "VALIDATION_COMPLETED")
MEASUREMENT_SLOTS = ("COUNTERFACTUAL_RECORDED", "ECONOMIC_MEASUREMENT_RECORDED", "BILLING_CORRECTION_RECORDED")


def measurement_section_entries(context: Any, analysis: Any, reconciliation: Any, review: Any) -> List[Any]:
    """The validation and measurement slots of a reviewed run as ledger entries, assembled here independently of the
    engine, in the order the single-file Realizer records them (ready, started, completed, counterfactual,
    measurement, billing correction)."""
    package, rpkg, observed = context.request.package, context.realization_package_id, analysis.observation
    app_id, episode, estimate = reconciliation.active.application_episode_id, review.validation, review.estimate
    val_id = episode.validation_episode_id

    def entry(event_type: Any, payload: Any, **ids: Any) -> Any:
        return engine.make_ledger_entry(package=package, rpkg_id=rpkg, event_type=event_type, effective_at_utc=observed.observed_at_utc,
                                        recorded_at_utc=observed.recorded_at_utc, payload=payload, evidence_refs=[observed.evidence_id],
                                        application_episode_id=app_id, validation_episode_id=val_id, **ids)

    out = [entry(rc.EventType.VALIDATION_READY, episode.ready_payload), entry(rc.EventType.VALIDATION_STARTED, episode.started_payload),
           entry(rc.EventType.VALIDATION_COMPLETED, episode.completed_payload)]
    if estimate.counterfactual.payload is not None:
        out.append(entry(rc.EventType.COUNTERFACTUAL_RECORDED, estimate.counterfactual.payload))
    m = estimate.measurement
    if m is not None:
        payload = {"measurement_id": m.measurement_id, "window_start_utc": m.window_start_utc, "window_end_utc": m.window_end_utc,
                   "counterfactual_id": m.counterfactual_id, "counterfactual_method": m.counterfactual_method,
                   "recognition_status": review.recognition_status, "economics": m.economics, "attribution": m.attribution,
                   "realization_state": m.realization_state, "mature_ttm_realized_savings": m.mature_ttm_realized_savings,
                   "correction_status": m.correction_status}
        out.append(entry(rc.EventType.ECONOMIC_MEASUREMENT_RECORDED, payload, measurement_id=m.measurement_id))
        if estimate.correction is not None:
            out.append(entry(rc.EventType.BILLING_CORRECTION_RECORDED, estimate.correction.payload, measurement_id=m.measurement_id,
                             corrects_entry_id=estimate.correction.corrects_entry_id))
    return out


def as_line(entry: Any) -> Dict[str, Any]:
    """A ledger entry as the parsed line the ledger file holds."""
    return json.loads(rc.canonical_json(entry.to_dict()))


MEASUREMENT_SECTION = set(VALIDATION_SLOTS) | set(MEASUREMENT_SLOTS)


def compare_measurement_section(test: Any, run: StagedRun) -> Optional[Tuple[Any, ...]]:
    """Assert (on *test*) that the validation and measurement entries the oracle appended in *run* are those the modular
    stages decided, with per-append duplicate suppression, and that the Estimator and the Reviewer ran only when
    validation was due. Returns ``(context, analysis, governance, reconciliation, estimate, review)``, or ``None``."""
    section = [line for line in run.appended if line["event_type"] in MEASUREMENT_SECTION]
    if not run.calls["estimator"]:
        test.assertEqual(run.calls["reviewer"], [])
        test.assertEqual(section, [], f"{run.scenario} run {run.index}")
        return None
    ((args, estimate),) = run.calls["estimator"]
    ((_, review),) = run.calls["reviewer"]
    context, analysis, governance, reconciliation = args
    test.assertTrue(reconciliation.validation_due)
    test.assertIsNone(estimate.halt)
    test.assertIsNone(review.halt)
    test.assertIs(review.estimate, estimate)
    existing = {line["ledger_entry_id"] for line in run.before} | {line["ledger_entry_id"] for line in run.appended if line["event_type"] not in MEASUREMENT_SECTION}
    expected = []
    for entry in measurement_section_entries(context, analysis, reconciliation, review):
        if entry.ledger_entry_id in existing:
            continue
        existing.add(entry.ledger_entry_id)
        expected.append(as_line(entry))
    test.assertEqual(section, expected, f"{run.scenario} run {run.index}")
    return context, analysis, governance, reconciliation, estimate, review
