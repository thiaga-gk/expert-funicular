"""Simulated post-change worlds for the Realizer's live path: a demo-fleet warehouse after an owner applied a change.

The package fixtures ``autostop-scoped-*`` are the Publisher's packages for demo-fleet warehouses at the demo analysis
time (2026-10-01). A world here is the same simulated warehouse with its configuration changed later (the epochs before
the change are the fleet's own, so the package and the world agree up to the change), run through
``publisher.analyzer.collector.demo.simulate_demo_world`` and read with the bridge's ``DemoWorldClient``, exactly as a
live run reads system tables. Worlds are cached per process.
"""
from __future__ import annotations

import copy
import functools
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from publisher.analyzer.collector import demo  # noqa: E402
from realizer.analyzer.collector import bridge  # noqa: E402
from realizer.analyzer.collector.collector import collect_measurement  # noqa: E402
from realizer.config.settings import DEFAULT_AWS_PRICE_REGISTRY  # noqa: E402

PACKAGES = ROOT / "tests" / "fixtures" / "realizer" / "packages"
# The scoped fixtures: package name -> (demo scenario, fleet index).
SCOPED = {"autostop-scoped-pro": ("C13", 13), "autostop-scoped-serverless": ("C02", 2), "autostop-scoped-never-stop": ("C01", 1)}
CHANGE_DAY = "2026-10-08"
CHANGE_MINUTE = 14 * 60 + 32  # 14:32 UTC: the change day is a partial day
AS_OF = "2026-11-07T12:00:00+00:00"  # 28 whole days after the change are settled


def package(name: str) -> Dict[str, Any]:
    return json.loads((PACKAGES / f"{name}.json").read_text(encoding="utf-8"))


def world_spec(scenario: str, after: Mapping[str, Any], *, change_day: str = CHANGE_DAY, change_minute: int = CHANGE_MINUTE,
               extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The fleet spec of *scenario* with its configuration changed to *after* (spec keys: auto_stop, size, min, max,
    type) at *change_day* + *change_minute*; the epochs before keep the fleet's values."""
    spec = copy.deepcopy(next(s for s in demo.DEMO_FLEET if s["scenario"] == scenario))
    before = {k: spec[k] for k in after}
    prior = spec.get("prior") or [{"start": spec.get("created") or spec.get("epoch_start") or "2025-07-01"}]
    prior = [dict(p, **{k: p.get(k, before[k]) for k in before}) for p in prior]
    if spec.get("epoch_start") and not spec.get("prior"):
        prior = [{"start": spec["epoch_start"], **before}]
    spec.update(after)
    spec.update({"prior": prior, "epoch_start": change_day, "epoch_start_minute": change_minute, **(extra or {})})
    spec.pop("created", None)
    return spec


def shifted_work(work: Any, factor: float, day: str = CHANGE_DAY) -> List[Dict[str, Any]]:
    """The workload components *work* until *day*, then scaled by *factor* from *day* on: periodic queries come 1/factor
    as often, steady load runs factor x as concurrent (the demo simulator's ``from``/``until`` bounds)."""
    before = [dict(w, until=day) for w in work]
    after = [dict(w, **({"every": w["every"] / factor} if w.get("every") else {"conc": w["conc"] * factor}), **{"from": day}) for w in work]
    return before + after


@functools.lru_cache(maxsize=None)
def _rows(spec_json: str, index: int, analysis_time: str) -> Dict[str, Any]:
    return demo.simulate_demo_world(json.loads(spec_json), index, analysis_time)


def world_rows(spec: Mapping[str, Any], index: int, analysis_time: str = AS_OF) -> Dict[str, Any]:
    return _rows(json.dumps(spec, sort_keys=True), index, analysis_time)


@functools.lru_cache(maxsize=1)
def registry() -> Dict[str, Any]:
    return bridge.load_registry(str(DEFAULT_AWS_PRICE_REGISTRY))


def measure(pkg: Mapping[str, Any], spec: Mapping[str, Any], index: int, *, as_of: str = AS_OF, source_region: Optional[str] = None,
            aws_registry: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Measurement evidence of *pkg*'s warehouse in the simulated world *spec* at *as_of* (the live path, simulated)."""
    rows = world_rows(spec, index, as_of)
    return collect_measurement(bridge.DemoWorldClient(rows, as_of), pkg, as_of=as_of, source="DEMO_SIMULATION",
                               source_region=source_region, aws_registry=registry() if aws_registry is None else aws_registry)
