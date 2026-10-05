"""Package estimate: the authoritative package economics as an envelope over the scenarios, without double counting."""
from __future__ import annotations

from typing import Optional, Sequence

from publisher.contracts import reporter_contract as rc
from publisher.contracts.source import WarehouseEvidence
from publisher.packager.adapter import _forward_cost_breakdown


def _economics_point_from_scenario(s: rc.Scenario, bound: rc.BoundName, *, savings: bool) -> rc.EconomicsPoint:
    ranges = s.portfolio_economics.savings_365d if savings else s.portfolio_economics.estimated_cost_365d
    attr = {rc.BoundName.LOW: "low", rc.BoundName.BASE: "base", rc.BoundName.HIGH: "high"}[bound]
    # Cost range has inverse relation to savings range: LOW savings corresponds HIGH cost.
    if not savings:
        attr = {rc.BoundName.LOW: "high", rc.BoundName.BASE: "base", rc.BoundName.HIGH: "low"}[bound]
    def get(r: Optional[rc.Range3]) -> Optional[float]:
        return None if r is None else round(float(getattr(r, attr)), 2)
    return rc.EconomicsPoint(get(ranges.dbu), get(ranges.aws), get(ranges.total))


def _package_economics(
    e: WarehouseEvidence,
    scenarios: Sequence[rc.Scenario],
    recommended_scenario_id: Optional[str],
) -> rc.PackageEconomics:
    baseline = _forward_cost_breakdown(e)
    own_priced = [
        s for s in scenarios
        if s.status not in {rc.ScenarioStatus.BLOCKED, rc.ScenarioStatus.INSUFFICIENT_EVIDENCE}
        and s.portfolio_economics.savings_365d.total is not None
    ]
    # The envelope's base is the recommended scenario, so nothing is summarized unless that scenario is itself priced.
    assessable = own_priced if recommended_scenario_id and any(s.scenario_id == recommended_scenario_id for s in own_priced) else []
    included = [s.scenario_id for s in assessable]
    own_priced_ids = {s.scenario_id for s in own_priced}

    def exclusion_reason(s: rc.Scenario) -> str:
        if s.scenario_id not in own_priced_ids:
            return "SCENARIO_ECONOMICS_UNAVAILABLE"  # this scenario has no authoritative price
        return "RECOMMENDED_SCENARIO_ECONOMICS_UNAVAILABLE" if recommended_scenario_id else "NO_RECOMMENDED_SCENARIO"

    excluded = [rc.ExcludedScenario(s.scenario_id, exclusion_reason(s)) for s in scenarios if s.scenario_id not in included]
    if not assessable or recommended_scenario_id is None:
        return rc.PackageEconomics(
            currency=e.currency,
            baseline_365d=baseline,
            estimated_savings_365d=None,
            estimated_cost_365d=None,
            scenario_envelope=rc.ScenarioEnvelope(None, None, None),
            included_scenario_ids=[],
            excluded_scenarios=excluded,
            semantics=rc.PackageEconomicsSemantics("SCENARIO_ENVELOPE", False, False),
        )

    low_s = min(assessable, key=lambda s: s.portfolio_economics.savings_365d.total.low)  # type: ignore[union-attr]
    high_s = max(assessable, key=lambda s: s.portfolio_economics.savings_365d.total.high)  # type: ignore[union-attr]
    base_s = next(s for s in assessable if s.scenario_id == recommended_scenario_id)
    low = round(low_s.portfolio_economics.savings_365d.total.low, 2)  # type: ignore[union-attr]
    base = round(base_s.portfolio_economics.savings_365d.total.base, 2)  # type: ignore[union-attr]
    high = round(high_s.portfolio_economics.savings_365d.total.high, 2)  # type: ignore[union-attr]
    savings_range = rc.Range3(low, base, high)
    total_baseline = baseline.total_cost
    estimated_cost = None if total_baseline is None else rc.Range3(
        round(total_baseline - high, 2), round(total_baseline - base, 2), round(total_baseline - low, 2)
    )
    envelope = rc.ScenarioEnvelope(
        minimum_savings=rc.ScenarioEnvelopePoint(
            low_s.scenario_id, rc.BoundName.LOW,
            _economics_point_from_scenario(low_s, rc.BoundName.LOW, savings=True),
            _economics_point_from_scenario(low_s, rc.BoundName.LOW, savings=False),
        ),
        recommended_base=rc.ScenarioEnvelopePoint(
            base_s.scenario_id, rc.BoundName.BASE,
            _economics_point_from_scenario(base_s, rc.BoundName.BASE, savings=True),
            _economics_point_from_scenario(base_s, rc.BoundName.BASE, savings=False),
        ),
        maximum_savings=rc.ScenarioEnvelopePoint(
            high_s.scenario_id, rc.BoundName.HIGH,
            _economics_point_from_scenario(high_s, rc.BoundName.HIGH, savings=True),
            _economics_point_from_scenario(high_s, rc.BoundName.HIGH, savings=False),
        ),
    )
    return rc.PackageEconomics(
        currency=e.currency,
        baseline_365d=baseline,
        estimated_savings_365d=savings_range,
        estimated_cost_365d=estimated_cost,
        scenario_envelope=envelope,
        included_scenario_ids=included,
        excluded_scenarios=excluded,
        semantics=rc.PackageEconomicsSemantics("SCENARIO_ENVELOPE", False, False),
    )
