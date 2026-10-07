"""End-to-end golden harness, Publisher to Realizer: case loading, the two stages and the ``expect`` checks.

The case format and the assertion DSL are documented in ``tests/golden/realizer/test_golden_e2e.py``; this module is
shared by that test and ``scripts/update_realizer_e2e_goldens.py``.

* Publisher stage: the Publisher demo run (``--demo --flat-output --no-workbook``, fixed analysis time) once per distinct
  (run scope, cost profile) group, every case warehouse of the group through a repeated ``--warehouse``; each case keeps
  its warehouse's package as ``expected/publisher/package.json``.
* Realizer stage: from the stored package, the case's simulated world (the demo-fleet warehouse with the owner's
  configuration changes on a timeline, ``publisher.analyzer.collector.demo.simulate_demo_world``) is measured through the
  live path (``collect_measurement`` on ``DemoWorldClient``), projected (``project_observation``) and realized
  (``realize`` into a temporary directory). Its canonical output, under ``expected/realizer/``, is the measurement
  evidence, the observation, the oracle and the realization files in the stored-baseline form of
  ``tests/regression/realizer/canonical.py`` (or the refusal, for a case that must be refused).
* Oracle: the same world with the measured knob held at the package's source value, simulated over the same days. The
  simulator's noise depends only on the scenario and the day, so the two worlds differ only by that knob, and the oracle
  saving is the billed DBU cost difference over the post-change window. Agreement is a consistency check of the replay,
  not accuracy evidence.
"""
from __future__ import annotations

import copy
import datetime as dt
import functools
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from publisher.analyzer.collector import demo  # noqa: E402
from realizer.analyzer.projector import project_observation  # noqa: E402
from realizer.common.errors import InputError  # noqa: E402
from tests import realizer_world  # noqa: E402
from tests.regression.realizer import canonical  # noqa: E402

CASES = Path(__file__).resolve().parent / "cases"
PACKAGE_FILE = Path("expected") / "publisher" / "package.json"
REALIZER_DIR = Path("expected") / "realizer"
DEFAULT_AS_OF = realizer_world.AS_OF
# Package knob -> demo fleet spec key.
SPEC_KEYS = {"auto_stop_minutes": "auto_stop", "warehouse_size": "size", "min_clusters": "min", "max_clusters": "max", "warehouse_type": "type"}
KNOB_OF_KEY = {v: k for k, v in SPEC_KEYS.items()}
EXPECT_KEYS = {"conformance", "relation", "applied_at", "window", "measured", "not_measured_reason", "near_oracle", "within_pkg",
               "cross_check", "independent_changes", "refused", "sign", "same_evidence_as", "realization_state", "classification",
               "claim", "origin", "disqualifiers", "recognition"}


def dumps(value: Any) -> str:
    """The stored serialization of Realizer-stage files (indent 2, sorted keys, trailing newline; the Realizer's own)."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def dumps_package(package: Mapping[str, Any]) -> str:
    """The stored serialization of a Publisher package (that of the Realizer's package fixtures)."""
    return json.dumps(package, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_names() -> List[str]:
    return sorted(p.name for p in CASES.iterdir() if (p / "case.yaml").is_file())


def case_dir(name: str) -> Path:
    path = CASES / name
    if not (path / "case.yaml").is_file():
        raise FileNotFoundError(f"end-to-end golden case {name!r} not found under {CASES}")
    return path


@functools.lru_cache(maxsize=None)
def _load(name: str) -> str:
    return (case_dir(name) / "case.yaml").read_text(encoding="utf-8")


def load_case(name: str) -> Dict[str, Any]:
    return yaml.safe_load(_load(name))


def stored_package(name: str) -> Dict[str, Any]:
    return json.loads((case_dir(name) / PACKAGE_FILE).read_text(encoding="utf-8"))


def stored_realizer_files(name: str) -> Dict[str, str]:
    base = case_dir(name) / REALIZER_DIR
    return {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8") for p in sorted(base.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# Publisher stage
# ---------------------------------------------------------------------------

def publisher_group(case: Mapping[str, Any]) -> Tuple[Tuple[str, ...], Optional[str]]:
    """A case's Publisher run: its ``--scope`` values (empty for the full scope) and its ``--cost-profile`` flag (None for
    the tag default)."""
    pub = case["publisher"]
    return tuple(pub.get("scope") or ()), pub.get("cost_profile")


def run_publisher(groups: Mapping[Tuple[Tuple[str, ...], Optional[str]], Sequence[str]]) -> Dict[Tuple[Tuple[Tuple[str, ...], Optional[str]], str], Dict[str, Any]]:
    """The Publisher demo's packages per (group, warehouse id): one demo run per group, all run concurrently."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    with tempfile.TemporaryDirectory(prefix="realizer-e2e-publisher-") as tmp:
        running = []
        for n, (group, warehouses) in enumerate(sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))):
            scope, profile = group
            out = Path(tmp) / str(n)
            argv = [sys.executable, "-m", "publisher", "--demo", "--flat-output", "--no-workbook", "--log-level", "WARNING", "--output-dir", str(out)]
            for value in scope:
                argv += ["--scope", value]
            if profile:
                argv += ["--cost-profile", profile]
            for warehouse in sorted(set(warehouses)):
                argv += ["--warehouse", warehouse]
            running.append((group, out, subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
        packages = {}
        for group, out, proc in running:
            _, err = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"publisher demo failed for scope={list(group[0])} cost_profile={group[1]}:\n{err[-3000:]}")
            for path in sorted((out / "packages").glob("*.json")):
                package = json.loads(path.read_text(encoding="utf-8"))
                packages[(group, package["warehouse"]["warehouse_id"])] = package
    return packages


def publisher_packages(names: Sequence[str]) -> Dict[str, str]:
    """The current Publisher output for each case in *names*: {case name: serialized package}."""
    cases = {name: load_case(name) for name in names}
    groups: Dict[Tuple[Tuple[str, ...], Optional[str]], List[str]] = {}
    for case in cases.values():
        groups.setdefault(publisher_group(case), []).append(case["publisher"]["warehouse"])
    built = run_publisher(groups)
    out = {}
    for name, case in cases.items():
        key = (publisher_group(case), case["publisher"]["warehouse"])
        if key not in built:
            raise RuntimeError(f"{name}: the publisher wrote no package for {key[1]}")
        out[name] = dumps_package(built[key])
    return out


# ---------------------------------------------------------------------------
# Worlds
# ---------------------------------------------------------------------------

def fleet_index(warehouse_id: str) -> int:
    """The demo fleet number of ``wh-demo-<NNN>`` (its 1-based position in ``DEMO_FLEET``)."""
    index = int(str(warehouse_id).rsplit("-", 1)[-1])
    if not 1 <= index <= len(demo.DEMO_FLEET):
        raise ValueError(f"{warehouse_id} is not a demo fleet warehouse")
    return index


def _ts(value: Any) -> dt.datetime:
    text = value.isoformat() if isinstance(value, (dt.datetime, dt.date)) else str(value)
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def world_spec(index: int, changes: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The fleet spec of warehouse *index* with the owner's *changes* applied: each ``{at: <UTC time>, set: {spec key:
    value}}`` starts a configuration epoch holding the values set so far. The fleet's own epochs before the first change
    are kept. The simulator starts only the last epoch at a time of day, so earlier changes must be at 00:00 UTC."""
    spec = copy.deepcopy(demo.DEMO_FLEET[index - 1])
    keys = tuple(SPEC_KEYS.values())
    current = {k: spec[k] for k in keys}
    epochs: List[Tuple[dt.date, Dict[str, Any]]] = [(dt.date.fromisoformat(p["start"]), {**current, **{k: v for k, v in p.items() if k != "start"}})
                                                     for p in spec.get("prior") or []]
    epochs.append((dt.date.fromisoformat(spec.get("epoch_start") or spec.get("created") or "2025-07-01"), dict(current)))
    minute = 0
    ordered = sorted(changes, key=lambda c: _ts(c["at"]))
    for n, change in enumerate(ordered):
        at = _ts(change["at"])
        unknown = set(change["set"]) - set(keys)
        if unknown:
            raise ValueError(f"unknown world knob(s) {sorted(unknown)}; use {list(keys)}")
        if at.date() <= epochs[-1][0]:
            raise ValueError(f"a change at {at.isoformat()} does not start after the fleet's last epoch ({epochs[-1][0]})")
        if n < len(ordered) - 1 and at.time() != dt.time(0):
            raise ValueError("only the last change can start at a time of day")
        epochs.append((at.date(), {**epochs[-1][1], **change["set"]}))
        minute = at.hour * 60 + at.minute
    spec.update(epochs[-1][1])
    spec["prior"] = [{"start": d.isoformat(), **cfg} for d, cfg in epochs[:-1]]
    spec["epoch_start"] = epochs[-1][0].isoformat()
    if minute:
        spec["epoch_start_minute"] = minute
    spec.pop("created", None)
    if not spec["prior"]:
        spec.pop("prior")
    return spec


def billed_cost(rows: Mapping[str, Any], warehouse_id: str, first_day: str, last_day: str) -> Decimal:
    """Billed DBU list cost of a simulated warehouse over whole days ``first_day``..``last_day``."""
    return sum((Decimal(str(r["list_cost"] or 0)) for r in rows.get("billing_daily_cost", [])
                if r["warehouse_id"] == warehouse_id and first_day <= r["usage_date"] <= last_day), Decimal("0"))


def oracle(case: Mapping[str, Any], package: Mapping[str, Any], observation: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The oracle of a measured case: the world with the measured knob held at the package's source value, billed over
    the same post-change days. None when nothing was measured."""
    meas = observation.get("measurement") or {}
    window = meas.get("post_window")
    if not window or observation.get("economics") is None:
        return None
    world = case["world"]
    as_of = str(world.get("as_of") or DEFAULT_AS_OF)
    index = fleet_index(world.get("warehouse") or package["warehouse"]["warehouse_id"])
    key = SPEC_KEYS[meas["knob"]]
    held = [{**c, "set": {**c["set"], key: meas["source_value"]}} for c in world["changes"]]
    wh = f"wh-demo-{index:03d}"
    new_rows = realizer_world.world_rows(world_spec(index, world["changes"]), index, as_of)
    old_rows = realizer_world.world_rows(world_spec(index, held), index, as_of)
    new, old = billed_cost(new_rows, wh, window["first_day"], window["last_day"]), billed_cost(old_rows, wh, window["first_day"], window["last_day"])
    return {"knob": meas["knob"], "held_value": meas["source_value"], "first_day": window["first_day"], "last_day": window["last_day"],
            "old_billed_cost": str(old.quantize(Decimal("0.01"))), "new_billed_cost": str(new.quantize(Decimal("0.01"))),
            "dbu_saving": str((old - new).quantize(Decimal("0.01")))}


# ---------------------------------------------------------------------------
# Realizer stage
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _realizer_stage(name: str, case_text: str, package_text: str) -> Tuple[Tuple[str, str], ...]:
    case, package = yaml.safe_load(case_text), json.loads(package_text)
    world = case["world"]
    as_of = str(world.get("as_of") or DEFAULT_AS_OF)
    index = fleet_index(world.get("warehouse") or package["warehouse"]["warehouse_id"])
    spec = world_spec(index, world["changes"])
    files: Dict[str, str] = {}
    stage = "measure"
    try:
        evidence = realizer_world.measure(package, spec, index, as_of=as_of)
        files["measurement.json"] = dumps(evidence)
        stage = "project"
        observation = project_observation(package, evidence)
        files["observation.json"] = dumps(observation)
        found = oracle(case, package, observation)
        if found is not None:
            files["oracle.json"] = dumps(found)
        stage = "realize"
        with tempfile.TemporaryDirectory(prefix=f"realizer-e2e-{name}-") as tmp:
            files.update(canonical.run_scenario(canonical.modular_engine(), {"package_doc": package, "runs": [{"observation": observation}]}, Path(tmp)))
    except InputError as exc:
        files["refusal.json"] = dumps({"stage": stage, "message": str(exc)})
    return tuple(sorted(files.items()))


def realizer_stage(name: str, package: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """The Realizer stage of case *name* from its stored package (or *package*): {path under expected/realizer: text}.
    Cached per process."""
    text = dumps_package(package) if package is not None else (case_dir(name) / PACKAGE_FILE).read_text(encoding="utf-8")
    return dict(_realizer_stage(name, _load(name), text))


# ---------------------------------------------------------------------------
# expect
# ---------------------------------------------------------------------------

def _json(files: Mapping[str, str], path: str) -> Optional[Any]:
    return json.loads(files[path]) if path in files else None


def _rpkg(files: Mapping[str, str]) -> Dict[str, Any]:
    paths = [p for p in files if p.startswith("realization/packages/")]
    return json.loads(files[paths[0]]) if len(paths) == 1 else {}


def _candidate_step(package: Mapping[str, Any], knob: str) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    scenario = next(s for s in package["scenarios"] if s["scenario_id"] == package["recommended_plan"]["scenario_id"])
    step = next(s for s in scenario["execution_plan"] if s["step_type"] == "CANDIDATE" and s.get("knob") == knob)
    return step, next(c for c in package["candidates"] if c["candidate_id"] == step["candidate_id"])


def _dec(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def check_expect(name: str, case: Mapping[str, Any], package: Mapping[str, Any], files: Mapping[str, str],
                 others: Optional[Mapping[str, Mapping[str, str]]] = None) -> List[str]:
    """Failures of the case's ``expect`` block against its Realizer-stage output (empty when it holds)."""
    expect = case.get("expect") or {}
    failures: List[str] = []
    unknown = set(expect) - EXPECT_KEYS
    if unknown:
        return [f"unknown expect key(s) {sorted(unknown)}"]
    refusal = _json(files, "refusal.json")
    if "refused" in expect:
        if refusal is None:
            failures.append(f"refused: expected a refusal containing {expect['refused']!r}, the case ran")
        elif expect["refused"] not in refusal["message"]:
            failures.append(f"refused: {refusal['message']!r} does not contain {expect['refused']!r}")
        return failures
    if refusal is not None:
        return [f"refused at {refusal['stage']}: {refusal['message']}"]
    obs = _json(files, "observation.json") or {}
    meas = obs.get("measurement") or {}
    economics = obs.get("economics")
    application = _rpkg(files).get("application") or {}

    def fail(key: str, want: Any, got: Any) -> None:
        failures.append(f"{key}: expected {want!r}, got {got!r}")

    if "conformance" in expect and application.get("implementation_conformance") != expect["conformance"]:
        fail("conformance", expect["conformance"], application.get("implementation_conformance"))
    if "relation" in expect and application.get("modification_relation") != expect["relation"]:
        fail("relation", expect["relation"], application.get("modification_relation"))
    if "applied_at" in expect and obs.get("applied_at_utc") != expect["applied_at"]:
        fail("applied_at", expect["applied_at"], obs.get("applied_at_utc"))
    if "window" in expect:
        want = expect["window"]
        post, pre = meas.get("post_window") or {}, meas.get("pre_window") or {}
        got = {"first": post.get("first_day"), "last": post.get("last_day"), "pre_first": pre.get("first_day"), "pre_last": pre.get("last_day")}
        if want is None:
            if meas.get("post_window") is not None:
                fail("window", None, got)
        elif any(got.get(k) != str(v) for k, v in want.items()):
            fail("window", want, {k: got.get(k) for k in want})
    snapshot = _rpkg(files)
    state, attribution, snap_econ = snapshot.get("current_state") or {}, snapshot.get("attribution") or {}, snapshot.get("economics") or {}
    for key, got in (("realization_state", state.get("realization_state")), ("classification", snap_econ.get("classification_detail")),
                     ("claim", attribution.get("attribution_claim_status")), ("origin", attribution.get("origin_attribution"))):
        if key in expect and got != expect[key]:
            fail(key, expect[key], got)
    if "disqualifiers" in expect and sorted(attribution.get("hard_disqualifiers") or []) != sorted(expect["disqualifiers"]):
        fail("disqualifiers", sorted(expect["disqualifiers"]), sorted(attribution.get("hard_disqualifiers") or []))
    if "recognition" in expect:
        ledger = [json.loads(line) for path, text in sorted(files.items()) if path.startswith("realization/ledger/") for line in text.splitlines() if line.strip()]
        got = [e["payload"].get("recognition_status") for e in ledger if e["event_type"] == "ECONOMIC_MEASUREMENT_RECORDED"]
        if got[-1:] != [expect["recognition"]]:
            fail("recognition", expect["recognition"], got[-1:] or None)
    if "measured" in expect and (economics is not None) != bool(expect["measured"]):
        fail("measured", expect["measured"], economics is not None)
    if "not_measured_reason" in expect and meas.get("not_measured_reason") != expect["not_measured_reason"]:
        fail("not_measured_reason", expect["not_measured_reason"], meas.get("not_measured_reason"))
    if "independent_changes" in expect and meas.get("other_knob_changes") != list(expect["independent_changes"]):
        fail("independent_changes", list(expect["independent_changes"]), meas.get("other_knob_changes"))
    if "cross_check" in expect:
        want = expect["cross_check"] if isinstance(expect["cross_check"], Mapping) else {"outcome": expect["cross_check"]}
        got = meas.get("cross_check") or {}
        if any(got.get(k) != v for k, v in want.items()):
            fail("cross_check", want, {k: got.get(k) for k in want})
    if any(k in expect for k in ("sign", "near_oracle", "within_pkg")) and economics is None:
        return failures + ["sign/near_oracle/within_pkg: the case measured no economics"]
    if "sign" in expect:
        realized = _dec(economics["counterfactual_old_cost"]) - _dec(economics["observed_new_cost"])
        if (realized > 0 and expect["sign"] != "positive") or (realized < 0 and expect["sign"] != "negative") or realized == 0:
            fail("sign", expect["sign"], str(realized))
    if "near_oracle" in expect:
        found = _json(files, "oracle.json")
        realized, truth = _dec(economics["dbu_realized"]), _dec(found["dbu_saving"])
        tolerance = Decimal(str(expect["near_oracle"]["pct"])) * truth.copy_abs() / 100
        if (realized - truth).copy_abs() > tolerance:
            failures.append(f"near_oracle: replay DBU saving {realized} is more than {expect['near_oracle']['pct']}% from the oracle's {truth}")
    if "within_pkg" in expect:
        rule = expect["within_pkg"]
        component = rule.get("component", "dbu")
        _, candidate = _candidate_step(package, meas["knob"])
        published = candidate["independent_economics"]["savings_365d"][component]
        period = Decimal(str(economics["period_days"]))
        realized = (_dec(economics["dbu_realized"]) if component == "dbu"
                    else _dec(economics["counterfactual_old_cost"]) - _dec(economics["observed_new_cost"]))
        annual = realized * 365 / period
        low = _dec(published["low"]) * Decimal(str(rule.get("low_factor", 1)))
        high = _dec(published["high"]) * Decimal(str(rule.get("high_factor", 1)))
        if not low <= annual <= high:
            failures.append(f"within_pkg: annualized {component} {annual.quantize(Decimal('0.01'))} outside "
                            f"[{low.quantize(Decimal('0.01'))}, {high.quantize(Decimal('0.01'))}]")
    if "same_evidence_as" in expect:
        other = (others or {}).get(expect["same_evidence_as"]) or realizer_stage(expect["same_evidence_as"])
        mine, theirs = _json(files, "measurement.json"), _json(other, "measurement.json")
        strip = lambda e: {k: v for k, v in (e or {}).items() if k != "assumptions"}  # noqa: E731
        if theirs is None or strip(mine) != strip(theirs):
            failures.append(f"same_evidence_as: the measurement differs from {expect['same_evidence_as']}'s (assumptions aside)")
    return failures
