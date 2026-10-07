"""Demo world: the simulated post-change world of a Publisher demo-fleet warehouse, measured through the live path.

``realizer --demo`` and the end-to-end goldens run the same thing. A world is the fleet warehouse's simulated system
tables (``simulate_demo_world``, reached through the bridge) with the owner's configuration changes on a timeline
(``world_spec``); ``DemoWorldClient`` answers the collector statements from it, so ``collect_measurement`` reads it
exactly as it reads live system tables and the projector turns the evidence into an observation. The simulator's noise
depends only on the scenario and the day, so two worlds that differ by one knob differ only by that knob's effect.

The demo applies the package's own plan: its first authority-eligible step (the recommended scenario's first CANDIDATE
step whose execution state is authoritative) is set in the world seven days after the package was generated, at 14:32
UTC (a partial change day), and the world is measured, by default, at 12:00 UTC thirty days after the change day, when
28 whole post-change days are settled. A package without such a step leaves the world unchanged. The run scope and the
cost profile the package was built with are honored because the plan already reflects them. Only demo-fleet warehouses
have a world: a package of any other warehouse is refused. A simulated world is never a measurement of a real
warehouse.
"""
from __future__ import annotations

import copy
import datetime as dt
import functools
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from realizer.analyzer.analysis import next_plan_step, normalize_value
from realizer.analyzer.collector import bridge
from realizer.analyzer.collector.collector import collect_measurement, measurement_path
from realizer.analyzer.projector import project_observation
from realizer.common.errors import InputError
from realizer.common.utils import parse_utc, utc_iso, write_json
from realizer.config.loader import validate_package
from realizer.config.settings import DEFAULT_AWS_PRICE_REGISTRY

SOURCE = "DEMO_SIMULATION"
# Package knob -> demo fleet spec key.
SPEC_KEYS = {"auto_stop_minutes": "auto_stop", "warehouse_size": "size", "min_clusters": "min", "max_clusters": "max", "warehouse_type": "type"}
INTEGER_KEYS = ("auto_stop", "min", "max")
CHANGE_DELAY_DAYS = 7  # the owner applies the step a week after the package was generated
CHANGE_MINUTE = 14 * 60 + 32  # at 14:32 UTC: the change day is a partial day
MEASURE_DELAY_DAYS = 30  # measured at 12:00 UTC thirty days after the change day: 28 whole days after it are settled
MEASURE_TIME = dt.time(12, 0)
FALLBACK_GENERATED_AT = "2026-10-01T12:00:00Z"  # the demo analysis time, for a package that states none
_FLEET_ID = re.compile(r"wh-demo-(\d{3})")


def fleet_index(warehouse_id: Any, workspace_id: Optional[Any] = None) -> int:
    """The demo fleet number of ``wh-demo-<NNN>`` (its 1-based position in the demo fleet). ``InputError`` when the
    warehouse is not a fleet member or, given *workspace_id*, the member lives in another workspace."""
    match = _FLEET_ID.fullmatch(str(warehouse_id))
    index = int(match.group(1)) if match else 0
    if not 1 <= index <= len(bridge.DEMO_FLEET):
        raise InputError("--demo simulates Publisher demo-fleet warehouses only: the package's warehouse is not one")
    if workspace_id is not None and str(workspace_id) != bridge.DEMO_WORKSPACES[bridge.DEMO_FLEET[index - 1]["ws"]]:
        raise InputError("--demo simulates Publisher demo-fleet warehouses only: the package's workspace is not the fleet warehouse's")
    return index


def _ts(value: Any) -> dt.datetime:
    text = value.isoformat() if isinstance(value, (dt.datetime, dt.date)) else str(value)
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def shifted_work(work: Any, factor: float, day: str) -> List[Dict[str, Any]]:
    """The workload components *work* until *day*, then scaled by *factor* from *day* on: periodic queries come 1/factor
    as often, steady load runs factor x as concurrent (the demo simulator's ``from``/``until`` bounds)."""
    before = [dict(w, until=day) for w in work]
    after = [dict(w, **({"every": w["every"] / factor} if w.get("every") else {"conc": w["conc"] * factor}), **{"from": day}) for w in work]
    return before + after


def world_spec(index: int, changes: Sequence[Mapping[str, Any]], workload: Optional[Mapping[str, Any]] = None,
               runtime: Optional[Mapping[str, Any]] = None, currency: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The fleet spec of warehouse *index* with the owner's *changes* applied: each ``{at: <UTC time>, set: {spec key:
    value}}`` starts a configuration epoch holding the values set so far. The fleet's own epochs before the first change
    are kept. The simulator starts only the last epoch at a time of day, so earlier changes must be at 00:00 UTC. An
    optional *workload* ``{from: <UTC day>, factor: x}`` scales the fleet workload from that day on, and an optional
    *runtime* ``{size: factor}`` makes a query started under a configuration of that size run factor x as long (the
    simulator's ``runtime_factor_by_size``; a size it does not name runs at factor 1). An optional *currency* ``{from:
    <UTC day>, code: <ISO 4217>}`` bills the warehouse in that currency from that day on (the simulator's
    ``currency_from``). A change that does not fit the fleet's timeline is a ``ValueError``."""
    spec = copy.deepcopy(bridge.DEMO_FLEET[index - 1])
    if workload:
        spec["work"] = shifted_work(spec["work"], float(workload["factor"]), str(workload["from"]))
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
    if runtime:
        spec["runtime_factor_by_size"] = {normalize_value("warehouse_size", str(k)): float(v) for k, v in runtime.items()}
    if currency:
        spec["currency_from"] = {"day": str(currency["from"]), "code": str(currency["code"])}
    if not spec["prior"]:
        spec.pop("prior")
    return spec


@functools.lru_cache(maxsize=None)
def _rows(spec_json: str, index: int, analysis_time: str) -> Dict[str, Any]:
    return bridge.simulate_demo_world(json.loads(spec_json), index, analysis_time)


def world_rows(spec: Mapping[str, Any], index: int, analysis_time: str) -> Dict[str, Any]:
    """The simulated system-table rows of warehouse *index* under *spec* at *analysis_time* (cached per process; the
    rows are shared, never mutate them)."""
    return _rows(json.dumps(spec, sort_keys=True), index, analysis_time)


@functools.lru_cache(maxsize=1)
def registry() -> Dict[str, Any]:
    """The committed regional EC2 price registry."""
    return bridge.load_registry(str(DEFAULT_AWS_PRICE_REGISTRY))


def measure_world(package: Mapping[str, Any], spec: Mapping[str, Any], index: int, *, as_of: str, source_region: Optional[str] = None,
                  aws_registry: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Measurement evidence of *package*'s warehouse in the simulated world *spec* at *as_of* (the live path, simulated)."""
    rows = world_rows(spec, index, as_of)
    return collect_measurement(bridge.DemoWorldClient(rows, as_of), package, as_of=as_of, source=SOURCE,
                               source_region=source_region, aws_registry=registry() if aws_registry is None else aws_registry)


def change_at(package: Mapping[str, Any]) -> dt.datetime:
    """When the demo owner applies the package's step: 14:32 UTC seven days after the package was generated."""
    generated = parse_utc((package.get("provenance") or {}).get("generated_at") or FALLBACK_GENERATED_AT)
    day = generated.date() + dt.timedelta(days=CHANGE_DELAY_DAYS)
    return dt.datetime.combine(day, dt.time(), dt.timezone.utc) + dt.timedelta(minutes=CHANGE_MINUTE)


def settled_as_of(change: dt.datetime) -> str:
    """The default measurement time of a change at *change*: 12:00 UTC thirty days after its day (28 whole days settled)."""
    return dt.datetime.combine(change.date() + dt.timedelta(days=MEASURE_DELAY_DAYS), MEASURE_TIME, dt.timezone.utc).isoformat()


def applied_change(package: Mapping[str, Any], *, modified: bool = False) -> Optional[Dict[str, Any]]:
    """The owner's change of the demo, as ``{spec key: value}``: the package's first authority-eligible step at its
    recommended value (``modified``: a size step to MEDIUM applied as SMALL); ``None`` when the plan has no such step or
    its knob has no world."""
    step = next_plan_step(package)
    key = SPEC_KEYS.get(str((step or {}).get("knob")))
    if step is None or key is None:
        return None
    value = step["to_value"]
    if key in INTEGER_KEYS:
        value = int(value)
    else:
        value = normalize_value(str(step["knob"]), str(value))
    if modified and key == "size" and value == "MEDIUM":
        value = "SMALL"
    return {key: value}


def simulated_evidence(package: Mapping[str, Any], *, as_of: Optional[str] = None, modified: bool = False) -> Dict[str, Any]:
    """Measurement evidence of *package*'s demo-fleet warehouse after the demo owner applied the package's plan (see the
    module docstring), measured at *as_of* (default: 28 settled days after the change). Before the change time, or
    without an eligible step, the world is the fleet's own. ``InputError`` for a warehouse outside the demo fleet."""
    validate_package(package)
    warehouse = package["warehouse"]
    index = fleet_index(warehouse["warehouse_id"], warehouse["workspace_id"])
    change, at = applied_change(package, modified=modified), change_at(package)
    try:
        when = settled_as_of(at) if as_of is None else utc_iso(as_of)
    except ValueError:
        raise InputError("--as-of is not a UTC ISO-8601 time") from None
    changes = [{"at": at.isoformat(), "set": change}] if change is not None and parse_utc(when) > at else []
    try:
        spec = world_spec(index, changes)
    except ValueError:
        raise InputError("--demo cannot apply the package's step: the demo-fleet warehouse's own last configuration change is not before it") from None
    return measure_world(package, spec, index, as_of=when)


def measure_demo(package: Mapping[str, Any], output_dir: Path, *, as_of: Optional[str] = None, modified: bool = False,
                 approvals: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The demo's observation of *package*: its simulated world measured through the live path. The measurement evidence
    is written first, where a live run writes it (``measurement_path``), so the run can be replayed with
    ``observe_from_evidence``."""
    evidence = simulated_evidence(package, as_of=as_of, modified=modified)
    write_json(measurement_path(output_dir, evidence, package["recommendation_package_id"]), evidence)
    return project_observation(package, evidence, approvals)
