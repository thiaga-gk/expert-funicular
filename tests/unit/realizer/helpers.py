"""Helpers shared by the Realizer unit tests: values that cannot be serialized, run trees, and staged scenario runs."""
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
from tests.regression.realizer import canonical  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer import realizer as _facade  # noqa: E402
from realizer.contracts import realization as rc  # noqa: E402
from realizer.estimator import estimator  # noqa: E402
from realizer.packager import packager  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402
from realizer.reviewer import reviewer  # noqa: E402
from realizer.governor import policy as policy_module  # noqa: E402
from realizer.packager import ledger as ledger_module  # noqa: E402


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


def with_quality(observation: Mapping[str, Any], **inputs: Any) -> Dict[str, Any]:
    """*observation* with some of its ``financial_quality`` inputs replaced (the quality inputs are nested in 2.0.0)."""
    return {**observation, "financial_quality": {**observation["financial_quality"], **inputs}}


def correction(**patch: Any) -> Dict[str, Any]:
    """A complete billing correction (format 2.0.0: every key present)."""
    return {"key": "c1", "type": "RESTATEMENT", "version": 2, "reason": None, "corrects_entry_id": None, **patch}


def outcome(fn: Callable[..., Any], *args: Any, **kwargs: Any):
    """``("ok", value)``, or ``(exception type name, message)`` when *fn* raises."""
    try:
        return "ok", fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - compared, not handled
        return type(exc).__name__, str(exc)


def fixture_package(name: str = "inplace-size-canary") -> Dict[str, Any]:
    """A Reporter JSON Contract 3.1.0 fixture package (``tests/fixtures/realizer/packages/<name>.json``), parsed."""
    return json.loads((canonical.PACKAGES / f"{name}.json").read_text(encoding="utf-8"))


def migration_scenario_id(package: Mapping[str, Any]) -> str:
    """The id of the package's standalone warehouse-type migration alternative."""
    return next(s["scenario_id"] for s in package["scenarios"] if s["scenario_type"] == "WAREHOUSE_TYPE_MIGRATION")


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


def compact_tree(run_tree: Mapping[str, Optional[str]]) -> Dict[str, Any]:
    """A run tree as compact data for a stored expectation: the ledger event types in append order, the directories, and
    the SHA-256 (first 16 hex digits) of every file."""
    import hashlib
    return {
        "events": events(run_tree),
        "directories": [rel for rel, text in run_tree.items() if text is None],
        "files": {rel: hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] for rel, text in run_tree.items() if text is not None},
    }


@contextlib.contextmanager
def package_gate_open() -> Iterator[None]:
    """Run the engine without the loader's package gate (``validate_package``).

    The gate refuses every package that does not pass the Publisher's schema and hash checks, so a package a stage
    must survive (a step number that is not a number, a plan step that is not a mapping) never reaches the stages in
    production. The stage-level failure-ordering tests still feed such packages to the engine, to pin what each stage
    leaves behind when it raises; this opens the gate for them, and only for them.
    """
    with mock.patch.object(_facade, "validate_package", lambda package: None):
        yield


@contextlib.contextmanager
def observation_gate_open() -> Iterator[None]:
    """Run the engine without the loader's observation gate (``validate_observation``).

    The gate refuses every observation that is not a complete, consistent format 2.0.0 observation of the package's
    warehouse before anything is written, so a malformed observation (a configuration that is not a mapping, an
    unreadable time, a flag with no truth value) never reaches the stages in production. The stage-level failure-ordering
    tests still feed such observations to the engine, to pin what each stage leaves behind when it raises; this opens the
    gate for them, and only for them.
    """
    with mock.patch.object(_facade, "validate_observation", lambda observation, package: None):
        yield


def run_sequence(package: Mapping[str, Any], observations: Sequence[Optional[Mapping[str, Any]]], policy: Any = None, *, gate: bool = True,
                 observation_gate: bool = True):
    """Run each observation through ``realize`` into one fresh directory, continuing past a failure.

    Returns ``(outcomes, tree)``: per observation ``("ok", snapshot id)`` or ``(exception type, message)``, and the
    files and directories left behind (so a crash is compared together with the ledger it left half written). With
    ``gate=False`` the package is not checked by the loader's gate (``package_gate_open``); with ``observation_gate=False``
    the observation is not (``observation_gate_open``).
    """
    with tempfile.TemporaryDirectory(prefix="realizer-run-") as tmp:
        out = Path(tmp)
        outcomes: List[Any] = []
        with contextlib.nullcontext() if gate else package_gate_open(), contextlib.nullcontext() if observation_gate else observation_gate_open():
            for observation in observations:
                kwargs = {} if policy is None else {"policy": policy_module.RealizerPolicy.from_mapping(policy)}
                result = outcome(_facade.realize, copy.deepcopy(package), copy.deepcopy(observation), out, **kwargs)
                outcomes.append(("ok", result[1].snapshot_id) if result[0] == "ok" else result)
        return outcomes, tree(out)


STAGES = {"reconciler": reconciler, "estimator": estimator, "reviewer": reviewer, "packager": packager}


@dataclasses.dataclass
class StagedRun:
    """One realize run of a scenario: the ledger lines before it and appended by it, each stage's ``(args, result)``
    calls, and the output tree after it. The ledger after every run is checked to be a prefix of the stored baseline's."""
    scenario: str
    index: int
    observation: Optional[Mapping[str, Any]]
    before: List[Dict[str, Any]]
    appended: List[Dict[str, Any]]
    calls: Dict[str, List[Any]]
    tree: Dict[str, Optional[str]]
    replay_through: Optional[str] = None


def _capturing(original: Callable[..., Any], calls: List[Any]) -> Callable[..., Any]:
    def run(*args: Any) -> Any:
        result = original(*args)
        calls.append((args, result))
        return result
    return run


def _stored_ledger(name: str) -> List[str]:
    base = canonical.EXPECTED / name / "realization" / "ledger"
    return [line for path in sorted(base.glob("*.jsonl")) for line in path.read_text(encoding="utf-8").splitlines()]


def staged_scenario_runs() -> Iterator[StagedRun]:
    """Every run of every scenario through the engine, with each stage's calls captured. After each run the ledger on
    disk must be a prefix of the stored baseline's ledger for the scenario (the history is append-only), which ties
    every run's appended entries to the stored baseline."""
    for name in canonical.scenario_names():
        scenario = canonical.load_scenario(name)
        package = scenario["package_doc"]
        stored = _stored_ledger(name)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for index, step in enumerate(scenario["runs"]):
                observation = step.get("observation")
                policy = step.get("policy")
                before = ledger_lines(out)
                calls: Dict[str, List[Any]] = {stage: [] for stage in STAGES}
                with contextlib.ExitStack() as stack:
                    for stage, module in STAGES.items():
                        stack.enter_context(mock.patch.object(module, "run", _capturing(module.run, calls[stage])))
                    _facade.realize(copy.deepcopy(package), copy.deepcopy(observation), out, replay_through=step.get("replay_through"),
                                   policy=None if policy is None else policy_module.RealizerPolicy.from_mapping(policy))
                after = ledger_lines(out)
                text_lines = [line for path in sorted((out / "realization" / "ledger").glob("*.jsonl")) for line in path.read_text(encoding="utf-8").splitlines()]
                assert text_lines == stored[:len(text_lines)], f"{name} run {index}: the ledger is not a prefix of the stored baseline's"
                yield StagedRun(name, index, observation, before, after[len(before):], calls, tree(out), step.get("replay_through"))


VALIDATION_SLOTS = ("VALIDATION_READY", "VALIDATION_STARTED", "VALIDATION_COMPLETED")
MEASUREMENT_SLOTS = ("COUNTERFACTUAL_RECORDED", "ECONOMIC_MEASUREMENT_RECORDED", "BILLING_CORRECTION_RECORDED")


def measurement_section_entries(context: Any, analysis: Any, reconciliation: Any, review: Any) -> List[Any]:
    """The validation and measurement slots of a reviewed run as ledger entries, assembled here independently of the
    engine, in the order the Realizer records them (ready, started, completed, counterfactual,
    measurement, billing correction)."""
    package, rpkg, observed = context.request.package, context.realization_package_id, analysis.observation
    app_id, episode, estimate = reconciliation.active.application_episode_id, review.validation, review.estimate
    val_id = episode.validation_episode_id

    def entry(event_type: Any, payload: Any, **ids: Any) -> Any:
        return ledger_module.make_ledger_entry(package=package, rpkg_id=rpkg, event_type=event_type, effective_at_utc=observed.observed_at_utc,
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
    """Assert (on *test*) that the validation and measurement entries appended in *run* are those the stages decided, with per-append duplicate suppression, and that the Estimator and the Reviewer did nothing unless
    validation was due. Returns ``(context, analysis, governance, reconciliation, estimate, review)``, or ``None``."""
    section = [line for line in run.appended if line["event_type"] in MEASUREMENT_SECTION]
    # Every stage runs once per realize; the Estimator and the Reviewer return an empty result unless validation is due.
    ((_, reconciliation),) = run.calls["reconciler"]
    ((args, estimate),) = run.calls["estimator"]
    ((_, review),) = run.calls["reviewer"]
    if not reconciliation.validation_due or reconciliation.halt is not None:
        test.assertEqual(estimate, rc.RealizationEstimate())
        test.assertIs(review.estimate, estimate)
        test.assertIsNone(review.validation)
        test.assertIsNone(review.recognition_status)
        test.assertIsNone(review.halt)
        test.assertEqual(section, [], f"{run.scenario} run {run.index}")
        return None
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
